"""Provision the kassi output dashboard on a local Splunk: the run index, an HEC token
scoped to it, and the dashboard view itself. Idempotent.

    uv run python scripts/setup_dashboard.py

Prints the `KASSI_HEC_TOKEN` to add to .env so `report` publishes each run. The dashboard
reads index=kassi_runs (kassi's own run records) plus index=web (the target's access log).

Env (defaults target a local non-root install):
    SPLUNK_MGMT   https://localhost:8089
    SPLUNK_USER   admin
    SPLUNK_PASS   kassi-admin-2026
    KASSI_RUN_INDEX  kassi_runs
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MGMT = os.environ.get("SPLUNK_MGMT", "https://localhost:8089")
USER = os.environ.get("SPLUNK_USER", "admin")
PASS = os.environ.get("SPLUNK_PASS", "kassi-admin-2026")
INDEX = os.environ.get("KASSI_RUN_INDEX", "kassi_runs")
DASHBOARD = "kassi_overview"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _req(url: str, data: dict | None = None) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode())
    try:
        with urllib.request.urlopen(req, context=_CTX) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def ensure_index() -> None:
    status, _ = _req(f"{MGMT}/services/data/indexes", {"name": INDEX, "output_mode": "json"})
    print(f"index {INDEX!r}: {'created' if status == 201 else f'exists ({status})'}")


def ensure_hec_token() -> str:
    _req(
        f"{MGMT}/servicesNS/nobody/splunk_httpinput/data/inputs/http/http",
        {"disabled": 0, "enableSSL": 0, "output_mode": "json"},
    )
    status, text = _req(
        f"{MGMT}/servicesNS/nobody/splunk_httpinput/data/inputs/http",
        {"name": DASHBOARD, "index": INDEX, "indexes": f"{INDEX},web", "output_mode": "json"},
    )
    if status not in (200, 201):
        status, text = _req(
            f"{MGMT}/servicesNS/nobody/splunk_httpinput/data/inputs/http/{DASHBOARD}?output_mode=json"
        )
    token = json.loads(text)["entry"][0]["content"]["token"]
    print(f"HEC token ({DASHBOARD}): {token}")
    return token


def install_stylesheet() -> None:
    """Copy the cover-matching CSS into the search app's static dir (referenced by the
    dashboard's `stylesheet` attribute). Best-effort: skipped if SPLUNK_HOME isn't local."""
    splunk_home = os.environ.get("SPLUNK_HOME") or os.path.expanduser("~/splunk")
    static = Path(splunk_home) / "etc" / "apps" / "search" / "appserver" / "static"
    if not static.parent.parent.exists():
        print(f"stylesheet: skipped (no app dir at {static}); copy kassi.css there manually")
        return
    static.mkdir(parents=True, exist_ok=True)
    (static / "kassi.css").write_text((ROOT / "docs" / "dashboard" / "kassi.css").read_text())
    print(f"stylesheet: installed kassi.css -> {static} (run /en-US/_bump if iterating)")


def install_dashboard() -> None:
    xml = (ROOT / "docs" / "dashboard" / "kassi_overview.xml").read_text()
    status, _ = _req(
        f"{MGMT}/servicesNS/nobody/search/data/ui/views",
        {"name": DASHBOARD, "eai:data": xml, "output_mode": "json"},
    )
    if status in (200, 201):
        print(f"dashboard {DASHBOARD!r}: created")
        return
    # already exists: update the definition in place
    status, text = _req(
        f"{MGMT}/servicesNS/nobody/search/data/ui/views/{DASHBOARD}",
        {"eai:data": xml, "output_mode": "json"},
    )
    print(
        f"dashboard {DASHBOARD!r}: {'updated' if status in (200, 201) else f'error {status}: {text[:200]}'}"
    )


if __name__ == "__main__":
    ensure_index()
    token = ensure_hec_token()
    install_stylesheet()
    install_dashboard()
    web = MGMT.replace("8089", "8000").replace("https://", "http://")
    print("\nDashboard:", f"{web}/en-US/app/search/{DASHBOARD}")
    print("Add to .env so each run publishes:")
    print(f"  KASSI_HEC_TOKEN={token}")
    print("  KASSI_HEC_URL=http://localhost:8088")
    print(f"  KASSI_RUN_INDEX={INDEX}")
