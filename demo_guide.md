# kassi demo: assembly + narration guide

Everything is pre-rendered. Drop the assets into Adobe Express in the order below, record the
voiceover from the script, and you have the sub-3-minute submission video. All clips are 1080p/60.

## Assets

Slides (`docs/deck/`): `slide-01.png` (title), `slide-02.png` (the problem), `slide-03.png`
(how it works / architecture), `slide-04.png` (live-demo card). `deck.mp4` is the four held
together if you want a base track.

Clips (delivered separately; gitignored, not in the repo):
- `clip-petclinic.mp4` — scenario 1, terminal: the API today, `git diff` of the new endpoint, then
  `kassi pilot` driving the whole machine to the regression verdict.
- `clip-dashboard-petclinic.mp4` — scenario 1, the Splunk dashboard (smooth, paced).
- `clip-feed.mp4` — scenario 2, terminal: a different diff, kassi drives it to a *degrading* verdict.
- `clip-dashboard-feed.mp4` — scenario 2, the dashboard (the different signature).

## Timeline (~2:30, leaves headroom under 3:00)

| # | Asset | Hold | Voiceover |
| --- | --- | --- | --- |
| 1 | slide-01 | 6s | "kassi is an AI agent that load-tests a code change, finds the regression in Splunk, and writes the fix, with the driver, the writer, and the auditor all running on a local 8B model." |
| 2 | slide-02 | 11s | "About 80% of outages trace back to a change. The warning is usually there, it just isn't believed, because a change's real impact only shows up in server-side telemetry after something exercises it. kassi closes that loop before you ship." |
| 3 | slide-03 | 12s | "A local Granite model drives a Burr state machine over MCP. k6 drives real load at the changed endpoint, the official Splunk MCP Server reads the server-side truth, the Splunk AI Toolkit forecasts the trend, and Granite writes the fix while a second model, Guardian, audits it." |
| 4 | slide-04 | 4s | "Two changes. Two different failures. Same agent." |
| 5 | clip-petclinic | ~35s | "A developer opens a PR that adds an endpoint. kassi reads that diff and drives the whole machine: it authors the load test, runs it through k6, and correlates with Splunk. More than half the requests fail, and the root cause, database is locked, only shows up server-side. Guardian confirms the analysis is grounded. Verdict: a regression, with a fix." |
| 6 | clip-dashboard-petclinic | ~18s | "It all lands in Splunk: the reading, the agent's own state-machine walk, and the server-side errors by endpoint. The agent doesn't just read Splunk, it's observable in Splunk." |
| 7 | clip-feed | ~35s | "A different change, a different failure. This one adds latency with zero errors. The error rate sees nothing, but Splunk's StateSpaceForecast catches the trend and flags the anomaly. kassi calls it: degrading, before it ever breaches." |
| 8 | clip-dashboard-feed | ~18s | "Same dashboard, a completely different signature: degrading, caught by the forecast, with no errors at all." |
| 9 | slide-01 (reprise) | 6s | "Driver, writer, and auditor, all on one local 8B model, the first ISO-42001-certified open LLM. On-prem, air-gapped, no per-token cost." |

Editing notes:
- The terminal clips already replay at a readable pace; trim the head/tail to fit your voiceover.
- Keep the Splunk MCP and k6 activity visible in the terminal clips, judges must see Splunk used
  live (the #1 disqualifier is simulated Splunk output).
- The two dashboards are deliberately different: scenario 1 is **REGRESSION / 5xx / database is
  locked**; scenario 2 is **DEGRADING / zero errors / forecast**.

## Re-recording (only if you want a fresh take)

Setup (one-time): Splunk up locally (`~/splunk/bin/splunk status`), `.env` present, Granite +
Guardian pulled on the Mini, then `uv run kassi warm-k6` and `uv run python scripts/setup_dashboard.py`.

Rebuild the clips:
```bash
# build the stable before/after demo repos (git diff HEAD~1 reads like a PR)
uv run python scripts/make_demo_repo.py petclinic
uv run python scripts/make_demo_repo.py feed

# capture a diff-mode pilot run (starts the app, Granite drives, saves /tmp/pilot_<name>.ansi)
bash /tmp/cap_scenario.sh petclinic 8400      # ~8-10 min on the Mini
bash /tmp/cap_scenario.sh feed 8402

# terminal clips replay the captured run at a readable pace, via vhs
vhs /tmp/clip_petclinic.tape ; vhs /tmp/clip_feed.tape

# dashboards (purge to the one run first for a clean view)
uv run python /tmp/purge.py
uv run --with playwright python scripts/capture_dashboard_video.py
```

If a phase stalls, the k6 extension wasn't warmed (`uv run kassi warm-k6`). If Ollama times out,
confirm `OLLAMA_HOST` points at the Mini and both models are loaded (`curl $OLLAMA_HOST/api/tags`).
