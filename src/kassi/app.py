"""The kassi workflow as a Burr state machine, served over MCP by Theodosia.

An agent drives it one ``step`` at a time. The graph's edges are the only legal
moves; illegal steps are refused with ``valid_next_actions`` and recorded. k6 work
is delegated to the Grafana k6 MCP server and the post-run correlation to the
Splunk MCP Server, both via ``call_upstream``. The driving agent never sees those
servers, only kassi's single ``step`` tool.

Flow:
    select_mode ─diff──→ read_diff → extract_endpoints ┐
                └intent─→ parse_intent ────────────────┴→ doc_lookup → scaffold → generate_script
    generate_script → validate_script ⇄ (retry) generate_script
    validate_script → run_test ─splunk?─→ splunk_preflight → correlate → report
                              └─else──────────────────────────────────→ report  (also on give-up)

``scaffold`` composes a deterministic k6 baseline from the OpenAPI spec; ``generate_script``
then has the model author the final script on top of it, guided by k6's own
``generate_script`` MCP prompt, and falls back to the scaffold when the model is absent.
``doc_lookup`` (k6 MCP docs) and ``splunk_preflight`` (Splunk index/metadata/info) are
MCP-native phases: both degrade gracefully via ``safe_upstream`` and record every upstream
tool call to ``mcp_calls`` for the report's provenance. ``report`` narrates the run with the
model, themed as a tarot reading (falling back to the static omens when the model is absent).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import structlog
from burr.core import ApplicationBuilder, Condition, State, action
from theodosia import call_upstream, mount, safe_upstream, tracker

from kassi import arcana, codegen, parse
from kassi.githost import get_diff
from kassi.k6gen import fetch_k6_generation_guidance
from kassi.llm import LLMError, make_llm
from kassi.state import MAX_FIX_ATTEMPTS, Endpoint
from kassi.upstream import K6_SERVER, SPLUNK_SERVER, splunk_configured, upstream

log = structlog.get_logger()


def _record(calls: list[dict[str, str]], server: str, tool: str, status: str) -> list[dict[str, str]]:
    """Append one upstream tool call to the provenance log (immutably)."""
    return [*calls, {"server": server, "tool": tool, "status": status}]


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


@action(reads=["endpoints", "mcp_calls"], writes=["doc_refs", "mcp_calls", "stage"])
async def doc_lookup(state: State) -> State:
    """Consult the k6 MCP documentation for the constructs kassi emits (HTTP requests, thresholds, checks, scenarios) and record version-grounded citations. Non-blocking: degrades to no references when the docs are unavailable."""
    calls = state["mcp_calls"]
    if not state["endpoints"]:
        return state.update(doc_refs=[], mcp_calls=calls, stage="documented")

    refs: list[dict[str, str]] = []
    sections = await safe_upstream(
        "k6_docs", K6_SERVER, "list_sections", {"root_slug": "using-k6", "depth": 2}, expect="dict"
    )
    calls = _record(calls, K6_SERVER, "list_sections", sections.status)
    if sections.usable:
        for slug in parse.select_doc_slugs(sections.data):
            doc = await safe_upstream(
                "k6_docs", K6_SERVER, "get_documentation", {"slug": slug}, expect="dict"
            )
            calls = _record(calls, K6_SERVER, "get_documentation", doc.status)
            if doc.usable:
                refs.append(parse.parse_documentation(slug, doc.data))
    log.info("doc_lookup_done", refs=len(refs))
    return state.update(doc_refs=refs, mcp_calls=calls, stage="documented")


@action(
    reads=["endpoints", "openapi_spec", "target_base_url"],
    writes=["plan", "scaffold_script", "stage", "error"],
)
async def scaffold(state: State) -> State:
    """Compose a deterministic, self-contained k6 scaffold from the OpenAPI spec (no model): per-endpoint requests with sample bodies, the baked base URL, and load options. This is the runnable baseline the next step builds on."""
    endpoints = [Endpoint(**e) for e in state["endpoints"]]
    if not endpoints:
        return state.update(stage="failed", error="scaffold: no endpoints to test")

    plan = codegen.default_plan(endpoints)
    script = codegen.compose(
        plan=plan,
        openapi_spec=state["openapi_spec"],
        endpoints=endpoints,
        base_url=state["target_base_url"],
    )
    log.info("scaffold_done", endpoints=len(endpoints))
    return state.update(plan=plan.model_dump(), scaffold_script=script, stage="scaffolded", error=None)


@action(
    reads=["scaffold_script", "endpoints", "user_intent", "fix_attempts", "validation_error", "mcp_calls"],
    writes=["generated_script", "stage", "fix_attempts", "mcp_calls"],
)
async def generate_script(state: State) -> State:
    """Author the final k6 script on top of the scaffold, using k6's own `generate_script` MCP prompt and best-practices to guide the model. Falls back to the scaffold when the model or guidance is unavailable."""
    scaffold_script = state["scaffold_script"]
    endpoints = [Endpoint(**e) for e in state["endpoints"]]
    description = parse.build_generation_description(
        endpoints, state["user_intent"], scaffold_script, state["validation_error"]
    )
    calls = state["mcp_calls"]

    guidance = await fetch_k6_generation_guidance(description)
    calls = _record(calls, K6_SERVER, "generate_script(prompt)", "ok" if guidance else "error")
    if guidance:
        system = guidance + "\n\nReturn ONLY the k6 script. No prose, no markdown fences."
    else:
        system = "You are a k6 expert. Return ONLY a single self-contained k6 script. No prose, no markdown fences."

    script = ""
    try:
        raw = await asyncio.to_thread(make_llm().generate, system=system, user=description)
        script = parse.extract_script(raw)
    except LLMError as exc:
        log.warning("generate_script_llm_failed", error=str(exc))

    if not script:
        log.info("generate_script_used_scaffold")
        script = scaffold_script

    retry_bump = 1 if state["validation_error"] else 0
    return state.update(
        generated_script=script,
        stage="generated",
        fix_attempts=state["fix_attempts"] + retry_bump,
        mcp_calls=calls,
    )


@action(
    reads=["generated_script", "scaffold_script", "fix_attempts", "mcp_calls"],
    writes=["validation_error", "generated_script", "stage", "mcp_calls"],
)
async def validate_script(state: State) -> State:
    """Validate the script via the k6 MCP `validate_script` tool (1 VU, 1 iteration)."""
    calls = state["mcp_calls"]
    try:
        payload = await call_upstream(K6_SERVER, "validate_script", {"script": state["generated_script"]})
    except Exception as exc:
        return state.update(
            validation_error=f"k6 MCP unavailable: {exc}",
            stage="failed_validation",
            mcp_calls=_record(calls, K6_SERVER, "validate_script", "error"),
        )

    calls = _record(calls, K6_SERVER, "validate_script", "ok")
    err = parse.parse_validation(payload)
    if err is None:
        return state.update(validation_error=None, stage="validated", mcp_calls=calls)
    if state["fix_attempts"] < MAX_FIX_ATTEMPTS:
        log.warning("validation_failed_retrying", attempts=state["fix_attempts"], error=err)
        return state.update(validation_error=err, stage="needs_fix", mcp_calls=calls)
    # Gave up fixing the authored script; run the deterministic scaffold if it is untried.
    if state["generated_script"] != state["scaffold_script"]:
        log.warning("validation_gave_up_using_scaffold", error=err)
        return state.update(
            validation_error=err,
            generated_script=state["scaffold_script"],
            stage="validated",
            mcp_calls=calls,
        )
    return state.update(validation_error=err, stage="failed_validation", mcp_calls=calls)


@action(
    reads=["generated_script", "mcp_calls"],
    writes=["run_result", "run_started_at", "run_ended_at", "stage", "error", "mcp_calls"],
)
async def run_test(state: State) -> State:
    """Execute the load test via the k6 MCP `run_script` tool and parse the metrics."""
    started = time.time()
    try:
        payload = await call_upstream(K6_SERVER, "run_script", {"script": state["generated_script"]})
    except Exception as exc:
        return state.update(
            run_result=None,
            stage="failed",
            error=f"k6 MCP run failed: {exc}",
            mcp_calls=_record(state["mcp_calls"], K6_SERVER, "run_script", "error"),
        )
    ended = time.time()
    result = parse.parse_run(payload)
    log.info("run_test_ok", reqs=result.http_reqs, exit_code=result.exit_code)
    return state.update(
        run_result=result.model_dump(),
        run_started_at=started,
        run_ended_at=ended,
        stage="ran",
        error=None,
        mcp_calls=_record(state["mcp_calls"], K6_SERVER, "run_script", "ok"),
    )


@action(
    reads=["splunk_index", "run_started_at", "run_ended_at", "mcp_calls"],
    writes=["splunk_preflight", "mcp_calls", "stage"],
)
async def splunk_preflight(state: State) -> State:
    """Before correlating, verify the target Splunk index exists and capture its event count, sourcetypes, and the Splunk version via the Splunk MCP `splunk_get_info` / `splunk_get_index_info` / `splunk_get_metadata` tools. Non-blocking: correlate still runs if a probe fails."""
    index = state["splunk_index"]
    earliest = int(state["run_started_at"] or (time.time() - 300))
    latest = int(state["run_ended_at"] or time.time())
    calls = state["mcp_calls"]

    info = await safe_upstream("splunk_info", SPLUNK_SERVER, "splunk_get_info", {}, expect="dict")
    calls = _record(calls, SPLUNK_SERVER, "splunk_get_info", info.status)
    idx = await safe_upstream(
        "splunk_index_info", SPLUNK_SERVER, "splunk_get_index_info", {"index_name": index}, expect="dict"
    )
    calls = _record(calls, SPLUNK_SERVER, "splunk_get_index_info", idx.status)
    meta = await safe_upstream(
        "splunk_metadata",
        SPLUNK_SERVER,
        "splunk_get_metadata",
        {"type": "sourcetypes", "index": index, "earliest_time": str(earliest), "latest_time": str(latest)},
        expect="dict",
    )
    calls = _record(calls, SPLUNK_SERVER, "splunk_get_metadata", meta.status)

    facts = parse.parse_index_facts(index, idx.data if idx.usable else None)
    preflight = {
        **facts,
        "sourcetypes": parse.parse_sourcetypes(meta.data) if meta.usable else [],
        "server": parse.parse_splunk_info(info.data) if info.usable else {},
    }
    log.info(
        "splunk_preflight_done",
        index=index,
        exists=facts["exists"],
        sourcetypes=len(preflight["sourcetypes"]),
    )
    return state.update(splunk_preflight=preflight, mcp_calls=calls, stage="preflighted")


@action(
    reads=["splunk_index", "run_started_at", "run_ended_at", "mcp_calls"],
    writes=["correlation", "mcp_calls", "stage"],
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
    return state.update(
        correlation=correlation,
        mcp_calls=_record(state["mcp_calls"], SPLUNK_SERVER, "splunk_run_query", result.status),
        stage="correlated",
    )


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
        "doc_refs",
        "splunk_preflight",
        "mcp_calls",
    ],
    writes=["report", "stage"],
)
async def report(state: State) -> State:
    """Assemble the final report and have the model narrate the run as a tarot reading. Terminal action."""
    verdict = _verdict(state)
    narration = await _narrate(state, verdict)
    summary = {
        "mode": state["mode"],
        "endpoints_tested": state["endpoints"],
        "plan": state["plan"],
        "validation_error": state["validation_error"],
        "error": state["error"],
        "run_result": state["run_result"],
        "splunk_enabled": state["splunk_enabled"],
        "correlation": state["correlation"],
        "mcp_provenance": {
            "tool_calls": state["mcp_calls"],
            "k6_doc_refs": state["doc_refs"],
            "splunk_preflight": state["splunk_preflight"],
        },
        "narration": narration,
        "verdict": verdict,
    }
    return state.update(report=summary, stage="done")


def _verdict(state: State) -> str:
    if state["error"]:
        return f"failed: {state['error']}"
    if state["run_result"] is None:
        return f"no run: {state['validation_error'] or 'validation gave up'}"
    rr = state["run_result"]
    return "passed" if rr.get("success") else f"ran with failures (exit {rr.get('exit_code')})"


_NARRATION_SYSTEM = (
    "You are narrating a load-test run, themed as a tarot reading. For each phase line you are "
    "given, write exactly one short, concrete sentence prefixed with its card name (for example "
    "'The Fool: ...'). Use the numbers from the facts. Keep each line under 18 words. No preamble, "
    "no markdown, one line per phase."
)


def _phase_facts(state: State, verdict: str) -> str:
    lines = [f"- The Fool (select_mode): {state['mode']} mode, {len(state['endpoints'])} endpoint(s)"]
    if refs := state["doc_refs"]:
        names = ", ".join(r.get("slug", "").split("/")[-1] for r in refs)
        lines.append(f"- The Hierophant (doc_lookup): consulted k6 docs ({names})")
    if plan := state["plan"]:
        lines.append(f"- The Chariot (scaffold): deterministic {plan.get('test_taxonomy', 'load')} scaffold")
    if state["validation_error"]:
        lines.append("- The Magician/Justice: authored script failed validation, ran the scaffold instead")
    else:
        lines.append("- The Magician/Justice: authored the script on the scaffold; it passed validation")
    if rr := state["run_result"]:
        lines.append(
            f"- The Tower (run_test): {rr.get('http_reqs')} requests, p95 {rr.get('http_req_duration_p95_ms')} ms, "
            f"failed rate {rr.get('http_req_failed_rate')}"
        )
    if state["splunk_enabled"]:
        pf = state["splunk_preflight"] or {}
        lines.append(
            f"- The Hermit (splunk_preflight): index {pf.get('index')}, exists={pf.get('exists')}, "
            f"events={pf.get('event_count')}"
        )
        rows = (state["correlation"] or {}).get("rows") or []
        if rows:
            lines.append(f"- The Lovers (correlate): server-side rollup {rows[0]}")
    lines.append(f"- Judgement (report): verdict = {verdict}")
    return "\n".join(lines)


def _omen_fallback(state: State, verdict: str) -> str:
    ran = ["select_mode", "doc_lookup", "scaffold", "generate_script", "validate_script"]
    if state["run_result"]:
        ran.append("run_test")
    if state["splunk_enabled"]:
        ran += ["splunk_preflight", "correlate"]
    ran.append("report")
    lines = []
    for phase in ran:
        card = arcana.ARCANA.get(phase)
        if card:
            _, name, omen = card
            lines.append(f"{name}: {omen}")
    lines.append(arcana.reading(verdict))
    return "\n".join(lines)


async def _narrate(state: State, verdict: str) -> str:
    facts = _phase_facts(state, verdict)
    try:
        text = await asyncio.to_thread(make_llm().generate, system=_NARRATION_SYSTEM, user=facts)
        if text and text.strip():
            return text.strip()
    except LLMError as exc:
        log.warning("narration_llm_failed", error=str(exc))
    return _omen_fallback(state, verdict)


def build_application():
    return (
        ApplicationBuilder()
        .with_actions(
            select_mode=select_mode,
            read_diff=read_diff,
            extract_endpoints=extract_endpoints,
            parse_intent=parse_intent,
            doc_lookup=doc_lookup,
            scaffold=scaffold,
            generate_script=generate_script,
            validate_script=validate_script,
            run_test=run_test,
            splunk_preflight=splunk_preflight,
            correlate=correlate,
            report=report,
        )
        .with_transitions(
            ("select_mode", "read_diff", Condition.expr("stage == 'selected' and mode == 'diff'")),
            ("select_mode", "parse_intent", Condition.expr("stage == 'selected' and mode == 'intent'")),
            ("read_diff", "report", Condition.expr("stage == 'failed'")),
            ("read_diff", "extract_endpoints", Condition.expr("stage == 'diffed'")),
            ("extract_endpoints", "doc_lookup", Condition.expr("stage == 'scoped'")),
            ("parse_intent", "report", Condition.expr("stage == 'failed'")),
            ("parse_intent", "doc_lookup", Condition.expr("stage == 'scoped'")),
            ("doc_lookup", "scaffold", Condition.expr("stage == 'documented'")),
            ("scaffold", "report", Condition.expr("stage == 'failed'")),
            ("scaffold", "generate_script", Condition.expr("stage == 'scaffolded'")),
            ("generate_script", "validate_script", Condition.expr("stage == 'generated'")),
            ("validate_script", "generate_script", Condition.expr("stage == 'needs_fix'")),
            ("validate_script", "run_test", Condition.expr("stage == 'validated'")),
            ("validate_script", "report", Condition.expr("stage == 'failed_validation'")),
            ("run_test", "splunk_preflight", Condition.expr("stage == 'ran' and splunk_enabled")),
            ("run_test", "report", Condition.expr("stage == 'ran' and not splunk_enabled")),
            ("run_test", "report", Condition.expr("stage == 'failed'")),
            ("splunk_preflight", "correlate", Condition.expr("stage == 'preflighted'")),
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
            doc_refs=[],
            plan=None,
            scaffold_script=None,
            generated_script=None,
            validation_error=None,
            fix_attempts=0,
            run_result=None,
            run_started_at=None,
            run_ended_at=None,
            splunk_preflight=None,
            correlation=None,
            mcp_calls=[],
            report=None,
            error=None,
        )
        .with_entrypoint("select_mode")
        .with_tracker(tracker(project="kassi"))
        .build()
    )


if __name__ == "__main__":
    mount(build_application, name="kassi", upstream=upstream()).run()
