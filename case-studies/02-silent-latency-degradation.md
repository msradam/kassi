# Case study 2: the degradation no error alarm will ever catch

**Service:** `examples/feed` &nbsp;·&nbsp; **Change:** new `POST /api/events` &nbsp;·&nbsp; **Class:** latency degradation, zero errors

## The change

A social-feed API gains `POST /api/events`. Every write appends to an in-memory
log and refreshes a "trending" view by rescanning the log. The store is never
trimmed, so the rescan touches more rows on every write and per-request cost grows
with the traffic the service has already taken.

```python
with _events_lock:
    _events.append({"topic": str(event.get("topic", "general"))})
    seen = len(_events)
time.sleep(min(0.12, seen * 2.5e-5))   # rescan cost grows with the unbounded store
```

## What the tests missed

Every request returns 200. There is no error to assert against, no exception, no
non-2xx status. A short test, or a serial one, looks perfectly healthy because the
store has not grown yet. The cost is cumulative: it only shows up after sustained
load, and it shows up as *time*, not *failure*. Unit tests, smoke tests, and error
rate alarms are all blind to it by construction.

## What kassi found

```
latency degradation: /api/events p95 50.75ms with no errors;
Splunk forecast p95 80.64ms, 1 anomalous bucket(s)
```

- **0 server errors, 0 client errors.** Nothing failed. Every classical alarm
  stayed quiet through the entire window.
- The signal is the *trend*. kassi leaned on the Splunk AI Toolkit's
  `StateSpaceForecast` to model where latency was heading: a forecast p95 of
  **80.64ms** against the measured window, with **1 anomalous bucket** where the
  observed latency breached its predicted band. The verdict class is `degradation`,
  explicitly distinct from a `regression`, because no error ever fired.
- kassi attributed the cost to the changed endpoint and described the mechanism:
  per-request work growing under sustained load with zero errors. Because there is
  no error string to ground against, the analysis grounds on the rising
  server-side time, and the auditor model checks that grounding before the verdict
  is sealed.

## The fix it proposed

Bound the store. The diff caps it so the rescan cost stops growing with lifetime
traffic.

```python
_events.append({"topic": str(event.get("topic", "general"))})
if len(_events) > 4000:
    del _events[:-4000]
seen = len(_events)
```

## Why it matters

This is the case that makes the architecture worth it. A 5xx regression is easy to
catch lots of ways. A degradation with no error and no threshold breach is almost
impossible to catch without forecasting the trend. By reading the server-side
latency back from Splunk over the test window and running Splunk's own ML over it,
kassi calls the climb before it hits the wall, while the dashboard still reads green.
The verdict labels it `DEGRADING`, not `REGRESSION`, so the on-call response is
"there is time to fix this" rather than "everything is fine."

## Reproduce

```bash
uv run python scripts/benchmark.py --scenarios feed --reps 1
```

Interactive against a running instance:

```bash
uv run --with fastapi --with uvicorn --with httpx \
  python examples/feed/app.py serve
uv run kassi pilot --intent "load test posting an event" \
  --openapi examples/feed/openapi.json --base-url http://127.0.0.1:8402
```
