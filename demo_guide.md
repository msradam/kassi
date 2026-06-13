# kassi demo guide

Exact commands for the 3-minute demo video. Two paths: the **pilot** path (local Granite drives
the whole run, the headline) and a **diff-mode** path (driven by Burr's executor, fully scripted
and fastest). Pick one for the recording; both produce the same dashboard.

## Before you record (one-time setup)

```bash
cd /Users/amsrahman/kassi

# 1. Splunk is local on this laptop; confirm it is up (web :8000, mgmt :8089).
~/splunk/bin/splunk status            # or: open http://localhost:8000

# 2. .env is present with KASSI_SPLUNK_MCP_ENDPOINT/TOKEN, KASSI_HEC_TOKEN, and
#    OLLAMA_HOST=http://192.168.1.237:11434 (Granite + Guardian run on the Mini).
cat .env | grep -E "OLLAMA_HOST|KASSI_LLM|HEC|SPLUNK_MCP" 

# 3. Granite + Guardian are pulled on the Mini (one-time):
#    ollama pull granite4.1:8b
#    ollama pull hf.co/ibm-granite/granite-guardian-4.1-8b-GGUF:Q4_K_M

# 4. Warm the k6 MCP extension so the first run does not stall.
uv run kassi warm-k6

# 5. Provision the Splunk dashboard + HEC token (idempotent).
uv run python scripts/setup_dashboard.py
```

Keep two windows ready: a **terminal** and a **browser** on the dashboard
(`http://localhost:8000/en-US/app/search/kassi_overview`).

## Recording sequence

### 1. The agent's shape  (~20s)

```bash
uv run kassi render      # the state machine: 16 phases, only legal edges
uv run kassi arcana      # the Major Arcana: a card per phase
```

Say: "kassi is a Burr state machine served over MCP. The graph's edges are the only legal moves,
and every step is sealed to a hash-chained ledger."

### 2. Granite drives a real run, end to end  (~90s) — the headline

Start the target app (a healthy petclinic plus a flawed `POST /api/visits`), then let the local
Granite model drive:

```bash
# terminal A: start the target (leave running)
SPLUNK_INDEX=web uv run --with fastapi --with uvicorn --with httpx \
  python examples/petclinic/app.py serve

# terminal B: Granite drives the FSM step by step
uv run kassi pilot \
  --intent "load test recording a new visit" \
  --repo-path examples/petclinic \
  --target-base-url http://127.0.0.1:8400 \
  --splunk-index web
```

Say, as the cards stream by: "The local Granite model is *driving* the state machine, one phase
per turn, doing the work as it goes: it authors the k6 script, runs real load through the k6 MCP
server, correlates with Splunk, and writes a cited analysis. At `The Hanged Man` it hands off to
Granite Guardian, a second model, to audit the analysis for groundedness. Then `Judgement`: the
verdict, sealed to the ledger." End on the verdict line (REGRESSION, `database is locked`, the
remediation).

### 3. The Splunk dashboard  (~40s)

Switch to the browser, refresh the dashboard. Point at:
- **the reading** — `/api/visits`, client vs server p95, `database is locked`, the forecast,
- **the agent's walk** — the run's own state-machine trace (The Fool → Judgement) with each
  phase's outcome and tool calls, keyed by Burr's `app_id`,
- **errors by endpoint** — the server-side truth from `index=web`.

Say: "kassi reads Splunk to observe your service, then publishes its *own* execution back to
Splunk. The dashboard shows not just what the change did, but how the agent reached the verdict."

### 4. The audit trail  (~20s)

```bash
uv run kassi sessions ls
uv run kassi verify <app-id>     # confirm the ledger was not tampered with
```

Say: "Every step and every refusal is on an immutable, hash-chained ledger, so the agent is safe
to run unattended. Driver, writer, and auditor all run on one local 8B model: on-prem,
air-gapped, no per-token cost."

## Alternative: diff-mode (fastest, fully scripted)

If you want the most reliable single command (Burr's executor drives; the model still authors and
audits), use the diff-mode scenario instead of the pilot:

```bash
uv run python scripts/verify_scenario.py petclinic
# or the other signatures: storefront | feed | gateway | orders
```

It starts the app, drives the whole FSM against live Splunk, and prints the verdict, the
correlation, the anomaly scan, and the cited analysis. The dashboard updates the same way.

## If something misbehaves

- **First pilot run is slow / a phase stalls:** the k6 MCP extension wasn't warmed; run
  `uv run kassi warm-k6` once.
- **A phase says "degraded" or falls back:** the upstream (k6 or Splunk) was briefly unavailable;
  the run still completes. Re-run for a clean take.
- **Ollama timeouts:** confirm `OLLAMA_HOST` points at the Mini and both models are loaded
  (`curl $OLLAMA_HOST/api/tags`). Context size is capped at 32K (`KASSI_NUM_CTX`).
- **Dashboard panels empty:** the run must be recent (the errors-by-endpoint panel is a 60-minute
  window); record the dashboard right after a run.
