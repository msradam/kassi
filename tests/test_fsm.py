from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client
from theodosia import bind_upstream, mount
from theodosia.testing import FakeUpstream
from theodosia.upstream import reset_upstream

from kassi import app as kassi_app
from kassi.app import build_application

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "petstore"

K6_RESPONSES = {
    "k6": {
        "validate_script": {"valid": True, "exit_code": 0, "stdout": "", "stderr": ""},
        "run_script": {
            "success": True,
            "exit_code": 0,
            "summary": "kassi summary",
            "metrics": {
                "http_reqs": {"count": 120},
                "http_req_duration": {"p(95)": 14.2, "avg": 6.1},
                "http_req_failed": {"rate": 0.0},
                "checks": {"passes": 120, "fails": 0, "rate": 1.0},
            },
        },
        "list_sections": {
            "tree": [
                {"slug": "using-k6/http-requests", "title": "HTTP Requests", "child_count": 0},
                {"slug": "using-k6/thresholds", "title": "Thresholds", "child_count": 0},
                {"slug": "using-k6/checks", "title": "Checks", "child_count": 0},
                {"slug": "using-k6/scenarios", "title": "Scenarios", "child_count": 0},
            ]
        },
        "get_documentation": {
            "section": {"slug": "using-k6/thresholds", "title": "Thresholds"},
            "content": "---\ntitle: 'Thresholds'\n---\n\n# Thresholds\n\nThresholds are pass/fail criteria for the system under test.",
        },
    }
}

K6_AND_SPLUNK_RESPONSES = {
    **K6_RESPONSES,
    "splunk": {
        # One rich row that satisfies all four correlation queries (FakeUpstream is arg-blind).
        "splunk_run_query": {
            "results": [
                {
                    "total_events": "120",
                    "server_errors": "7",
                    "client_errors": "3",
                    "p95_ms": "284.3",
                    "path": "/api/visits",
                    "reqs": "120",
                    "errors": "7",
                    "err_pct": "59.2",
                    "error_message": "database is locked",
                    "count": "7",
                }
            ]
        },
        "splunk_get_info": {"results": [{"version": "10.4.0", "serverName": "Mac", "health_info": "green"}]},
        "splunk_get_index_info": {
            "results": [
                {"title": "web", "totalEventCount": "640", "currentDBSizeMB": "1", "datatype": "event"}
            ]
        },
        "splunk_get_metadata": {
            "results": [
                {"sourcetype": "access_json", "totalCount": "640", "lastTimeIso": "2026-06-11T10:17:10Z"}
            ]
        },
    },
}


_K6_SCRIPT = (
    "import http from 'k6/http';\n"
    "export const options = { vus: 5, duration: '10s' };\n"
    "export default function () {\n"
    "  http.get('http://localhost:8000/api/pets');\n"
    "}\n"
)


class _FakeLLM:
    def generate(self, *, system: str, user: str, stop=None, format=None) -> str:
        if "narrat" in system.lower() or "tarot" in system.lower():
            return "The Fool: the run begins.\nThe Tower: load applied.\nJudgement: passed."
        return _K6_SCRIPT


async def _no_guidance(description: str) -> None:
    return None


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    monkeypatch.setattr(kassi_app, "make_llm", lambda *a, **k: _FakeLLM())
    monkeypatch.setattr(kassi_app, "fetch_k6_generation_guidance", _no_guidance)


async def test_full_run_intent_mode():
    fake = FakeUpstream(K6_RESPONSES)
    token = bind_upstream(fake)
    try:
        application = build_application()
        _, _, state = await application.arun(
            halt_after=["report"],
            inputs={"repo_path": str(EXAMPLE), "intent": "load test listing the pets"},
        )
    finally:
        reset_upstream(token)

    report = state["report"]
    assert report["verdict"] == "passed"
    assert report["run_result"]["http_reqs"] == 120
    assert report["run_result"]["http_req_duration_p95_ms"] == 14.2

    # scaffold built a deterministic load plan; the model authored the script on top of it.
    assert report["plan"]["test_taxonomy"] == "load"
    validated = fake.calls_to("k6", "validate_script")
    assert len(validated) == 1
    script = validated[0].args["script"]
    assert "import http from 'k6/http'" in script
    assert "import" in script and "openapi-to-k6" not in script
    assert fake.calls_to("k6", "run_script")

    # the report narrates the run (model in tests, omens otherwise).
    assert isinstance(report["narration"], str) and report["narration"].strip()

    # doc_lookup consulted the k6 MCP docs and recorded version-grounded citations.
    assert fake.calls_to("k6", "list_sections")
    provenance = report["mcp_provenance"]
    assert provenance["k6_doc_refs"], "expected k6 doc references"
    assert {r["slug"] for r in provenance["k6_doc_refs"]} & {"using-k6/thresholds", "using-k6/checks"}
    tools_called = {(c["server"], c["tool"]) for c in provenance["tool_calls"]}
    assert ("k6", "list_sections") in tools_called
    assert ("k6", "generate_script(prompt)") in tools_called
    assert ("k6", "validate_script") in tools_called
    assert ("k6", "run_script") in tools_called
    # Splunk was not configured, so no preflight ran.
    assert provenance["splunk_preflight"] is None


async def test_splunk_correlation_when_configured(monkeypatch):
    monkeypatch.setenv("KASSI_SPLUNK_MCP_ENDPOINT", "https://splunk.example/mcp")
    monkeypatch.setenv("KASSI_SPLUNK_TOKEN", "encrypted-token")

    fake = FakeUpstream(K6_AND_SPLUNK_RESPONSES)
    token = bind_upstream(fake)
    try:
        application = build_application()
        _, _, state = await application.arun(
            halt_after=["report"],
            inputs={"repo_path": str(EXAMPLE), "intent": "load test listing the pets", "splunk_index": "web"},
        )
    finally:
        reset_upstream(token)

    report = state["report"]
    assert report["splunk_enabled"] is True
    correlation = report["correlation"]
    assert correlation["available"] is True

    # correlate runs four windowed queries (overview, timeline, by-path, root cause);
    # detect_anomalies adds two ML queries (predict forecast + anomalydetection).
    queries = fake.calls_to("splunk", "splunk_run_query")
    assert len(queries) == 6
    assert all("earliest=" in q.args["query"] for q in queries)
    assert set(correlation["queries"]) == {"rollup", "timeline", "by_path", "root_cause"}
    assert "index=web" in correlation["queries"]["rollup"]["spl"]

    # detect_anomalies runs Splunk's own predict + anomalydetection over the same window.
    anomalies = report["anomalies"]
    assert anomalies["available"] is True
    assert set(anomalies["queries"]) == {"forecast", "anomalies"}
    assert "predict" in anomalies["queries"]["forecast"]["spl"]
    assert "anomalydetection" in anomalies["queries"]["anomalies"]["spl"]
    assert "anomaly" in correlation["findings"]

    # the synthesized findings are what makes the Splunk side actionable.
    f = correlation["findings"]
    assert f["server_errors"] == 7
    assert f["worst_path"]["path"] == "/api/visits"
    assert f["worst_path"]["err_pct"] == "59.2"
    assert f["top_error"]["error_message"] == "database is locked"

    # splunk_preflight verified the index and captured sourcetypes + version before correlating.
    assert fake.calls_to("splunk", "splunk_get_index_info")
    preflight = report["mcp_provenance"]["splunk_preflight"]
    assert preflight["index"] == "web"
    assert preflight["exists"] is True
    assert preflight["event_count"] == 640
    assert preflight["sourcetypes"][0]["sourcetype"] == "access_json"
    assert preflight["server"]["version"] == "10.4.0"
    tools_called = {(c["server"], c["tool"]) for c in report["mcp_provenance"]["tool_calls"]}
    assert ("splunk", "splunk_get_info") in tools_called
    assert ("splunk", "splunk_get_metadata") in tools_called
    assert ("splunk", "splunk_run_query") in tools_called


async def test_enforcement_refuses_illegal_first_step():
    server = mount(build_application, name="kassi", upstream=FakeUpstream(K6_RESPONSES))
    async with Client(server) as client:
        result = await client.call_tool("step", {"action": "run_test", "inputs": {}})
        payload = result.structured_content
    assert payload.get("error") == "invalid_transition"
    assert "select_mode" in payload.get("valid_next_actions", [])
