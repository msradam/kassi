#!/usr/bin/env bash
# Print stdin one line at a time with a small delay, so a terminal recording scrolls readably.
#   some-command | bash scripts/slowcat.sh [delay-seconds]
d="${1:-0.06}"
while IFS= read -r l; do printf '%s\n' "$l"; sleep "$d"; done
