"""A small demo target for kassi: a healthy baseline plus one flawed new endpoint.

    uv run --with fastapi --with uvicorn --with httpx python examples/petclinic/app.py serve
    uv run --with fastapi python examples/petclinic/app.py dump   # write openapi.json

GET /api/owners, GET /api/vets and GET /healthz are fine under load. The "new"
endpoint POST /api/visits writes inside a held SQLite IMMEDIATE transaction with a
short busy timeout and no connection pooling, so concurrent writers collide and
SQLite raises "database is locked". That is invisible serially and only bites under
concurrency, the classic load-only regression.

An access-log middleware ships one event per request to Splunk's HEC (index web,
sourcetype access_json): {path, method, status, response_time, db_time,
error_message}. That server-side telemetry is what kassi's correlate step reads back
over the exact test window.
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

DB_PATH = Path(tempfile.gettempdir()) / "kassi_petclinic.db"

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
app = FastAPI(title="kassi petclinic", version="1.0.0")


def _init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS visits (id INTEGER PRIMARY KEY, pet TEXT, note TEXT)")
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


@app.get("/api/owners")
def list_owners() -> list[dict]:
    return [{"id": 1, "name": "George Franklin"}, {"id": 2, "name": "Betty Davis"}]


@app.get("/api/vets")
def list_vets() -> list[dict]:
    return [{"id": 1, "name": "James Carter", "specialty": "radiology"}]


@app.post("/api/visits")
def create_visit(request: Request, visit: dict) -> JSONResponse:
    """New in this change: record a visit. Writes inside a held IMMEDIATE transaction
    with a 50ms busy timeout and no pooling, so it serializes and locks under load."""
    pet = str(visit.get("pet", "unknown"))
    note = str(visit.get("note", ""))
    t0 = time.perf_counter()
    conn = sqlite3.connect(DB_PATH, timeout=0.25)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO visits (pet, note) VALUES (?, ?)", (pet, note))
        time.sleep(0.015)  # the new code holds the write lock while it "processes"
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        request.state.db_time = (time.perf_counter() - t0) * 1000
        request.state.error_message = str(exc)  # "database is locked"
        return JSONResponse(status_code=500, content={"error": str(exc)})
    finally:
        conn.close()
    request.state.db_time = (time.perf_counter() - t0) * 1000
    return JSONResponse(status_code=201, content={"pet": pet, "note": note})


def main() -> None:
    import sys

    _init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        (Path(__file__).parent / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))
        print("wrote openapi.json")
        return
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8400, log_level="warning")


if __name__ == "__main__":
    main()
