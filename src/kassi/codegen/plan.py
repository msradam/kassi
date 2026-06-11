"""Single LLM call that returns a filled :class:`Plan`.

Uses Ollama's ``format=json`` to constrain output, round-trips through Pydantic,
one retry on parse failure, then falls back to a default plan. The composer is
fully functional on the default; the LLM call is enrichment, never load-bearing.
"""

from __future__ import annotations

import json

import structlog
from pydantic import ValidationError

from kassi.codegen.slots import DEFAULT_PLAN, EndpointEmphasis, Plan
from kassi.llm import LLM, LLMError
from kassi.state import Endpoint

log = structlog.get_logger()

SYSTEM = """You are a senior performance-testing engineer. Your only job is to fill
in a JSON plan that a deterministic codegen tool uses to assemble a k6 load test.

You do NOT write code. You do NOT explain. You return a single JSON object.

Schema:
{
  "test_taxonomy":   one of "load" | "regression_comparison" | "smoke",
  "parameterization": one of "static_examples" | "faker" | "csv_data" | "response_extracted",
  "endpoints": [
    { "method": "GET"|"POST"|...,
      "path":   "/api/...",
      "flags":  list of any of "risk_n_plus_one" | "risk_unbounded_query" | "risk_pagination" | "auth_required" }
  ]
}

Pick `regression_comparison` if the user describes a comparison-against-baseline
intent. Pick `smoke` only if the user explicitly asked for a 1-VU / few-iterations
sanity check. Otherwise pick `load`.

Pick `static_examples` unless the user gave a CSV path (`csv_data`), asked for
faker-style randomization (`faker`), or asked to chain calls so one response feeds
the next (`response_extracted`).

Set `auth_required` on any endpoint whose path or summary clearly requires a
logged-in user.

Output ONLY the JSON object. No prose, no markdown."""


def _user_prompt(
    *,
    endpoints: list[Endpoint],
    diff_text: str | None,
    user_intent: str | None,
) -> str:
    lines: list[str] = []
    if user_intent:
        lines.append(f"User intent: {user_intent}")
    lines.append("Changed endpoints:")
    for ep in endpoints:
        lines.append(f"  - {ep.method} {ep.path}")
    if diff_text:
        lines.append("\nDiff (truncated):")
        lines.append(diff_text[:1500])
    lines.append("\nReturn the JSON plan now.")
    return "\n".join(lines)


def fill_plan(
    *,
    endpoints: list[Endpoint],
    diff_text: str | None,
    user_intent: str | None,
    llm: LLM,
) -> Plan:
    user = _user_prompt(endpoints=endpoints, diff_text=diff_text, user_intent=user_intent)

    last_err: str | None = None
    for attempt in range(2):
        try:
            raw = llm.generate(system=SYSTEM, user=user, format="json")
        except LLMError as exc:
            last_err = f"llm_error: {exc}"
            log.warning("slot_filling_llm_error", attempt=attempt, error=str(exc))
            break

        try:
            plan = Plan.model_validate(json.loads(raw.strip()))
        except (json.JSONDecodeError, ValidationError) as exc:
            last_err = f"parse_error: {exc}"
            log.warning("slot_filling_parse_error", attempt=attempt, error=str(exc))
            user = f"{user}\n\nYour previous JSON was invalid: {exc}\nReturn ONLY the JSON object."
            continue

        log.info("slot_filling_ok", taxonomy=plan.test_taxonomy, endpoints=len(plan.endpoints))
        return plan

    log.warning("slot_filling_fell_back_to_default", last_error=last_err)
    return DEFAULT_PLAN.model_copy(
        update={"endpoints": [EndpointEmphasis(method=ep.method, path=ep.path) for ep in endpoints]}
    )
