"""A demo target for kassi: client-side throttling (429), not server faults (5xx).

    uv run --with fastapi --with uvicorn --with httpx python examples/gateway/app.py serve
    uv run --with fastapi python examples/gateway/app.py dump   # write openapi.json

GET /api/status and GET /healthz are fine under load. The "new" endpoint GET /api/quote is
guarded by a per-process token bucket (40 req/s) that is far below the concurrency a load
test offers, so under load most requests are rejected with 429. This is the capacity/config
mismatch class: the server is healthy (no 5xx), the failures are client-side throttling that
only appears once offered load exceeds the limit. kassi's correlation separates these 4xx
from 5xx, so the verdict reads "throttled, not broken".

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
app = FastAPI(title="kassi gateway", version="1.0.0")

# Token bucket: 40 tokens/s, burst 40. Far below the load test's offered concurrency.
_RATE = 40.0
_BURST = 40.0
_bucket_lock = threading.Lock()
_tokens = _BURST
_refilled_at = time.monotonic()


def _take_token() -> bool:
    global _tokens, _refilled_at
    with _bucket_lock:
        now = time.monotonic()
        _tokens = min(_BURST, _tokens + (now - _refilled_at) * _RATE)
        _refilled_at = now
        if _tokens >= 1.0:
            _tokens -= 1.0
            return True
        return False


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


@app.get("/api/status")
def status() -> dict:
    return {"region": "us-east-1", "healthy": True}


@app.get("/api/quote")
def quote(request: Request) -> JSONResponse:
    """New in this change: a per-process token bucket (40 req/s) on the quote endpoint. The
    limit is well below the concurrency a load test offers, so under load most requests are
    throttled with 429. Invisible at low volume, it dominates under load."""
    if not _take_token():
        request.state.error_message = "rate limited"
        return JSONResponse(status_code=429, content={"error": "rate limited"})
    return JSONResponse(status_code=200, content={"symbol": "ACME", "price": 42.0})


def main() -> None:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        (Path(__file__).parent / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))
        print("wrote openapi.json")
        return
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8403, log_level="warning")


if __name__ == "__main__":
    main()
