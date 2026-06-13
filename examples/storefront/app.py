"""A second demo target for kassi: a latency regression with no error signal.

    uv run --with fastapi --with uvicorn --with httpx python examples/storefront/app.py serve
    uv run --with fastapi python examples/storefront/app.py dump   # write openapi.json

GET /api/products, GET /api/products/{sku} and GET /healthz are fine under load.
The "new" endpoint POST /api/checkout prices an order by fetching each line's price
in its own query through a single shared SQLite connection guarded by one process
lock, with a short per-line hold. Serially that is a handful of cheap reads. Under
concurrency the orders serialize on the shared connection, so p95 climbs while every
request still returns 200. This is the regression load testing alone cannot explain:
the client side sees slower responses and zero errors, and only the server-side
``db_time`` over the test window says the new N+1 checkout is the cause. The Splunk
``predict`` and ``anomalydetection`` phase flags where the latency breaches its band.

The access-log middleware ships one event per request to Splunk's HEC (index web,
sourcetype access_json): {path, method, status, response_time, db_time,
error_message}, the same shape petclinic emits, so kassi's correlate step reads it
back over the exact test window with no query changes.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import sqlite3
import ssl
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DB_PATH = Path(tempfile.gettempdir()) / "kassi_storefront.db"

SPLUNK_MGMT = os.environ.get("SPLUNK_MGMT", "https://localhost:8089")
SPLUNK_HEC = os.environ.get("SPLUNK_HEC", "http://localhost:8088")
SPLUNK_USER = os.environ.get("SPLUNK_USER", "admin")
SPLUNK_PASS = os.environ.get("SPLUNK_PASS", "kassi-admin-2026")
SPLUNK_INDEX = os.environ.get("SPLUNK_INDEX", "web")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

CATALOG = [
    {"sku": "BOOK-01", "name": "The Pragmatic Programmer", "price": 39.99},
    {"sku": "BOOK-02", "name": "Designing Data-Intensive Applications", "price": 54.50},
    {"sku": "BOOK-03", "name": "Release It!", "price": 44.00},
    {"sku": "BOOK-04", "name": "Site Reliability Engineering", "price": 0.00},
]


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
app = FastAPI(title="kassi storefront", version="1.0.0")

# The new checkout shares one connection across all requests, guarded by this lock.
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_db_lock = threading.Lock()


def _init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, name TEXT, price REAL)")
    conn.executemany(
        "INSERT OR REPLACE INTO products (sku, name, price) VALUES (?, ?, ?)",
        [(p["sku"], p["name"], p["price"]) for p in CATALOG],
    )
    conn.commit()
    conn.close()


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


@app.get("/api/products")
def list_products() -> list[dict]:
    return CATALOG


@app.get("/api/products/{sku}")
def get_product(sku: str) -> JSONResponse:
    for p in CATALOG:
        if p["sku"] == sku:
            return JSONResponse(status_code=200, content=p)
    return JSONResponse(status_code=404, content={"error": "not found"})


@app.post("/api/checkout")
def checkout(request: Request, order: dict) -> JSONResponse:
    """New in this change: price an order line by line. Each line is a separate query
    through one shared connection held under a single lock, so concurrent checkouts
    serialize and p95 climbs under load. No errors: every order still totals and
    returns 201. The cost is only visible server-side as db_time."""
    lines = order.get("items") or [{"sku": p["sku"], "qty": 1} for p in CATALOG[:3]]
    t0 = time.perf_counter()
    total = 0.0
    with _db_lock:
        for line in lines:
            row = _conn.execute(
                "SELECT price FROM products WHERE sku = ?", (str(line.get("sku", "")),)
            ).fetchone()
            time.sleep(0.004)  # the new code holds the shared connection while it "prices" each line
            total += (row[0] if row else 0.0) * int(line.get("qty", 1))
    request.state.db_time = round((time.perf_counter() - t0) * 1000, 2)
    return JSONResponse(status_code=201, content={"total": round(total, 2), "lines": len(lines)})


def main() -> None:
    import sys

    _init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        (Path(__file__).parent / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))
        print("wrote openapi.json")
        return
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8401, log_level="warning")


if __name__ == "__main__":
    main()
