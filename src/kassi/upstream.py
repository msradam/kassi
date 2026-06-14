"""Upstream MCP servers wired into Theodosia.

kassi orchestrates two MCP servers from one agent-driven state machine:

* **k6**: the official Grafana k6 MCP server. Validates and runs the generated
  load test. Always configured. k6 2.0 ships this server as the ``k6 x mcp``
  subcommand (auto-provisioned on first run), so the single k6 binary is the
  only install needed; the standalone ``mcp-k6`` binary and the Docker image are
  still selectable.
* **splunk**: the official Splunk MCP Server (Splunkbase 7931). After a run, the
  FSM queries Splunk for the target service's server-side telemetry over the test
  window and correlates it with the client-side k6 metrics. Configured only when
  the endpoint + token env vars are set; absent that, kassi degrades gracefully to
  k6-only.

Theodosia maps a ``{"command": ..., "args": [...]}`` dict to a stdio transport and
spawns the server as a subprocess; action bodies reach a server with
``call_upstream(server, tool, args)``. The agent driving kassi only ever sees
kassi's single ``step`` tool, never these servers.

Install / configure:
    # k6: install k6 2.0+; the MCP server is the built-in `k6 x mcp` subcommand,
    # provisioned automatically on first run. No separate install needed.
    # Standalone binary or Docker are opt-in (see Env overrides).

    # Splunk: install the MCP Server app on your Splunk instance, generate an
    # encrypted token, copy the endpoint, then:
    export KASSI_SPLUNK_MCP_ENDPOINT="https://<host>/.../mcp"
    export KASSI_SPLUNK_TOKEN="<encrypted-token>"

Env overrides:
    KASSI_K6_CMD                        k6 MCP server command line (default "k6 x mcp";
                                        set to "mcp-k6" for the standalone binary)
    KASSI_K6_DOCKER / KASSI_K6_IMAGE    run the k6 server via Docker instead
    KASSI_SPLUNK_MCP_ENDPOINT           streamable-HTTP endpoint of the Splunk MCP Server
    KASSI_SPLUNK_TOKEN                  encrypted MCP token (sent as `Authorization: Bearer`)
    KASSI_SPLUNK_MCP_CMD                stdio bridge command (default "npx")
"""

from __future__ import annotations

import os
import shlex
from typing import Any

K6_SERVER = "k6"
SPLUNK_SERVER = "splunk"

DEFAULT_K6_CMD = "k6 x mcp"


def k6_upstream_config() -> dict[str, Any]:
    if os.environ.get("KASSI_K6_DOCKER"):
        image = os.environ.get("KASSI_K6_IMAGE", "grafana/mcp-k6:latest")
        # --add-host lets k6 inside the container reach a target on the host as
        # http://host.docker.internal:<port>.
        return {
            "command": "docker",
            "args": ["run", "--rm", "-i", "--add-host=host.docker.internal:host-gateway", image],
        }
    # k6 2.0's `k6 x mcp` is the same server as the standalone `mcp-k6` binary,
    # delivered as a subcommand extension. Both default to stdio transport, which
    # is what Theodosia spawns. Override the whole command line with KASSI_K6_CMD.
    cmd = shlex.split(os.environ.get("KASSI_K6_CMD", DEFAULT_K6_CMD))
    return {"command": cmd[0], "args": cmd[1:]}


def k6_warm_command() -> list[str]:
    """Argv that provisions and exits, to warm the `k6 x mcp` extension cache.

    The first `k6 x mcp` invocation downloads and caches a custom k6 binary
    (~5s); appending ``--help`` triggers that provisioning, then exits without
    starting the server. Harmless for the standalone binary and Docker forms too.
    """
    cfg = k6_upstream_config()
    return [cfg["command"], *cfg["args"], "--help"]


def splunk_configured() -> bool:
    return bool(os.environ.get("KASSI_SPLUNK_MCP_ENDPOINT") and os.environ.get("KASSI_SPLUNK_TOKEN"))


def splunk_upstream_config() -> dict[str, Any] | None:
    """Bridge to the Splunk MCP Server via ``mcp-remote``.

    The official Splunk client config runs ``npx -y mcp-remote <endpoint> --header
    'Authorization: Bearer <token>'`` to bridge stdio to the server's streamable-HTTP
    transport. Theodosia spawns that command directly as a stdio upstream, so the
    Splunk tool names (``splunk_run_query`` etc.) reach action bodies un-prefixed.
    """
    endpoint = os.environ.get("KASSI_SPLUNK_MCP_ENDPOINT")
    token = os.environ.get("KASSI_SPLUNK_TOKEN")
    if not (endpoint and token):
        return None
    command = os.environ.get("KASSI_SPLUNK_MCP_CMD", "npx")
    config: dict[str, Any] = {
        "command": command,
        # Pin mcp-remote: newer releases negotiate a transport the Splunk MCP Server rejects (405).
        "args": ["-y", "mcp-remote@0.1.38", endpoint, "--header", f"Authorization: Bearer {token}"],
    }
    if os.environ.get("KASSI_SPLUNK_INSECURE"):
        # Local Splunk's management port uses a self-signed cert; tell mcp-remote's
        # Node runtime to skip TLS verification. Do not set this against a real CA.
        config["env"] = {**os.environ, "NODE_TLS_REJECT_UNAUTHORIZED": "0"}
    return config


def upstream() -> dict[str, Any]:
    """The ``upstream=`` dict passed to ``mount`` / ``build_cli``."""
    servers: dict[str, Any] = {K6_SERVER: k6_upstream_config()}
    splunk = splunk_upstream_config()
    if splunk is not None:
        servers[SPLUNK_SERVER] = splunk
    return servers
