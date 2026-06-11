"""The kassi workflow as a Burr state machine, served over MCP by theodosia.

An agent drives it one ``step`` at a time. The graph's edges are the only legal
moves; illegal steps are refused with ``valid_next_actions`` and recorded. k6 work
is delegated to the Grafana k6 MCP server and the post-run correlation to the
Splunk MCP Server, both via ``call_upstream`` — the driving agent never sees those
servers, only kassi's single ``step`` tool.

Flow:
    select_mode ─diff──→ read_diff → extract_endpoints ┐
                └intent─→ parse_intent ────────────────┴→ generate_script
    generate_script → validate_script ⇄ (retry) generate_script
    validate_script → run_test ─splunk?─→ correlate → report
                              └─else────────────────→ report   (also on give-up)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import structlog
from burr.core import ApplicationBuilder, Condition, State, action
from theodosia import call_upstream, mount, safe_upstream, tracker

from kassi import codegen, parse
from kassi.githost import get_diff
from kassi.llm import DEFAULT_MODEL, OllamaLLM
from kassi.state import MAX_FIX_ATTEMPTS, Endpoint
from kassi.upstream import K6_SERVER, SPLUNK_SERVER, splunk_configured, upstream

log = structlog.get_logger()


def _load_spec(repo_path: str | None) -> dict[str, Any] | None:
    if not repo_path:
        return None
    spec_path = Path(repo_path) / "openapi.json"
    if not spec_path.exists():
        return None
    try:
        return json.loads(spec_path.read_text())
    except json.JSONDecodeError as exc:
        log.warning("openapi_parse_failed", error=str(exc))
        return None


@action(
    reads=[],
    writes=[
        "mode",
        "repo_path",
        "ref",
        "target_base_url",
        "user_intent",
        "splunk_index",
        "splunk_enabled",
        "stage",
        "error",
    ],
)
async def select_mode(
    state: State,
    repo_path: str = "",
    ref: str = "HEAD~1",
    target_base_url: str = "http://localhost:8000",
    intent: str = "",
    splunk_index: str = "main",
) -> State:
    """Start a run. Pass `intent` for natural-language mode, or just `repo_path`/`ref` for diff mode. `repo_path` is also where openapi.json is read from; `splunk_index` is the index holding the target's server-side telemetry."""
    intent = (intent or "").strip()
    return state.update(
        mode="intent" if intent else "diff",
        repo_path=repo_path or None,
        ref=ref,
        target_base_url=target_base_url,
        user_intent=intent or None,
        splunk_index=splunk_index or "main",
        splunk_enabled=splunk_configured(),
        stage="selected",
        error=None,
    )


@action(reads=["repo_path", "ref"], writes=["diff_text", "stage", "error"])
async def read_diff(state: State) -> State:
    """Read `git diff <ref>..HEAD` from the repo."""
    try:
        diff = await asyncio.to_thread(get_diff, Path(state["repo_path"]), state["ref"])
    except Exception as exc:
        log.error("read_diff_failed", error=str(exc))
        return state.update(diff_text=None, stage="failed", error=f"read_diff: {exc}")
    return state.update(diff_text=diff, stage="diffed", error=None)


@action(reads=["diff_text", "repo_path"], writes=["endpoints", "openapi_spec", "stage"])
async def extract_endpoints(state: State) -> State:
    """Pull changed routes from the diff and load the sibling openapi.json."""
    endpoints = parse.extract_endpoints_from_diff(state["diff_text"] or "")
    spec = _load_spec(state["repo_path"])
    log.info("extract_endpoints_ok", count=len(endpoints), has_spec=spec is not None)
    return state.update(
        endpoints=[ep.model_dump() for ep in endpoints],
        openapi_spec=spec,
        stage="scoped",
    )


@action(reads=["user_intent", "repo_path"], writes=["endpoints", "openapi_spec", "stage", "error"])
async def parse_intent(state: State) -> State:
    """Score OpenAPI operations against the natural-language intent and pick the top matches."""
    spec = _load_spec(state["repo_path"])
    if spec is None:
        return state.update(
            endpoints=[],
            openapi_spec=None,
            stage="failed",
            error=f"parse_intent: no readable openapi.json under {state['repo_path']!r}",
        )
    endpoints = parse.score_intent(spec, state["user_intent"] or "")
    log.info("parse_intent_ok", matched=len(endpoints))
    return state.update(
        endpoints=[ep.model_dump() for ep in endpoints],
        openapi_spec=spec,
        stage="scoped",
        error=None,
    )


@action(
    reads=[
        "endpoints",
        "openapi_spec",
        "diff_text",
        "user_intent",
        "target_base_url",
        "fix_attempts",
        "validation_error",
    ],
    writes=["plan", "generated_script", "stage", "fix_attempts", "error"],
)
async def generate_script(state: State) -> State:
    """Fill the plan with the LLM, then compose a single self-contained k6 script."""
    endpoints = [Endpoint(**e) for e in state["endpoints"]]
    if not endpoints:
        return state.update(stage="failed", error="generate_script: no endpoints to test")

    llm = OllamaLLM(model=os.environ.get("KASSI_MODEL", DEFAULT_MODEL))
    plan = await asyncio.to_thread(
        codegen.fill_plan,
        endpoints=endpoints,
        diff_text=state["diff_text"],
        user_intent=state["user_intent"],
        llm=llm,
    )
    script = codegen.compose(
        plan=plan,
        openapi_spec=state["openapi_spec"],
        endpoints=endpoints,
        base_url=state["target_base_url"],
    )
    retry_bump = 1 if state["validation_error"] else 0
    return state.update(
        plan=plan.model_dump(),
        generated_script=script,
        stage="generated",
        fix_attempts=state["fix_attempts"] + retry_bump,
        error=None,
    )


@action(reads=["generated_script", "fix_attempts"], writes=["validation_error", "stage"])
async def validate_script(state: State) -> State:
    """Validate the script via the k6 MCP `validate_script` tool (1 VU, 1 iteration)."""
    try:
        payload = await call_upstream(K6_SERVER, "validate_script", {"script": state["generated_script"]})
    except Exception as exc:
        return state.update(validation_error=f"k6 MCP unavailable: {exc}", stage="failed_validation")

    err = parse.parse_validation(payload)
    if err is None:
        return state.update(validation_error=None, stage="validated")
    if state["fix_attempts"] < MAX_FIX_ATTEMPTS:
        log.warning("validation_failed_retrying", attempts=state["fix_attempts"], error=err)
        return state.update(validation_error=err, stage="needs_fix")
    return state.update(validation_error=err, stage="failed_validation")


@action(reads=["generated_script"], writes=["run_result", "run_started_at", "run_ended_at", "stage", "error"])
async def run_test(state: State) -> State:
    """Execute the load test via the k6 MCP `run_script` tool and parse the metrics."""
    started = time.time()
    try:
        payload = await call_upstream(K6_SERVER, "run_script", {"script": state["generated_script"]})
    except Exception as exc:
        return state.update(run_result=None, stage="failed", error=f"k6 MCP run failed: {exc}")
    ended = time.time()
    result = parse.parse_run(payload)
    log.info("run_test_ok", reqs=result.http_reqs, exit_code=result.exit_code)
    return state.update(
        run_result=result.model_dump(),
        run_started_at=started,
        run_ended_at=ended,
        stage="ran",
        error=None,
    )


@action(
    reads=["splunk_index", "run_started_at", "run_ended_at"],
    writes=["correlation", "stage"],
)
async def correlate(state: State, splunk_spl: str = "") -> State:
    """Query Splunk (via the Splunk MCP `splunk_run_query` tool) for the target's server-side telemetry over the exact test window, and pair it with the k6 client-side metrics. Pass `splunk_spl` to override the default rollup."""
    earliest = state["run_started_at"] or (time.time() - 300)
    latest = state["run_ended_at"] or time.time()
    spl = splunk_spl.strip() or parse.build_correlation_spl(state["splunk_index"], earliest, latest)

    result = await safe_upstream(
        "splunk_telemetry", SPLUNK_SERVER, "splunk_run_query", {"query": spl}, expect="dict"
    )
    correlation = {
        "spl": spl,
        "status": result.status,
        "available": result.usable,
        "rows": parse.summarize_correlation(result.data),
        "detail": result.detail,
    }
    log.info("correlate_done", status=result.status, rows=len(correlation["rows"]))
    return state.update(correlation=correlation, stage="correlated")


@action(
    reads=[
        "endpoints",
        "plan",
        "run_result",
        "correlation",
        "validation_error",
        "error",
        "mode",
        "splunk_enabled",
    ],
    writes=["report", "stage"],
)
async def report(state: State) -> State:
    """Assemble the final report. Terminal action."""
    summary = {
        "mode": state["mode"],
        "endpoints_tested": state["endpoints"],
        "plan": state["plan"],
        "validation_error": state["validation_error"],
        "error": state["error"],
        "run_result": state["run_result"],
        "splunk_enabled": state["splunk_enabled"],
        "correlation": state["correlation"],
        "verdict": _verdict(state),
    }
    return state.update(report=summary, stage="done")


def _verdict(state: State) -> str:
    if state["error"]:
        return f"failed: {state['error']}"
    if state["run_result"] is None:
        return f"no run: {state['validation_error'] or 'validation gave up'}"
    rr = state["run_result"]
    return "passed" if rr.get("success") else f"ran with failures (exit {rr.get('exit_code')})"


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            select_mode=select_mode,
            read_diff=read_diff,
            extract_endpoints=extract_endpoints,
            parse_intent=parse_intent,
            generate_script=generate_script,
            validate_script=validate_script,
            run_test=run_test,
            correlate=correlate,
            report=report,
        )
        .with_transitions(
            ("select_mode", "read_diff", Condition.expr("stage == 'selected' and mode == 'diff'")),
            ("select_mode", "parse_intent", Condition.expr("stage == 'selected' and mode == 'intent'")),
            ("read_diff", "report", Condition.expr("stage == 'failed'")),
            ("read_diff", "extract_endpoints", Condition.expr("stage == 'diffed'")),
            ("extract_endpoints", "generate_script", Condition.expr("stage == 'scoped'")),
            ("parse_intent", "report", Condition.expr("stage == 'failed'")),
            ("parse_intent", "generate_script", Condition.expr("stage == 'scoped'")),
            ("generate_script", "report", Condition.expr("stage == 'failed'")),
            ("generate_script", "validate_script", Condition.expr("stage == 'generated'")),
            ("validate_script", "generate_script", Condition.expr("stage == 'needs_fix'")),
            ("validate_script", "run_test", Condition.expr("stage == 'validated'")),
            ("validate_script", "report", Condition.expr("stage == 'failed_validation'")),
            ("run_test", "correlate", Condition.expr("stage == 'ran' and splunk_enabled")),
            ("run_test", "report", Condition.expr("stage == 'ran' and not splunk_enabled")),
            ("run_test", "report", Condition.expr("stage == 'failed'")),
            ("correlate", "report", Condition.expr("stage == 'correlated'")),
        )
        .with_state(
            stage="new",
            mode=None,
            repo_path=None,
            ref="HEAD~1",
            target_base_url="http://localhost:8000",
            user_intent=None,
            splunk_index="main",
            splunk_enabled=False,
            diff_text=None,
            endpoints=[],
            openapi_spec=None,
            plan=None,
            generated_script=None,
            validation_error=None,
            fix_attempts=0,
            run_result=None,
            run_started_at=None,
            run_ended_at=None,
            correlation=None,
            report=None,
            error=None,
        )
        .with_entrypoint("select_mode")
        .with_tracker(tracker(project="kassi"))
        .build()
    )


if __name__ == "__main__":
    mount(build_application, name="kassi", upstream=upstream()).run()
