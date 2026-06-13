"""Ship a finished run's verdict and metrics to Splunk via HEC, for the kassi dashboard.

This is the one place kassi writes to Splunk rather than reading: after `report`, the run
summary (the k6 client-side metrics, the server-side correlation, the forecast, the verdict)
is posted as a `kassi:run` event, and the agent's own state-machine walk is posted as one
`kassi:step` event per executed phase. Both are keyed by Burr's `app_id`, the same session id
`kassi sessions show` and Burr's tracker use, so the dashboard can render not just what the
change did but how the agent reached the verdict, step by step. Gated on `KASSI_HEC_TOKEN`;
a no-op when unset, so a run never fails for lack of a dashboard.
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
    grounded = (report.get("groundedness") or {}).get("grounded")
    session = report.get("session") or {}

    failed_rate = rr.get("http_req_failed_rate")
    return {
        "app_id": session.get("app_id"),
        "verdict": report.get("verdict"),
        "recommendation": report.get("recommendation"),
        "analysis": report.get("analysis"),
        "grounded": grounded,
        "steps_total": len(report.get("steps") or []),
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


def build_step_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One flat event per executed state-machine phase: the agent's walk, keyed by `app_id`."""
    app_id = (report.get("session") or {}).get("app_id")
    events = []
    for step in report.get("steps") or []:
        events.append(
            {
                "app_id": app_id,
                "seq": step.get("seq"),
                "phase": step.get("phase"),
                "card": step.get("card"),
                "card_num": step.get("card_num"),
                "status": step.get("status"),
                "tool_calls": step.get("tool_calls"),
                "tools": step.get("tools") or None,
            }
        )
    return events


def publish_run(
    report: dict[str, Any],
    *,
    url: str | None = None,
    token: str | None = None,
    index: str | None = None,
) -> bool:
    """Post the run summary and the per-phase step trace to Splunk HEC in one request. Returns
    True on success, False otherwise. Never raises: publishing is best-effort and must not fail
    a run."""
    token = token or os.environ.get("KASSI_HEC_TOKEN")
    if not token:
        return False
    url = url or os.environ.get("KASSI_HEC_URL", "http://localhost:8088")
    index = index or os.environ.get("KASSI_RUN_INDEX", "kassi_runs")

    base = time.time()
    payloads = [{"time": base, "index": index, "sourcetype": "kassi:run", "event": build_event(report)}]
    # Each step gets a slightly later time so Splunk orders the walk by phase sequence.
    for ev in build_step_events(report):
        payloads.append(
            {
                "time": base + (ev["seq"] or 0) * 0.001,
                "index": index,
                "sourcetype": "kassi:step",
                "event": ev,
            }
        )
    body = "\n".join(json.dumps(p) for p in payloads).encode()
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
