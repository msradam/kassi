#!/usr/bin/env bash
# Drive the headline petclinic diff-mode run against live Splunk, filtered to the spread.
# Set KASSI_LLM=anthropic (and supply ANTHROPIC_API_KEY) to narrate with Claude.
cd "$(dirname "$0")/.." || exit 1
exec uv run python -u scripts/verify_petclinic.py 2>&1 | grep --line-buffered -E \
  'target app:|diff mode:|llm backend:|verdict:|endpoints:|k6 client-side:|what Splunk|totals:|worst endpoint:|root cause:|anomaly scan:|the reading:|🂠|_ok |_done '
