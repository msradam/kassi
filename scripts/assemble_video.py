"""Assemble the kassi demo into one sub-3-minute 1080p/60 video -> docs/clips/kassi-demo-full.mp4.

    uv run python scripts/assemble_video.py

Order: title -> problem -> architecture -> scenario 1 (terminal + audit + dashboard) -> scenario 2
(terminal + audit + dashboard) -> close. Each segment's length is tuned to the narration beat that
plays over it (see demo_guide.md), so the voiceover stays in sync. No audio: add the voiceover in
your editor. Every segment is normalized to 1920x1080/60/yuv420p so the pieces concat cleanly.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECK = ROOT / "docs" / "deck"
CLIPS = ROOT / "docs" / "clips"
OUT = CLIPS / "kassi-demo-full.mp4"

# (source, mode, seconds), each timed to its narration beat in demo_guide.md:
#   hold = still slide held for `seconds`
#   fit  = clip time-scaled to exactly `seconds`, every frame kept (just faster), so the verdict at
#          the tail is never trimmed away
#   cap  = clip truncated to its first `seconds` (the dashboards' eased scroll only needs the top)
SEGMENTS = [
    (DECK / "slide-01.png", "hold", 12),
    (DECK / "slide-02.png", "hold", 21),
    (DECK / "slide-03.png", "hold", 16),
    (CLIPS / "clip-petclinic.mp4", "fit", 32),
    (CLIPS / "clip-audit-petclinic.mp4", "fit", 10),
    (CLIPS / "clip-dashboard-petclinic.mp4", "cap", 16),
    (CLIPS / "clip-feed.mp4", "fit", 30),
    (CLIPS / "clip-audit-feed.mp4", "fit", 9),
    (CLIPS / "clip-dashboard-feed.mp4", "cap", 11),
    (DECK / "slide-05.png", "hold", 12),
]

_VF = "scale=1920:1080,setsar=1,format=yuv420p"
_ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]


def _duration(src: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    return float(out)


def _normalize(src: Path, mode: str, secs: int, dst: Path) -> None:
    if mode == "hold":  # still image held for `secs`
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", str(secs), "-i", str(src),
               "-r", "60", "-vf", _VF, *_ENC, "-an", str(dst)]  # fmt: skip
    elif mode == "fit":  # time-scale the whole clip to `secs`, keeping every frame
        factor = _duration(src) / secs
        cmd = ["ffmpeg", "-y", "-i", str(src), "-r", "60",
               "-vf", f"setpts=PTS/{factor:.6f},{_VF}", *_ENC, "-an", str(dst)]  # fmt: skip
    else:  # cap: the first `secs` of the clip
        cmd = ["ffmpeg", "-y", "-i", str(src), "-t", str(secs),
               "-r", "60", "-vf", _VF, *_ENC, "-an", str(dst)]  # fmt: skip
    subprocess.run(cmd, check=True, capture_output=True)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        segs = []
        for i, (src, mode, secs) in enumerate(SEGMENTS):
            seg = tmpd / f"seg_{i:02d}.mp4"
            _normalize(src, mode, secs, seg)
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
