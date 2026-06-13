from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from burr.core import State
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
    def generate(self, *, system: str, user: str, stop=None, format=None, documents=None) -> str:
        s = system.lower()
        if "narrat" in s or "tarot" in s:
            return "The Fool: the run begins.\nThe Tower: load applied.\nJudgement: passed."
        if "site-reliability" in s or "analysis" in s:
            return "Summary\nA load-only regression.\n\nEvidence\n- 5xx observed [Splunk correlate]\n\nRecommendation\nAdd pooling."
        return _K6_SCRIPT


async def _no_guidance(description: str) -> None:
    return None


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    monkeypatch.setattr(kassi_app, "make_llm", lambda *a, **k: _FakeLLM())
    monkeypatch.setattr(kassi_app, "fetch_k6_generation_guidance", _no_guidance)
    # Keep the Guardian screen hermetic; full-run tests exercise the phase with it disabled.
    monkeypatch.setenv("KASSI_GUARDIAN", "0")


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

    # the analyze and screen phases ran: a cited analysis exists and the screen verdict is recorded
    # (unavailable here because Guardian is disabled for the test).
    assert isinstance(report["analysis"], str) and report["analysis"].strip()
    assert report["groundedness"] == {"available": False, "grounded": None}

    # the run is keyed by Burr's own app_id (not a kassi-minted id), and the state-machine walk is
    # captured as an ordered step trace for the Splunk dashboard.
    assert report["session"]["app_id"]
    steps = report["steps"]
    phases = [s["phase"] for s in steps]
    assert phases[0] == "select_mode" and phases[-1] == "report"
    assert phases[-2] == "analyze"  # screen is skipped here (Guardian disabled)
    assert [s["seq"] for s in steps] == list(range(len(steps)))
    run_test_step = next(s for s in steps if s["phase"] == "run_test")
    assert "k6.run_script=ok" in run_test_step["tools"]

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

    # detect_anomalies runs the AI Toolkit's StateSpaceForecast + anomalydetection, both
    # scoped to the same window. The forecaster falls back to core `predict` only when the
    # StateSpaceForecast query is unusable; the fake upstream returns usable rows, so the
    # default StateSpaceForecast query is what gets recorded.
    anomalies = report["anomalies"]
    assert anomalies["available"] is True
    assert anomalies["forecaster"] == "statespace"
    assert set(anomalies["queries"]) == {"forecast", "anomalies"}
    assert "StateSpaceForecast" in anomalies["queries"]["forecast"]["spl"]
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


async def test_run_test_timeout_falls_back_to_scaffold(monkeypatch):
    """A hung authored-script run must not block the pipeline: run_test times out and
    re-runs the deterministic scaffold once, recording timeout then ok."""

    async def fake_call_upstream(server, tool, args):
        if args["script"] == "AUTHORED":
            await asyncio.sleep(5)  # never returns within the timeout
        return {
            "success": True,
            "exit_code": 0,
            "metrics": {"http_reqs": {"count": 42}, "http_req_duration": {"p(95)": 10.0}},
        }

    monkeypatch.setattr(kassi_app, "call_upstream", fake_call_upstream)
    monkeypatch.setattr(kassi_app, "_run_timeout", lambda _d: 0.05)

    state = State(
        {
            "generated_script": "AUTHORED",
            "scaffold_script": "SCAFFOLD",
            "plan": {"test_taxonomy": "load"},
            "mcp_calls": [],
        }
    )
    result = await kassi_app.run_test(state)
    assert result["stage"] == "ran"
    assert result["run_result"]["http_reqs"] == 42  # the scaffold run's payload
    statuses = [c["status"] for c in result["mcp_calls"] if c["tool"] == "run_script"]
    assert statuses == ["timeout", "ok"]


async def test_screen_records_guardian_groundedness(monkeypatch):
    """The screen phase passes the evidence as context and the analysis as the judged response to
    a separate Guardian model, and seals the grounded verdict to the report state."""
    monkeypatch.setenv("KASSI_GUARDIAN", "1")
    seen = {}

    class _FakeGuardian:
        def groundedness(self, *, context, response):
            seen["context"], seen["response"] = context, response
            return {"available": True, "grounded": True, "label": "No", "model": "granite3-guardian:8b"}

    monkeypatch.setattr(kassi_app.guardian, "make_guardian", lambda: _FakeGuardian())
    state = State(
        {"analysis": "Summary\nA load-only regression.", "analysis_context": "[Splunk correlate] 7 5xx"}
    )
    result = await kassi_app.screen(state)
    assert result["stage"] == "screened"
    assert result["groundedness"]["grounded"] is True
    assert result["groundedness"]["available"] is True
    assert seen["context"] == "[Splunk correlate] 7 5xx"
    assert seen["response"] == "Summary\nA load-only regression."


async def test_screen_skips_when_guardian_disabled(monkeypatch):
    monkeypatch.setenv("KASSI_GUARDIAN", "0")
    result = await kassi_app.screen(State({"analysis": "x", "analysis_context": "y"}))
    assert result["stage"] == "screened"
    assert result["groundedness"] == {"available": False, "grounded": None}


def test_publish_builds_run_and_step_events_keyed_by_app_id():
    """The run event and the per-phase step events both carry Burr's app_id, so the dashboard
    correlates the verdict with the agent's state-machine walk."""
    from kassi import publish

    report = {
        "session": {"app_id": "abc123", "partition_key": None, "sequence_id": 14},
        "verdict": "server-side regression: /api/visits ... cause: database is locked",
        "recommendation": "enable WAL",
        "groundedness": {"grounded": True},
        "steps": [
            {
                "seq": 0,
                "phase": "select_mode",
                "card": "The Fool",
                "card_num": "0",
                "status": "ok",
                "tool_calls": 0,
                "tools": [],
            },
            {
                "seq": 1,
                "phase": "run_test",
                "card": "The Tower",
                "card_num": "XVI",
                "status": "ok",
                "tool_calls": 1,
                "tools": ["k6.run_script=ok"],
            },
        ],
    }
    run_event = publish.build_event(report)
    assert run_event["app_id"] == "abc123"
    assert run_event["steps_total"] == 2

    steps = publish.build_step_events(report)
    assert [s["app_id"] for s in steps] == ["abc123", "abc123"]
    assert [s["phase"] for s in steps] == ["select_mode", "run_test"]
    assert steps[1]["tools"] == ["k6.run_script=ok"]


def test_remediate_applies_validates_and_diffs():
    """The model proposes SEARCH/REPLACE; kassi applies it, validates the AST, and renders a
    real unified diff. A non-matching block or invalid result is rejected, never returned."""
    from kassi import remediate

    source = "import time\n\n\ndef f():\n    x = 1\n    time.sleep(0.015)\n    return x\n"
    text = (
        "<<<<<<< SEARCH\n    time.sleep(0.015)\n=======\n"
        "    # removed sleep held inside the critical section\n>>>>>>> REPLACE"
    )
    blocks = remediate.parse_blocks(text)
    assert blocks == [("    time.sleep(0.015)", "    # removed sleep held inside the critical section")]

    patched = remediate.apply_blocks(source, blocks)
    assert patched is not None and "time.sleep" not in patched
    assert remediate.valid_python(patched)

    diff = remediate.unified(source, patched, "app.py")
    assert "--- a/app.py" in diff and "+++ b/app.py" in diff and "@@" in diff

    # a search block that does not match exactly is rejected (no partial/hallucinated edit)
    assert remediate.apply_blocks(source, [("does not exist", "x")]) is None
    # a syntactically invalid result is caught
    assert not remediate.valid_python("def (:\n")
    assert remediate.changed_file("+++ b/examples/petclinic/app.py") == "examples/petclinic/app.py"
