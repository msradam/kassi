# kassi: the AI agent that divines the outage and writes the cure

> It reads the omens in your telemetry to divine what your next change will break, then writes
> the fix, a remediation diff, before production ever sees it. Cassandra foresaw disaster and
> was never believed; kassi's prophecy comes with proof, and a patch.

**Elevator pitch.** Roughly 80% of production outages are self-inflicted: Gartner attributes
unplanned downtime to people and process rather than technology, and change is the single biggest
cause. The warning is usually there; it just isn't believed, because a change's real impact
only surfaces in server-side telemetry after something exercises the system, and nobody
generates traffic and correlates it with Splunk by hand before shipping. kassi closes that loop
autonomously. Point it at a code change and it exercises the affected endpoints through the
Grafana k6 MCP server, watches the server-side telemetry land in Splunk through the official
Splunk MCP Server, and explains what the change did and *why*, root cause (`database is
locked`), cited evidence, an ML forecast of the trend, and the fix, then publishes the verdict
back to a Splunk dashboard. A change goes in, an explained outcome comes out: agentic
observability, every step sealed to a hash-chained, auditable ledger, so the prophecy comes
with proof and it is safe to run unattended. The audited state machine over MCP keeps the whole
loop model-agnostic, so it scales from a hosted frontier model down to a local 8B that runs the
driver, writer, and auditor on one box; and the agent publishes its own state-machine walk back to
Splunk, so it is observable in the very system it observes.

**Track:** Observability (primary); also Platform & Developer Experience.
**Bonus prizes targeted:** Best Use of Splunk MCP Server; Best Use of Splunk Developer Tools.

Verified end-to-end against Splunk Enterprise 10.4.0 with the official Splunk MCP Server
(Splunkbase 7931, v1.2.0) and a local IBM Granite 4.1 model. See the case study in the README
and `docs/SPLUNK_SETUP.md`. The Splunk integration was built during the submission period, extending a diff-to-k6
tool from an earlier project of mine; see Additional information.

## Inspiration

Roughly 80% of production outages are self-inflicted: Gartner attributes unplanned downtime to
people and process rather than technology, and change is the single biggest cause, which is
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
5. writes a cited, grounded analysis (root cause, evidence, recommendation) and a **proposed
   remediation**: a minimal unified diff that fixes the root cause, written from the diff that
   introduced it,
6. screens that analysis with a separate IBM Granite Guardian 4.1 model, an independent check
   that no claim is unsupported by or contradicts the telemetry it cites, and seals the pass/fail
   to the ledger, then reports the combined client plus server verdict. The model narrates each
   phase as a tarot reading; every upstream tool call is on a provenance record; the run is
   published back to Splunk twice over: a run summary, **and the agent's own state-machine walk**,
   one event per phase keyed by the run's id, so the agent is observable in the same system it
   observes, not just what the change did, but how the agent reached the verdict.

So the loop closes both ways: a change comes in, and a change that fixes it goes out. In the
verified petclinic run, kassi diagnosed `database is locked` and proposed a minimal, validated
fix (dropping the `time.sleep` held inside the write transaction, or lightening the lock with
WAL mode), the real cause of the contention. Crucially the patch is not a model's guess at a
diff: the model emits SEARCH/REPLACE edits (the format LLMs handle reliably), kassi applies
them to the file, re-parses the result to confirm it still compiles, and only then renders a
real unified diff with difflib, so the line numbers are correct and the fix is known to apply.

The driving agent never sees k6 or Splunk. It sees one tool, `step(action, inputs)`, and takes
the workflow one move at a time. That driver is pluggable: Claude Code, Cursor, any MCP client,
or a **local Granite model** via `kassi pilot`, which reads the reachable actions and calls
`step` for each phase itself. With the Granite driver, the driver, the writer, and the auditor
are all the same local model family, so the entire loop runs on one box with no cloud brain. The
orchestration is model-neutral, so the architecture scales with the model rather than depending
on one; Granite 4.1 is the default because it proves the whole loop fits on a local 8B.

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
and recommendation, and the verdict reflects the kind of failure: a 5xx change reads as a
regression, while a zero-error latency change reads as "degrading", flagged off Splunk's own
forecast, a regression the error rate would miss entirely.

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
- **Two models, two roles: a writer and an auditor.** The analysis is written by Granite 4.1
  (the writer), then a separate phase, `screen`, hands that analysis and the evidence it cites to
  IBM Granite Guardian 4.1 (the auditor: an 8B model fine-tuned from the same Granite 4.1 base,
  Apache-2.0) to judge groundedness: does the writeup include any claim unsupported by, or
  contradicting, the measured telemetry it was grounded on? Guardian returns a yes/no that is
  sealed to the report ledger. So the writer model is not trusted on its own word; an independent
  model checks it before the verdict is published, catching both an analysis that contradicts the
  numbers and one that invents a cause the telemetry never showed. The phase degrades to
  "unavailable" (never blocks) when Guardian is off. Different state-machine phase, different
  model: the FSM makes that composition clean, and the whole loop, writer and auditor alike,
  stays within the open Granite 4.1 family on one local host.
- **An 8B model runs the whole loop, on-prem.** kassi defaults to IBM Granite 4.1 (8B) served
  locally by Ollama, the first open-source LLM certified to ISO/IEC 42001 (the AI management
  system standard), Apache-2.0 licensed and shipped with IBM's IP indemnity. Every model task
  in the pipeline (authoring the k6 script, writing the grounded analysis, proposing the
  remediation edits) runs on that one local model. So an enterprise runs kassi fully on-prem or
  air-gapped: no code, no diffs, and no telemetry leave the building, there is no per-token cloud
  bill, and the model backing it carries a recognized governance certification, which matters for
  a tool that has autonomy over ops infrastructure. The expensive frontier model is optional, not
  required.
- **The driver is local too, so nothing leaves the box.** Beyond authoring and auditing, the
  *agent that drives the state machine* can be a local Granite model. `kassi pilot` connects
  Granite to the mounted MCP server and lets it call `step` for each phase itself, recovering
  from the graph's refusals, until it reaches the verdict. Driver, writer, and auditor are then
  the same local family. Because the driver and the per-phase models sit behind a model-neutral
  surface (the MCP `step` tool, an `LLM` protocol with Ollama and Anthropic backends), the model
  is pluggable: swap in a larger or hosted one and nothing else changes.
- **The agent is observable in Splunk, not just its results.** kassi reads Splunk to observe the
  target; it then publishes its *own* execution back to Splunk over HEC, one `kassi:step` event
  per state-machine phase (its card, outcome, and the tool calls it made) alongside the run
  summary, all keyed by the run's id. A dashboard renders the agent's walk from The Fool to
  Judgement next to the client-and-server join, so an operator sees how the agent reached the
  verdict. Agentic ops, taken literally: the agent is a first-class observable.

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
- The loop closes both ways: kassi does not just diagnose, it proposes the fix. From the diff
  that introduced the regression and the correlated root cause, the model writes a minimal
  unified **remediation diff** (for review, not auto-applied), so a change comes in and a change
  that fixes it goes out.
- The whole loop, including the remediation, runs on a single local 8B model (IBM Granite 4.1,
  the first ISO/IEC 42001-certified open-source LLM), so kassi is deployable on-prem or
  air-gapped with no code or telemetry leaving the building and no per-token cost.
- The published analysis is screened by an independent model. A separate Granite Guardian 4.1
  phase judges whether the writeup is grounded in the telemetry it cites and seals that verdict to
  the ledger, so the writer model is audited by a second model rather than trusted on its own word.
- The whole agent runs locally, including the part that *drives* it: `kassi pilot` lets a local
  Granite model walk the state machine itself, so driver, writer, and auditor are all on-box, with
  no cloud agent in the loop.
- The agent publishes its own state-machine walk back to Splunk, so kassi is observable in the
  same system it reads, the dashboard shows not just what the change did but how the agent decided.

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

## What's next for kassi

- Use a Splunk-hosted model for a root-cause narrative over the correlated window.
- Use the AI Assistant for SPL to generate correlation queries from intent.
- Grow synthetic load generation: schema-aware request synthesis, realistic traffic mixes
  and ramps, and seeded fault scenarios so a single intent produces a richer, more
  representative test rather than a static replay.

## Additional information

Answers to the submission form, stated plainly so a judge can check each one against the repo.

**Track.** Observability. kassi helps an engineering team understand how a change behaves under
load, detect the regression in server-side telemetry before production, and automate the response
(a cited diagnosis plus a proposed fix). It also touches Platform & Developer Experience, since it
slots into the SDLC at the pull request, but Observability is the primary track.

**Splunk AI capabilities used at runtime.** Two, both called live against Splunk Enterprise
10.4.0, neither mocked:

- **Splunk MCP Server** (Splunkbase 7931, v1.2.0), the primary integration. The agent reads all
  server-side telemetry through it: `splunk_get_info`, `splunk_get_index_info`, and
  `splunk_get_metadata` to preflight the index, then `splunk_run_query` for the four correlation
  queries and the anomaly scan. It connects over the official `mcp-remote` stdio bridge with an
  encrypted Bearer token. This is the integration entered for **Best Use of Splunk MCP Server**.
- **Splunk AI Toolkit**, invoked through that same `splunk_run_query` tool. The `StateSpaceForecast`
  algorithm forecasts the latency band in `detect_anomalies` (core `predict` is the automatic
  fallback when the toolkit's Python for Scientific Computing add-on is absent), and
  `anomalydetection` flags statistically outlying buckets. Splunk's own ML locates the saturation
  onset, and the forecast band and flagged buckets fold into the verdict, so a zero-error latency
  change still reads as "degrading."

To not overstate anything: kassi does **not** use Splunk Hosted Models, the Splunk AI Assistant for
SPL (it composes its SPL in pure Python by design, and the model never writes SPL), or the
AI-for-Splunk-Apps Python SDK. The reasoning models in the loop (writer, auditor, driver) are a
local IBM Granite 4.1 family on Ollama, not Splunk-hosted. A Splunk-hosted root-cause narrative and
AI-Assistant SPL generation are listed under "What's next," not claimed here.

**Bonus prizes.** Primary is **Best Use of Splunk MCP Server**: the whole observability correlation
is orchestrated through it. The project also builds on the Splunk developer ecosystem (the MCP
Server and AI Toolkit apps from Splunkbase / dev.splunk.com), the basis for a **Best Use of Splunk
Developer Tools** consideration; it does not use App Inspect or the Splunk SDK, so that is the
lighter of the two claims.

**New or significantly updated.** Significantly updated. kassi extends an earlier project of mine,
Kassandra (github.com/msradam/kassandra), which generated k6 load tests from a git diff. That
diff-to-k6 idea is the shared part: read a diff, pick the changed endpoints, and generate a targeted
k6 test from the OpenAPI spec. The rest is new, built during this submission period. The workflow was rebuilt as an audited Burr
state machine over MCP, and it now drives k6 through the official Grafana k6 2.0 MCP server
(`k6 x mcp`) instead of shelling out to the binary. The whole Splunk side is new: it correlates the
load test with Splunk telemetry through the official Splunk MCP Server, forecasts with the Splunk AI
Toolkit, runs local Granite models for the analysis and a groundedness audit, proposes a remediation
diff, and publishes its own run back to Splunk.

**How to test it.** The repository is public and open source (Apache-2.0, license at the repo
root). Two paths:

- **Offline, no setup:** `uv sync && uv run pytest` runs the full FSM suite against Theodosia's
  `FakeUpstream` for both MCP servers and a fake model, exercising the state machine, the refusals,
  and the audit ledger with no k6, Splunk, Ollama, or network.
- **Full live run:** `docs/SPLUNK_SETUP.md` walks through a local Splunk Enterprise install,
  seeding sample telemetry, and the official Splunk MCP Server; then `scripts/verify_petclinic.py`
  drives the entire FSM end to end with nothing canned (real app, real k6, live Splunk). The README
  "Case study" shows the expected output, and `kassi verify <app-id>` confirms the run's ledger was
  not tampered with.
