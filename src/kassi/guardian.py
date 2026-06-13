"""Independent groundedness screen via IBM Granite Guardian.

After the writer model (Granite 4.1) composes the run analysis, a separate Guardian model judges
whether that analysis is faithful to the evidence it was grounded on. Guardian is prompted with
the risk name "groundedness", which its own chat template selects: the evidence goes in a context
message, the analysis in an assistant message, and the model returns a single token, "Yes" when the
text makes claims unsupported by or contradicting the context, "No" when it is grounded. So a
published analysis carries an independent check from a second model, not just the writer's own word.

Granite Guardian on Ollama is the 3.x line (`granite3-guardian`); it is Apache-2.0 and from the
same open Granite family as the 4.1 writer model. The verdict is recorded to the report ledger.
"""

from __future__ import annotations

import os

import httpx

DEFAULT_GUARDIAN_MODEL = "granite3-guardian:8b"
GROUNDEDNESS = "groundedness"


def guardian_configured() -> bool:
    """On by default; set KASSI_GUARDIAN=0 to skip the screen (it then degrades to unavailable)."""
    return os.environ.get("KASSI_GUARDIAN", "1").strip().lower() not in {"0", "false", "no", ""}


class Guardian:
    def __init__(
        self,
        model: str = DEFAULT_GUARDIAN_MODEL,
        host: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.timeout = timeout

    def groundedness(self, *, context: str, response: str) -> dict:
        """Judge whether `response` is grounded in `context`. Returns a verdict dict; on any
        transport error it is marked unavailable rather than raising, so the screen phase degrades
        gracefully like the other model-backed phases."""
        payload = {
            "model": self.model,
            # The Guardian chat template selects the risk from the system string; "groundedness"
            # routes the context+assistant messages into the faithfulness criterion.
            "system": GROUNDEDNESS,
            "messages": [
                {"role": "context", "content": context},
                {"role": "assistant", "content": response},
            ],
            "stream": False,
            "options": {"temperature": 0},
        }
        try:
            resp = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            label = (resp.json().get("message", {}).get("content") or "").strip()
        except httpx.HTTPError as exc:
            return {
                "available": False,
                "grounded": None,
                "label": None,
                "model": self.model,
                "error": str(exc),
            }
        # Guardian emits "Yes" when the text is ungrounded/unfaithful, "No" when it is grounded.
        return {
            "available": True,
            "grounded": label.lower().startswith("no"),
            "label": label,
            "risk": GROUNDEDNESS,
            "model": self.model,
        }


def make_guardian() -> Guardian:
    return Guardian(model=os.environ.get("KASSI_GUARDIAN_MODEL", DEFAULT_GUARDIAN_MODEL))
