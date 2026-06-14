"""Build a stable demo git repo for a scenario: commit 1 is the healthy baseline, commit 2 adds
the flawed new endpoint, so `git diff HEAD~1` is exactly the change a developer's PR introduces.

    uv run python scripts/make_demo_repo.py petclinic [/path/to/repo]
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "petclinic"
    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(f"/tmp/kassi-demo/{name}")
    app_dir = ROOT / "examples" / name
    src = (app_dir / "app.py").read_text()

    # the flawed new endpoint is the last route decorator before def main()
    head = src.split("def main(", 1)[0]
    markers = list(re.finditer(r'@app\.(?:post|put|get)\("[^"]+"\)', head))
    marker = markers[-1].group(0)
    before, rest = src.split(marker, 1)
    _, main_block = rest.split("def main(", 1)
    baseline = before.rstrip() + "\n\n\ndef main(" + main_block

    dest.mkdir(parents=True, exist_ok=True)
    for f in dest.glob("*"):
        f.unlink()
    (dest / "openapi.json").write_text((app_dir / "openapi.json").read_text())

    def git(*a: str) -> None:
        subprocess.run(["git", "-C", str(dest), *a], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "dev@example.com")
    git("config", "user.name", "A. Developer")
    (dest / "app.py").write_text(baseline)
    git("add", "-A")
    git("commit", "-q", "-m", f"{name}: service baseline")
    (dest / "app.py").write_text(src)
    git("add", "-A")
    git("commit", "-q", "-m", f"feat: add {marker.split('(')[1].strip(chr(34) + ')')} endpoint")
    print(f"{name}: {dest}  (diff adds {marker})")


if __name__ == "__main__":
    main()
