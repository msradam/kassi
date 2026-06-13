"""The Major Arcana of kassi: a card for each workflow phase.

Pure flavor. This is the project's theming layer and is deliberately kept out of
the audited state, the report JSON, and the MCP tool descriptions. The agent draws
a card at each step; ``kassi arcana`` prints the full spread.
"""

from __future__ import annotations

# Keyed by action name, in the order the agent draws them.
ARCANA: dict[str, tuple[str, str, str]] = {
    "select_mode": ("0", "The Fool", "The querent sets out: diff or intent, the journey begins."),
    "read_diff": ("II", "The High Priestess", "Hidden knowledge: the change is read from the diff."),
    "extract_endpoints": ("IV", "The Emperor", "Order from change: the affected routes are named."),
    "parse_intent": ("III", "The Empress", "Intuition reads the intent and brings forth the endpoints."),
    "doc_lookup": ("V", "The Hierophant", "Doctrine consulted: the k6 docs ground the rite."),
    "scaffold": (
        "VII",
        "The Chariot",
        "The vehicle is assembled from the spec: a runnable scaffold takes shape.",
    ),
    "generate_script": (
        "I",
        "The Magician",
        "As above, so below: the agent authors the script atop the scaffold.",
    ),
    "validate_script": ("XI", "Justice", "The script is weighed; the unworthy is turned back."),
    "fix_script": ("XIV", "Temperance", "The flawed draft is tempered against k6's judgment until it holds."),
    "run_test": ("XVI", "The Tower", "Load strikes the structure; what breaks is revealed."),
    "splunk_preflight": ("IX", "The Hermit", "A lantern into the index before the reading."),
    "correlate": ("VI", "The Lovers", "Client and server are joined over one window."),
    "detect_anomalies": (
        "XVII",
        "The Star",
        "Splunk's own forecast is cast; where the load breaches the band is revealed.",
    ),
    "analyze": (
        "XIX",
        "The Sun",
        "The reading is made plain: cause, evidence, and the cure laid bare.",
    ),
    "screen": (
        "XII",
        "The Hanged Man",
        "Seen again through another's eyes: the reading is judged grounded, or not.",
    ),
    "report": ("XX", "Judgement", "The verdict is spoken and sealed to the ledger."),
}

# One sigil drawn on every line: a face-down tarot card.
SIGIL = "🂠"


def adorn(text: str) -> str:
    """Prefix each non-empty line of a reading with the card sigil."""
    return "\n".join(f"{SIGIL}  {line}" if line.strip() else line for line in text.splitlines())


LEDGER = ("XXI", "The World", "The cycle closes: an immutable, hash-chained record.")
REFUSAL = ("XV", "The Devil", "You are bound: only the legal moves are permitted.")

TAGLINE = "Divinate your stack's performance."


def reading(verdict: str) -> str:
    """A closing line for the report, drawn from the verdict."""
    v = (verdict or "").lower()
    if v.startswith("passed"):
        return "Judgement (XX), upright. The cards favor your stack."
    if v.startswith("ran with failures"):
        return "The Tower (XVI), reversed. It cracks under load; read the omens."
    if v.startswith("no run") or v.startswith("failed"):
        return "The Devil (XV). The rite was refused; consult the errors above."
    return "The Moon (XVIII). The reading is uncertain."


def spread() -> str:
    """Render the full deck as an aligned terminal spread."""
    rows = [(num, name, action, omen) for action, (num, name, omen) in ARCANA.items()]
    num_w = max(len(r[0]) for r in rows)
    name_w = max(len(r[1]) for r in rows)
    act_w = max(len(r[2]) for r in rows)

    lines = [f"{SIGIL}  kassi: {TAGLINE}", ""]
    for num, name, action, omen in rows:
        lines.append(f"{SIGIL}  {num:>{num_w}}  {name:<{name_w}}  {action:<{act_w}}  {omen}")
    lines.append("")
    for num, name, omen in (LEDGER, REFUSAL):
        label = "ledger" if name == "The World" else "refusal"
        lines.append(f"{SIGIL}  {num:>{num_w}}  {name:<{name_w}}  {label:<{act_w}}  {omen}")
    return "\n".join(lines)
