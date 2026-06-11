"""Typed values that flow through the FSM state.

Burr state holds plain JSON-serialisable values (so the Theodosia ledger can
record every snapshot). These models are used at action boundaries for
validation, then dumped to / loaded from dicts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

MAX_FIX_ATTEMPTS = 3


class Endpoint(BaseModel):
    method: str
    path: str
    source_file: str | None = None


class RunResult(BaseModel):
    success: bool
    exit_code: int
    http_reqs: int = 0
    http_req_duration_p95_ms: float | None = None
    http_req_failed_rate: float | None = None
    checks_passed: int = 0
    checks_failed: int = 0
    summary_text: str = ""
    detail: str = ""
    raw_metrics: dict[str, Any] = Field(default_factory=dict)
