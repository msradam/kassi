# Local Splunk setup and verification

How to stand up a local Splunk Enterprise, seed sample target telemetry, and verify
kassi's correlation against it. Two parts: a reproducible local instance, and the one
step that must be done by hand (installing the official Splunk MCP Server app).

## 1. Splunk Enterprise (local, non-root)

The macOS trial ships as a `.dmg` containing a `.pkg`. To run without root, extract the
payload and launch from a user directory:

```bash
hdiutil attach ~/Downloads/splunk-10.4.0-*-darwin-arm64.dmg
pkgutil --expand-full "/Volumes/Splunk 10.4.0/.payload/Splunk_10.4.0.pkg" /tmp/splunk_pkg
mv /tmp/splunk_pkg/Splunk.pkg/Payload ~/splunk

# seed admin credentials before first start
cat > ~/splunk/etc/system/local/user-seed.conf <<'EOF'
[user_info]
USERNAME = admin
PASSWORD = kassi-admin-2026
EOF

SPLUNK_HOME=~/splunk ~/splunk/bin/splunk start --accept-license --answer-yes --no-prompt
```

Web UI: http://localhost:8000  ·  management REST: https://localhost:8089
Stop with `~/splunk/bin/splunk stop`.

## 2. Seed sample telemetry and verify the SPL

`scripts/seed_splunk.py` creates the `web` index, enables HTTP Event Collector, ingests
sample HTTP access events (a `status` and `response_time` field), and runs the exact SPL
kassi generates:

```bash
uv run python scripts/seed_splunk.py
```

Verified output against Splunk Enterprise 10.4.0:

```
SPL kassi generates:
  search index=web earliest=... latest=... | stats count AS total_events,
    sum(eval(if(status>=500,1,0))) AS server_errors,
    sum(eval(if(status>=400 AND status<500,1,0))) AS client_errors,
    avg(response_time) AS avg_response_ms
result: {"total_events": "400", "server_errors": "14", "client_errors": "10", "avg_response_ms": "21.36"}
```

## 3. End-to-end correlate against live Splunk

`scripts/verify_correlate_live.py` drives the whole kassi state machine. k6 responses are
canned (so the k6 MCP server need not be installed), and the `correlate` step runs through
`scripts/dev_splunk_mcp.py` (a local stdio MCP bridge to Splunk REST) against the live
instance. It emits telemetry during a simulated run window so the auto-generated, windowed
SPL returns real rows:

```bash
uv run python scripts/verify_correlate_live.py
```

Verified output:

```
verdict:         passed
splunk_enabled:  True
k6 http_reqs:    200
correlation SPL: search index=web earliest=... latest=... | stats count AS total_events, ...
correlation OK:  True
server-side rows: [{"total_events": "80", "server_errors": "7", "client_errors": "3", "avg_response_ms": "21.25"}]
```

This proves the full path: kassi action -> Theodosia `call_upstream` -> MCP (stdio) ->
Splunk REST -> windowed rollup back into the report.

## 4. The official Splunk MCP Server (production path, verified)

`dev_splunk_mcp.py` is a development convenience, not the shipped integration. The official
**Splunk MCP Server** app (Splunkbase 7931, v1.2.0) installs onto the Splunk instance, adds
the `mcp_tool_execute` capability, and generates an encrypted MCP token in its UI. It exposes
a streamable-HTTP endpoint at the management port:

```
https://localhost:8089/services/mcp
```

Put the endpoint and token in `.env` (git-ignored; see `.env.example`). `kassi serve` and the
verify script load it automatically:

```
KASSI_SPLUNK_MCP_ENDPOINT=https://localhost:8089/services/mcp
KASSI_SPLUNK_TOKEN=<encrypted-token>
KASSI_SPLUNK_INSECURE=1   # local self-signed cert only
```

kassi connects via `npx mcp-remote <endpoint> --header "Authorization: Bearer <token>"` and
calls the official `splunk_run_query` tool with the windowed SPL. Verified: the tools list is
`splunk_run_query, splunk_get_indexes, splunk_get_info, ...` and `verify_correlate_live.py`
returns the same server-side rollup through the official server as through the dev bridge.

Only the Splunkbase download and the UI token generation are manual; everything else is
scripted.
