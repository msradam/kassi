# kassi demo for the Splunk Agentic Ops Hackathon.
# Record with:  vhs docs/demo.tape
# Uses the local Granite 4.1 model on the Mini (per .env: KASSI_LLM=ollama,
# KASSI_MODEL=granite4.1:8b, OLLAMA_HOST). Needs a local Splunk per
# docs/SPLUNK_SETUP.md plus the .env endpoint + token. Granite on the M4 is slower
# than a hosted model, so the run beat sleeps ~135s; bump it if the run is clipped.

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
Type "# point kassi at a code change (a git diff): it picks the new endpoint, load-tests it live, and explains the result from Splunk"
Enter
Sleep 800ms
Type "./scripts/divine_petclinic.sh"
Enter
Sleep 135s

Type "# one agent, 18 tool calls across two MCP servers -- the new endpoint's 5xx explained by Splunk as 'database is locked', narrated as a tarot reading"
Enter
Sleep 3s
