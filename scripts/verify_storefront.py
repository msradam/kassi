"""Second demo: drive kassi end-to-end against the storefront latency regression.

    KASSI_LLM=anthropic envchain ai uv run python scripts/verify_storefront.py

Nothing is canned. It starts the real FastAPI app (which ships access logs to
Splunk's HEC), runs REAL k6 through the k6 MCP server against the new POST /api/checkout
endpoint, and reads the server-side regression back from Splunk. Unlike petclinic, there
is no client-side error signal: every request returns 201. The story lives entirely in
the server-side db_time over the test window and in the predict + anomalydetection phase.
Prereqs: local Splunk seeded by scripts/seed_splunk.py and .env set.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from theodosia import UpstreamManager, bind_upstream
from theodosia.upstream import reset_upstream

from kassi import arcana
from kassi.app import build_application
from kassi.upstream import k6_upstream_config, splunk_configured, splunk_upstream_config

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "examples" / "storefront"
APP_URL = "http://127.0.0.1:8401"


def _wait_for_app(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{APP_URL}/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


async def main() -> None:
    load_dotenv(ROOT / ".env")
    if not splunk_configured():
        print("Splunk is not configured in .env (KASSI_SPLUNK_MCP_ENDPOINT + TOKEN). Aborting.")
        return

    app_env = {**os.environ, "SPLUNK_INDEX": "web"}
    app_env.setdefault("KASSI_SPLUNK_INSECURE", "1")
    app = subprocess.Popen(
        [
            "uv",
            "run",
            "--with",
            "fastapi",
            "--with",
            "uvicorn",
            "--with",
            "httpx",
            "python",
            str(APP_DIR / "app.py"),
            "serve",
        ],
        cwd=str(ROOT),
        env=app_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_app():
        app.terminate()
        print("storefront app did not come up. Aborting.")
        return
    print(f"target app:  storefront (slow new POST /api/checkout) at {APP_URL}")
    print(f"llm backend: {os.environ.get('KASSI_LLM', 'ollama')}; k6 + splunk run live")

    upstream = UpstreamManager({"k6": k6_upstream_config(), "splunk": splunk_upstream_config()})
    token = bind_upstream(upstream)
    try:
        application = build_application()
        print("running real k6 load through the k6 MCP server (this takes ~40s)...")
        _, _, state = await application.arun(
            halt_after=["report"],
            inputs={
                "repo_path": str(APP_DIR),
                "intent": "load test placing an order through checkout and listing products",
                "target_base_url": APP_URL,
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
    anomalies = report.get("anomalies") or {}
    print("\nverdict:        ", report["verdict"])
    print("endpoints:      ", [f"{e['method']} {e['path']}" for e in report["endpoints_tested"]])
    print("k6 client-side: ", report["run_result"])
    print("\n--- what Splunk gave us (server-side, over the exact window) ---")
    wp = findings.get("worst_path")
    print(
        f"totals:        {findings.get('total_events')} events, "
        f"{findings.get('server_errors')} 5xx, {findings.get('client_errors')} 4xx, "
        f"p95 {findings.get('p95_ms')} ms"
    )
    print("note:           zero errors. the regression is latency only, invisible to k6's error rate.")
    if wp:
        print(f"worst endpoint: {wp['path']}  err {wp['err_pct']}%  p95 {wp['p95_ms']} ms")
    print("\n--- the saturation onset, found statistically (predict + anomalydetection) ---")
    print(
        f"forecast band:  p95 {anomalies.get('forecast_p95_ms')} ms over "
        f"{anomalies.get('buckets_analyzed', 0)} buckets (peak {anomalies.get('peak_p95_ms')} ms)"
    )
    print(
        f"breaches:       {anomalies.get('forecast_breaches', 0)} band breach(es), "
        f"{anomalies.get('anomalous_buckets', 0)} anomalous bucket(s); "
        f"first at {anomalies.get('first_anomaly')}"
    )
    print("\nby-path breakdown:")
    for row in corr.get("queries", {}).get("by_path", {}).get("rows", []):
        print("   ", json.dumps(row))
    print("\nthe reading:")
    for line in (report.get("narration") or arcana.reading(report["verdict"])).splitlines():
        print("   ", line)


if __name__ == "__main__":
    asyncio.run(main())
