"""Unit tests for `_verdict`, locking in the two failure modes kassi-bench surfaced:
client-side throttling must not read as a server regression or a latency degradation, and a single
anomalous bucket on a fast (sub-floor) endpoint must not read as a degradation.

`_verdict` only subscripts its state, so a plain dict stands in for the Burr State here.
"""

from __future__ import annotations

from kassi.app import _LATENCY_FLOOR_MS, _verdict


def _state(findings: dict, anomalies: dict | None = None) -> dict:
    return {
        "error": None,
        "run_result": {"success": True, "exit_code": 0},
        "correlation": {"findings": findings},
        "anomalies": anomalies or {},
    }


def test_server_regression() -> None:
    v = _verdict(
        _state(
            {
                "worst_path": {"path": "/api/visits", "p95_ms": 300, "err_pct": 20},
                "top_error": {"error_message": "database is locked", "count": 100},
                "server_errors": 100,
                "client_errors": 0,
                "total_events": 500,
                "p95_ms": 300,
            }
        )
    )
    assert v.startswith("server-side regression")
    assert "/api/visits" in v and "database is locked" in v


def test_client_throttling_is_not_a_regression_or_degradation() -> None:
    # 4xx dominate, no 5xx, near-zero p95 with a spurious anomalous bucket.
    v = _verdict(
        _state(
            {
                "worst_path": {"path": "/api/quote", "p95_ms": 9, "err_pct": 0},
                "top_error": None,
                "server_errors": 0,
                "client_errors": 800,
                "total_events": 1000,
                "p95_ms": 9,
            },
            {"available": True, "anomalous_buckets": 1, "forecast_p95_ms": 11},
        )
    )
    assert "throttling" in v
    assert "degradation" not in v and "regression" not in v


def test_latency_degradation_above_floor() -> None:
    v = _verdict(
        _state(
            {
                "worst_path": {"path": "/api/checkout", "p95_ms": 90, "err_pct": 0},
                "top_error": None,
                "server_errors": 0,
                "client_errors": 0,
                "total_events": 500,
                "p95_ms": 90,
            },
            {"available": True, "anomalous_buckets": 1, "forecast_p95_ms": 95},
        )
    )
    assert "latency degradation" in v


def test_single_bucket_below_floor_stays_passed() -> None:
    # rising forecast AND a flagged bucket, but p95 is sub-floor jitter: must not cry wolf.
    v = _verdict(
        _state(
            {
                "worst_path": {"path": "/api/owners", "p95_ms": 8, "err_pct": 0},
                "top_error": None,
                "server_errors": 0,
                "client_errors": 0,
                "total_events": 500,
                "p95_ms": 8,
            },
            {"available": True, "anomalous_buckets": 1, "forecast_p95_ms": 12},
        )
    )
    assert v == "passed"
    assert _LATENCY_FLOOR_MS > 8
