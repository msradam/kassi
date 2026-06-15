# Case studies

Four real runs of kassi against deliberately flawed code changes, one per failure
class. Each change passes its unit tests and only misbehaves under concurrent load.
Every verdict, number, and forecast below is copied from an actual run; the
`Reproduce` block at the end of each study reruns it end to end against a live
Splunk.

| # | Case | Class | Changed endpoint | Verdict in one line |
|---|------|-------|------------------|---------------------|
| 1 | [Load-only write-lock regression](01-load-only-write-lock-regression.md) | 5xx regression | `POST /api/visits` | `database is locked`, 22% 5xx under load |
| 2 | [The degradation no alarm catches](02-silent-latency-degradation.md) | latency degradation | `POST /api/events` | p95 creeping toward the forecast wall, zero errors |
| 3 | [Throttled, not broken](03-throttled-not-broken.md) | 4xx throttling | `GET /api/quote` | 49% 4xx, no server fault, do not page anyone |
| 4 | [The cause is a dependency](04-downstream-timeout-cascade.md) | downstream cascade | `POST /api/order` | `payment upstream timed out`, fix is resilience not code |

The point of the set is the spread. Two of these would page you (1 and 4), one
would not (3), and one (2) never trips a threshold at all. kassi tells them apart
from the same evidence, and on the control versions of these same services it
returns `passed` with zero false alarms.

Each target lives under [`examples/`](../examples/); each is a healthy baseline
service with one flawed "new" endpoint, instrumented to ship an access log to
Splunk's HEC. The same harness scores all of them in
[`scripts/benchmark.py`](../scripts/benchmark.py).
