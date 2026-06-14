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
    {  # 1. TITLE
        "kicker": "SPLUNK AGENTIC OPS HACKATHON  ·  OBSERVABILITY TRACK",
        "title": "kassi",
        "title_size": 150,
        "accent": "Divines disaster, crafts the cure.",
        "body": [
            "An AI agent that load-tests a code change, finds the regression in Splunk,",
            "and writes the fix. Driver, writer, and auditor all run on a local 8B model.",
            "It uses the official Splunk MCP Server and the Splunk AI Toolkit at runtime.",
        ],
    },
    {  # 2. WHY
        "kicker": "THE PROBLEM",
        "title": "80% of outages are self-inflicted.",
        "title_size": 72,
        "body": [
            "Gartner: caused by people and process, not technology. Change is the biggest cause.",
            "",
            "The warning is usually there. It just isn't believed, because a change's real impact",
            "only surfaces in server-side telemetry, after something exercises it. So it almost",
            "never gets caught before prod goes down at 2am.",
        ],
        "accent": "kassi makes the warning undeniable.",
    },
    {  # 3. WHAT IT DOES (architecture)
        "kicker": "HOW IT WORKS  ·  ONE AUDITED STATE MACHINE",
        "title": "It closes the loop.",
        "body": [
            "Any tool-calling model drives a Burr state machine over MCP, one phase at a time",
            "(Granite 4.1 is the default: it proves the whole loop fits on a local 8B):",
            "",
            "→   Grafana k6 MCP Server drives real load at the changed endpoint",
            "→   the official Splunk MCP Server reads the server-side truth",
            "→   the Splunk AI Toolkit forecasts the trend and flags the anomaly",
            "→   Granite writes the cited analysis + fix, Guardian audits it, sealed to the ledger",
        ],
    },
    {  # 4. LIVE DEMO
        "kicker": "LIVE  ·  NOTHING CANNED",
        "title": "Watch it run.",
        "accent": "Real app, real k6, the official Splunk MCP Server, a local model.",
        "body": ["Two changes, two different failure signatures. Same agent, same dashboard."],
    },
    {  # 5. THANK YOU
        "kicker": "SPLUNK AGENTIC OPS HACKATHON  ·  OBSERVABILITY",
        "title": "Thank you!",
        "title_size": 150,
        "accent": "Divines disaster, crafts the cure.",
        "body": ["github.com/msradam/kassi"],
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

    d.text(
        (x, H - 130 * s),
        "kassi  ·  divines disaster, crafts the cure",
        font=font("mono", 24 * s),
        fill=(110, 116, 130),
    )
    d.text(
        (W - 230 * s, H - 130 * s),
        f"{index:02d} / {total:02d}",
        font=font("mono", 24 * s),
        fill=(110, 116, 130),
    )

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
