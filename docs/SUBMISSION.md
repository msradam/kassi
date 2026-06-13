# Kassi: Splunk Agentic Ops Hackathon submission

Track: Observability (primary); also Platform & Developer Experience.
Bonus prize targeted: Best Use of Splunk MCP Server.

Verified end-to-end against Splunk Enterprise 10.4.0 with the official Splunk MCP Server
(Splunkbase 7931, v1.2.0). See the case study in the README and `docs/SPLUNK_SETUP.md`.

## Inspiration

Load testing tells you *that* a change made an endpoint slower. It does not tell you
*why*. The client-side numbers (p95, error rate, throughput) live in one tool. The
server-side truth (5xx spikes, slow queries, saturation) lives in Splunk. Connecting the
two is manual, done after the fact, and almost never happens inside CI. We wanted an
agent that runs the load test and explains the result from server-side telemetry in one
pass, and we wanted that agent to be safe to run autonomously: bounded, auditable, and
unable to wander off its rails.

The name is the theme: Kassandra saw what others would not believe, so kassi divines your
stack's performance. Each workflow phase is a card of the Major Arcana the agent turns,
from The Fool (the run begins) to Judgement (the verdict, sealed to the ledger).

## What it does

Kassi is an AI agent that closes the load-test-to-observability loop. Give it a code
change (a git diff) or a plain-language intent and it:

1. picks the affected HTTP endpoints, from the diff or by scoring an OpenAPI spec against
   the intent,
2. consults the k6 MCP documentation tools to ground the test in the live k6 API,
3. composes a deterministic k6 scaffold from the OpenAPI schema, then has the model author
   the final script on top of it using k6's own `generate_script` MCP prompt, and validates
   and runs it through the Grafana k6 MCP server,
4. preflights the Splunk index (existence, event count, sourcetypes, version), then
   queries Splunk through the official Splunk MCP Server for the target's server-side
   telemetry over the exact test window, then runs Splunk's own `predict` and
   `anomalydetection` over that window to locate the saturation onset statistically, and
5. reports a combined client plus server verdict, with the model narrating each phase as a
   tarot reading and a provenance record of every upstream tool call.

The driving agent (Claude Code, Cursor, any MCP client) never sees k6 or Splunk. It sees
one tool, `step(action, inputs)`, and takes the workflow one move at a time.

In a verified run against live Splunk, one agent orchestrated 18 tool calls across both
MCP servers: it grounded generation in the live k6 docs, authored the script with k6's own
`generate_script` prompt and repaired it from a real k6 validation error, confirmed the
`web` index on the official Splunk MCP Server (`access_json` sourcetype, Splunk 10.4.0),
drove 6666 client-side k6 requests (p95 280.92 ms, 15% failed), then correlated them to the
server-side telemetry over the test window: the new `POST /api/visits` at 45.2% 5xx and p95
285.59 ms against healthy baselines near 2 ms, root cause `database is locked` (990x), and
Splunk's own `predict` + `anomalydetection` confirmed the saturation bucket statistically.
The client failure rate is explained by the server-side errors, correlated automatically,
with every tool call on an audit ledger.

## How we built it

- **Theodosia + Burr.** The workflow is a Burr state machine served over MCP by
  [Theodosia](https://msradam.github.io/theodosia/). The graph's edges are the only legal
  moves. An illegal step is refused with the list of valid next actions, and every step
  and every refusal is written to an immutable, hash-chained ledger. `kassi verify` proves
  the audit trail was not tampered with. Autonomy is governable by construction, which is
  what makes the agent safe to run unattended on ops infrastructure.
- **Two MCP upstreams from one agent, deep usage.** Theodosia spawns the Grafana k6 MCP
  server and the official Splunk MCP Server as hidden stdio upstreams. The agent uses the
  k6 documentation tools (`list_sections`, `get_documentation`) to ground generation, k6's
  own `generate_script` prompt and `best_practices` resource to author the script, the
  Splunk metadata tools (`splunk_get_info`, `splunk_get_index_info`, `splunk_get_metadata`)
  to preflight the index, and `splunk_run_query` to correlate. That spans tools, prompts,
  and resources across both servers, every call recorded to an `mcp_provenance` block. With
  k6 2.0 the k6 server is the built-in `k6 x mcp` subcommand, so one binary covers load
  generation with no separate install.
- **How Splunk is used.** After the run, `run_test` records the wall-clock window.
  `splunk_preflight` verifies the target index and captures its event count, sourcetypes,
  and the Splunk version. `correlate` then builds an SPL error/latency rollup scoped to
  that window and calls the official `splunk_run_query` tool over the documented
  `mcp-remote` bridge, authenticated with an encrypted Bearer token. `detect_anomalies`
  then runs Splunk's own `predict` (latency band forecast) and `anomalydetection` over the
  same window, so the saturation onset is found by Splunk's ML rather than a fixed
  threshold in kassi. All three Splunk phases degrade gracefully to k6-only when not
  configured.
- **Deterministic scaffold, model on top.** A deterministic `scaffold` phase composes a
  runnable k6 baseline from the OpenAPI schema with no model. The `generate_script` phase
  then has the model author the final script on top of it, guided by k6's own
  `generate_script` prompt, and `report` has the model narrate each phase as a tarot
  reading. The backend is pluggable (a local Ollama model or Claude Haiku via the Messages
  API, one env var). The model never writes SPL, and when it is offline or its output keeps
  failing validation the pipeline runs the deterministic scaffold, so a run never fails for
  lack of a model.

## Challenges we ran into

- The k6 MCP runs a single script string and cannot resolve local imports, so the codegen
  had to emit one self-contained file built from the OpenAPI schema, with no imported
  client and no aux files.
- Keeping the LLM on a short leash (an enum plan only) while still producing useful,
  endpoint-aware tests.
- Correlation only works if the SPL window matches the test exactly. A near-instant run
  produces a zero-width window and zero rows, so the run has to record a real wall-clock
  span and the SPL has to be scoped to it.
- Wiring the official Splunk MCP Server over its streamable-HTTP transport, with an
  encrypted token and a local self-signed certificate, through the `mcp-remote` bridge.

## Accomplishments that we're proud of

- One agent orchestrating two MCP servers, load generation and observability, inside a
  single audited state machine.
- Client-side and server-side performance data correlated automatically over a precise
  test window, driven from a code change.
- A durable, verifiable ledger of everything the agent did and everything it was refused.
- A reproducible end-to-end run against the official Splunk MCP Server, not a mock.

## What we learned

- Constraining the agent makes it more useful, not less. A single `step` tool with
  refusals turned out to be easier to drive and far easier to trust than a broad toolset.
- The valuable signal is in the join. Neither the k6 numbers nor the Splunk rollup is new,
  but pairing them over the exact window is what turns "it got slower" into "5xx errors
  caused it."
- A deterministic scaffold is what lets the model do more safely. Because a runnable k6
  baseline always exists, we can let the model author the final script (and narrate the
  run) without the pipeline ever failing for lack of a model, and Python still owns the SPL.
- k6 2.0 folding the MCP server into the `k6 x mcp` subcommand removed an entire install
  step and made the demo easier to reproduce.

## What's next for Kassi: Synthetic Load Generation

- Ship k6 results into Splunk via HEC so client-side and server-side metrics live together
  in dashboards.
- Use a Splunk-hosted model for a root-cause narrative over the correlated window.
- Use the AI Assistant for SPL to generate correlation queries from intent.
- Grow synthetic load generation: schema-aware request synthesis, realistic traffic mixes
  and ramps, and seeded fault scenarios so a single intent produces a richer, more
  representative test rather than a static replay.
