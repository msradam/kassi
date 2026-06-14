#!/usr/bin/env bash
# Replay a captured `kassi pilot` run at a readable, demo-paced cadence (real output, demo timing).
# Usage: bash scripts/replay_pilot.sh [captured.ansi]   (default /tmp/pilot_clean.ansi)
F="${1:-/tmp/pilot_clean.ansi}"
while IFS= read -r line; do
  printf '%s\n' "$line"
  if [[ "$line" == *"→"* ]]; then sleep 1.15          # phase lines: hold so each reads
  elif [[ "$line" == *"verdict"* ]]; then sleep 0.9
  else sleep 0.45; fi                                  # header / narration / footer
done < "$F"
