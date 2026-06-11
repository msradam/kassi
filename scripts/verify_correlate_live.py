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

from dotenv import load_dotenv
from theodosia import UpstreamManager, bind_upstream
from theodosia.upstream import reset_upstream

from kassi import arcana
from kassi.app import build_application
from kassi.upstream import splunk_configured, splunk_upstream_config

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


async def main() -> None:
    load_dotenv(ROOT / ".env")

    backend = os.environ.get("KASSI_LLM", "ollama").strip().lower()
    model = os.environ.get(
        "KASSI_MODEL", "claude-haiku-4-5" if backend == "anthropic" else "qwen2.5-coder:7b"
    )
    print(f"llm backend:     {backend} ({model}); falls back to a default plan if unreachable")

    if splunk_configured():
        print("splunk upstream: OFFICIAL Splunk MCP Server (from .env)")
        splunk_config = splunk_upstream_config()
    else:
        print("splunk upstream: local dev bridge (scripts/dev_splunk_mcp.py)")
        os.environ.setdefault("KASSI_SPLUNK_MCP_ENDPOINT", "dev")
        os.environ.setdefault("KASSI_SPLUNK_TOKEN", "dev")
        splunk_config = {
            "command": sys.executable,
            "args": [str(ROOT / "scripts" / "dev_splunk_mcp.py")],
            "cwd": str(ROOT),
        }

    splunk = UpstreamManager({"splunk": splunk_config})
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
    plan = report["plan"] or {}
    print(
        "scaffold plan:  ",
        f"taxonomy={plan.get('test_taxonomy')} parameterization={plan.get('parameterization')} "
        f"endpoints={len(plan.get('endpoints') or [])} (deterministic)",
    )
    print("splunk_enabled: ", report["splunk_enabled"])
    print("k6 http_reqs:   ", report["run_result"]["http_reqs"])
    corr = report["correlation"]
    print("correlation SPL:", corr["spl"])
    print("correlation OK: ", corr["available"])
    print("server-side rows:", json.dumps(corr["rows"]))

    prov = report["mcp_provenance"]
    print("k6 doc refs:    ", [r["slug"] for r in prov["k6_doc_refs"]])
    pf = prov["splunk_preflight"]
    if pf:
        print(
            "splunk preflight:",
            f"index={pf['index']} exists={pf['exists']} events={pf['event_count']} "
            f"sourcetypes={[s['sourcetype'] for s in pf['sourcetypes']]} "
            f"splunk={pf['server'].get('version')}",
        )
    print("mcp tool calls: ", [f"{c['server']}.{c['tool']}={c['status']}" for c in prov["tool_calls"]])
    print("the reading:")
    for line in (report.get("narration") or arcana.reading(report["verdict"])).splitlines():
        print("   ", line)


if __name__ == "__main__":
    asyncio.run(main())
