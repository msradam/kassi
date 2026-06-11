"""Drive the whole kassi FSM end-to-end with `correlate` hitting a real local Splunk.

k6 responses are canned (the k6 MCP server need not be installed); the Splunk step
runs through the dev bridge (scripts/dev_splunk_mcp.py) against live Splunk. Proves
that kassi -> theodosia call_upstream -> MCP -> Splunk REST returns a real rollup.

    uv run python scripts/verify_correlate_live.py

Prereqs: a local Splunk seeded by scripts/seed_splunk.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

from theodosia import UpstreamManager, bind_upstream
from theodosia.upstream import reset_upstream

import kassi.app as kassi_app
from kassi.app import build_application

ROOT = Path(__file__).resolve().parents[1]

MGMT = os.environ.get("SPLUNK_MGMT", "https://localhost:8089")
HEC = os.environ.get("SPLUNK_HEC", "http://localhost:8088")
USER = os.environ.get("SPLUNK_USER", "admin")
PASS = os.environ.get("SPLUNK_PASS", "kassi-admin-2026")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

K6_FAKE = {
    "validate_script": {"valid": True, "exit_code": 0},
    "run_script": {
        "success": True,
        "exit_code": 0,
        "metrics": {
            "http_reqs": {"count": 200},
            "http_req_duration": {"p(95)": 21.4},
            "http_req_failed": {"rate": 0.06},
            "checks": {"passes": 188, "fails": 12, "rate": 0.94},
        },
    },
}


def _hec_token() -> str:
    req = urllib.request.Request(
        f"{MGMT}/servicesNS/nobody/splunk_httpinput/data/inputs/http/kassi?output_mode=json"
    )
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode())
    with urllib.request.urlopen(req, context=_CTX) as resp:
        return json.loads(resp.read())["entry"][0]["content"]["token"]


def _ingest_burst(token: str, t0: float, n: int = 80, span: float = 3.0) -> None:
    """Simulate the target emitting telemetry during the load test window."""
    events = []
    for i in range(n):
        status = 500 if i % 12 == 0 else 404 if i % 25 == 0 else 200
        events.append(
            {
                "time": t0 + (i / n) * span,
                "index": "web",
                "sourcetype": "access_json",
                "event": {"status": status, "response_time": 8 + (i % 30), "path": "/api/pets"},
            }
        )
    body = "\n".join(json.dumps(e) for e in events).encode()
    req = urllib.request.Request(
        f"{HEC}/services/collector/event", data=body, headers={"Authorization": f"Splunk {token}"}
    )
    with urllib.request.urlopen(req, context=_CTX) as resp:
        resp.read()


class _RoutingManager:
    """k6 -> canned (with a realistic run window that emits live telemetry to Splunk);
    everything else -> the real Splunk dev bridge subprocess."""

    def __init__(self, splunk: UpstreamManager, hec_token: str):
        self._splunk = splunk
        self._token = hec_token

    async def call(self, server: str, tool: str, args: dict) -> object:
        if server == "k6":
            if tool == "run_script":
                t0 = time.time()
                _ingest_burst(self._token, t0)
                await asyncio.sleep(4.0)  # the load test runs for a few seconds
            return K6_FAKE[tool]
        return await self._splunk.call(server, tool, args)


class _FakeLLM:
    def generate(self, *, system: str, user: str, stop=None, format=None) -> str:
        return json.dumps({"test_taxonomy": "load", "parameterization": "static_examples", "endpoints": []})


async def main() -> None:
    os.environ.setdefault("KASSI_SPLUNK_MCP_ENDPOINT", "dev")
    os.environ.setdefault("KASSI_SPLUNK_TOKEN", "dev")

    def _fake_llm(*_a, **_k):
        return _FakeLLM()

    kassi_app.OllamaLLM = _fake_llm  # type: ignore[assignment]

    splunk = UpstreamManager(
        {
            "splunk": {
                "command": sys.executable,
                "args": [str(ROOT / "scripts" / "dev_splunk_mcp.py")],
                "cwd": str(ROOT),
            }
        }
    )
    token = bind_upstream(_RoutingManager(splunk, _hec_token()))
    try:
        app = build_application()
        _, _, state = await app.arun(
            halt_after=["report"],
            inputs={
                "repo_path": str(ROOT / "examples" / "petstore"),
                "intent": "load test listing the pets",
                "splunk_index": "web",
            },
        )
    finally:
        await splunk.aclose()
        reset_upstream(token)

    report = state["report"]
    print("verdict:        ", report["verdict"])
    print("splunk_enabled: ", report["splunk_enabled"])
    print("k6 http_reqs:   ", report["run_result"]["http_reqs"])
    corr = report["correlation"]
    print("correlation SPL:", corr["spl"])
    print("correlation OK: ", corr["available"])
    print("server-side rows:", json.dumps(corr["rows"], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
