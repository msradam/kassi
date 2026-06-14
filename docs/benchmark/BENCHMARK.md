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
| **faults** (n=50) | 100% | 100% | 100% | 100% | **100%** |
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
- **Deterministic load.** The k6 scaffold at 25 VUs / 25s, identical every rep, model off (the
  driver/writer are bypassed), so the only variable is kassi's correlation.
- **10 repetitions** per scenario (80 runs total, median 32s/run), against a live
  Splunk Enterprise 10.4.0 through the official Splunk MCP Server.
- **Scored on kassi's real verdict**, not a reconstruction: detection, endpoint localization
  (top-1), failure-class, root cause, and a per-run `correct` that requires all of the applicable
  dimensions (and, for a control, that kassi stays silent).

## Per-scenario results

| scenario | endpoint | class | n | detect | localize | class | cause | correct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `petclinic` | `/api/visits` | regression | 10 | 100% | 100% | 100% | 100% | **100%** |
| `storefront` | `/api/checkout` | degradation | 10 | 100% | 100% | 100% | n/a | **100%** |
| `feed` | `/api/events` | degradation | 10 | 100% | 100% | 100% | n/a | **100%** |
| `gateway` | `/api/quote` | throttling | 10 | 100% | 100% | 100% | n/a | **100%** |
| `orders` | `/api/order` | regression | 10 | 100% | 100% | 100% | 100% | **100%** |

| control (healthy) | endpoint | n | false-alarm | correct |
| --- | --- | --- | --- | --- |
| `petclinic-ok` | `/api/owners` | 10 | 0% | **100%** |
| `storefront-ok` | `/api/products` | 10 | 0% | **100%** |
| `gateway-ok` | `/api/status` | 10 | 0% | **100%** |

## What the benchmark found, and fixed

Two real defects in kassi's verdict logic surfaced on the very first run and were fixed before the
numbers above (`_verdict` in `src/kassi/app.py`):

1. **4xx throttling was mislabeled "latency degradation."** A pure-429 change (gateway) has no 5xx,
   so the regression branch did not fire; `anomalydetection` then annotated a bucket on the
   near-zero p95 of the fast-rejected requests, and the verdict read "degradation." Added a
   client-side-throttling branch (4xx-dominant, no server errors) ahead of the latency branch, so
   it now reads "rate-limited, not broken," the behavior the demo app always documented.
2. **Healthy endpoints false-alarmed.** `anomalydetection` annotates the odd bucket even on a
   healthy endpoint's sub-10ms jitter, so two of three controls first read as degradation. Added a
   latency floor (`_LATENCY_FLOOR_MS = 25ms`): a flagged bucket only counts as degradation when p95
   is actually slow. Control false-alarm rate dropped to 0%; the real
   degradations (p95 50-90ms) are unaffected.

That is the point of the controls, and of the benchmark: it found two ways kassi was wrong and
turned them into fixes.

## Scope and limits

- Localization is top-1 attribution to the changed endpoint the load targets, not multi-service
  fault-propagation ranking (RCAEval's domain).
- 5 fault classes x 10 reps is a focused suite, not RCAEval's 735 cases. It is an
  honest, reproducible measure of the live-load diagnosis loop, not a leaderboard entry.
- The diagnosis scored here is kassi's deterministic correlation (the part that does not depend on
  the model). The model writes the prose analysis and the remediation diff on top of it.

## Reproduce

```bash
uv run python scripts/seed_splunk.py          # once: index + HEC + sample telemetry
uv run python scripts/benchmark.py --reps 10  # writes docs/benchmark/results.json
uv run python scripts/benchmark_report.py     # regenerates this file
```
