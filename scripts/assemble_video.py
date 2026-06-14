"""Assemble the kassi demo into one sub-3-minute 1080p/60 video -> docs/clips/kassi-demo-full.mp4.

    uv run python scripts/assemble_video.py

Order: title -> problem -> architecture -> live-demo card -> scenario 1 (terminal + dashboard)
-> scenario 2 (terminal + dashboard) -> thank-you. Slides are held briefly (they are intro/
transition cards; the demo clips carry the narration). No audio: add the voiceover in your editor.
Every segment is normalized to 1920x1080/60/yuv420p so the pieces concat cleanly.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECK = ROOT / "docs" / "deck"
CLIPS = ROOT / "docs" / "clips"
OUT = CLIPS / "kassi-demo-full.mp4"

# (source, seconds): for a .png it is the hold; for a clip it is a hard cap (None = full length).
# Per scenario: terminal (Granite drives) -> audit (the JSONL ledger + verify) -> dashboard.
# Terminals and audits play full; dashboards are capped (the walk is already shown in the audit).
SEGMENTS = [
    (DECK / "slide-01.png", 3),
    (DECK / "slide-02.png", 4),
    (DECK / "slide-03.png", 4),
    (CLIPS / "clip-petclinic.mp4", None),
    (CLIPS / "clip-audit-petclinic.mp4", None),
    (CLIPS / "clip-dashboard-petclinic.mp4", 16),
    (CLIPS / "clip-feed.mp4", None),
    (CLIPS / "clip-audit-feed.mp4", None),
    (CLIPS / "clip-dashboard-feed.mp4", 16),
    (DECK / "slide-05.png", 4),
]

_VF = "scale=1920:1080,setsar=1,format=yuv420p"
_ENC = ["-r", "60", "-vf", _VF, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]


def _normalize(src: Path, secs: int | None, dst: Path) -> None:
    if src.suffix == ".png":  # still image held for `secs`
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", str(secs), "-i", str(src), *_ENC, "-an", str(dst)]
    else:  # clip, optionally capped at `secs`
        cap = ["-t", str(secs)] if secs is not None else []
        cmd = ["ffmpeg", "-y", "-i", str(src), *cap, *_ENC, "-an", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        segs = []
        for i, (src, hold) in enumerate(SEGMENTS):
            seg = tmpd / f"seg_{i:02d}.mp4"
            _normalize(src, hold, seg)
            segs.append(seg)
        listing = tmpd / "list.txt"
        listing.write_text("".join(f"file '{s}'\n" for s in segs))
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(OUT)],
            check=True, capture_output=True,
        )  # fmt: skip
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(OUT)],
        capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    print(f"wrote {OUT}  ({float(dur):.1f}s)")


if __name__ == "__main__":
    main()
