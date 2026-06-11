"""Pure parsing helpers: diff -> endpoints, intent -> endpoints, k6 MCP payloads -> metrics.

None of these touch the network or a subprocess. The k6 MCP payload parsers are
written defensively because the upstream's ``metrics`` object nests differently
depending on the k6 version (flat numbers, ``{"count": ...}`` envelopes, or a
``{"values": {...}}`` wrapper).
"""

from __future__ import annotations

import re
from typing import Any

from kassi.state import Endpoint, RunResult

_ROUTE_DECORATOR = re.compile(r'@(?:app|router)\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']')
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def extract_endpoints_from_diff(diff_text: str) -> list[Endpoint]:
    """Regex over added (`+`) diff lines for FastAPI route decorators."""
    endpoints: list[Endpoint] = []
    seen: set[tuple[str, str]] = set()
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        match = _ROUTE_DECORATOR.search(line)
        if not match:
            continue
        method, path = match.group(1).upper(), match.group(2)
        if (method, path) not in seen:
            endpoints.append(Endpoint(method=method, path=path))
            seen.add((method, path))
    return endpoints


def score_intent(spec: dict, intent: str) -> list[Endpoint]:
    """Score each operation by token overlap with the intent; top 3, else all."""
    intent_lc = intent.lower()
    scored: list[tuple[int, Endpoint]] = []
    paths = spec.get("paths", {}) or {}
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        tokens = [t for t in path.lower().split("/") if t and not t.startswith("{") and len(t) > 2]
        path_score = sum(1 for t in tokens if t in intent_lc)
        for method, op in ops.items():
            if method.upper() not in _HTTP_METHODS:
                continue
            summary = (op.get("summary") or "").lower() if isinstance(op, dict) else ""
            score = path_score + sum(1 for w in summary.split() if len(w) > 3 and w in intent_lc)
            if score > 0:
                scored.append((score, Endpoint(method=method.upper(), path=path)))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ep for _, ep in scored[:3]]

    return [
        Endpoint(method=method.upper(), path=path)
        for path, ops in paths.items()
        if isinstance(ops, dict)
        for method in ops
        if method.upper() in _HTTP_METHODS
    ]


def build_correlation_spl(index: str, earliest: float, latest: float) -> str:
    """Default server-side rollup over the test window.

    Tuned for HTTP-access-log-shaped data (a `status` field). Override per run by
    passing `splunk_spl` to the `correlate` step.
    """
    return (
        f"search index={index} earliest={int(earliest)} latest={int(latest)} "
        "| stats count AS total_events, "
        "sum(eval(if(status>=500,1,0))) AS server_errors, "
        "sum(eval(if(status>=400 AND status<500,1,0))) AS client_errors, "
        "avg(response_time) AS avg_response_ms"
    )


def summarize_correlation(data: Any) -> list[dict[str, Any]]:
    """Pull result rows out of a Splunk MCP `splunk_run_query` payload, tolerating
    the common envelope shapes (`{"results": [...]}`, `{"data": {...}}`, a bare list).
    """
    if isinstance(data, dict):
        for key in ("results", "rows", "data"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _metric_value(metrics: dict, name: str, *keys: str) -> float | None:
    """Pull one number from a k6 metric, tolerating flat / count / values shapes."""
    v = metrics.get(name)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        nest = v.get("values") if isinstance(v.get("values"), dict) else v
        for k in keys:
            got = nest.get(k)
            if isinstance(got, (int, float)):
                return float(got)
    return None


def parse_validation(payload: Any) -> str | None:
    """Return an error string when the k6 MCP `validate_script` payload is a failure."""
    if not isinstance(payload, dict):
        return f"unexpected validate_script response: {payload!r:.200}"
    if payload.get("valid") is True or payload.get("exit_code") == 0:
        return None
    detail = payload.get("error") or payload.get("stderr") or payload.get("stdout") or ""
    return f"k6 validate failed (exit {payload.get('exit_code')}): {str(detail)[:400]}"


def parse_run(payload: Any) -> RunResult:
    """Turn a k6 MCP `run_script` payload into a typed RunResult."""
    if not isinstance(payload, dict):
        return RunResult(
            success=False, exit_code=-1, detail=f"unexpected run_script response: {payload!r:.200}"
        )

    metrics = payload.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}

    p95 = _metric_value(metrics, "http_req_duration", "p(95)", "p95")
    failed = _metric_value(metrics, "http_req_failed", "rate", "value")
    reqs = _metric_value(metrics, "http_reqs", "count", "value", "rate")
    passed = _metric_value(metrics, "checks", "passes")
    failed_checks = _metric_value(metrics, "checks", "fails")

    return RunResult(
        success=bool(payload.get("success", payload.get("exit_code") == 0)),
        exit_code=int(payload.get("exit_code", -1)),
        http_reqs=int(reqs or 0),
        http_req_duration_p95_ms=p95,
        http_req_failed_rate=failed,
        checks_passed=int(passed or 0),
        checks_failed=int(failed_checks or 0),
        summary_text=str(payload.get("summary") or ""),
        detail=str(payload.get("error") or "")[:400],
        raw_metrics=metrics,
    )
