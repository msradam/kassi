"""Generate the cover image and tidy the gallery screenshots.

    uv run --with pillow python scripts/make_cover.py

Tight-crops docs/assets/shot-*.png to their content, then composes
docs/assets/cover.png (1280x720), a tarot-framed hero with the gold icon.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"

GOLD = (230, 193, 112)
DIM_GOLD = (158, 134, 86)
WHITE = (236, 238, 243)
GRAY = (151, 158, 171)
BG_TOP = (18, 20, 28)
BG_BOT = (9, 10, 15)

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


def gold_icon() -> Image.Image:
    icon = Image.open(ASSETS / "kassi-tarot.png").convert("RGBA")
    w, h = icon.size
    icon = icon.crop((0, 0, w, int(h * 0.85)))  # drop the baked-in credit line
    bbox = icon.split()[3].getbbox()
    icon = icon.crop(bbox)
    out = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    out.paste(Image.new("RGBA", icon.size, (*GOLD, 255)), (0, 0), icon.split()[3])
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


def make_cover() -> None:
    s = 2  # supersample, then downscale for crisp text
    W, H = 1280 * s, 720 * s
    img = vgradient((W, H), BG_TOP, BG_BOT)
    d = ImageDraw.Draw(img)

    # tarot-card double border framing the whole cover
    for inset, width, col in ((26 * s, 2 * s, DIM_GOLD), (34 * s, 1 * s, (90, 78, 52))):
        d.rectangle((inset, inset, W - inset, H - inset), outline=col, width=width)

    # right-hand hero: the gold tarot icon
    icon = gold_icon()
    ih = 338 * s
    iw = round(icon.width * ih / icon.height)
    icon = icon.resize((iw, ih), Image.LANCZOS)
    icon_cx = 1042 * s
    img.paste(icon, (icon_cx - iw // 2, (H - ih) // 2 - 4 * s), icon)

    x = 78 * s
    d.text(
        (x, 96 * s), "SPLUNK AGENTIC OPS HACKATHON · OBSERVABILITY", font=font("mono", 19 * s), fill=DIM_GOLD
    )
    d.text((x, 150 * s), "kassi", font=font("serif", 168 * s), fill=WHITE)
    d.text((x, 360 * s), "Divinate your stack's performance.", font=font("serif_italic", 43 * s), fill=GOLD)

    sub = font("sans", 27 * s)
    lines = [
        "An agent draws a load test from a code change, then explains",
        "it with Splunk telemetry. One audited workflow, two MCP servers.",
    ]
    for i, line in enumerate(lines):
        d.text((x + 2 * s, (432 + i * 38) * s), line, font=sub, fill=GRAY)

    d.text(
        (x + 2 * s, 548 * s),
        "k6   ·   Splunk   ·   Burr   ·   Theodosia   ·   MCP",
        font=font("mono", 22 * s),
        fill=(120, 128, 142),
    )

    d.text(
        (x, 664 * s),
        "Tarot icon: Eucalyp / Noun Project (CC BY 3.0)",
        font=font("sans", 15 * s),
        fill=(96, 102, 114),
    )

    img.resize((1280, 720), Image.LANCZOS).save(ASSETS / "cover.png")


def main() -> None:
    for shot in sorted(ASSETS.glob("shot-*.png")):
        autocrop(shot)
        print(f"cropped {shot.name}")
    make_cover()
    print("wrote cover.png")


if __name__ == "__main__":
    main()
