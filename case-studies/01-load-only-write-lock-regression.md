# Case study 1: the regression that only exists under load

**Service:** `examples/petclinic` &nbsp;·&nbsp; **Change:** new `POST /api/visits` &nbsp;·&nbsp; **Class:** server-side 5xx regression

## The change

A pull request adds one endpoint to a pet-clinic API: `POST /api/visits` records a
visit. It writes inside a held SQLite `IMMEDIATE` transaction with a short busy
timeout and no connection pooling. The author tested it, the reviewer read it, it
merged.

```python
conn = sqlite3.connect(DB_PATH, timeout=0.25)
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO visits (pet, note) VALUES (?, ?)", (pet, note))
time.sleep(0.015)            # holds the write lock while it "processes"
conn.commit()
```

## What the tests missed

Every unit test passes. One request at a time, the endpoint works fine: it opens
a connection, takes the lock, writes, commits, returns 201. Nothing in a serial
test suite touches the lock contention, because there is no second writer to
contend with. This is the canonical load-only regression. The code is correct one
request at a time and breaks only under concurrency, so the test suite that shipped
it never had a chance to see it.

## What kassi found

kassi generated real k6 load against the changed endpoint, then read the
server-side access log back from Splunk over the exact test window. The verdict:

```
server-side regression: /api/visits p95 285.90ms, 22.0% 5xx,
cause: database is locked
```

- **332 server-side errors**, **0 client-side errors.** The k6 client only sees
  slow 500s coming back; the *reason* (`database is locked`) lives entirely in the
  server's telemetry, which is why correlating against Splunk, not the load tool's
  own output, is what closes the case.
- **p95 285.90ms** under load against a baseline that serves the healthy endpoints
  in single-digit milliseconds.
- The Splunk AI Toolkit's `StateSpaceForecast` and `anomalydetection` flagged the
  bucket independently: forecast p95 333.54ms, **1 anomalous bucket**. The spike is
  confirmed by Splunk's own ML, not by a fixed threshold inside kassi.
- A second model audited the write-up for groundedness before the verdict was
  sealed, and the run plus its full step trace were published back to Splunk.

## The fix it proposed

kassi emits the remediation as a reviewable unified diff, not prose. The fix moves
the work out of the critical section and lets SQLite handle concurrent writers the
way it is designed to: enable WAL, set a real busy timeout, and stop sleeping while
holding the lock.

```python
conn = sqlite3.connect(DB_PATH, timeout=5.0)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")
conn.execute("INSERT INTO visits (pet, note) VALUES (?, ?)", (pet, note))
conn.commit()
# processing moved outside the transaction
```

## Why it matters

This is the failure mode the hackathon brief is about: a change that ships green
and pages you at 2am. The lock contention never shows up in CI, and it stays hidden
in staging too if staging is quiet. It only appears once real production traffic
hits the endpoint. The warning signal, `database is locked`, is server-side, so a
load test alone never surfaces it. kassi runs the load, reads the server truth back,
names the cause, and hands back a diff a reviewer can merge.

## Reproduce

```bash
# spins up the petclinic target, runs the full FSM against it, scores the verdict
uv run python scripts/benchmark.py --scenarios petclinic --reps 1
```

Or drive it interactively against a running instance:

```bash
uv run --with fastapi --with uvicorn --with httpx \
  python examples/petclinic/app.py serve   # in one shell
uv run kassi pilot --intent "load test recording a visit" \
  --openapi examples/petclinic/openapi.json --base-url http://127.0.0.1:8400
```
