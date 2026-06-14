"""Record a smooth, narration-paced video of the kassi Splunk dashboard -> docs/clips/clip-dashboard.mp4.

    uv run --with playwright python scripts/capture_dashboard_video.py

Logs in (off-record) to warm the search cache, then records a second context that dwells on each
panel section (room for voiceover) and eases smoothly between them. Env: SPLUNK_WEB / SPLUNK_USER /
SPLUNK_PASS (defaults target the local Splunk).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

WEB = os.environ.get("SPLUNK_WEB", "http://localhost:8000")
USER = os.environ.get("SPLUNK_USER", "admin")
PASS = os.environ.get("SPLUNK_PASS", "kassi-admin-2026")
DASH = f"{WEB}/en-US/app/search/kassi_overview"
CLIPS = Path(__file__).resolve().parents[1] / "docs" / "clips"
TMP = CLIPS / "_vid"
VP = {"width": 1920, "height": 1080}

# JS easing: scroll over `dur` ms with easeInOutQuad so motion reads smooth at the capture fps.
_EASE = """([targetY, dur]) => new Promise((resolve) => {
  const startY = window.scrollY, dist = targetY - startY, t0 = performance.now();
  function step(now) {
    const t = Math.min(1, (now - t0) / dur);
    const e = t < 0.5 ? 2*t*t : 1 - Math.pow(-2*t + 2, 2)/2;
    window.scrollTo(0, startY + dist*e);
    if (t < 1) requestAnimationFrame(step); else resolve();
  }
  requestAnimationFrame(step);
})"""


def main() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        warm = browser.new_context(viewport=VP)
        pg = warm.new_page()
        pg.goto(f"{WEB}/en-US/account/login", wait_until="domcontentloaded")
        pg.fill("input[name=username]", USER)
        pg.fill("input[name=password]", PASS)
        pg.click("input[type=submit], button[type=submit]")
        pg.wait_for_url("**/app/**", timeout=30000)
        pg.goto(DASH, wait_until="domcontentloaded")
        pg.wait_for_timeout(20000)
        state = warm.storage_state()
        warm.close()

        rec = browser.new_context(viewport=VP, storage_state=state, record_video_dir=str(TMP), record_video_size=VP)
        page = rec.new_page()
        page.goto(DASH, wait_until="domcontentloaded")
        page.wait_for_timeout(9000)  # panels paint from the warm cache

        # dwell on each section (narration room), ease smoothly to the next
        height = page.evaluate("document.body.scrollHeight")
        stops = [0, 560, 1150, 1750, max(0, height - 1080)]
        page.wait_for_timeout(4000)  # the verdict + the reading
        for y in stops[1:]:
            page.evaluate(_EASE, [y, 1600])
            page.wait_for_timeout(3800)  # hold for narration
        page.evaluate(_EASE, [0, 1400])
        page.wait_for_timeout(1500)
        video = page.video
        rec.close()
        src = Path(video.path()) if video else None
        browser.close()

    out = CLIPS / "clip-dashboard.mp4"
    if src and src.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "8", "-i", str(src), "-r", "60",
             "-vf", "scale=1920:1080,format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
             "-an", str(out)],
            capture_output=True, text=True,
        )  # fmt: skip
        print(f"wrote {out}")
    else:
        print("no video captured")


if __name__ == "__main__":
    main()
