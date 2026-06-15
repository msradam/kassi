# Case study 3: throttled, not broken

**Service:** `examples/gateway` &nbsp;·&nbsp; **Change:** new `GET /api/quote` &nbsp;·&nbsp; **Class:** 4xx client throttling, no server fault

## The change

An API gateway adds `GET /api/quote`, guarded by a per-process token bucket of 40
requests per second. Under a load test that offers far more concurrency than that,
most requests are rejected with 429.

```python
if not _take_token():       # 40 req/s token bucket, far below offered load
    return JSONResponse(status_code=429, content={"error": "rate limited"})
```

## What a naive tool would do

A load test that only watches the failure rate sees almost half its requests fail
and screams. A diff-only reviewer sees a new endpoint returning errors and assumes
a bug. Both are wrong. Nothing is broken. The server is healthy; the load test is
simply offering more traffic than the configured limit allows, and the rate limiter
is doing exactly its job. Paging an engineer for this is a false alarm, and false
alarms are how monitoring loses trust.

## What kassi found

```
client-side throttling: /api/quote 49% 4xx, no server errors
(rate-limited, not broken)
```

- **980 client-side 4xx, 0 server-side 5xx.** kassi's correlation separates the two
  classes from the Splunk access log. The failures are all 429s; there is not a
  single server fault behind them.
- **p95 11.29ms.** The requests that get through are fast. There is no latency
  regression and no error regression. The endpoint is healthy under the load it is
  configured to accept.
- The verdict class is `throttling`, a distinct outcome from both `regression` and
  `degradation`. kassi does not propose a code fix, because there is no code fault.
  The finding is a capacity and configuration mismatch: either the limit is set
  too low for expected traffic, or the client needs backpressure. That is a
  decision for a human, and kassi says so rather than inventing a patch.

## Why it matters

The hard part of an automated diagnoser isn't catching faults. It's staying quiet
when nothing is actually wrong. A tool that cries wolf on healthy traffic gets
muted, and then it misses the real regression too. This case shows kassi reads the
evidence rather than pattern-matching on "errors went up": 49% of requests failing
is a five-alarm number, and the correct response is to not page anyone. The same run,
told apart from a real 5xx regression by reading server-side status codes from
Splunk, is what lets the other three case studies be trusted.

This pairs with the control scenarios. On the *healthy* versions of these services
(`gateway-ok`, `petclinic-ok`, `storefront-ok`), kassi returns `passed` every time:
zero false alarms across the live benchmark.

## Reproduce

```bash
uv run python scripts/benchmark.py --scenarios gateway --reps 1

# and the no-fault control, which must come back `passed`:
uv run python scripts/benchmark.py --scenarios gateway-ok --reps 1
```
