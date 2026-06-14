"""Keep only the latest run in index=kassi_runs (drop test/older runs) for a clean dashboard.

    uv run python scripts/purge_runs.py

Needs the 'can_delete' capability on the Splunk user. Env: SPLUNK_MGMT / SPLUNK_USER / SPLUNK_PASS.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.parse
import urllib.request

MGMT = os.environ.get("SPLUNK_MGMT", "https://localhost:8089")
USER = os.environ.get("SPLUNK_USER", "admin")
PASS = os.environ.get("SPLUNK_PASS", "kassi-admin-2026")
INDEX = os.environ.get("KASSI_RUN_INDEX", "kassi_runs")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def search(spl: str) -> list:
    auth = "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    data = urllib.parse.urlencode(
        {"search": "search " + spl, "exec_mode": "oneshot", "output_mode": "json",
         "earliest_time": "-30d", "latest_time": "now"}
    ).encode()  # fmt: skip
    req = urllib.request.Request(f"{MGMT}/services/search/jobs", data=data, headers={"Authorization": auth})
    return json.load(urllib.request.urlopen(req, context=_CTX, timeout=60)).get("results", [])


def main() -> None:
    latest = f"[search index={INDEX} sourcetype=kassi:run | head 1 | return app_id]"
    print("deleted:", search(f"index={INDEX} NOT {latest} | delete"))
    print("remaining:", search(f"index={INDEX} | stats count by sourcetype"))


if __name__ == "__main__":
    main()
