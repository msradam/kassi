"""Seed a local Splunk Enterprise with sample target telemetry and verify the SPL
kassi generates against it.

Idempotent: creates the index and HEC token if missing, ingests sample HTTP access
events (a `status` and `response_time` field), then runs kassi's correlation rollup
and prints the result.

    uv run python scripts/seed_splunk.py

Env (defaults target a local non-root install):
    SPLUNK_MGMT   https://localhost:8089
    SPLUNK_HEC    http://localhost:8088
    SPLUNK_USER   admin
    SPLUNK_PASS   kassi-admin-2026
    SPLUNK_INDEX  web
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request

from kassi import parse

MGMT = os.environ.get("SPLUNK_MGMT", "https://localhost:8089")
HEC = os.environ.get("SPLUNK_HEC", "http://localhost:8088")
USER = os.environ.get("SPLUNK_USER", "admin")
PASS = os.environ.get("SPLUNK_PASS", "kassi-admin-2026")
INDEX = os.environ.get("SPLUNK_INDEX", "web")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _req(
    url: str, data: dict | None = None, *, auth: tuple[str, str] | None = None, headers: dict | None = None
):
    body = None
    if data is not None:
        body = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in data.items()).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {})
    if auth:
        import base64

        tok = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
    try:
        with urllib.request.urlopen(req, context=_CTX) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def ensure_index() -> None:
    status, _ = _req(
        f"{MGMT}/services/data/indexes", {"name": INDEX, "output_mode": "json"}, auth=(USER, PASS)
    )
    print(f"index {INDEX!r}: {'created' if status == 201 else f'exists ({status})'}")


def ensure_hec_token() -> str:
    _req(
        f"{MGMT}/servicesNS/nobody/splunk_httpinput/data/inputs/http/http",
        {"disabled": 0, "enableSSL": 0, "output_mode": "json"},
        auth=(USER, PASS),
    )
    status, text = _req(
        f"{MGMT}/servicesNS/nobody/splunk_httpinput/data/inputs/http",
        {"name": "kassi", "index": INDEX, "indexes": INDEX, "output_mode": "json"},
        auth=(USER, PASS),
    )
    if status not in (200, 201):
        # already exists (409) or other: read the token back by name
        status, text = _req(
            f"{MGMT}/servicesNS/nobody/splunk_httpinput/data/inputs/http/kassi?output_mode=json",
            auth=(USER, PASS),
        )
    token = json.loads(text)["entry"][0]["content"]["token"]
    print(f"HEC token: {token}")
    return token


def ingest(token: str, n: int = 200) -> None:
    now = time.time()
    events = []
    for i in range(n):
        status = 500 if i < 7 else 404 if i < 12 else 200
        rt = 5 + (i % 35)
        events.append(
            {
                "time": now - 1 + i * 0.001,
                "index": INDEX,
                "sourcetype": "access_json",
                "event": {"status": status, "response_time": rt, "path": "/api/pets"},
            }
        )
    body = "\n".join(json.dumps(e) for e in events).encode()
    req = urllib.request.Request(
        f"{HEC}/services/collector/event", data=body, headers={"Authorization": f"Splunk {token}"}
    )
    with urllib.request.urlopen(req, context=_CTX) as resp:
        print("HEC ingest:", json.load(resp))


def verify() -> None:
    spl = parse.build_correlation_spl(INDEX, time.time() - 300, time.time())
    print("\nSPL kassi generates:\n ", spl)
    status, text = _req(
        f"{MGMT}/services/search/jobs/export",
        {"search": spl, "output_mode": "json", "earliest_time": "-5m", "latest_time": "now"},
        auth=(USER, PASS),
    )
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "result" in row:
            print("result:", json.dumps(row["result"]))


if __name__ == "__main__":
    ensure_index()
    token = ensure_hec_token()
    ingest(token)
    time.sleep(6)
    verify()
