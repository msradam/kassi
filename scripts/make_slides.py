"""Render the kassi pitch deck as 1080p PNG slides and stitch them into an MP4.

    uv run --with pillow python scripts/make_slides.py

Writes docs/deck/slide-NN.png (1920x1080) and, if ffmpeg is present, docs/deck/deck.mp4
(each slide held a few seconds). Load the PNGs or the MP4 into Adobe Express. The palette and
fonts match the cover (scripts/make_cover.py).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DECK = ROOT / "docs" / "deck"

WHITE = (236, 238, 243)
GRAY = (171, 178, 191)
DIM = (176, 92, 134)
ACCENT = (244, 86, 161)
FRAME = (128, 64, 100)
BG_TOP = (23, 13, 21)
BG_BOT = (11, 7, 12)

FONTS = {
    "serif": "/System/Library/Fonts/Supplemental/Didot.ttc",
    "serif_italic": "/System/Library/Fonts/Supplemental/Georgia Italic.ttf",
    "sans": "/System/Library/Fonts/SFNS.ttf",
    "mono": "/System/Library/Fonts/SFNSMono.ttf",
}


def font(role: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONTS[role]
    if not Path(path).exists():
        path = "/System/Library/Fonts/Supplemental/Georgia.ttf"
    return ImageFont.truetype(path, size)


def vgradient(w: int, h: int) -> Image.Image:
    base = Image.new("RGB", (w, h))
    px = base.load()
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(round(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = c
    return base


# Each slide: kicker, optional huge `big`, optional `title`, optional `accent` (italic), body lines.
# Ordered for a sub-3-minute demo: hook, one dense pitch slide, then straight to the live demo with
# a card per scenario, then the close. The judge sees the essentials in the first 20 seconds.
SLIDES = [
    {  # 1. HOOK — front-load what it is and that Splunk AI is used live
        "kicker": "SPLUNK AGENTIC OPS HACKATHON  ·  OBSERVABILITY TRACK",
        "title": "kassi",
        "title_size": 150,
        "accent": "Divines disaster, crafts the cure.",
        "body": [
            "An AI agent that load-tests a code change, finds the regression in Splunk,",
            "and writes the fix — driver, writer, and auditor all on a local 8B model.",
            "Live at runtime: the official Splunk MCP Server + the Splunk AI Toolkit.",
        ],
    },
    {  # 2. THE PITCH — the one slide a judge needs; problem + loop + Splunk proof
        "kicker": "WHY  ·  AND WHAT IT DOES, ALL ON SPLUNK AI AT RUNTIME",
        "title": "~80% of outages trace to a change.",
        "title_size": 72,
        "body": [
            "The warning is there; it isn't believed, because the impact only shows up in",
            "server-side telemetry after something exercises the change. kassi closes the loop:",
            "",
            "1.  Grafana k6 MCP Server drives real load at the changed endpoint",
            "2.  the official Splunk MCP Server reads the server-side truth",
            "3.  the Splunk AI Toolkit forecasts the trend and flags the anomaly",
            "4.  a local model writes the cited analysis and the remediation fix",
        ],
    },
    {  # 3. DEMO transition
        "kicker": "LIVE  ·  NOTHING CANNED",
        "title": "Watch it run.",
        "accent": "Real app, real k6, the official Splunk MCP Server, a local model.",
        "body": ["Two changes. Two different failure signatures. Same agent, same dashboard."],
    },
    {  # 4. SCENARIO 1 — errors, with a root cause only Splunk shows
        "kicker": "SCENARIO 1   ·   petclinic   ·   POST /api/visits",
        "title": "A write-lock under load",
        "accent": "58.8% 5xx",
        "body": [
            "What the client sees: more than half of requests fail under concurrency.",
            "What only Splunk reveals: root cause  database is locked, 1,797 times.",
            "kassi's fix: enable WAL / shorten the held write transaction.",
        ],
    },
    {  # 5. SCENARIO 2 — the opposite signature: latency, ZERO errors
        "kicker": "SCENARIO 2   ·   storefront   ·   POST /api/checkout",
        "title": "An N+1 query",
        "accent": "latency, and zero errors",
        "body": [
            "What the client sees: it just got slower. No errors at all.",
            "What only Splunk reveals: server-side db_time on the changed endpoint.",
            "Invisible to the error rate — and obvious in the client-plus-server join.",
        ],
    },
    {  # 6. CLOSE — why it wins, the differentiators a judge scores
        "kicker": "WHY KASSI",
        "title": "Fully local. Fully audited.",
        "body": [
            "Driver, writer, and auditor are one local 8B family: Granite 4.1 + Guardian 4.1,",
            "the first ISO/IEC 42001-certified open LLM. On-prem, air-gapped, no per-token cost.",
            "Two MCP servers, deep usage. Splunk's own ML does the forecasting.",
            "And the agent is observable too: its state-machine walk streams back to Splunk.",
        ],
    },
    {  # 7. SIGN-OFF
        "kicker": "",
        "title": "kassi",
        "title_size": 150,
        "accent": "Divines disaster, crafts the cure.",
        "body": ["Observability  ·  Best Use of Splunk MCP Server  ·  Best Use of Developer Tools"],
    },
]


def render(slide: dict, index: int, total: int) -> Image.Image:
    s = 2
    W, H = 1920 * s, 1080 * s
    img = vgradient(W, H)
    d = ImageDraw.Draw(img)
    for inset, width, col in ((40 * s, 3 * s, DIM), (52 * s, 1 * s, FRAME)):
        d.rectangle((inset, inset, W - inset, H - inset), outline=col, width=width)

    x = 150 * s
    if slide.get("kicker"):
        d.text((x, 150 * s), slide["kicker"], font=font("mono", 28 * s), fill=DIM)

    y = 300 * s
    if slide.get("big"):
        d.text((x, 250 * s), slide["big"], font=font("serif", 330 * s), fill=ACCENT)
        y = 720 * s
    if slide.get("title"):
        ts = slide.get("title_size", 96)
        d.text((x, y), slide["title"], font=font("serif", ts * s), fill=slide.get("title_color", WHITE))
        y += int(ts * 1.35) * s
    if slide.get("accent"):
        d.text((x, y), slide["accent"], font=font("serif_italic", 50 * s), fill=ACCENT)
        y += 110 * s

    body_font = font("sans", 42 * s)
    for line in slide.get("body", []):
        d.text((x, y), line, font=body_font, fill=GRAY)
        y += 64 * s

    d.text((x, H - 130 * s), "kassi  ·  divines disaster, crafts the cure", font=font("mono", 24 * s), fill=(110, 116, 130))
    d.text((W - 230 * s, H - 130 * s), f"{index:02d} / {total:02d}", font=font("mono", 24 * s), fill=(110, 116, 130))

    return img.resize((1920, 1080), Image.LANCZOS)


def main() -> None:
    DECK.mkdir(parents=True, exist_ok=True)
    for stale in DECK.glob("slide-*.png"):  # drop any leftover frames from a longer prior deck
        stale.unlink()
    total = len(SLIDES)
    for i, slide in enumerate(SLIDES, 1):
        render(slide, i, total).save(DECK / f"slide-{i:02d}.png")
    print(f"wrote {total} slides to {DECK}")

    if shutil.which("ffmpeg"):
        out = DECK / "deck.mp4"
        cmd = [
            "ffmpeg", "-y", "-framerate", "1/5", "-i", str(DECK / "slide-%02d.png"),
            "-vf", "scale=1920:1080,format=yuv420p", "-c:v", "libx264", "-r", "30", str(out),
        ]  # fmt: skip
        r = subprocess.run(cmd, capture_output=True, text=True)
        print(f"wrote {out}" if r.returncode == 0 else f"ffmpeg failed:\n{r.stderr[-400:]}")
    else:
        print("ffmpeg not found; slides written, skip mp4")


if __name__ == "__main__":
    main()
