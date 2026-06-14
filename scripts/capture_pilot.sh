#!/usr/bin/env bash
# Capture a diff-mode `kassi pilot` run for a demo scenario: build the before/after demo repo,
# start the example app, let Granite drive the FSM against the diff, and save the streamed cards.
#   bash scripts/capture_pilot.sh petclinic 8400   ->  /tmp/pilot_petclinic.ansi
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="$1"; PORT="$2"
uv run python scripts/make_demo_repo.py "$NAME" >/dev/null
SPLUNK_INDEX=web KASSI_SPLUNK_INSECURE=1 uv run --with fastapi --with uvicorn --with httpx \
  python "examples/$NAME/app.py" serve > "/tmp/${NAME}_app.log" 2>&1 &
APP=$!
trap 'kill $APP 2>/dev/null' EXIT
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 0.5; done
echo "app up :$PORT"
KASSI_SPLUNK_INSECURE=1 uv run kassi pilot --repo-path "/tmp/kassi-demo/$NAME" --ref HEAD~1 \
  --target-base-url "http://127.0.0.1:$PORT" --splunk-index web | grep "$(printf '\xf0\x9f\x82\xa0')" > "/tmp/pilot_$NAME.ansi"
echo "wrote /tmp/pilot_$NAME.ansi ($(wc -l < /tmp/pilot_$NAME.ansi) lines)"
