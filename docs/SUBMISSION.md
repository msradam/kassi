# kassi: foresee what your change does to production

> *Closed-loop observability, driven by change.* Most outages are self-inflicted by a change;
> kassi reads your change's fate in Splunk's telemetry before prod does, and unlike Cassandra,
> it brings the proof.

**Elevator pitch.** Roughly 80% of production outages are self-inflicted, they trace back to a
change. The warning is usually there; it just isn't believed, because a change's real impact
only surfaces in server-side telemetry after something exercises the system, and nobody
generates traffic and correlates it with Splunk by hand before shipping. kassi closes that loop
autonomously. Point it at a code change and it exercises the affected endpoints through the
Grafana k6 MCP server, watches the server-side telemetry land in Splunk through the official
Splunk MCP Server, and explains what the change did and *why*, root cause (`database is
locked`), cited evidence, an ML forecast of the trend, and the fix, then publishes the verdict
back to a Splunk dashboard. A change goes in, an explained outcome comes out: agentic
observability, every step sealed to a hash-chained, auditable ledger, so the prophecy comes
with proof and it is safe to run unattended.

**Track:** Observability (primary); also Platform & Developer Experience.
**Bonus prizes targeted:** Best Use of Splunk MCP Server; Best Use of Splunk Developer Tools.

Verified end-to-end against Splunk Enterprise 10.4.0 with the official Splunk MCP Server
(Splunkbase 7931, v1.2.0) and a local IBM Granite 4.1 model. See the case study in the README
and `docs/SPLUNK_SETUP.md`. Built new during the submission period for this hackathon.

## Inspiration

Roughly 80% of production outages are self-inflicted, they trace back to a change, which is
why "change failure rate" is one of the four DORA metrics teams are measured on. And every
engineer has been Cassandra about a change: you sensed it was risky, you couldn't prove it, it
shipped, and it took down prod at 2am. The warning existed; it just wasn't believed. The reason
is structural, a change's real impact only surfaces in server-side telemetry once something
exercises the system, and catching it means generating traffic, digging through Splunk, and
correlating the two by hand, after the fact. So it almost never happens before production.

kassi is the seer that gets believed. It closes the loop autonomously: take a change, exercise
the affected endpoints, and read the server-side truth from Splunk in one pass, root cause,
cited evidence, an ML forecast of the trend, a recommended fix, every step sealed to a
hash-chained ledger, so the prophecy comes with proof. The name is the theme: Kassandra foresaw
what others would not believe; kassi foresees a change's impact and makes it undeniable. Each
workflow phase is a card of the Major Arcana the agent turns, from The Fool (the run begins) to
Judgement (the verdict, sealed to the ledger).

## What it does

kassi is an AI agent that closes the observability loop on a change. Give it a code
change (a git diff) or a plain-language intent and it:

1. picks the affected HTTP endpoints, from the diff or by scoring an OpenAPI spec against
   the intent,
2. consults the k6 MCP documentation tools to ground the test in the live k6 API,
3. composes a deterministic k6 scaffold from the OpenAPI schema, then has the model author
   the final script on top of it using k6's own `generate_script` MCP prompt, and validates
   and runs it through the Grafana k6 MCP server,
4. preflights the Splunk index (existence, event count, sourcetypes, version), then
   queries Splunk through the official Splunk MCP Server for the target's server-side
   telemetry over the exact test window, then runs the AI Toolkit's `StateSpaceForecast`
   (with core `predict` as a fallback) and `anomalydetection` over that window to locate the
   saturation onset statistically, and
5. reports a combined client plus server verdict with a cited, grounded analysis (root cause,
   evidence, recommendation), the model narrating each phase as a tarot reading, a provenance
   record of every upstream tool call, and the run published to a Splunk dashboard.

The driving agent (Claude Code, Cursor, any MCP client) never sees k6 or Splunk. It sees
one tool, `step(action, inputs)`, and takes the workflow one move at a time.

In a verified run against live Splunk, driven from a git diff that adds `POST /api/visits`,
one agent orchestrated 18 tool calls across both MCP servers: it extracted the changed
endpoint from the diff, grounded generation in the live k6 docs, authored the script with
k6's own `generate_script` prompt and repaired it from a real k6 validation error, confirmed
the `web` index on the official Splunk MCP Server (`access_json` sourcetype, Splunk 10.4.0),
drove 2937 client-side k6 requests (p95 318 ms, 59.4% failed), then correlated them to the
server-side telemetry over the test window: the new `POST /api/visits` at 59.4% 5xx and p95
318.44 ms, root cause `database is locked` (1797x), and the AI Toolkit's `StateSpaceForecast`
forecast the latency band while `anomalydetection` flagged the anomalous bucket statistically.
The client failure rate is explained by the server-side errors, correlated automatically, with
every tool call on an audit ledger.

It ships **five demo targets spanning the load-failure taxonomy**, so the correlation is proven
on distinct signatures rather than one trick: a SQLite write-lock (5xx under concurrency, the
case above), an N+1 query (latency with **zero errors**, visible only in server-side `db_time`),
an unbounded recompute (latency **rising over the run**, where the forecast earns its keep:
Granite projects p95 climbing past the current value), a too-tight rate limit (**429** throttling,
exercising the 4xx-vs-5xx split), and a downstream timeout cascade (latency **plus 504s**, a
dependency root cause whose fix is resilience, not query tuning). Each yields a different cause
and recommendation.

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
  then runs the AI Toolkit's `StateSpaceForecast` (latency band forecast, with core `predict`
  as a fallback when the toolkit is absent) and `anomalydetection` over the same window, so
  the saturation onset is found by Splunk's ML rather than a fixed
  threshold in kassi. All three Splunk phases degrade gracefully to k6-only when not
  configured.
- **Deterministic scaffold, grounded model on top.** A deterministic `scaffold` phase
  composes a runnable k6 baseline from the OpenAPI schema with no model. The `generate_script`
  phase then has the model author the final script on top of it, guided by k6's own
  `generate_script` prompt; `report` has the model write a cited analysis (root cause,
  evidence, recommendation) and narrate the run as a tarot reading. The default backend is a
  local **IBM Granite 4.1** model via Ollama, whose chat template natively grounds the analysis
  on the run's evidence documents, so it stays to the measured facts and cites each one's
  source. Claude is an alternative (one env var). The model never writes SPL, and when it is
  offline or its output keeps failing validation the pipeline runs the deterministic scaffold
  and a deterministic analysis, so a run never fails for lack of a model.

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
- The forecast runs on Splunk's own ML: the AI Toolkit's `StateSpaceForecast` over the test
  window, invoked through the same `splunk_run_query` MCP tool, with the core `predict`
  command as an automatic fallback so the phase works on any Splunk.
- The result loop closes back into Splunk: every run publishes its verdict and metrics to
  `index=kassi_runs` over HEC, and a Splunk dashboard renders the client-and-server join over
  time, so the analysis lives where the ops team already works.
- A practical, cited analysis (root cause, evidence, recommendation), written by a local
  **IBM Granite 4.1** model that grounds every fact on the run's evidence documents via its
  native document role, so the writeup is attributable and resistant to hallucination, and it
  runs fully offline with no hosted-API dependency.

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
- Nothing in an autonomous agent can be allowed to hang. A model-authored k6 script can wedge
  the runner so the MCP call never returns, so every blocking upstream call is bounded by a
  timeout that falls back to the deterministic scaffold: the pipeline degrades, never stalls.

## What's next for kassi: Synthetic Load Generation

- Use a Splunk-hosted model for a root-cause narrative over the correlated window.
- Use the AI Assistant for SPL to generate correlation queries from intent.
- Grow synthetic load generation: schema-aware request synthesis, realistic traffic mixes
  and ramps, and seeded fault scenarios so a single intent produces a richer, more
  representative test rather than a static replay.
