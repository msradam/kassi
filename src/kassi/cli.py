"""The ``kassi`` command: a Theodosia CLI branded for this agent.

``kassi serve`` mounts the workflow as an MCP server with the k6 upstream wired
in; ``kassi doctor``, ``kassi render``, ``kassi sessions``, ``kassi logs`` and the
rest come from theodosia. Sessions are stored under ``~/.kassi``.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from pathlib import Path

import typer
from dotenv import load_dotenv
from theodosia import UpstreamManager, bind_upstream, mount
from theodosia.cli import build_cli, run
from theodosia.upstream import reset_upstream

from kassi import arcana
from kassi.app import build_application
from kassi.pilot import drive_granite
from kassi.upstream import k6_upstream_config, k6_warm_command, splunk_upstream_config, upstream

# ANSI palette for the pilot stream, keyed to kassi's magenta cover scheme.
_MAGENTA, _CYAN, _GREEN, _YELLOW = "\033[38;5;205m", "\033[38;5;80m", "\033[38;5;78m", "\033[38;5;179m"
_RED = "\033[38;5;167m"
_DIM, _BOLD, _RESET = "\033[2m", "\033[1m", "\033[0m"


def _color_diff(diff: str) -> str:
    """Render a unified diff with added/removed lines colored, for the pilot summary."""
    out = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            out.append(f"{_BOLD}{line}{_RESET}")
        elif line.startswith("@@"):
            out.append(f"{_CYAN}{line}{_RESET}")
        elif line.startswith("+"):
            out.append(f"{_GREEN}{line}{_RESET}")
        elif line.startswith("-"):
            out.append(f"{_RED}{line}{_RESET}")
        else:
            out.append(f"{_DIM}{line}{_RESET}")
    return "\n".join(out)


def _outcome_color(stage: str) -> str:
    s = stage.lower()
    if any(k in s for k in ("regression", "fail", "ungrounded", "error", "no run")):
        return _MAGENTA
    if any(k in s for k in ("degrad", "timeout", "repaired", "refused")):
        return _YELLOW
    return _GREEN


def _phase_detail(action: str, st: dict) -> tuple[str, str]:
    """The trustworthy per-phase detail, read from the step payload's state: which MCP tools the
    phase called, and the key fact it produced. Returns (tool-calls summary, facts)."""
    calls = [c for c in (st.get("mcp_calls") or []) if c.get("phase") == action]
    tools = ""
    if calls:
        srv = calls[0]["server"]
        counts: dict[str, int] = {}
        for c in calls:
            name = c["tool"][len(srv) + 1 :] if c["tool"].startswith(srv + "_") else c["tool"]
            counts[name] = counts.get(name, 0) + 1
        parts = [f"{t}x{n}" if n > 1 else t for t, n in counts.items()]
        tools = f"{srv}: " + ", ".join(parts)
    findings = (st.get("correlation") or {}).get("findings") or {}
    facts = ""
    if action in ("parse_intent", "extract_endpoints"):
        facts = ", ".join(f"{e['method']} {e['path']}" for e in (st.get("endpoints") or [])[:2])
    elif action == "run_test":
        rr = st.get("run_result") or {}
        if rr:
            pct = round((rr.get("http_req_failed_rate") or 0) * 100)
            facts = f"{rr.get('http_reqs')} reqs, p95 {rr.get('http_req_duration_p95_ms')}ms, {pct}% failed"
    elif action == "splunk_preflight":
        pf = st.get("splunk_preflight") or {}
        facts = f"index {pf.get('index')}, {pf.get('event_count')} events"
    elif action == "correlate":
        wp, te = findings.get("worst_path") or {}, findings.get("top_error") or {}
        if wp:
            facts = f"{wp.get('path')} {wp.get('err_pct')}% 5xx, {te.get('error_message')}"
    elif action == "detect_anomalies":
        an = st.get("anomalies") or {}
        facts = f"forecast p95 {an.get('forecast_p95_ms')}ms, {an.get('anomalous_buckets')} anomalous"
    elif action == "analyze":
        rec = (st.get("recommendation") or "").strip()
        facts = rec[:58].rsplit(" ", 1)[0] + "..." if len(rec) > 58 else rec
    elif action == "screen":
        g = st.get("groundedness") or {}
        facts = (
            "verified against the evidence"
            if g.get("grounded")
            else ("flagged ungrounded" if g.get("available") else "")
        )
    elif action == "report":
        facts = "published to Splunk, sealed to the ledger"
    return tools, facts


_ROUTE_CHANGE = re.compile(r"^[+-]\s*@(?:app|router)\.(?:get|post|put|patch|delete)\(", re.MULTILINE)


def _git(repo: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return out.stdout.strip()


def _diff_touches_endpoint(diff: str) -> bool:
    """True when the diff adds or removes an HTTP route decorator (an API-surface change)."""
    return bool(_ROUTE_CHANGE.search(diff or ""))


async def _diagnose_diff(repo: str, ref: str, target_base_url: str, splunk_index: str) -> None:
    """Drive the whole workflow once, in diff mode, and print the verdict + proposed fix."""
    servers: dict = {"k6": k6_upstream_config()}
    splunk = splunk_upstream_config()
    if splunk is not None:
        servers["splunk"] = splunk
    upstream_mgr = UpstreamManager(servers)
    token = bind_upstream(upstream_mgr)
    try:
        _, _, state = await build_application().arun(
            halt_after=["report"],
            inputs={
                "repo_path": repo,
                "ref": ref,
                "target_base_url": target_base_url,
                "splunk_index": splunk_index,
            },
        )
    finally:
        await upstream_mgr.aclose()
        reset_upstream(token)
    report = (state or {}).get("report") or {}
    verdict = report.get("verdict")
    if verdict:
        print(f"{arcana.SIGIL}  {_BOLD}verdict:{_RESET} {_outcome_color(verdict)}{verdict}{_RESET}")
    remediation = report.get("remediation")
    if remediation:
        print(
            f"\n{_BOLD}{arcana.SIGIL}  proposed fix{_RESET} {_DIM}(validated diff, review before merging){_RESET}"
        )
        print(_color_diff(remediation))


def main() -> int:
    # Load KASSI_* / OLLAMA_* settings from a project .env (e.g. the Splunk endpoint
    # and token). Real environment variables already set take precedence.
    load_dotenv()
    cli = build_cli(
        "kassi",
        application=build_application,
        help=(
            f"{arcana.TAGLINE} Diff/intent-driven load-test agent that drives a Burr FSM "
            "over MCP and correlates k6 results with Splunk telemetry."
        ),
        server_name="kassi",
        home="~/.kassi",
        upstream=upstream(),
    )

    @cli.command("arcana")
    def arcana_cmd() -> None:
        """Print the Major Arcana: the card kassi draws at each workflow phase."""
        print(arcana.spread())

    @cli.command("pilot")
    def pilot(
        repo_path: str = typer.Option("", help="repo for diff mode (and where openapi.json lives)"),
        ref: str = typer.Option("HEAD~1", help="diff base ref"),
        intent: str = typer.Option("", help="natural-language intent (intent mode)"),
        target_base_url: str = typer.Option("http://localhost:8000", help="target service base URL"),
        splunk_index: str = typer.Option("main", help="index holding the target's telemetry"),
        model: str = typer.Option("", help="Ollama model tag (default: KASSI_MODEL / granite4.1:8b)"),
    ) -> None:
        """Let the local Granite model drive the FSM step by step (not Burr's executor).

        Granite reads the reachable actions and calls `step` for each phase itself, doing the
        per-phase work as it goes; the `screen` phase hands off to Granite Guardian. Driver,
        writer, and auditor are all local.
        """
        repo = str(Path(repo_path).resolve()) if repo_path else ""
        inputs = {"target_base_url": target_base_url, "splunk_index": splunk_index}
        if intent.strip():
            inputs["intent"] = intent
            inputs["repo_path"] = repo
        else:
            inputs["repo_path"] = repo
            inputs["ref"] = ref
        task = "Drive the kassi workflow to completion, one phase per turn, until it reaches report."

        async def on_step(action: str, payload: dict) -> None:
            num, card, _ = arcana.ARCANA.get(action, ("", action, ""))
            st = payload.get("state") or {}
            if payload.get("error"):
                status = "refused"
            elif action == "screen":
                g = st.get("groundedness") or {}
                status = (
                    "grounded" if g.get("grounded") else ("ungrounded" if g.get("available") else "screened")
                )
            elif action == "report":
                v = (st.get("verdict") or "").lower()
                if "regression" in v:
                    status = "regression"
                elif "degradation" in v:
                    status = "degrading"
                else:
                    status = "failed" if v.startswith(("failed", "no run")) else "passed"
            else:
                status = st.get("stage") or "ok"
            col = _outcome_color(status)
            tools, facts = _phase_detail(action, st)
            if tools and facts:
                detail = f"{_CYAN}{tools}{_RESET}  {_DIM}·  {facts}{_RESET}"
            elif tools:
                detail = f"{_CYAN}{tools}{_RESET}"
            else:
                detail = f"{_DIM}{facts}{_RESET}"
            print(
                f"{_DIM}{arcana.SIGIL}{_RESET} {_DIM}{num:>4}{_RESET}  "
                f"{_BOLD}{_MAGENTA}{card:<19}{_RESET}{_DIM}{action:<18}{_RESET}"
                f"{_DIM}→{_RESET} {col}{status:<11}{_RESET} {detail}"
            )

        server = mount(build_application, name="kassi", upstream=upstream())
        print(
            f"\n{_BOLD}{_MAGENTA}{arcana.SIGIL}  Granite is driving.{_RESET} {_CYAN}{arcana.TAGLINE}{_RESET}\n"
        )
        kwargs: dict = {"prompt": task, "prelude": ("select_mode", inputs), "on_step": on_step}
        if model:
            kwargs["model"] = model
        transcript = asyncio.run(drive_granite(server, **kwargs))
        state = transcript.get("final_state") or {}
        report = state.get("report") if isinstance(state, dict) else None
        verdict = (report or {}).get("verdict") if isinstance(report, dict) else None
        steps = len(transcript.get("turns", []))
        print(
            f"\n{_DIM}{arcana.SIGIL}  stopped on {transcript.get('stopped_on')}, {steps} phases driven by Granite{_RESET}"
        )
        if verdict:
            print(f"{arcana.SIGIL}  {_BOLD}verdict:{_RESET} {_outcome_color(verdict)}{verdict}{_RESET}")
        remediation = (report or {}).get("remediation") if isinstance(report, dict) else None
        if remediation:
            print(
                f"\n{_BOLD}{arcana.SIGIL}  proposed fix{_RESET} "
                f"{_DIM}(validated diff: applies cleanly and still parses; review before merging){_RESET}"
            )
            print(_color_diff(remediation))
        narration = (report or {}).get("narration") if isinstance(report, dict) else None
        if narration:
            print(f"\n{_DIM}{arcana.SIGIL}  the reading (model narration):{_RESET}")
            print(narration)

    @cli.command("warm-k6")
    def warm_k6() -> None:
        """Provision the k6 MCP server so the first real run does not stall.

        k6 2.0 fetches and caches the `k6 x mcp` extension binary on first use;
        running this once up front pays that cost outside the MCP session.
        """
        argv = k6_warm_command()
        print(f"warming k6 MCP upstream: {' '.join(argv)}")
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode == 0:
            print("k6 MCP upstream ready.")
        else:
            print(f"k6 warm-up exited {result.returncode}:\n{result.stderr.strip()}")
            raise SystemExit(result.returncode)

    @cli.command("watch")
    def watch(
        repo_path: str = typer.Option(".", help="git repo to guard"),
        target_base_url: str = typer.Option("http://localhost:8000", help="target service base URL"),
        splunk_index: str = typer.Option("main", help="index holding the target's telemetry"),
        interval: float = typer.Option(10.0, help="seconds between git-HEAD polls"),
        once: bool = typer.Option(False, help="diagnose the current HEAD once, then exit"),
    ) -> None:
        """Run kassi in the background, triggered on diff detection.

        Polls the repo's git HEAD; when a new commit lands that changes an HTTP endpoint, kassi
        drives the whole workflow in diff mode against that change (real k6 load correlated with
        Splunk telemetry), prints the verdict and a proposed fix, and publishes the run to Splunk.
        A change comes in, a verdict goes out, hands-free: the regression is caught the moment it is
        committed, not at 2am. Point it at a repo and forget it, or drop it in a post-commit hook.
        """
        repo = str(Path(repo_path).resolve())
        head = _git(repo, "rev-parse", "HEAD")
        if not head:
            print(f"{repo} is not a git repository.")
            raise SystemExit(1)
        print(
            f"{_BOLD}{_MAGENTA}{arcana.SIGIL}  kassi is watching{_RESET} {_CYAN}{repo}{_RESET} "
            f"{_DIM}· diff-triggered, polling every {interval:g}s{_RESET}\n"
        )
        last = "" if once else head
        try:
            while True:
                head = _git(repo, "rev-parse", "HEAD")
                if once or (head and head != last):
                    short = head[:8]
                    diff = _git(repo, "diff", "HEAD~1", "HEAD")
                    if _diff_touches_endpoint(diff):
                        print(
                            f"{arcana.SIGIL}  {_BOLD}change at {short}{_RESET} touches an endpoint, diagnosing...\n"
                        )
                        asyncio.run(_diagnose_diff(repo, "HEAD~1", target_base_url, splunk_index))
                        print()
                    else:
                        print(f"{_DIM}{arcana.SIGIL}  {short}: no endpoint change, skipping{_RESET}")
                    last = head
                    if once:
                        break
                time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\n{_DIM}{arcana.SIGIL}  kassi stopped watching.{_RESET}")

    return run(cli)


if __name__ == "__main__":
    raise SystemExit(main())
