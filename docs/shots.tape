# Capture crisp gallery screenshots. Record with:
#   vhs docs/shots.tape
# Uses the local Granite 4.1 model (per .env). Produces docs/assets/shot-*.png.
# The throwaway gif is removed afterwards.

Output docs/assets/_shots.gif

Require uv

Set Shell "bash"
Set FontSize 18
Set Width 1500
Set Height 1120
Set Padding 26
Set TypingSpeed 8ms

Hide
Type "cd \"$(git rev-parse --show-toplevel)\"" Enter
Type "clear" Enter
Show

Type "uv run kassi render" Enter
Sleep 3500ms
Screenshot docs/assets/shot-render.png
Sleep 1200ms

Hide
Type "clear" Enter
Sleep 400ms
Show
Type "uv run kassi arcana" Enter
Sleep 3s
Screenshot docs/assets/shot-arcana.png
Sleep 1200ms

Hide
Type "clear" Enter
Sleep 400ms
Show
Type "uv run kassi doctor --runtime" Enter
Sleep 4s
Screenshot docs/assets/shot-doctor.png
Sleep 1200ms

Hide
Type "clear" Enter
Sleep 400ms
Show
Type "./scripts/divine_petclinic.sh" Enter
Sleep 150s
Screenshot docs/assets/shot-correlate.png
Sleep 1500ms
