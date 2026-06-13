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
SLIDES = [
    {
        "kicker": "SPLUNK AGENTIC OPS HACKATHON  ·  OBSERVABILITY",
        "title": "kassi",
        "title_size": 150,
        "accent": "Divines disaster, crafts the cure.",
        "body": [
            "An AI agent that load-tests a code change, finds the regression in Splunk,",
            "and writes the fix. Driver, writer, and auditor all run on a local 8B model.",
        ],
    },
    {
        "kicker": "THE PROBLEM",
        "big": "~80%",
        "body": [
            "of production outages are self-inflicted: they trace back to a change.",
            "The warning is usually there. It just isn't believed, because a change's",
            "real impact only surfaces in server-side telemetry, after something runs it.",
        ],
    },
    {
        "kicker": "WHAT KASSI DOES",
        "title": "It closes the loop.",
        "body": [
            "1.   exercise the affected endpoints with real k6 load",
            "2.   read the server-side truth from Splunk over the exact window",
            "3.   name the root cause, with cited evidence and an ML forecast",
            "4.   write the fix: a validated remediation diff",
        ],
        "accent": "A change comes in. A fix goes out.",
    },
    {
        "kicker": "HOW IT WORKS",
        "title": "One agent, two MCP servers.",
        "body": [
            "A Burr state machine served over MCP: the graph's edges are the only legal",
            "moves, and every step is sealed to a hash-chained, auditable ledger.",
            "Grafana k6 drives the load. The official Splunk MCP Server reads the truth.",
        ],
    },
    {
        "kicker": "THE 8B STORY",
        "title": "One local model does all of it.",
        "body": [
            "Granite 4.1 drives the state machine, authors the k6 script, writes the cited",
            "analysis, and proposes the fix. Granite Guardian 4.1 audits it for groundedness.",
            "First ISO/IEC 42001-certified open LLM. On-prem, air-gapped, no per-token cost.",
        ],
        "accent": "Driver, writer, auditor: all local.",
    },
    {
        "kicker": "TWO MODELS, TWO ROLES",
        "title": "The writer isn't trusted on its word.",
        "body": [
            "The writer model explains the regression, grounded on the measured evidence.",
            "A separate Guardian model judges whether that analysis contradicts the",
            "telemetry it cites. The verdict is sealed to the ledger before it publishes.",
        ],
    },
    {
        "kicker": "AGENTIC OPS, LITERALLY",
        "title": "The agent is observable too.",
        "body": [
            "kassi reads Splunk to observe your service. Then it publishes its own",
            "state-machine walk back to Splunk: one event per phase, keyed by app_id.",
            "The dashboard shows not just what the change did, but how the agent decided.",
        ],
    },
    {
        "kicker": "A VERIFIED RUN",
        "title": "REGRESSION",
        "title_color": ACCENT,
        "body": [
            "POST /api/visits   ·   58.8% 5xx   ·   p95 283 ms   ·   cause: database is locked",
            "forecast p95 280 ms   ·   1 anomalous bucket   ·   screened: grounded",
            "remediation: enable WAL / shorten the held write transaction",
        ],
    },
    {
        "kicker": "",
        "title": "kassi",
        "title_size": 150,
        "accent": "Divines disaster, crafts the cure.",
        "body": ["Built for the Splunk Agentic Ops Hackathon."],
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
