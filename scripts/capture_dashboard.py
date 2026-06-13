"""Headless screenshot of the kassi Splunk dashboard into docs/assets/shot-dashboard.png.

    uv run --with playwright python scripts/capture_dashboard.py

Logs into Splunk web, opens the kassi_overview dashboard, waits for the panels' searches to
finish, and captures the dashboard body. Env (defaults target the local/tunneled Splunk):
    SPLUNK_WEB   http://localhost:8000
    SPLUNK_USER  admin
    SPLUNK_PASS  kassi-admin-2026
"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

WEB = os.environ.get("SPLUNK_WEB", "http://localhost:8000")
USER = os.environ.get("SPLUNK_USER", "admin")
PASS = os.environ.get("SPLUNK_PASS", "kassi-admin-2026")
OUT = Path(__file__).resolve().parents[1] / "docs" / "assets" / "shot-dashboard.png"
DASH = f"{WEB}/en-US/app/search/kassi_overview"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 2200}, device_scale_factor=2)

        page.goto(f"{WEB}/en-US/account/login", wait_until="domcontentloaded")
        page.fill("input[name=username]", USER)
        page.fill("input[name=password]", PASS)
        page.click("input[type=submit], button[type=submit]")
        # Splunk holds long-poll connections open, so networkidle never settles; wait for the
        # post-login redirect into an app instead.
        page.wait_for_url("**/app/**", timeout=30000)

        page.goto(DASH, wait_until="domcontentloaded")
        # let every panel's search complete and the charts paint
        page.wait_for_timeout(22000)

        body = page.query_selector(".dashboard-body") or page.query_selector("div[data-view='views/dashboard/Dashboard']")
        (body or page).screenshot(path=str(OUT))
        browser.close()
        print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
