"""LOCAL DEVELOPMENT ONLY: a minimal stdio MCP server that exposes the one Splunk
tool kassi calls (`splunk_run_query`) against a local Splunk instance over REST.

Production kassi uses the official Splunk MCP Server (Splunkbase 7931) via
`KASSI_SPLUNK_MCP_ENDPOINT` + `KASSI_SPLUNK_TOKEN`. That app must be installed on the
Splunk instance and its encrypted token generated through the app UI, which cannot be
scripted. This bridge lets you exercise kassi's full correlate path against a local
Splunk Enterprise without it. It is not the official server and is not part of the
shipped agent.

Wire it as kassi's splunk upstream for local testing:
    export KASSI_SPLUNK_MCP_CMD=uv
    export KASSI_SPLUNK_MCP_ENDPOINT=run            # placeholder; bridge ignores it
    export KASSI_SPLUNK_TOKEN=dev                    # placeholder
    # then point upstream args at this file (see scripts/verify_correlate_live.py)

Env:
    SPLUNK_MGMT  https://localhost:8089
    SPLUNK_USER  admin
    SPLUNK_PASS  kassi-admin-2026
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.request

from fastmcp import FastMCP

MGMT = os.environ.get("SPLUNK_MGMT", "https://localhost:8089")
USER = os.environ.get("SPLUNK_USER", "admin")
PASS = os.environ.get("SPLUNK_PASS", "kassi-admin-2026")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

mcp = FastMCP("splunk-dev-bridge")


def _post(path: str, data: dict) -> str:
    import base64

    body = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in data.items()).encode()
    req = urllib.request.Request(f"{MGMT}{path}", data=body)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode())
    with urllib.request.urlopen(req, context=_CTX) as resp:
        return resp.read().decode()


@mcp.tool
def splunk_run_query(query: str, earliest_time: str = "-15m", latest_time: str = "now") -> dict:
    """Run an SPL search and return its result rows."""
    text = _post(
        "/services/search/jobs/export",
        {"search": query, "output_mode": "json", "earliest_time": earliest_time, "latest_time": latest_time},
    )
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "result" in row:
            results.append(row["result"])
    return {"results": results}


@mcp.tool
def splunk_get_indexes() -> dict:
    """List index names."""
    text = _post("/services/data/indexes", {"output_mode": "json", "count": 0})
    data = json.loads(text)
    return {"indexes": [e["name"] for e in data.get("entry", [])]}


if __name__ == "__main__":
    mcp.run()
