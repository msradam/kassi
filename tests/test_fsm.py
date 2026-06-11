from __future__ import annotations

import json
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
    }
}

K6_AND_SPLUNK_RESPONSES = {
    **K6_RESPONSES,
    "splunk": {
        "splunk_run_query": {
            "results": [
                {"total_events": "4210", "server_errors": "7", "client_errors": "0", "avg_response_ms": "9.4"}
            ]
        }
    },
}


class _FakeLLM:
    def generate(self, *, system: str, user: str, stop=None, format=None) -> str:
        return json.dumps({"test_taxonomy": "load", "parameterization": "static_examples", "endpoints": []})


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    monkeypatch.setattr(kassi_app, "OllamaLLM", lambda *a, **k: _FakeLLM())


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

    # k6 work went through the upstream, and the generated script is self-contained.
    validated = fake.calls_to("k6", "validate_script")
    assert len(validated) == 1
    script = validated[0].args["script"]
    assert "import http from 'k6/http'" in script
    assert "import" in script and "openapi-to-k6" not in script
    assert fake.calls_to("k6", "run_script")


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
    assert correlation["rows"][0]["server_errors"] == "7"
    assert "index=web" in correlation["spl"]

    queries = fake.calls_to("splunk", "splunk_run_query")
    assert len(queries) == 1
    assert "earliest=" in queries[0].args["query"]


async def test_enforcement_refuses_illegal_first_step():
    server = mount(build_application, name="kassi", upstream=FakeUpstream(K6_RESPONSES))
    async with Client(server) as client:
        result = await client.call_tool("step", {"action": "run_test", "inputs": {}})
        payload = result.structured_content
    assert payload.get("error") == "invalid_transition"
    assert "select_mode" in payload.get("valid_next_actions", [])
