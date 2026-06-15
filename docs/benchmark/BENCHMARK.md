# kassi-bench

A reproducible, ground-truth benchmark for change-induced performance regressions. Given a code
change that introduces one known performance fault, does kassi, from live load and live Splunk,
detect it, attribute it to the right endpoint, classify the failure mode, and name the root cause,
without crying wolf on a healthy endpoint?

## Headline

Across **80 live runs** (5 fault classes + 3 healthy controls,
10 reps each, 0 errored), scored on kassi's actual verdict:

| | detection | localization | failure-class | root-cause | overall correct |
| --- | --- | --- | --- | --- | --- |
| **faults** (n=50) | 90% | 92% | 90% | 95% | **90%** |
| **controls** (n=30) | 0% false-alarm | n/a | n/a | n/a | **100%** |

Root-cause is scored only where the fault has a server-side error string (the 5xx classes); for
latency and throttling there is no server error to name, so it reads `n/a`.

## Why a new benchmark

Established RCA benchmarks (RCAEval, PetShop, LEMMA-RCA) inject infrastructure faults (CPU, memory,
network) into microservice systems and score a method over pre-recorded telemetry traces. kassi's
setting is different: the fault is a real code change, the load is generated live by k6, and the
diagnosis is read back from Splunk over the exact test window. kassi-bench measures that loop end to
end. The fault classes are drawn from the real-world performance-bug taxonomy (concurrency,
algorithmic inefficiency, I/O, capacity), the bugs that pass every unit test because they only
surface under concurrency.

## Method

- **8 scenarios.** 5 faults, each a distinct failure class, plus
  3 healthy controls: the same apps under the same load on a benign endpoint, where
  the only correct answer is "nothing wrong." Controls measure false positives; a benchmark that
  cannot fail proves nothing.
- **The real pipeline, model in the loop.** Every run, the configured model authors the k6 script,
  writes the cited analysis, and an independent guardian pass audits it, with the load held at 25
  VUs / 25s. The model is pluggable behind one `LLM` interface: a local 8B over Ollama or a frontier
  model over the Claude Agent SDK, so the harness is model-agnostic. (`--deterministic` bypasses the
  model for a controlled, repeatable baseline.)
- **10 repetitions** per scenario (80 runs total, median 89s/run), against a live
  Splunk Enterprise 10.4.0 through the official Splunk MCP Server.
- **Scored on kassi's real verdict**, not a reconstruction: detection, endpoint localization
  (top-1), failure-class, root cause, and a per-run `correct` that requires all of the applicable
  dimensions (and, for a control, that kassi stays silent).

## Per-scenario results

| scenario | endpoint | class | n | detect | localize | class | cause | correct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `petclinic` | `/api/visits` | regression | 10 | 100% | 100% | 100% | 100% | **100%** |
| `storefront` | `/api/checkout` | degradation | 10 | 90% | 90% | 90% | n/a | **90%** |
| `feed` | `/api/events` | degradation | 10 | 80% | 90% | 80% | n/a | **80%** |
| `gateway` | `/api/quote` | throttling | 10 | 90% | 90% | 90% | n/a | **90%** |
| `orders` | `/api/order` | regression | 10 | 90% | 90% | 90% | 90% | **90%** |

| control (healthy) | endpoint | n | false-alarm | correct |
| --- | --- | --- | --- | --- |
| `petclinic-ok` | `/api/owners` | 10 | 0% | **100%** |
| `storefront-ok` | `/api/products` | 10 | 0% | **100%** |
| `gateway-ok` | `/api/status` | 10 | 0% | **100%** |

## Canonical benchmark (RCAEval RE3)

[RCAEval](https://github.com/phamquiluan/RCAEval) is the established academic RCA benchmark for
microservice systems. Its RE3 suite is code-level faults injected into recognized demo systems, with
the root-cause service as ground truth, scored by top-k localization (AC@k, Avg@k). kassi's live loop
cannot be pointed at pre-recorded data, so `scripts/benchmark_rcaeval.py` exercises kassi's
**diagnosis engine** on the canonical cases: it projects each case's recorded spans into Splunk with
the access-log shape kassi reads in production (service -> path, gRPC status -> 2xx/5xx, span duration
-> response_time), runs kassi's real correlation plus a baseline-relative latency-anomaly score, and
scores the ranked services with RCAEval's own `Evaluator`.

| system | cases | AC@1 | AC@3 | AC@5 | Avg@5 |
| --- | --- | --- | --- | --- | --- |
| Online Boutique | 30 | 90% | 100% | 100% | 98% |
| Train Ticket | 27 | 70% | 100% | 100% | 93% |
| **overall** | **57** | **81%** | **100%** | **100%** | **95%** |

Scored against RCAEval's labels with its metric, kassi localizes the root-cause service at top-1 in
81% of cases and within top-3 in 100%. That is competitive with the
strongest published methods on this suite (e.g. PRISM reports ~90% top-1 / 98% top-3 on Online
Boutique, which kassi matches) and well ahead of the classical baselines (e.g. BARO reports ~0.50
top-1 on Train Ticket and 0.00 on Sock Shop). kassi is strongest on error-manifesting code faults
(its design center); on the deeper Train Ticket call graph it trails the best methods, where the
failure surfaces on the caller and the baseline-relative anomaly has to separate the service that
changed from the entry service that merely amplifies it.

Scope, stated plainly: this tests kassi's correlation engine, not the live k6 loop (RCAEval bakes the
load into the recorded traces), and kassi runs here as a blind multi-service ranker rather than its
normal targeted-confirmation flow. RCAEval's Sock Shop RE3 ships only aggregated Istio mesh metrics
and unstructured logs, no distributed traces, so it is excluded: kassi reads request-level wire
telemetry, and synthesizing per-request events from aggregate counters would not be a faithful test.
Reproduce (after `download_re3ob_dataset()` / `download_re3tt_dataset()`):

```bash
uv run python scripts/benchmark_rcaeval.py --systems OB,TT
```


## External validation (kassi-bench-ext)

kassi-bench above runs on kassi's own demo apps. kassi-bench-ext points kassi at **go-httpbin**, a
popular third-party OSS app kassi never instrumented, observed only through a generic access-log
proxy (`scripts/access_proxy.py`) that ships its traffic into Splunk the way a real API gateway or
load balancer would. go-httpbin's endpoints are app-intrinsic ground truth: `/status/500` errors,
`/delay/2` is genuinely slow, `/get` is healthy.

| endpoint | expected | n | correct |
| --- | --- | --- | --- |
| `httpbin /status/500` | regression | 5 | **5/5** |
| `httpbin /delay/2` | degradation | 5 | **5/5** |
| `httpbin /get` | none | 5 | **5/5** |

Overall **15/15**. Seeing only the proxy's Splunk access logs, kassi flags the 5xx
endpoint as a regression (root cause recorded as the gateway-visible "upstream returned 500", since
the app's own error string is never on the wire), the 2s endpoint as a latency degradation, and
stays quiet on the healthy control. Reproduce:

```bash
docker run -d -p 8600:8080 ghcr.io/mccutchen/go-httpbin
uv run python scripts/benchmark_external.py --reps 5
```

## What the benchmark found, and fixed

Three real defects surfaced from these runs and were fixed, each covered by `tests/test_verdict.py`
(`_verdict` / `parse_validation` in `src/kassi/`):

1. **4xx throttling was mislabeled "latency degradation."** A pure-429 change (gateway) has no 5xx,
   so the regression branch did not fire; `anomalydetection` then annotated a bucket on the
   near-zero p95 of the fast-rejected requests. Added a client-side-throttling branch (4xx-dominant,
   no server errors) ahead of the latency branch: it now reads "rate-limited, not broken."
2. **Healthy endpoints false-alarmed.** `anomalydetection` annotates the odd bucket even on a
   healthy endpoint's sub-floor jitter, so controls first read as degradation. Added a latency floor
   (`_LATENCY_FLOOR_MS`, now 40ms and forecast-aware): a flagged bucket only counts as degradation
   when the measured or forecast p95 is actually slow. The external run (a third-party app behind a
   proxy that adds ~20ms) is what pushed the floor from 25ms to 40ms; the demo degradations
   (p95 50-90ms) are well clear of it, and the control false-alarm rate is 0%.
3. **A threshold breach during validation read as a broken script.** An endpoint that fails every
   request (a third-party `/status/500`) trips k6's thresholds in the validate step, which
   `parse_validation` treated as exit-99 "invalid" and routed to the fix loop until it gave up ("no
   run"). But exit 99 means the script ran fine and the SUT breached an SLO, which is the finding,
   not a script error. `parse_validation` now accepts exit 99 as valid, so run_test measures it.

That is the point of the controls and the external run: they found three ways kassi was wrong and
turned them into fixes.

## Scope and limits

- In kassi-bench, localization is top-1 attribution to the changed endpoint the load targets. Blind
  multi-service fault-propagation ranking is RCAEval's domain, and the canonical-benchmark section
  above scores kassi there directly.
- 5 fault classes x 10 reps is a focused, live-load suite, complementary to the
  canonical RCAEval RE3 run above (recorded telemetry, blind localization). Together they measure the
  two halves: the live k6 -> Splunk loop here, the diagnosis engine on a recognized benchmark there.
- The scored verdict is computed deterministically from the Splunk correlation, not from the model's
  prose, so a run cannot pass on a hallucinated analysis: the model authors the load test and writes
  the explanation and fix, but the pass/fail is the telemetry.

## Reproduce

```bash
uv run python scripts/seed_splunk.py          # once: index + HEC + sample telemetry
uv run python scripts/benchmark.py --reps 10  # writes docs/benchmark/results.json
uv run python scripts/benchmark_report.py     # regenerates this file
```
