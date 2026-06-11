"""Pure parsing helpers: diff -> endpoints, intent -> endpoints, k6 MCP payloads -> metrics.

None of these touch the network or a subprocess. The k6 MCP payload parsers are
written defensively because the upstream's ``metrics`` object nests differently
depending on the k6 version (flat numbers, ``{"count": ...}`` envelopes, or a
``{"values": {...}}`` wrapper).
"""

from __future__ import annotations

import json
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


def build_correlation_queries(index: str, earliest: float, latest: float, span: str = "2s") -> dict[str, str]:
    """The server-side questions k6 cannot answer, one SPL each, scoped to the test window.

    rollup: overview. timeline: when it degraded (the saturation onset). by_path: which
    endpoint degraded. root_cause: the dominant server-side error. Tuned for
    HTTP-access-log-shaped data (`status`, `response_time`, `path`, `error_message`).
    """
    base = f"search index={index} earliest={int(earliest)} latest={int(latest)}"
    return {
        "rollup": (
            f"{base} | stats count AS total_events, "
            "sum(eval(if(status>=500,1,0))) AS server_errors, "
            "sum(eval(if(status>=400 AND status<500,1,0))) AS client_errors, "
            "perc95(response_time) AS p95_ms"
        ),
        "timeline": (
            f"{base} | timechart span={span} count AS reqs, "
            "sum(eval(if(status>=500,1,0))) AS errors, perc95(response_time) AS p95_ms"
        ),
        "by_path": (
            f"{base} | stats count AS reqs, sum(eval(if(status>=500,1,0))) AS errors, "
            "perc95(response_time) AS p95_ms by path "
            "| eval err_pct=round(100*errors/reqs,1) | sort -err_pct"
        ),
        "root_cause": f"{base} status>=500 | top limit=3 error_message",
    }


def summarize_findings(results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Synthesize the actionable verdict facts from the four query result sets."""
    rollup = (results.get("rollup") or [{}])[0]
    by_path = results.get("by_path") or []
    worst = by_path[0] if by_path else None

    onset = None
    for row in results.get("timeline") or []:
        if _to_int(row.get("errors")):
            onset = {"time": row.get("_time"), "errors": _to_int(row.get("errors"))}
            break

    causes = results.get("root_cause") or []
    top = causes[0] if causes else None

    return {
        "total_events": _to_int(rollup.get("total_events")),
        "server_errors": _to_int(rollup.get("server_errors")),
        "client_errors": _to_int(rollup.get("client_errors")),
        "p95_ms": _to_float(rollup.get("p95_ms")),
        "worst_path": (
            {"path": worst.get("path"), "err_pct": worst.get("err_pct"), "p95_ms": worst.get("p95_ms")}
            if worst and worst.get("path")
            else None
        ),
        "onset": onset,
        "top_error": (
            {"error_message": top.get("error_message"), "count": _to_int(top.get("count"))}
            if top and top.get("error_message")
            else None
        ),
    }


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


_SCRIPT_FENCE = re.compile(r"```(?:javascript|js|typescript|ts|k6)?\s*\n(.*?)```", re.DOTALL)


def extract_script(raw: Any) -> str:
    """Pull the k6 source out of a model response, stripping any markdown fence."""
    if not isinstance(raw, str):
        return ""
    match = _SCRIPT_FENCE.search(raw)
    return (match.group(1) if match else raw).strip()


def build_generation_description(
    endpoints: list[Endpoint], intent: str | None, scaffold: str, validation_error: str | None = None
) -> str:
    """Compose the request handed to k6's generate_script prompt and the model."""
    eps = "\n".join(f"  - {ep.method} {ep.path}" for ep in endpoints)
    parts = []
    if intent:
        parts.append(f"Intent: {intent}")
    parts.append("Target endpoints:\n" + eps)
    parts.append(
        "Build on this deterministic scaffold. Keep it a single self-contained file with "
        "plain k6/http calls and no local imports. Use minimal think time (no sleep, or at "
        "most 0.1s) so the runner's virtual users produce real concurrency, and do not declare "
        "your own `options` or scenarios block, the runner sets the VUs and duration:\n\n" + scaffold
    )
    if validation_error:
        parts.append(f"The previous attempt failed k6 validation:\n{validation_error}\nFix it.")
    return "\n\n".join(parts)


def build_fix_description(script: str, validation_error: str | None, endpoints: list[Endpoint]) -> str:
    """The repair request for the fix_script phase: the failing script plus the k6 error."""
    eps = "\n".join(f"  - {ep.method} {ep.path}" for ep in endpoints)
    return (
        "This k6 script failed validation. Fix it so it validates and runs. Keep it a single "
        "self-contained file with plain k6/http calls, no local imports, no `options` or "
        "scenarios block, and minimal think time.\n\n"
        f"Target endpoints:\n{eps}\n\n"
        f"k6 validation error:\n{validation_error}\n\n"
        f"Failing script:\n{script}"
    )


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


_DOC_TERMS = ("http-requests", "thresholds", "checks", "scenarios")


def flatten_sections(payload: Any) -> list[dict[str, str]]:
    """Flatten a k6 MCP `list_sections` tree to ``[{slug, title}, ...]``."""
    tree = payload.get("tree") if isinstance(payload, dict) else payload
    out: list[dict[str, str]] = []

    def walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            out.append({"slug": str(node.get("slug", "")), "title": str(node.get("title", ""))})
            walk(node.get("children"))

    walk(tree)
    return out


def select_doc_slugs(payload: Any, limit: int = 4) -> list[str]:
    """Pick the doc slugs for the k6 constructs the composer emits, from a live tree."""
    nodes = flatten_sections(payload)
    picked: list[str] = []
    for term in _DOC_TERMS:
        for node in nodes:
            slug = node["slug"]
            if term in slug.lower() and slug not in picked:
                picked.append(slug)
                break
    return picked[:limit]


def _doc_excerpt(content: Any, limit: int = 200) -> str:
    if not isinstance(content, str):
        return ""
    text = content
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:limit]
    return ""


def parse_documentation(slug: str, payload: Any) -> dict[str, str]:
    """Turn a k6 MCP `get_documentation` payload into a compact citation."""
    section = payload.get("section", {}) if isinstance(payload, dict) else {}
    content = payload.get("content", "") if isinstance(payload, dict) else ""
    return {
        "slug": slug,
        "title": str(section.get("title") or slug) if isinstance(section, dict) else slug,
        "excerpt": _doc_excerpt(content),
    }


def parse_index_facts(index_name: str, index_info: Any) -> dict[str, Any]:
    """Extract index facts from a Splunk MCP `splunk_get_index_info` payload."""
    rows = summarize_correlation(index_info)
    row = rows[0] if rows else {}
    return {
        "index": index_name,
        "exists": bool(row),
        "event_count": _to_int(row.get("totalEventCount")),
        "size_mb": _to_int(row.get("currentDBSizeMB")),
        "datatype": row.get("datatype"),
    }


def parse_sourcetypes(metadata: Any) -> list[dict[str, Any]]:
    """Extract sourcetypes from a Splunk MCP `splunk_get_metadata` payload."""
    return [
        {
            "sourcetype": r.get("sourcetype"),
            "count": _to_int(r.get("totalCount")),
            "last_seen": r.get("lastTimeIso"),
        }
        for r in summarize_correlation(metadata)
    ]


def parse_splunk_info(info: Any) -> dict[str, Any]:
    """Extract version/health from a Splunk MCP `splunk_get_info` payload."""
    rows = summarize_correlation(info)
    row = rows[0] if rows else {}
    return {
        "version": row.get("version"),
        "server_name": row.get("serverName"),
        "health": row.get("health_info"),
    }


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


def _k6_stderr_errors(stderr: Any) -> str:
    """Pull the human-meaningful error lines out of k6's JSON-lines stderr."""
    if not isinstance(stderr, str):
        return ""
    msgs: list[str] = []
    for line in stderr.strip().splitlines()[-15:]:
        try:
            obj = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if obj.get("level") == "error" and obj.get("msg"):
            msgs.append(str(obj["msg"]).splitlines()[0][:200])
    return " | ".join(dict.fromkeys(msgs))[:600]


def parse_validation(payload: Any) -> str | None:
    """Return an actionable error when the k6 MCP `validate_script` payload is a failure,
    surfacing the real k6 stderr plus the server's structured issues and suggestions, so
    the fix loop has something to act on instead of a bare exit code."""
    if not isinstance(payload, dict):
        return f"unexpected validate_script response: {payload!r:.200}"
    if payload.get("valid") is True and payload.get("exit_code") in (0, None):
        return None

    parts = [f"k6 validation failed (exit {payload.get('exit_code')})."]
    if err := _k6_stderr_errors(payload.get("stderr")):
        parts.append("k6 error: " + err)
    issues = payload.get("issues")
    if isinstance(issues, list):
        lines = [
            f"- {i['message']}" + (f" (fix: {i['suggestion']})" if i.get("suggestion") else "")
            for i in issues[:3]
            if isinstance(i, dict) and i.get("message")
        ]
        if lines:
            parts.append("issues:\n" + "\n".join(lines))
    if not err and len(parts) == 1:
        parts.append(str(payload.get("error") or payload.get("stderr") or payload.get("stdout") or "")[:400])
    return "\n".join(parts)[:1200]


_K6_REQS = re.compile(r"http_reqs[.\s]*:\s*([\d,]+)")
_K6_P95 = re.compile(r"http_req_duration[^\n]*?p\(95\)=([\d.]+)\s*(us|µs|ms|s)\b")
_K6_FAILED = re.compile(r"http_req_failed[.\s]*:\s*([\d.]+)%")
_K6_CHECKS = re.compile(r"✓\s*([\d,]+).*?✗\s*([\d,]+)", re.DOTALL)


def _to_ms(value: str, unit: str) -> float:
    v = float(value)
    return round(v / 1000 if unit in ("us", "µs") else v * 1000 if unit == "s" else v, 2)


def parse_run_stdout(stdout: Any) -> dict[str, Any]:
    """The k6 MCP returns the summary as stdout text, not structured metrics, so pull
    http_reqs / p95 / failure rate out of the default k6 end-of-test summary."""
    if not isinstance(stdout, str):
        return {}
    out: dict[str, Any] = {}
    if m := _K6_REQS.search(stdout):
        out["http_reqs"] = int(m.group(1).replace(",", ""))
    if m := _K6_P95.search(stdout):
        out["p95_ms"] = _to_ms(m.group(1), m.group(2))
    if m := _K6_FAILED.search(stdout):
        out["failed_rate"] = round(float(m.group(1)) / 100, 4)
    return out


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

    if reqs is None or p95 is None:
        summary = parse_run_stdout(payload.get("stdout"))
        reqs = reqs if reqs is not None else summary.get("http_reqs")
        p95 = p95 if p95 is not None else summary.get("p95_ms")
        failed = failed if failed is not None else summary.get("failed_rate")

    return RunResult(
        success=bool(payload.get("success", payload.get("exit_code") == 0)),
        exit_code=int(payload.get("exit_code", -1)),
        http_reqs=int(reqs or 0),
        http_req_duration_p95_ms=p95,
        http_req_failed_rate=failed,
        checks_passed=int(passed or 0),
        checks_failed=int(failed_checks or 0),
        summary_text=str(payload.get("summary") or payload.get("stdout") or "")[:600],
        detail=str(payload.get("error") or "")[:400],
        raw_metrics=metrics,
    )
