#!/usr/bin/env bash
# Capture the gallery terminal shots with charmbracelet/freeze (https://github.com/charmbracelet/freeze).
# freeze runs each command in a pty, so ANSI color is preserved, and sizes the image to the content
# (no dead whitespace). Writes docs/assets/shot-*.png.
#
#   brew install freeze            # if not present
#   ./scripts/capture_shots.sh
#
# The pilot shot needs a live target + Splunk; capture it separately (see the bottom of this file).
set -euo pipefail
cd "$(dirname "$0")/.."

STYLE=(--window --border.radius 8 --padding 26 --margin 24
       --background "#0b070c" --shadow.blur 22 --shadow.y 12)

freeze --execute "uv run kassi render" -o docs/assets/shot-render.png "${STYLE[@]}"
freeze --execute "uv run kassi arcana" -o docs/assets/shot-arcana.png "${STYLE[@]}"
freeze --execute "uv run kassi doctor --runtime" -o docs/assets/shot-doctor.png "${STYLE[@]}" --wrap 92

echo "wrote docs/assets/shot-{render,arcana,doctor}.png"

# Pilot (Granite driving a real run): start a target, run the pilot capturing its colored stream,
# then render that stream. Example:
#   SPLUNK_INDEX=web uv run --with fastapi --with uvicorn --with httpx \
#     python examples/petclinic/app.py serve &
#   uv run kassi pilot --intent "load test recording a new visit" \
#     --repo-path examples/petclinic --target-base-url http://127.0.0.1:8400 --splunk-index web \
#     | grep '🂠' > /tmp/pilot.ansi
#   freeze --execute "cat /tmp/pilot.ansi" -o docs/assets/shot-pilot.png "${STYLE[@]}"
