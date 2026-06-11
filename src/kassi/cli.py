"""The ``kassi`` command: a Theodosia CLI branded for this agent.

``kassi serve`` mounts the workflow as an MCP server with the k6 upstream wired
in; ``kassi doctor``, ``kassi render``, ``kassi sessions``, ``kassi logs`` and the
rest come from theodosia. Sessions are stored under ``~/.kassi``.
"""

from __future__ import annotations

import subprocess

from dotenv import load_dotenv
from theodosia.cli import build_cli, run

from kassi import arcana
from kassi.app import build_application
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
