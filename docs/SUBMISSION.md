# kassi — Splunk Agentic Ops Hackathon submission (draft)

Track: Observability (primary). Also relevant to Platform & Developer Experience.
Bonus prizes targeted: Best Use of Splunk MCP Server.

This is a draft of the Devpost text description. Edit before submitting.

Verified end-to-end against Splunk Enterprise 10.4.0 (see `docs/SPLUNK_SETUP.md`): the
full state machine runs, and the `correlate` step queries live Splunk through an MCP
server and returns a server-side rollup scoped to the test window.

## The problem

Load testing tells you *that* a change made an endpoint slower. It does not tell you
*why*. The client-side numbers (p95, error rate, throughput) live in one tool; the
server-side truth (5xx spikes, slow queries, saturation) lives in Splunk. Connecting
the two is manual, after the fact, and rarely happens inside CI.

## What kassi does

kassi is an AI agent that closes that loop. Give it a code change or a plain-language
intent and it:

1. picks the affected HTTP endpoints (from a git diff, or by scoring an OpenAPI spec
   against the intent),
2. generates a self-contained k6 load test,
3. validates and runs it through the **Grafana k6 MCP server**,
4. queries **Splunk** through the **Splunk MCP Server** for the target service's
   server-side telemetry over the exact test window, and
5. reports a combined client + server verdict.

## How Splunk is used

The Splunk MCP Server is wired in as an upstream MCP server. After a run, kassi builds
an SPL rollup scoped to the test window and calls the `splunk_run_query` tool to read
back server-side errors and latency. The agent driving kassi never talks to Splunk
directly; it only takes workflow steps, and the Splunk call happens inside a workflow
action. This is the "Best Use of Splunk MCP Server" angle: a single agent orchestrating
the Splunk MCP Server alongside another MCP server, with every query recorded.

## How it is built

- **theodosia + Burr**: the workflow is a state machine served over MCP. The agent
  drives it one `step` at a time; illegal steps are refused with the legal next actions,
  and every step and refusal is written to an immutable, hash-chained ledger. `kassi
  verify` proves the audit trail was not tampered with. This makes autonomous operation
  governable by construction, which matters for ops tooling.
- **Two MCP upstreams**: the Grafana k6 MCP server and the Splunk MCP Server, both
  hidden from the driving agent.
- **Narrow AI**: a local model fills a typed, closed-enum plan; pure Python composes the
  k6 script and the SPL. The model never writes executable code or queries, so generation
  stays deterministic and auditable.

## What is novel

- An agent that orchestrates two MCP servers (load generation and observability) inside
  one audited state machine.
- Client-side and server-side performance data correlated automatically over a precise
  test window, driven from a code change.
- A durable, verifiable ledger of everything the agent did and everything it was refused.

## Challenges

- The k6 MCP runs a single script string, so the codegen had to emit one self-contained
  file (no imported client, no aux files) built from the OpenAPI schema.
- Keeping the LLM on a short leash (enum plan only) while still producing useful,
  endpoint-aware tests.

## What is next

- Ship k6 results into Splunk via HEC for dashboards.
- Use a Splunk-hosted model for the correlation/root-cause narrative.
- Use the AI Assistant for SPL to generate correlation queries from intent.
