"""A demo target for kassi: latency that creeps up over the test, not a constant step.

    uv run --with fastapi --with uvicorn --with httpx python examples/feed/app.py serve
    uv run --with fastapi python examples/feed/app.py dump   # write openapi.json

GET /api/feed and GET /healthz are fine under load. The "new" endpoint POST /api/events
appends to an unbounded in-memory log and recomputes "trending" by sorting the entire log
on every write, so per-request work grows O(n log n) with the traffic the test has already
sent. A short or serial test looks fine; under sustained load p95 climbs steadily as the log
grows, the cumulative-degradation regression that only a soak reveals. No errors: every
request returns 200, the cost is server-side time that rises over the window. That rising
trend is what kassi's predict/StateSpaceForecast forecast and anomalydetection flag.

The access-log middleware ships one event per request to Splunk's HEC (index web,
sourcetype access_json): {path, method, status, response_time, db_time, error_message}.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import ssl
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

SPLUNK_MGMT = os.environ.get("SPLUNK_MGMT", "https://localhost:8089")
SPLUNK_HEC = os.environ.get("SPLUNK_HEC", "http://localhost:8088")
SPLUNK_USER = os.environ.get("SPLUNK_USER", "admin")
SPLUNK_PASS = os.environ.get("SPLUNK_PASS", "kassi-admin-2026")
SPLUNK_INDEX = os.environ.get("SPLUNK_INDEX", "web")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _hec_token() -> str:
    req = urllib.request.Request(
        f"{SPLUNK_MGMT}/servicesNS/nobody/splunk_httpinput/data/inputs/http/kassi?output_mode=json"
    )
    auth = base64.b64encode(f"{SPLUNK_USER}:{SPLUNK_PASS}".encode()).decode()
    req.add_header("Authorization", "Basic " + auth)
    with urllib.request.urlopen(req, context=_CTX) as resp:
        return json.loads(resp.read())["entry"][0]["content"]["token"]


class HecShipper:
    """Background thread that batches access-log events to Splunk's HEC."""

    def __init__(self) -> None:
        self._q: queue.Queue[dict] = queue.Queue()
        self._token: str | None = None
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, event: dict) -> None:
        self._q.put({"time": time.time(), "index": SPLUNK_INDEX, "sourcetype": "access_json", "event": event})

    def _run(self) -> None:
        while True:
            batch = [self._q.get()]
            time.sleep(0.4)
            while not self._q.empty():
                batch.append(self._q.get_nowait())
            try:
                if self._token is None:
                    self._token = _hec_token()
                body = "\n".join(json.dumps(e) for e in batch).encode()
                req = urllib.request.Request(
                    f"{SPLUNK_HEC}/services/collector/event",
                    data=body,
                    headers={"Authorization": f"Splunk {self._token}"},
                )
                with urllib.request.urlopen(req, context=_CTX) as resp:
                    resp.read()
            except Exception:
                self._token = None  # refetch the token next time


shipper = HecShipper()
app = FastAPI(title="kassi feed", version="1.0.0")

# The new endpoint accumulates here and never trims it.
_events: list[dict] = []
_events_lock = threading.Lock()


@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    request.state.db_time = 0.0
    request.state.error_message = None
    response = await call_next(request)
    elapsed = round((time.perf_counter() - start) * 1000, 2)
    shipper.send(
        {
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "response_time": elapsed,
            "db_time": round(getattr(request.state, "db_time", 0.0), 2),
            "error_message": getattr(request.state, "error_message", None),
        }
    )
    return response


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/feed")
def get_feed() -> list[dict]:
    return [{"id": 1, "topic": "release", "title": "v1.0 is out"}]


@app.post("/api/events")
def add_event(request: Request, event: dict) -> JSONResponse:
    """New in this change: record an event into an unbounded store and rescan it to refresh
    "trending". The store is never trimmed, so the rescan touches more rows on every write and
    its cost grows with how much traffic the test has already sent. A short or serial test
    looks fine; under sustained load the per-request time creeps up as the store grows, the
    cumulative-degradation regression a soak reveals. The scan time is modeled as a bounded
    delay proportional to the accumulated count (it stands in for an unindexed read over a
    growing table); no CPU spin, so throughput stays up and the trend is the signal."""
    t0 = time.perf_counter()
    with _events_lock:
        _events.append({"topic": str(event.get("topic", "general"))})
        seen = len(_events)
    # rescan cost grows with the unbounded store (capped so the demo stays bounded)
    time.sleep(min(0.12, seen * 2.5e-5))
    request.state.db_time = round((time.perf_counter() - t0) * 1000, 2)
    return JSONResponse(status_code=200, content={"seen": seen})


def main() -> None:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        (Path(__file__).parent / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))
        print("wrote openapi.json")
        return
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8402, log_level="warning")


if __name__ == "__main__":
    main()
