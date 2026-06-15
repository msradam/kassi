# Case study 4: the cause is a dependency, not your code

**Service:** `examples/orders` &nbsp;·&nbsp; **Change:** new `POST /api/order` &nbsp;·&nbsp; **Class:** downstream timeout cascade

## The change

An orders service adds `POST /api/order`, which charges a payment downstream
synchronously with no retry and no circuit breaker. The downstream serves only a
few callers at once, so under load order requests queue on it; those that wait past
the budget return 504 `payment upstream timed out`, mixed with slow 201s.

```python
got_slot = _downstream.acquire(timeout=_WAIT_BUDGET_S)
if not got_slot:
    return JSONResponse(status_code=504, content={"error": "payment upstream timed out"})
time.sleep(_DOWNSTREAM_MS)   # the downstream "processes" the charge
```

## What the tests missed

In CI the payment downstream is a mock that answers instantly, so the endpoint
passes. The fault is not in this service's logic at all; the code that touches the
database and builds the order is fine. It is a *resilience* gap that only appears
when a real dependency under real load runs out of capacity. The failure is also
mixed: some requests 504, others succeed but slowly, so neither a pure error
detector nor a pure latency detector sees the whole shape.

## What kassi found

```
server-side regression: /api/order p95 68.24ms, 1.0% 5xx,
cause: payment upstream timed out
```

- **18 server-side errors** alongside slow successes, the exact mixed signature of
  a cascade. kassi read the access log back from Splunk and pulled the dominant
  error string straight from the server-side telemetry: `payment upstream timed
  out`. That string is what distinguishes "your code threw" from "your dependency
  did not answer in time."
- The verdict names the cause as the downstream, and the proposed remediation is
  resilience, not a logic change: add a timeout budget, a bounded retry, and a
  circuit breaker around the payment call so a slow dependency degrades gracefully
  instead of cascading into 504s.

## Catching it at commit time with `kassi watch`

This is the case that motivates the background mode. `kassi watch` polls a repo's
git HEAD and, the moment a commit changes an endpoint, runs the whole workflow in
diff mode automatically, hands-free:

```bash
kassi watch --repo-path . --target-base-url http://127.0.0.1:8404 --splunk-index web
```

When the `POST /api/order` change lands, the guard fires on its own, drives the
same FSM, and surfaces the `payment upstream timed out` verdict with the resilience
diff before the change is anywhere near production. Add `--once` to wire it into a
pre-push hook or CI step. The agent guards the repo instead of waiting to be asked.

## Why it matters

Most "fix the diff" tooling assumes the bug is in the diff. Here it is not: the
changed code is correct, and the cause is an architectural omission that only a
dependency under load reveals. kassi gets this right because it runs the real
experiment and reads the real server-side error, rather than reasoning about the
patch in the abstract. The verdict points the engineer at the dependency boundary,
which is where the fix actually belongs.

## Reproduce

```bash
uv run python scripts/benchmark.py --scenarios orders --reps 1
```
