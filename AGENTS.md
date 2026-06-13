# AGENTS.md

Guidance for coding agents working in this repo. Read this before editing.

## What kassi is

kassi is a diff/intent-driven load-testing agent that closes the loop with
observability. Given a code change or a plain-language intent it picks the affected
HTTP endpoints, generates a k6 load test, runs it, then correlates the client-side
results with the target service's server-side telemetry in Splunk, and reports a
combined verdict.

The whole workflow is a [Burr](https://github.com/apache/burr) state machine served
over MCP by [Theodosia](https://msradam.github.io/theodosia/). An external agent
(Claude Code, Cursor, a custom loop) drives it one `step(action, inputs)` at a time.
The graph's edges are the only legal moves: an illegal step is refused with the list
of valid next actions, and every step and refusal is written to an immutable,
hash-chained ledger. One agent orchestrates two upstream MCP servers, neither visible
to the driver:

- **k6** (`grafana/mcp-k6`): validate and run the generated load test.
- **splunk** (official Splunk MCP Server, Splunkbase 7931): run SPL to read the
  target's server-side telemetry over the exact test window.

Built for the Splunk Agentic Ops Hackathon (Observability track; also Platform & Dev
Experience). Targets the Best Use of Splunk MCP Server bonus prize.

## Repo map

```
src/kassi/
  __init__.py        exports build_application
  app.py             THE FSM: async @action functions + build_application() + mount();
                     doc_lookup, scaffold, generate_script, fix_script (validation loop),
                     splunk_preflight, correlate (4 queries),
                     detect_anomalies (StateSpaceForecast/predict + anomalydetection),
                     analyze (writer: analysis + remediation), screen (auditor: Guardian
                     groundedness), report (narration + publish run & step trace)
  cli.py             `kassi` console command (Theodosia build_cli); loads .env; warm-k6
  upstream.py        k6 + splunk upstream MCP configs; splunk_configured()
  k6gen.py           fetch_k6_generation_guidance(): k6 MCP generate_script prompt + best_practices
  state.py           Pydantic models (Endpoint, RunResult); MAX_FIX_ATTEMPTS
  parse.py           pure helpers: diff->endpoints, intent scoring, k6 stdout/doc parsers,
                     build_correlation_queries + summarize_findings, generation helpers
  analysis.py        gather_evidence + compose_analysis (deterministic fallback) + recommend
  remediate.py       SEARCH/REPLACE edit -> apply -> ast validate -> difflib unified diff
  guardian.py        Granite Guardian 4.1 groundedness screen (the `screen` phase auditor)
  publish.py         ship kassi:run + per-phase kassi:step events to Splunk HEC (keyed by app_id)
  llm.py             OllamaLLM + AnthropicLLM clients, LLM Protocol, make_llm() factory
  arcana.py          theming only: phase -> Major Arcana card; kept out of state/report
  githost.py         git diff via subprocess (the only non-MCP shell-out kassi keeps)
  codegen/
    __init__.py      exports compose, Plan, default_plan, slots
    slots.py         the typed Plan (TestTaxonomy, Parameterization, flags) + default_plan()
    compose.py       pure-Python composer -> a single self-contained k6 scaffold
tests/test_fsm.py    offline FSM tests (FakeUpstream + fake LLM)
examples/petstore/   sample openapi.json for intent mode + tests
examples/petclinic/  headline demo target: healthy baseline + flawed POST /api/visits
                     (SQLite write-lock); FastAPI app ships access logs to Splunk HEC
examples/{storefront,feed,gateway,orders}/  more demo targets spanning the load-failure
                     taxonomy (latency/N+1, rising/soak, 429 throttling, downstream 504s);
                     same access_json HEC telemetry. See the scenario matrix in README.
scripts/             local Splunk helpers (see docs/SPLUNK_SETUP.md)
  seed_splunk.py        create index + HEC, ingest sample telemetry, verify the SPL
  dev_splunk_mcp.py     LOCAL DEV ONLY stdio MCP bridge to Splunk REST
  verify_correlate_live.py  drive the whole FSM; correlate hits live Splunk (canned k6)
  verify_petclinic.py   headline demo: real app + real k6 + real Splunk root-cause
docs/SUBMISSION.md   Devpost writeup draft
docs/SPLUNK_SETUP.md reproducible local Splunk install + verified results
architecture_diagram.md  required hackathon diagram (mermaid + prose)
.env / .env.example  Splunk endpoint + token (.env is git-ignored; never commit it)
```

## Architecture and the FSM

The state machine lives entirely in `app.py`. Flow:

```
select_mode ─diff──→ read_diff → extract_endpoints ┐
            └intent─→ parse_intent ────────────────┴→ doc_lookup → scaffold → generate_script
generate_script → validate_script ─needs_fix─→ fix_script → validate_script   (bounded loop)
validate_script → run_test ─splunk?─→ splunk_preflight → correlate → detect_anomalies ┐
                          └─else──────────────────────────────────────────────────┐  │
(also on validation give-up) ─────────────────────────────────────→ analyze → screen → report
```

Every path converges on `analyze` (writer: Granite 4.1 composes the cited analysis and, in diff
mode, the remediation diff) → `screen` (auditor: Granite Guardian 4.1 judges the analysis for
groundedness, non-blocking) → `report`. All the failure/no-splunk edges route to `analyze`, so
the analysis, the groundedness screen, and the publish always run.

`scaffold` composes a deterministic k6 baseline (no model); `generate_script` has the model
author the final script on top of it using k6's `generate_script` MCP prompt (fetched via
`k6gen.py`). `validate_script` gates the script: on failure it routes to `fix_script`, an
explicit correction node that repairs the script from the real k6 error (`parse_validation`
surfaces stderr + the server's `issues`/`suggestions`) and loops back to validation, bounded
by `MAX_FIX_ATTEMPTS`, then falls back to the scaffold. An unvalidated script never reaches
`run_test`.
`doc_lookup` (k6 docs) and `splunk_preflight` (Splunk index/metadata/info) are MCP-native
phases: both use `safe_upstream` (never block the run) and append to the `mcp_calls`
provenance list (each entry tagged with its `phase`) that `report` surfaces as `mcp_provenance`.
`report` narrates the run (a tarot reading, falling back to static omens when the model is
absent), assembles the per-phase step trace from the phase-tagged provenance, and publishes both
the run summary and the step trace to Splunk via `publish.py`, keyed by Burr's `app_id` (read from
`ApplicationContext.get()`, not a kassi-minted id).

Control flow is driven by a single `stage` string in state, plus a few flags
(`mode`, `splunk_enabled`, `fix_attempts`). Transitions are gated with
`Condition.expr("stage == '...'")`. The first matching edge from the current action
wins, so keep stage values mutually exclusive.

`select_mode` is the entrypoint and the only action that takes inputs from the driver
(`repo_path`, `ref`, `target_base_url`, `intent`, `splunk_index`). Inputs are normal
function parameters with defaults; Theodosia passes the `step` tool's `inputs` to
them. `correlate` also takes an optional `splunk_spl` input to override the SPL.

### Invariants when adding or changing an action

Theodosia's `kassi doctor --runtime` enforces most of these; run it after edits.

1. **Action bodies must be `async def`.** Theodosia awaits them and persists after
   each step. A sync body fails doctor.
2. **Wrap blocking work in `asyncio.to_thread`.** git and the Ollama HTTP call are
   sync; do not block the event loop (see `read_diff`, `generate_script`).
3. **State values must be JSON-serializable.** They are written to the ledger.
   Dump Pydantic models with `.model_dump()`; rebuild with `Model(**d)`. Do not put
   Pydantic objects, Paths, or datetimes directly in state. `time.time()` floats are
   fine (`run_started_at` / `run_ended_at`).
4. **Declare every field you read/write** in the `@action(reads=[...], writes=[...])`
   decorator. doctor checks that every `reads` key is covered by some `writes` or the
   initial state, and that every initial-state key is used.
5. **Add three things together** for a new action: register it in
   `.with_actions(...)`, add its `.with_transitions(...)` edges (gated on `stage`),
   and add any new state fields to `.with_state(...)` with a default.
6. **Errors route to `report`, not a dead end.** On failure set `stage="failed"` (or
   `"failed_validation"`) and ensure a transition to `report` exists. Never run a
   broken script; the validate retry loop is bounded by `MAX_FIX_ATTEMPTS`.

## Codegen invariants (do not break these)

- **The k6 script must be a single self-contained file.** The k6 MCP
  `run_script`/`validate_script` tools take one `script` string and cannot resolve
  local imports. `compose.py` emits plain `k6/http` calls with inlined `options` and
  `handleSummary`; the `generate_script` prompt tells the model to keep it one file.
  Do not reintroduce an imported client or aux files (this is why the original
  `@grafana/openapi-to-k6` substrate was dropped).
- **`scaffold` is the deterministic safety net.** It composes a runnable script from
  the OpenAPI schema with a default load `Plan`, no model. `generate_script` then has
  the model author the final script on top of it (the model authors k6 source now, a
  deliberate change), guided by k6's own `generate_script` MCP prompt. On model
  absence, empty output, or validation give-up, the pipeline runs the scaffold. So the
  pipeline must always work with the model backend (Ollama/Granite or Anthropic) unreachable.
- **The model never writes SPL.** `parse.build_correlation_spl` composes the
  correlation query in pure Python; keep it that way.
- **The analysis is grounded, not free-form, and independently screened.** `analyze` builds an
  evidence list (`analysis.py`) and passes it to the writer model as documents. The default
  model, IBM `granite4.1:8b`, grounds on them via its document role (`llm.OllamaLLM` emits
  `document_<source>` messages); Anthropic inlines them. `analysis.compose_analysis` is the
  deterministic fallback. Keep evidence facts paired with their source tool so citations stay
  accurate. The `screen` phase then re-checks the analysis against that same evidence with
  Granite Guardian 4.1 (`guardian.py`); the writer is never trusted on its own word.
- Sample request data in the scaffold is derived best-effort from the OpenAPI schema.
  It only has to exercise the endpoint shape, not be semantically perfect.

## Upstream MCP servers and config

`upstream.py` builds the `upstream=` dict passed to `mount`/`build_cli`. Theodosia
maps a `{"command","args"}` dict to a stdio transport and reaches tools with
`call_upstream(server, tool, args)` / `safe_upstream(...)` (the latter never raises
and classifies the result). The driving agent never sees these servers.

- **k6** is always configured. Default is the built-in `k6 x mcp` subcommand
  (k6 2.0+, provisioned on first run; `kassi warm-k6` warms the cache).
  `KASSI_K6_CMD=mcp-k6` selects the standalone binary; `KASSI_K6_DOCKER=1` runs
  via Docker. All three speak stdio and expose the same tools, so codegen and parsers
  are unaffected. kassi uses the k6 `list_sections` + `get_documentation` tools
  (doc_lookup), `validate_script` + `run_script` tools, and the `generate_script` prompt
  + `docs://k6/best_practices` resource (fetched in `k6gen.py` with a short-lived fastmcp
  client, since theodosia's upstream API calls tools only).
- **splunk** is configured only when `KASSI_SPLUNK_MCP_ENDPOINT` and
  `KASSI_SPLUNK_TOKEN` are set. `select_mode` records `splunk_enabled =
  splunk_configured()`; the graph branches on it, and both `splunk_preflight` and
  `correlate` degrade gracefully (via `safe_upstream`) when absent. kassi calls the
  Splunk tools: `splunk_get_info` + `splunk_get_index_info` + `splunk_get_metadata`
  (splunk_preflight) and `splunk_run_query` x6 (correlate runs a rollup, timeline,
  by-path, and root-cause query, then `summarize_findings` synthesizes the verdict;
  detect_anomalies adds the AI Toolkit's `StateSpaceForecast`, or core `predict` as a
  fallback, plus `anomalydetection` over the same window). The
  official server is reached
  through `npx mcp-remote <endpoint> --header "Authorization: Bearer <token>"`; this is
  the documented official client transport. `KASSI_SPLUNK_INSECURE=1` adds
  `NODE_TLS_REJECT_UNAUTHORIZED=0` for a local self-signed Splunk cert only.

Env vars (full table in README). `kassi serve` loads them from `.env` in the project
root via python-dotenv; real environment variables take precedence.

**Secrets:** `.env` holds the Splunk MCP token and is git-ignored. Never commit it,
never echo the token in output or commit messages, never paste it into code. Use
`.env.example` for documentation. If you must read the token to wire something, do it
programmatically without printing the value.

## Dev workflow

```bash
uv sync                         # install (Python >=3.12)
uv run kassi render             # print the FSM
uv run kassi render --mermaid   # mermaid source (kept in sync in README)
uv run kassi doctor --runtime   # validate graph + runtime tool shape (run after FSM edits)
uv run kassi serve              # mount as an MCP server over stdio
uv run pytest -q                # offline tests
```

Quality passes after any code change, loop until clean:

```bash
uv run ruff format .
uv run ruff check --fix .
uv run pytest -q
```

ruff config is in `pyproject.toml` (line length 110; rule groups I, UP, FURB, B,
SIM). There is no mypy gate; keep type hints accurate anyway.

### Tests

`tests/test_fsm.py` runs fully offline:
- Theodosia's `theodosia.testing.FakeUpstream` fakes both k6 and Splunk MCP servers
  (responses keyed by `{server: {tool: payload}}`).
- a `_FakeLLM` returns a canned k6 script (and a narration when the prompt is the
  narrator), and an autouse fixture monkeypatches `kassi.app.make_llm` plus
  `kassi.app.fetch_k6_generation_guidance` (so tests never spawn k6 for the prompt).
- the happy-path test drives the Burr app directly with `await app.arun(...)`;
  the enforcement test mounts the server and calls the `step` tool through a
  `fastmcp.Client` to assert an `invalid_transition` refusal.

When you add an action or change a payload shape, update `FakeUpstream` responses and
the parsers in `parse.py` together. Async tests rely on `asyncio_mode = auto`.

### Local Splunk

See `docs/SPLUNK_SETUP.md`. Splunk Enterprise runs non-root from `~/splunk`
(`~/splunk/bin/splunk start|status|stop`); admin / `kassi-admin-2026`; web :8000,
management REST :8089. The official Splunk MCP Server app exposes
`https://localhost:8089/services/mcp`. `scripts/seed_splunk.py` seeds sample
telemetry; `scripts/verify_correlate_live.py` drives the whole FSM against live Splunk
(official server when `.env` is set, else the dev bridge). The dev bridge is a test
convenience, not the shipped integration.

## Conventions

- **Comments:** default to none. Only comment to dispel confusion (unidiomatic code,
  a workaround, a spec link). No narrative or session-referencing comments. Match the
  surrounding density.
- **Prose (README/docs/commits):** plain declarative. No marketing voice, no em
  dashes, one idea per sentence.
- **Commits:** do not add `Co-Authored-By` or any AI attribution trailer, and no
  "Generated with" footers. Branch off `main` and only commit/push when asked.
- **License:** Apache-2.0 (matches Theodosia and Burr).
- **Library APIs:** Theodosia, Burr, FastMCP, and the MCP servers move fast. Verify
  against the installed package in `.venv` before writing non-trivial code against
  them rather than relying on memory.

## Gotchas

- The k6 MCP enforces caps (max 50 VUs, max 5 min). The load taxonomy in `compose.py`
  stays within them; keep new scenarios under those limits.
- **The k6 MCP `run_script` ignores the script's own `options`/scenarios** and defaults
  to 1 VU. `run_test` must pass `vus` + `duration` as tool args (see `_load_profile`);
  the generated script's options are vestigial. The generate prompt tells the model to
  keep think time minimal so those VUs produce real concurrency.
- **`run_test` is bounded by a timeout (`_run_timeout`, duration + 120s).** An authored
  script can wedge k6 so it runs but never exits cleanly, and the MCP call would then block
  forever (observed with a model-authored script on the feed target). On timeout `run_test`
  re-runs the deterministic `scaffold_script` once, so a run degrades instead of hanging.
  Keep any new upstream call that can block under a timeout.
- **`run_script` returns the k6 summary as `stdout` text, not structured `metrics`.**
  `parse.parse_run` falls back to `parse_run_stdout`, which scrapes `http_reqs` / `p(95)`
  / failure-rate from the default k6 summary. Do not reintroduce a custom `handleSummary`
  in `compose.py`; it replaces that default summary and breaks the parse.
- The k6 MCP has a security validator that rejects some JS patterns (e.g. `function(`
  with no space reads as dynamic-function creation). Authored scripts that trip it fail
  validation and fall back to the scaffold.
- `__ENV.TARGET_BASE_URL` is not forwarded into the k6 MCP subprocess; the composer
  bakes the base URL in as the literal default, so the target must be reachable from
  wherever the k6 server runs (Docker needs `host.docker.internal`).
- `run_test` records the wall-clock window; `correlate`'s SPL is scoped to it. A
  near-instant run produces a zero-width window and zero rows. `verify_correlate_live.py`
  cans k6 and ingests synthetic telemetry; `verify_petclinic.py` is the real path (real k6
  span + a real app emitting to HEC), which is what makes the server-side findings genuine.
- `safe_upstream` classifies a dict containing a truthy `error` key as ERROR. For k6
  validate we use `call_upstream` directly so a well-formed "script invalid" payload
  is parsed by `parse.parse_validation`, not swallowed as an upstream error.
