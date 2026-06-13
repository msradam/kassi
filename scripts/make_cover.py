"""Generate the cover image and tidy the gallery screenshots.

    uv run --with pillow python scripts/make_cover.py            # default palette
    uv run --with pillow python scripts/make_cover.py amethyst   # pick a palette

Tight-crops docs/assets/shot-*.png to their content, then composes
docs/assets/cover.png (1280x720), a tarot-framed hero with the recolored icon.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"

WHITE = (236, 238, 243)
GRAY = (151, 158, 171)

# Each palette: accent (icon + tagline), dim (kicker), frame (border), bg gradient.
PALETTES = {
    "gold": {
        "accent": (230, 193, 112),
        "dim": (158, 134, 86),
        "frame": (90, 78, 52),
        "bg_top": (18, 20, 28),
        "bg_bot": (9, 10, 15),
    },
    "amethyst": {
        "accent": (179, 140, 255),
        "dim": (138, 110, 190),
        "frame": (92, 74, 138),
        "bg_top": (22, 16, 33),
        "bg_bot": (10, 8, 18),
    },
    "teal": {
        "accent": (94, 234, 212),
        "dim": (96, 168, 158),
        "frame": (54, 116, 110),
        "bg_top": (10, 23, 25),
        "bg_bot": (6, 13, 14),
    },
    "splunk": {
        "accent": (244, 86, 161),
        "dim": (176, 92, 134),
        "frame": (128, 64, 100),
        "bg_top": (23, 13, 21),
        "bg_bot": (11, 7, 12),
    },
}

DEFAULT_PALETTE = "splunk"

FONT_CANDIDATES = {
    "serif": [
        "/System/Library/Fonts/Supplemental/Didot.ttc",
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
    ],
    "serif_italic": [
        "/System/Library/Fonts/Supplemental/Georgia Italic.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
    ],
    "sans": ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Geneva.ttf"],
    "mono": ["/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc"],
}


def font(role: str, size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES[role]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def autocrop(path: Path, margin: int = 30) -> None:
    im = Image.open(path).convert("RGB")
    bg = Image.new("RGB", im.size, im.getpixel((1, 1)))
    bbox = ImageChops.difference(im, bg).getbbox()
    if not bbox:
        return
    left, top, right, bottom = bbox
    box = (
        max(0, left - margin),
        max(0, top - margin),
        min(im.width, right + margin),
        min(im.height, bottom + margin),
    )
    im.crop(box).save(path)


def recolor_icon(accent: tuple) -> Image.Image:
    icon = Image.open(ASSETS / "kassi-tarot.png").convert("RGBA")
    w, h = icon.size
    icon = icon.crop((0, 0, w, int(h * 0.85)))  # drop the baked-in credit line
    icon = icon.crop(icon.split()[3].getbbox())
    out = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    out.paste(Image.new("RGBA", icon.size, (*accent, 255)), (0, 0), icon.split()[3])
    return out


def vgradient(size: tuple[int, int], top: tuple, bot: tuple) -> Image.Image:
    w, h = size
    base = Image.new("RGB", size)
    px = base.load()
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = c
    return base


def make_cover(
    palette: str = DEFAULT_PALETTE, out: Path | None = None, size: tuple[int, int] = (1280, 720)
) -> None:
    p = PALETTES[palette]
    accent, dim, frame = p["accent"], p["dim"], p["frame"]
    s = 2  # supersample, then downscale for crisp text
    W, H = size[0] * s, size[1] * s
    # The composition is tuned for a 720-tall canvas; on a taller (e.g. 3:2) canvas, center the
    # text block vertically and keep the credit pinned to the bottom edge.
    dy = (H - 720 * s) // 2
    img = vgradient((W, H), p["bg_top"], p["bg_bot"])
    d = ImageDraw.Draw(img)

    # tarot-card double border framing the whole cover
    for inset, width, col in ((26 * s, 2 * s, dim), (34 * s, 1 * s, frame)):
        d.rectangle((inset, inset, W - inset, H - inset), outline=col, width=width)

    # right-hand hero: the recolored tarot icon (vertically centered on the canvas)
    icon = recolor_icon(accent)
    ih = 338 * s
    iw = round(icon.width * ih / icon.height)
    icon = icon.resize((iw, ih), Image.LANCZOS)
    icon_cx = 1042 * s
    img.paste(icon, (icon_cx - iw // 2, (H - ih) // 2 - 4 * s), icon)

    x = 78 * s
    d.text((x, dy + 96 * s), "SPLUNK AGENTIC OPS HACKATHON · OBSERVABILITY", font=font("mono", 19 * s), fill=dim)
    d.text((x, dy + 150 * s), "kassi", font=font("serif", 168 * s), fill=WHITE)
    d.text((x, dy + 360 * s), "Divines disaster, crafts the cure.", font=font("serif_italic", 43 * s), fill=accent)

    sub = font("sans", 27 * s)
    lines = [
        "An AI agent load-tests a code change, finds the regression in",
        "Splunk, then writes the fix. Audited end to end, on a local model.",
    ]
    for i, line in enumerate(lines):
        d.text((x + 2 * s, dy + (432 + i * 38) * s), line, font=sub, fill=GRAY)

    d.text(
        (x + 2 * s, dy + 548 * s),
        "k6   ·   Splunk   ·   Granite   ·   Burr   ·   Theodosia   ·   MCP",
        font=font("mono", 22 * s),
        fill=(120, 128, 142),
    )
    d.text(
        (x, H - 56 * s),
        "Tarot icon: Eucalyp / Noun Project (CC BY 3.0)",
        font=font("sans", 15 * s),
        fill=(110, 116, 130),
    )

    img.resize(size, Image.LANCZOS).save(out or ASSETS / "cover.png")


def main() -> None:
    palette = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PALETTE
    for shot in sorted(ASSETS.glob("shot-*.png")):
        autocrop(shot)
    make_cover(palette)
    make_cover(palette, out=ASSETS / "cover-3x2.png", size=(1280, 853))  # Devpost 3:2
    print(f"wrote cover.png + cover-3x2.png ({palette})")


if __name__ == "__main__":
    main()
