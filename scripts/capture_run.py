"""Stream a real kassi diff-mode run, phase by phase, for a terminal recording.

    uv run python scripts/capture_run.py

Drives the same end-to-end run as scripts/verify_petclinic.py (real petclinic app, real
k6 through the k6 MCP server, live Splunk correlation, the configured model for the
per-phase work), but prints each Major Arcana phase as the Burr executor walks it, then the
verdict and the proposed fix. Nothing is canned. Used to record the demo GIF; the styling
matches `kassi pilot`.
"""
# ruff: noqa: E402  (sys.path must be set before importing the sibling verify_petclinic)

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))

from burr.lifecycle import PostRunStepHook
from dotenv import load_dotenv
from theodosia import UpstreamManager, bind_upstream
from theodosia.upstream import reset_upstream
from verify_petclinic import APP_DIR, APP_URL, _build_diff_repo, _wait_for_app

from kassi import arcana
from kassi.app import build_application
from kassi.cli import (
    _BOLD,
    _CYAN,
    _DIM,
    _MAGENTA,
    _RESET,
    _color_diff,
    _outcome_color,
    _phase_detail,
)
from kassi.upstream import k6_upstream_config, splunk_configured, splunk_upstream_config


def _status(action: str, st: dict) -> str:
    if st.get("error"):
        return "refused"
    if action == "screen":
        g = st.get("groundedness") or {}
        return "grounded" if g.get("grounded") else ("ungrounded" if g.get("available") else "screened")
    if action == "report":
        v = (st.get("verdict") or "").lower()
        if "regression" in v:
            return "regression"
        if "degradation" in v:
            return "degrading"
        return "failed" if v.startswith(("failed", "no run")) else "passed"
    return st.get("stage") or "ok"


class _Narrator(PostRunStepHook):
    def post_run_step(self, *, state, action, **_kw) -> None:
        name = action.name
        num, card, _ = arcana.ARCANA.get(name, ("", name, ""))
        st = dict(state.get_all()) if hasattr(state, "get_all") else dict(state)
        status = _status(name, st)
        col = _outcome_color(status)
        tools, facts = _phase_detail(name, st)
        if tools and facts:
            detail = f"{_CYAN}{tools}{_RESET}  {_DIM}·  {facts}{_RESET}"
        elif tools:
            detail = f"{_CYAN}{tools}{_RESET}"
        else:
            detail = f"{_DIM}{facts}{_RESET}"
        print(
            f"{_DIM}{arcana.SIGIL}{_RESET} {_DIM}{num:>4}{_RESET}  "
            f"{_BOLD}{_MAGENTA}{card:<19}{_RESET}{_DIM}{name:<18}{_RESET}"
            f"{_DIM}→{_RESET} {col}{status:<11}{_RESET} {detail}",
            flush=True,
        )


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if not splunk_configured():
        print("Splunk is not configured in .env. Aborting.")
        return

    app_env = {**os.environ, "SPLUNK_INDEX": "web"}
    app_env.setdefault("KASSI_SPLUNK_INSECURE", "1")
    app = subprocess.Popen(
        [
            "uv",
            "run",
            "--with",
            "fastapi",
            "--with",
            "uvicorn",
            "--with",
            "httpx",
            "python",
            str(APP_DIR / "app.py"),
            "serve",
        ],
        env=app_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_app():
        app.terminate()
        print("petclinic app did not come up. Aborting.")
        return
    diff_repo = _build_diff_repo()
    print(
        f"\n{_BOLD}{_MAGENTA}{arcana.SIGIL}  kassi is running.{_RESET} "
        f"{_CYAN}{arcana.TAGLINE}{_RESET}  {_DIM}(diff mode: POST /api/visits){_RESET}\n"
    )

    upstream = UpstreamManager({"k6": k6_upstream_config(), "splunk": splunk_upstream_config()})
    token = bind_upstream(upstream)
    try:
        application = build_application(hooks=[_Narrator()])
        _, _, state = await application.arun(
            halt_after=["report"],
            inputs={
                "repo_path": str(diff_repo),
                "ref": "HEAD~1",
                "target_base_url": APP_URL,
                "splunk_index": "web",
            },
        )
    finally:
        await upstream.aclose()
        reset_upstream(token)
        app.terminate()
        shutil.rmtree(diff_repo, ignore_errors=True)

    report = state["report"]
    verdict = report.get("verdict")
    print(f"\n{arcana.SIGIL}  {_BOLD}verdict:{_RESET} {_outcome_color(verdict)}{verdict}{_RESET}")
    remediation = report.get("remediation")
    if remediation:
        print(
            f"\n{_BOLD}{arcana.SIGIL}  proposed fix{_RESET} "
            f"{_DIM}(validated diff: applies cleanly and still parses){_RESET}"
        )
        print(_color_diff(remediation))


if __name__ == "__main__":
    asyncio.run(main())
