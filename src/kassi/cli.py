"""The ``kassi`` command: a Theodosia CLI branded for this agent.

``kassi serve`` mounts the workflow as an MCP server with the k6 upstream wired
in; ``kassi doctor``, ``kassi render``, ``kassi sessions``, ``kassi logs`` and the
rest come from theodosia. Sessions are stored under ``~/.kassi``.
"""

from __future__ import annotations

import asyncio
import subprocess

import typer
from dotenv import load_dotenv
from theodosia import mount
from theodosia.cli import build_cli, run

from kassi import arcana
from kassi.app import build_application
from kassi.pilot import drive_granite
from kassi.upstream import k6_warm_command, upstream


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
        if intent.strip():
            task = f'Run kassi in intent mode. Call step(select_mode) with inputs intent="{intent}"'
        else:
            task = f'Run kassi in diff mode. Call step(select_mode) with inputs repo_path="{repo_path}", ref="{ref}"'
        task += (
            f', target_base_url="{target_base_url}", splunk_index="{splunk_index}". '
            "Then drive every following phase to completion."
        )

        async def on_step(action: str, payload: dict) -> None:
            card = arcana.ARCANA.get(action, ("", action, ""))[1]
            stage = payload.get("stage") or payload.get("error") or "refused"
            print(f"{arcana.SIGIL}  {card} ({action}) -> {stage}")

        server = mount(build_application, name="kassi", upstream=upstream())
        print(f"{arcana.SIGIL}  Granite is driving. {arcana.TAGLINE}\n")
        kwargs: dict = {"prompt": task}
        if model:
            kwargs["model"] = model
        transcript = asyncio.run(drive_granite(server, **kwargs))
        state = transcript.get("final_state") or {}
        report = state.get("report") if isinstance(state, dict) else None
        verdict = (report or {}).get("verdict") if isinstance(report, dict) else None
        print(
            f"\n{arcana.SIGIL}  stopped on: {transcript.get('stopped_on')} ({len(transcript.get('turns', []))} steps)"
        )
        if verdict:
            print(f"{arcana.SIGIL}  verdict: {verdict}")

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

    return run(cli)


if __name__ == "__main__":
    raise SystemExit(main())
