"""Upstream MCP servers wired into theodosia.

kassi orchestrates two MCP servers from one agent-driven state machine:

* **k6** — the official Grafana k6 MCP server. Validates and runs the generated
  load test. Always configured.
* **splunk** — the official Splunk MCP Server (Splunkbase 7931). After a run, the
  FSM queries Splunk for the target service's server-side telemetry over the test
  window and correlates it with the client-side k6 metrics. Configured only when
  the endpoint + token env vars are set; absent that, kassi degrades gracefully to
  k6-only.

theodosia maps a ``{"command": ..., "args": [...]}`` dict to a stdio transport and
spawns the server as a subprocess; action bodies reach a server with
``call_upstream(server, tool, args)``. The agent driving kassi only ever sees
kassi's single ``step`` tool, never these servers.

Install / configure:
    # k6 (one of)
    brew tap grafana/grafana && brew install mcp-k6
    docker pull grafana/mcp-k6:latest        # then set KASSI_K6_DOCKER=1

    # Splunk: install the MCP Server app on your Splunk instance, generate an
    # encrypted token, copy the endpoint, then:
    export KASSI_SPLUNK_MCP_ENDPOINT="https://<host>/.../mcp"
    export KASSI_SPLUNK_TOKEN="<encrypted-token>"

Env overrides:
    KASSI_K6_MCP / KASSI_K6_MCP_ARGS    native k6 server command + args (default "mcp-k6")
    KASSI_K6_DOCKER / KASSI_K6_IMAGE    run the k6 server via Docker
    KASSI_SPLUNK_MCP_ENDPOINT           streamable-HTTP endpoint of the Splunk MCP Server
    KASSI_SPLUNK_TOKEN                  encrypted MCP token (sent as `Authorization: Bearer`)
    KASSI_SPLUNK_MCP_CMD                stdio bridge command (default "npx")
"""

from __future__ import annotations

import os
from typing import Any

K6_SERVER = "k6"
SPLUNK_SERVER = "splunk"


def k6_upstream_config() -> dict[str, Any]:
    if os.environ.get("KASSI_K6_DOCKER"):
        image = os.environ.get("KASSI_K6_IMAGE", "grafana/mcp-k6:latest")
        # --add-host lets k6 inside the container reach a target on the host as
        # http://host.docker.internal:<port>.
        return {
            "command": "docker",
            "args": ["run", "--rm", "-i", "--add-host=host.docker.internal:host-gateway", image],
        }
    command = os.environ.get("KASSI_K6_MCP", "mcp-k6")
    extra = os.environ.get("KASSI_K6_MCP_ARGS", "").split()
    return {"command": command, "args": extra}


def splunk_configured() -> bool:
    return bool(os.environ.get("KASSI_SPLUNK_MCP_ENDPOINT") and os.environ.get("KASSI_SPLUNK_TOKEN"))


def splunk_upstream_config() -> dict[str, Any] | None:
    """Bridge to the Splunk MCP Server via ``mcp-remote``.

    The official Splunk client config runs ``npx -y mcp-remote <endpoint> --header
    'Authorization: Bearer <token>'`` to bridge stdio to the server's streamable-HTTP
    transport. theodosia spawns that command directly as a stdio upstream, so the
    Splunk tool names (``splunk_run_query`` etc.) reach action bodies un-prefixed.
    """
    endpoint = os.environ.get("KASSI_SPLUNK_MCP_ENDPOINT")
    token = os.environ.get("KASSI_SPLUNK_TOKEN")
    if not (endpoint and token):
        return None
    command = os.environ.get("KASSI_SPLUNK_MCP_CMD", "npx")
    config: dict[str, Any] = {
        "command": command,
        "args": ["-y", "mcp-remote", endpoint, "--header", f"Authorization: Bearer {token}"],
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
