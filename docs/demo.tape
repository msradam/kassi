# kassi demo for the Splunk Agentic Ops Hackathon.
# Record with:  KASSI_LLM=anthropic envchain ai vhs docs/demo.tape
# (envchain supplies ANTHROPIC_API_KEY; the recorded shell falls back to a
#  default plan if no LLM backend is reachable.) Needs a local Splunk per
#  docs/SPLUNK_SETUP.md plus the .env endpoint + token.

Output docs/demo.gif

Require uv

Set Shell "bash"
Set FontSize 15
Set Width 1280
Set Height 760
Set Padding 18
Set TypingSpeed 28ms

Hide
Type "cd /Users/amsrahman/kassi" Enter
Type "export KASSI_LLM=anthropic KASSI_MODEL=claude-haiku-4-5" Enter
Type "clear" Enter
Show

Sleep 500ms
Type "# kassi: divinate your stack's performance -- a load test, explained by Splunk telemetry"
Enter
Sleep 900ms
Type "# the workflow is a Burr state machine served over MCP -- only graph edges are legal moves"
Enter
Sleep 700ms
Type "uv run kassi render"
Enter
Sleep 4000ms

Type "clear"
Enter
Sleep 300ms
Type "# each phase is a card the agent turns -- the Major Arcana of your stack"
Enter
Sleep 700ms
Type "uv run kassi arcana"
Enter
Sleep 4500ms

Type "clear"
Enter
Sleep 400ms
Type "# now draw the whole spread against live Splunk; the plan is filled by Claude Haiku"
Enter
Sleep 800ms
Type "./scripts/divine.sh"
Enter
Sleep 22s

Type "# one agent, 18 tool calls across two MCP servers -- client 5xx explained by server-side telemetry, narrated as a tarot reading"
Enter
Sleep 3s
