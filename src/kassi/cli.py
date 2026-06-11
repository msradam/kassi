"""The ``kassi`` command — a theodosia CLI branded for this agent.

``kassi serve`` mounts the workflow as an MCP server with the k6 upstream wired
in; ``kassi doctor``, ``kassi render``, ``kassi sessions``, ``kassi logs`` and the
rest come from theodosia. Sessions are stored under ``~/.kassi``.
"""

from __future__ import annotations

from theodosia.cli import build_cli, run

from kassi.app import build_application
from kassi.upstream import upstream


def main() -> int:
    cli = build_cli(
        "kassi",
        application=build_application,
        help="Diff-driven load-test agent: drive a Burr FSM over MCP; k6 work runs via the k6 MCP upstream.",
        server_name="kassi",
        home="~/.kassi",
        upstream=upstream(),
    )
    return run(cli)


if __name__ == "__main__":
    raise SystemExit(main())
