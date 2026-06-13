#!/usr/bin/env bash
# Drive the full kassi reading against live Splunk, filtered to the spread.
# Set KASSI_LLM=anthropic (and supply ANTHROPIC_API_KEY) to fill the plan with Claude.
cd "$(dirname "$0")/.." || exit 1
exec uv run python scripts/verify_correlate_live.py 2>&1 | grep --line-buffered -E \
  'backend:|upstream:|verdict:|plan |splunk_enabled:|http_reqs:|correlation|server-side|anomaly scan:|doc refs:|preflight:|tool calls:|the reading:|🂠|^    [A-Z]|_ok |_done '
