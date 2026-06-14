"""A generic access-log proxy: put kassi in front of ANY HTTP service.

    UPSTREAM=http://localhost:8080 uv run --with fastapi --with uvicorn --with httpx \
      python scripts/access_proxy.py serve --port 8500

Reverse-proxies every request to the UPSTREAM service and ships one access_json event per request to
Splunk's HEC (index=web, sourcetype=access_json: {method, path, status, response_time, db_time,
error_message}), the same shape kassi's demo apps emit. So kassi can observe a third-party app it
never instrumented, the way a real API gateway or load balancer feeds its access logs to Splunk.
The proxy sees status, path, and latency; it cannot see app-internal db_time, and for a 5xx it
records a generic "upstream <code>" rather than the app's own error string.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import ssl
import sys
import threading
import time
import urllib.request

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

UPSTREAM = os.environ.get("UPSTREAM", "http://localhost:8080").rstrip("/")
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
                self._token = None


shipper = HecShipper()
app = FastAPI(title="kassi access-log proxy", version="1.0.0")
_client = httpx.AsyncClient(base_url=UPSTREAM, timeout=30.0)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "upstream": UPSTREAM}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request) -> Response:
    start = time.perf_counter()
    body = await request.body()
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    error_message = None
    try:
        up = await _client.request(
            request.method, "/" + path, params=request.query_params, content=body, headers=fwd
        )
        status, content = up.status_code, up.content
    except Exception as exc:  # upstream unreachable / timed out
        status, content, error_message = 502, b'{"error":"upstream unreachable"}', f"upstream error: {exc}"
    elapsed = round((time.perf_counter() - start) * 1000, 2)
    if status >= 500 and error_message is None:
        error_message = f"upstream returned {status}"
    shipper.send(
        {
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "response_time": elapsed,
            "db_time": 0.0,
            "error_message": error_message,
        }
    )
    media = (
        up.headers.get("content-type", "application/json") if error_message is None else "application/json"
    )
    return Response(content=content, status_code=status, media_type=media.split(";")[0])


def main() -> None:
    port = 8500
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        import uvicorn

        print(
            f"access-log proxy on :{port} -> {UPSTREAM} (shipping access_json to Splunk index={SPLUNK_INDEX})"
        )
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
