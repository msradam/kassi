"""Turn a run's structured facts into a practical, cited analysis: what regressed, where,
why, the evidence (each fact attributed to the tool that produced it), and what to do.

`gather_evidence` pulls the facts and their sources. `compose_analysis` writes a deterministic
report from them (the fallback when no model is available). `ANALYSIS_SYSTEM` + `analysis_facts`
drive the model when one is configured, which writes the same sections more fluently.
"""

from __future__ import annotations

from typing import Any

ANALYSIS_SYSTEM = (
    "You are a site-reliability engineer writing a short, practical post-run analysis of a load "
    "test that has been correlated with the target's server-side telemetry. Use exactly these "
    "section headers, each on its own line followed by prose or '- ' bullets: Summary, Affected "
    "endpoints, Root cause, Evidence, Recommendation. In Evidence, keep each fact's source tag in "
    "square brackets exactly as given (for example [k6 run_script], [Splunk correlate]). Ground "
    "every claim in the provided facts only: never invent numbers, endpoints, or causes. If the "
    "data is thin, say so. Be concise, a few sentences or bullets per section. No preamble, no "
    "markdown emphasis, no closing remarks."
)

# Substring -> remediation hint for the deterministic fallback's Recommendation.
_REMEDIATION = [
    (
        ("database is locked", "lock"),
        "The write path serializes under concurrency. Add connection pooling, enable WAL mode, "
        "or shorten the held transaction so concurrent writers stop colliding.",
    ),
    (
        ("timeout", "timed out"),
        "Requests are exceeding a downstream or pool timeout under load. Raise pool size, tune the "
        "timeout, or add backpressure so the queue does not build.",
    ),
    (
        ("connection", "too many"),
        "The service is exhausting a connection or resource pool. Raise the limit or pool the "
        "resource so it is reused across requests.",
    ),
]


def _fmt(value: Any, unit: str = "") -> str:
    return "n/a" if value is None else f"{value}{unit}"


def gather_evidence(
    *,
    run_result: dict[str, Any] | None,
    findings: dict[str, Any],
    anomalies: dict[str, Any] | None,
    preflight: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    """Each fact with the upstream tool that produced it, for the Evidence section and citations."""
    out: list[tuple[str, str]] = []
    rr = run_result or {}
    if rr:
        failed = rr.get("http_req_failed_rate")
        out.append(
            (
                f"{_fmt(rr.get('http_reqs'))} requests driven, client p95 "
                f"{_fmt(rr.get('http_req_duration_p95_ms'), ' ms')}, "
                f"{_fmt(round(failed * 100, 1) if failed is not None else None, '%')} failed",
                "k6 run_script",
            )
        )
    if findings:
        out.append(
            (
                f"{_fmt(findings.get('total_events'))} server events over the window: "
                f"{_fmt(findings.get('server_errors'))} 5xx, {_fmt(findings.get('client_errors'))} 4xx, "
                f"p95 {_fmt(findings.get('p95_ms'), ' ms')}",
                "Splunk correlate",
            )
        )
        if wp := findings.get("worst_path"):
            out.append(
                (
                    f"worst route {wp.get('path')} at {_fmt(wp.get('err_pct'), '%')} errors, "
                    f"p95 {_fmt(wp.get('p95_ms'), ' ms')}",
                    "Splunk by-path query",
                )
            )
        if te := findings.get("top_error"):
            out.append(
                (
                    f"dominant server error '{te.get('error_message')}' x{_fmt(te.get('count'))}",
                    "Splunk root-cause query",
                )
            )
    if anomalies:
        algo = "StateSpaceForecast" if anomalies.get("forecaster") == "statespace" else "predict"
        out.append(
            (
                f"{algo} forecast p95 {_fmt(anomalies.get('forecast_p95_ms'), ' ms')} over "
                f"{_fmt(anomalies.get('buckets_analyzed'))} buckets; anomalydetection flagged "
                f"{_fmt(anomalies.get('anomalous_buckets'))} bucket(s)",
                "Splunk AI Toolkit",
            )
        )
    if preflight:
        out.append(
            (
                f"target index '{preflight.get('index')}', {_fmt(preflight.get('event_count'))} events, "
                f"Splunk {(preflight.get('server') or {}).get('version', '?')}",
                "Splunk preflight",
            )
        )
    return out


def recommend(findings: dict[str, Any]) -> str:
    te = (findings.get("top_error") or {}).get("error_message") or ""
    low = te.lower()
    for needles, hint in _REMEDIATION:
        if any(n in low for n in needles):
            return hint
    if findings.get("server_errors"):
        return (
            "Investigate the dominant server-side error on the worst endpoint. The regression is "
            "concurrency-dependent, so it will not reproduce at low request volume."
        )
    return (
        "No server-side errors were correlated. If the client saw latency, look at server-side "
        "time (db_time) on the worst endpoint rather than error rate."
    )


def analysis_facts(verdict: str, evidence: list[tuple[str, str]]) -> str:
    lines = [f"verdict: {verdict}", "", "facts (claim [source]):"]
    lines += [f"- {claim} [{source}]" for claim, source in evidence]
    return "\n".join(lines)


def compose_analysis(
    verdict: str,
    *,
    mode: str,
    endpoints: list[dict[str, Any]],
    findings: dict[str, Any],
    evidence: list[tuple[str, str]],
) -> str:
    """Deterministic, sectioned analysis from the facts: the fallback when no model is present."""
    wp = findings.get("worst_path") or {}
    te = findings.get("top_error") or {}
    eps = ", ".join(f"{e.get('method')} {e.get('path')}" for e in endpoints) or "the tested endpoint(s)"

    summary = (
        f"{wp.get('path', eps)} regressed under load: server-side p95 {_fmt(wp.get('p95_ms'), ' ms')} "
        f"with {_fmt(wp.get('err_pct'), '%')} of requests returning 5xx. The failures are server-side "
        f"and load-only, surfaced from a {mode}-driven test."
        if wp
        else f"The test on {eps} did not correlate a server-side error regression over the window."
    )

    affected = (
        f"- {wp.get('path')} ({wp.get('err_pct')}% 5xx, p95 {wp.get('p95_ms')} ms), the changed endpoint."
        if wp
        else f"- {eps}: no endpoint crossed the error threshold."
    )

    if te:
        cause = (
            f"The dominant server-side error is '{te.get('error_message')}' "
            f"({te.get('count')} occurrences). It appears only under concurrency, which is why "
            f"low-volume testing misses it."
        )
    else:
        cause = "No single dominant server-side error was isolated over the test window."

    evidence_lines = "\n".join(f"- {claim} [{source}]" for claim, source in evidence) or "- none"

    return (
        "Summary\n"
        f"{summary}\n\n"
        "Affected endpoints\n"
        f"{affected}\n\n"
        "Root cause\n"
        f"{cause}\n\n"
        "Evidence\n"
        f"{evidence_lines}\n\n"
        "Recommendation\n"
        f"{recommend(findings)} Re-run kassi after the fix to confirm the regression clears."
    )
