"""Run any kassi demo scenario end-to-end (intent mode) against live Splunk.

    uv run python scripts/verify_scenario.py feed
    uv run python scripts/verify_scenario.py [petclinic|storefront|feed|gateway|orders]

Starts the target app, drives the whole FSM (real k6 + live Splunk + the configured model),
and prints the verdict, the server-side correlation, the anomaly scan, and the cited analysis.
Nothing is canned. Prereqs: local Splunk seeded by scripts/seed_splunk.py and .env set.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from theodosia import UpstreamManager, bind_upstream
from theodosia.upstream import reset_upstream

from kassi.app import build_application
from kassi.upstream import k6_upstream_config, splunk_configured, splunk_upstream_config

ROOT = Path(__file__).resolve().parents[1]

# scenario -> (port, intent that targets the flawed "new" endpoint)
SCENARIOS = {
    "petclinic": (8400, "load test recording a new visit"),
    "storefront": (8401, "load test the checkout endpoint placing an order"),
    "feed": (8402, "load test recording new activity events"),
    "gateway": (8403, "load test requesting a price quote"),
    "orders": (8404, "load test placing a new order"),
}


def _wait_for_app(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "petclinic"
    if name not in SCENARIOS:
        print(f"unknown scenario {name!r}. choose one of: {', '.join(SCENARIOS)}")
        return
    port, intent = SCENARIOS[name]
    app_dir = ROOT / "examples" / name
    url = f"http://127.0.0.1:{port}"

    load_dotenv(ROOT / ".env")
    if not splunk_configured():
        print("Splunk is not configured in .env (KASSI_SPLUNK_MCP_ENDPOINT + TOKEN). Aborting.")
        return

    app_env = {**os.environ, "SPLUNK_INDEX": "web"}
    app_env.setdefault("KASSI_SPLUNK_INSECURE", "1")
    app = subprocess.Popen(
        ["uv", "run", "--with", "fastapi", "--with", "uvicorn", "--with", "httpx",
         "python", str(app_dir / "app.py"), "serve"],
        cwd=str(ROOT),
        env=app_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    if not _wait_for_app(url):
        app.terminate()
        print(f"{name} app did not come up. Aborting.")
        return
    print(f"scenario:    {name} at {url}")
    print(f"intent:      {intent}")
    print(
        f"llm backend: {os.environ.get('KASSI_LLM', 'ollama')} ({os.environ.get('KASSI_MODEL', '')}); k6 + splunk live"
    )

    upstream = UpstreamManager({"k6": k6_upstream_config(), "splunk": splunk_upstream_config()})
    token = bind_upstream(upstream)
    try:
        application = build_application()
        print("running real k6 load through the k6 MCP server...")
        _, _, state = await application.arun(
            halt_after=["report"],
            inputs={
                "repo_path": str(app_dir),
                "intent": intent,
                "target_base_url": url,
                "splunk_index": "web",
            },
        )
    finally:
        await upstream.aclose()
        reset_upstream(token)
        app.terminate()

    report = state["report"]
    corr = report.get("correlation") or {}
    findings = corr.get("findings") or {}
    rr = report["run_result"] or {}
    anom = report.get("anomalies") or {}
    print("\nverdict:        ", report["verdict"])
    print("endpoints:      ", [f"{e['method']} {e['path']}" for e in report["endpoints_tested"]])
    print(
        f"k6 client-side:  {rr.get('http_reqs')} reqs, p95 {rr.get('http_req_duration_p95_ms')} ms, "
        f"{round((rr.get('http_req_failed_rate') or 0) * 100, 1)}% failed"
    )
    print(
        f"server-side:     {findings.get('total_events')} events, {findings.get('server_errors')} 5xx, "
        f"{findings.get('client_errors')} 4xx, p95 {findings.get('p95_ms')} ms"
    )
    if anom:
        print(
            f"anomaly scan:    {anom.get('method', 'forecast')} over {anom.get('buckets_analyzed', 0)} buckets, "
            f"peak {anom.get('peak_p95_ms')}ms, forecast {anom.get('forecast_p95_ms')}ms, "
            f"{anom.get('anomalous_buckets', 0)} anomalous, {anom.get('forecast_breaches', 0)} breach(es)"
        )
    print("\nby-path breakdown:")
    for row in corr.get("queries", {}).get("by_path", {}).get("rows", []):
        print("   ", json.dumps(row))
    if report.get("analysis"):
        print("\n=== analysis ===")
        for line in report["analysis"].splitlines():
            print("   ", line)


if __name__ == "__main__":
    asyncio.run(main())
