"""Ship a finished run's verdict and metrics to Splunk via HEC, for the kassi dashboard.

This is the one place kassi writes to Splunk rather than reading: after `report`, the run
summary (the k6 client-side metrics, the server-side correlation, the forecast, the verdict)
is posted as a single `kassi:run` event to the run index. A Splunk dashboard then renders the
client-and-server join over time. Gated on `KASSI_HEC_TOKEN`; a no-op when unset, so a run
never fails for lack of a dashboard.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request
from typing import Any

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def publish_configured() -> bool:
    return bool(os.environ.get("KASSI_HEC_TOKEN"))


def build_event(report: dict[str, Any]) -> dict[str, Any]:
    """Flatten the report into the flat field set the dashboard charts."""
    rr = report.get("run_result") or {}
    corr = report.get("correlation") or {}
    findings = corr.get("findings") or {}
    worst = findings.get("worst_path") or {}
    top_error = findings.get("top_error") or {}
    anomaly = report.get("anomalies") or {}
    endpoints = report.get("endpoints_tested") or []

    failed_rate = rr.get("http_req_failed_rate")
    return {
        "verdict": report.get("verdict"),
        "mode": report.get("mode"),
        "endpoints": ", ".join(f"{e.get('method')} {e.get('path')}" for e in endpoints) or None,
        "endpoint_count": len(endpoints),
        "k6_reqs": rr.get("http_reqs"),
        "k6_p95_ms": rr.get("http_req_duration_p95_ms"),
        "k6_failed_pct": round(failed_rate * 100, 1) if failed_rate is not None else None,
        "srv_events": findings.get("total_events"),
        "srv_5xx": findings.get("server_errors"),
        "srv_4xx": findings.get("client_errors"),
        "srv_p95_ms": findings.get("p95_ms"),
        "worst_path": worst.get("path"),
        "worst_err_pct": worst.get("err_pct"),
        "worst_p95_ms": worst.get("p95_ms"),
        "root_cause": top_error.get("error_message"),
        "root_cause_count": top_error.get("count"),
        "forecaster": anomaly.get("forecaster"),
        "forecast_p95_ms": anomaly.get("forecast_p95_ms"),
        "anomalous_buckets": anomaly.get("anomalous_buckets"),
        "forecast_breaches": anomaly.get("forecast_breaches"),
    }


def publish_run(
    report: dict[str, Any],
    *,
    url: str | None = None,
    token: str | None = None,
    index: str | None = None,
) -> bool:
    """Post the run summary to Splunk HEC. Returns True on success, False otherwise.
    Never raises: publishing is best-effort and must not fail a run."""
    token = token or os.environ.get("KASSI_HEC_TOKEN")
    if not token:
        return False
    url = url or os.environ.get("KASSI_HEC_URL", "http://localhost:8088")
    index = index or os.environ.get("KASSI_RUN_INDEX", "kassi_runs")

    payload = {
        "time": time.time(),
        "index": index,
        "sourcetype": "kassi:run",
        "event": build_event(report),
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/services/collector/event",
        data=body,
        headers={"Authorization": f"Splunk {token}"},
    )
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
