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

## Recording sequence (target: under 3:00)

The slide deck (`docs/deck/`) carries the title, the one pitch slide, the two scenario cards, and
the close. Intercut the live runs below. **Editing note:** a real run is a few minutes of
wall-clock; record it, then speed it up (6-10x) or cut to the streaming cards + the verdict line.
Judges need to *see Splunk queried live* (the #1 disqualifier is simulated Splunk), so keep the
k6/Splunk MCP activity and the dashboard on screen.

### 0. Open (slides 1-2, ~20s)
Title card, then the pitch slide. Optional quick cutaway: `uv run kassi render` (the 16-phase
state machine) while you say "a Burr state machine over MCP; the edges are the only legal moves,
every step sealed to a hash-chained ledger."

### 1. Scenario 1 — errors with a hidden cause (slide 4, ~60s) — the headline
Local Granite *drives* the run, one phase per turn:

```bash
# terminal A: start the target (healthy petclinic + a flawed POST /api/visits); leave running
SPLUNK_INDEX=web uv run --with fastapi --with uvicorn --with httpx \
  python examples/petclinic/app.py serve

# terminal B: Granite drives the FSM step by step
uv run kassi pilot --intent "load test recording a new visit" \
  --repo-path examples/petclinic --target-base-url http://127.0.0.1:8400 --splunk-index web
```

Say, over the streaming cards: "The local Granite model is *driving* the state machine, doing the
work as it goes: it authors the k6 script, runs real load through the k6 MCP server, correlates
with the official Splunk MCP Server, and forecasts the trend with the Splunk AI Toolkit. At The
Hanged Man it hands off to Granite Guardian to audit the analysis. Then Judgement." End on the
verdict: REGRESSION, `database is locked`, the remediation. **More than half the requests fail,
but only Splunk shows why.**

### 2. Scenario 2 — the opposite signature (slide 5, ~45s)
A different change, a different failure. storefront's `POST /api/checkout` gets *slower with zero
errors* (an N+1 query), invisible to the client, visible only in server-side `db_time`:

```bash
uv run python scripts/verify_scenario.py storefront
```

Say: "Same agent, same dashboard, a completely different signature. The client sees no errors at
all, just latency. The Splunk join surfaces the server-side `db_time` on the changed endpoint.
The regression is invisible to the error rate and obvious in the correlation." (This is the
fast, scripted path; the model still authors the analysis and Guardian still audits it.)

### 3. The Splunk dashboard (~30s)
Switch to the browser (`.../app/search/kassi_overview`). Point at:
- **the reading** — client vs server p95, the root cause, the forecast,
- **the agent's walk** — the run's own state-machine trace (The Fool → Judgement) with each
  phase's outcome and tool calls, keyed by Burr's `app_id`,
- **errors by endpoint** — the server-side truth from `index=web`.

Say: "kassi reads Splunk to observe your service, then publishes its *own* execution back to
Splunk. You see not just what the change did, but how the agent reached the verdict."

### 4. Close (slide 6-7, ~25s)
Optional cutaway: `uv run kassi verify <app-id>` (the tamper-proof ledger). Say: "Driver, writer,
and auditor all run on one local 8B model, Granite 4.1 and Guardian 4.1, the first ISO/IEC
42001-certified open LLM: on-prem, air-gapped, no per-token cost." End on the sign-off card.

## Notes
- Both scenarios can also run via the **pilot** (Granite drives) or **`verify_scenario.py`** (Burr
  drives; the model still authors + audits). Pilot is the wow; `verify_scenario.py` is faster and
  the most reliable for a clean take. Other signatures: `feed` (latency rising over the run, where
  the AI Toolkit forecast earns its keep), `gateway` (429 throttling), `orders` (504 cascade).
- Warm k6 first (`uv run kassi warm-k6`) so the first run does not stall.
- Record the dashboard right after a run (the errors-by-endpoint panel is a 60-minute window).

## If something misbehaves

- **First pilot run is slow / a phase stalls:** the k6 MCP extension wasn't warmed; run
  `uv run kassi warm-k6` once.
- **A phase says "degraded" or falls back:** the upstream (k6 or Splunk) was briefly unavailable;
  the run still completes. Re-run for a clean take.
- **Ollama timeouts:** confirm `OLLAMA_HOST` points at the Mini and both models are loaded
  (`curl $OLLAMA_HOST/api/tags`). Context size is capped at 32K (`KASSI_NUM_CTX`).
- **Dashboard panels empty:** the run must be recent (the errors-by-endpoint panel is a 60-minute
  window); record the dashboard right after a run.
