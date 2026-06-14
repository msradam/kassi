"""Render docs/benchmark/BENCHMARK.md from the kassi-bench results.

    uv run python scripts/benchmark_report.py

Reads docs/benchmark/results.json (written by scripts/benchmark.py) and the LABELS it was scored
against, computes the accuracy metrics, and writes the benchmark report. Idempotent: rerun after a
fresh benchmark to refresh the numbers.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from benchmark import LABELS  # same dir on sys.path when run as a script

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "benchmark"


def _pct(rows: list[dict], key: str) -> str:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return f"{round(100 * sum(vals) / len(vals))}%" if vals else "n/a"


def _n(rows: list[dict], key: str) -> int:
    return len([r for r in rows if r.get(key) is not None])


def main() -> None:
    runs = json.loads((OUT / "results.json").read_text())
    ok = [r for r in runs if "error" not in r]
    errored = len(runs) - len(ok)
    faults = [n for n in LABELS if LABELS[n]["klass"] != "none"]
    controls = [n for n in LABELS if LABELS[n]["klass"] == "none"]
    frows = [r for r in ok if LABELS[r["scenario"]]["klass"] != "none"]
    crows = [r for r in ok if LABELS[r["scenario"]]["klass"] == "none"]
    reps = max((r["rep"] for r in runs), default=-1) + 1
    secs = [r["seconds"] for r in ok if "seconds" in r]
    med = round(statistics.median(secs)) if secs else 0

    def frow(name: str) -> str:
        rows = [r for r in ok if r["scenario"] == name]
        lab = LABELS[name]
        return (
            f"| `{name}` | `{lab['endpoint']}` | {lab['klass']} | {len(rows)} | "
            f"{_pct(rows, 'detected')} | {_pct(rows, 'localized')} | {_pct(rows, 'class_ok')} | "
            f"{_pct(rows, 'cause_ok')} | **{_pct(rows, 'correct')}** |"
        )

    def crow(name: str) -> str:
        rows = [r for r in ok if r["scenario"] == name]
        lab = LABELS[name]
        return f"| `{name}` | `{lab['endpoint']}` | {len(rows)} | {_pct(rows, 'detected')} | **{_pct(rows, 'correct')}** |"

    fault_lines = "\n".join(frow(n) for n in faults if any(r["scenario"] == n for r in ok))
    ctrl_lines = "\n".join(crow(n) for n in controls if any(r["scenario"] == n for r in ok))

    md = f"""# kassi-bench

A reproducible, ground-truth benchmark for change-induced performance regressions. Given a code
change that introduces one known performance fault, does kassi, from live load and live Splunk,
detect it, attribute it to the right endpoint, classify the failure mode, and name the root cause,
without crying wolf on a healthy endpoint?

## Headline

Across **{len(ok)} live runs** ({len(faults)} fault classes + {len(controls)} healthy controls,
{reps} reps each, {errored} errored), scored on kassi's actual verdict:

| | detection | localization | failure-class | root-cause | overall correct |
| --- | --- | --- | --- | --- | --- |
| **faults** (n={len(frows)}) | {_pct(frows, "detected")} | {_pct(frows, "localized")} | {_pct(frows, "class_ok")} | {_pct(frows, "cause_ok")} | **{_pct(frows, "correct")}** |
| **controls** (n={len(crows)}) | {_pct(crows, "detected")} false-alarm | n/a | n/a | n/a | **{_pct(crows, "correct")}** |

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

- **{len(LABELS)} scenarios.** {len(faults)} faults, each a distinct failure class, plus
  {len(controls)} healthy controls: the same apps under the same load on a benign endpoint, where
  the only correct answer is "nothing wrong." Controls measure false positives; a benchmark that
  cannot fail proves nothing.
- **Deterministic load.** The k6 scaffold at 25 VUs / 25s, identical every rep, model off (the
  driver/writer are bypassed), so the only variable is kassi's correlation.
- **{reps} repetitions** per scenario ({len(runs)} runs total, median {med}s/run), against a live
  Splunk Enterprise 10.4.0 through the official Splunk MCP Server.
- **Scored on kassi's real verdict**, not a reconstruction: detection, endpoint localization
  (top-1), failure-class, root cause, and a per-run `correct` that requires all of the applicable
  dimensions (and, for a control, that kassi stays silent).

## Per-scenario results

| scenario | endpoint | class | n | detect | localize | class | cause | correct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{fault_lines}

| control (healthy) | endpoint | n | false-alarm | correct |
| --- | --- | --- | --- | --- |
{ctrl_lines}

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
   is actually slow. Control false-alarm rate dropped to {_pct(crows, "detected")}; the real
   degradations (p95 50-90ms) are unaffected.

That is the point of the controls, and of the benchmark: it found two ways kassi was wrong and
turned them into fixes.

## Scope and limits

- Localization is top-1 attribution to the changed endpoint the load targets, not multi-service
  fault-propagation ranking (RCAEval's domain).
- {len(faults)} fault classes x {reps} reps is a focused suite, not RCAEval's 735 cases. It is an
  honest, reproducible measure of the live-load diagnosis loop, not a leaderboard entry.
- The diagnosis scored here is kassi's deterministic correlation (the part that does not depend on
  the model). The model writes the prose analysis and the remediation diff on top of it.

## Reproduce

```bash
uv run python scripts/seed_splunk.py          # once: index + HEC + sample telemetry
uv run python scripts/benchmark.py --reps 10  # writes docs/benchmark/results.json
uv run python scripts/benchmark_report.py     # regenerates this file
```
"""
    (OUT / "BENCHMARK.md").write_text(md)
    print(f"wrote {OUT / 'BENCHMARK.md'}  ({len(ok)}/{len(runs)} runs)")


if __name__ == "__main__":
    main()
