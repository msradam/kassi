"""A demo target for kassi: a downstream timeout cascade, latency plus 504s.

    uv run --with fastapi --with uvicorn --with httpx python examples/orders/app.py serve
    uv run --with fastapi python examples/orders/app.py dump   # write openapi.json

GET /api/catalog and GET /healthz are fine under load. The "new" endpoint POST /api/order
calls a payment downstream synchronously with no timeout budget, retry, or circuit breaker.
The downstream has limited concurrency (a small worker pool), so under load order requests
queue on it and breach the gateway's wait budget, returning 504 "payment upstream timed out"
mixed with slow 201s. This is the cascading-timeout class: the regression is neither a pure
error nor pure latency but a mix, and the cause is a dependency, not this service's own code,
so the fix is resilience (timeout, retry, circuit breaker), which kassi's analysis recommends.

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
app = FastAPI(title="kassi orders", version="1.0.0")

# The payment downstream handles only 4 calls at once; each takes ~50ms.
_downstream = threading.Semaphore(4)
_DOWNSTREAM_MS = 0.05
_WAIT_BUDGET_S = 0.15


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


@app.get("/api/catalog")
def catalog() -> list[dict]:
    return [{"sku": "WIDGET-1", "price": 9.99}]


@app.post("/api/order")
def create_order(request: Request, order: dict) -> JSONResponse:
    """New in this change: charge the payment downstream synchronously, with no timeout
    budget, retry, or circuit breaker. The downstream serves few callers at once, so under
    load orders queue on it; those that wait past the budget return 504."""
    t0 = time.perf_counter()
    got_slot = _downstream.acquire(timeout=_WAIT_BUDGET_S)
    if not got_slot:
        request.state.error_message = "payment upstream timed out"
        request.state.db_time = round((time.perf_counter() - t0) * 1000, 2)
        return JSONResponse(status_code=504, content={"error": "payment upstream timed out"})
    try:
        time.sleep(_DOWNSTREAM_MS)  # the downstream "processes" the charge
    finally:
        _downstream.release()
    request.state.db_time = round((time.perf_counter() - t0) * 1000, 2)
    return JSONResponse(status_code=201, content={"order_id": 1, "status": "paid"})


def main() -> None:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        (Path(__file__).parent / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))
        print("wrote openapi.json")
        return
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8404, log_level="warning")


if __name__ == "__main__":
    main()
