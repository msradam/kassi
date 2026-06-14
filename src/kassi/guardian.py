"""Independent groundedness screen via IBM Granite Guardian 4.1.

After the writer model (Granite 4.1) composes the run analysis, a separate Guardian model, also
Granite 4.1 (`granite-guardian-4.1-8b`, fine-tuned from the same base, Apache-2.0), judges whether
that analysis is faithful to the evidence it was grounded on. The evidence is passed as a document,
the analysis as the assistant message being judged, and a final user message carries the guardian
block: a judge instruction, the groundedness criterion, and a scoring schema. The model returns
``<score>yes</score>`` when the analysis is ungrounded (makes claims unsupported by or contradicting
the document) and ``<score>no</score>`` when it is grounded. So a published analysis carries an
independent check from a second model, not just the writer's own word. The verdict is recorded to
the report ledger.

Prompt strings are from IBM's granite-guardian cookbook for 4.1.
"""

from __future__ import annotations

import os
import re

import httpx

DEFAULT_GUARDIAN_MODEL = "hf.co/ibm-granite/granite-guardian-4.1-8b-GGUF:Q4_K_M"
GROUNDEDNESS = "groundedness"

_CRITERIA = (
    "A text is considered ungrounded or unfaithful if it includes information lacking support "
    "from, or directly contradicting, the provided document(s)."
)
_SCHEMA = "If the last assistant's text meets the criteria, return 'yes'; otherwise, return 'no'."
_JUDGE_NOTHINK = (
    "As a judge agent, assess whether the provided text meets the given judging criteria using "
    "all available information, including conversations, documents, and tools. Provide your score "
    "immediately without explanation."
)
_GUARDIAN_BLOCK = f"{_JUDGE_NOTHINK}\n\n### Criteria: {_CRITERIA}\n\n### Scoring Schema: {_SCHEMA}"
_SCORE = re.compile(r"<score>\s*(.*?)\s*</score>", re.DOTALL)


def guardian_configured() -> bool:
    """On by default; set KASSI_GUARDIAN=0 to skip the screen (it then degrades to unavailable)."""
    return os.environ.get("KASSI_GUARDIAN", "1").strip().lower() not in {"0", "false", "no", ""}


def _parse_score(text: str) -> str | None:
    """The verdict token, from a ``<score>`` tag if present, else a bare yes/no reply."""
    tags = _SCORE.findall(text or "")
    token = (tags[-1] if tags else (text or "")).strip().lower()
    if token.startswith("yes"):
        return "yes"
    if token.startswith("no"):
        return "no"
    return None


_AGENT_BACKENDS = {"claude_agent", "claude-agent", "agent", "claude_code", "claude-code"}


class Guardian:
    def __init__(
        self,
        model: str = DEFAULT_GUARDIAN_MODEL,
        host: str | None = None,
        timeout: float = 120.0,
        use_agent: bool = False,
    ) -> None:
        self.model = model
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.timeout = timeout
        self.use_agent = use_agent

    def groundedness(self, *, context: str, response: str) -> dict:
        """Judge whether `response` is grounded in `context`. Returns a verdict dict; on any
        transport error it is marked unavailable rather than raising, so the screen phase degrades
        gracefully like the other model-backed phases."""
        if self.use_agent:
            return self._groundedness_agent(context=context, response=response)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "document", "content": context},
                {"role": "assistant", "content": response},
                {"role": "user", "content": _GUARDIAN_BLOCK},
            ],
            "stream": False,
            "options": {"temperature": 0},
        }
        try:
            resp = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            raw = (resp.json().get("message", {}).get("content") or "").strip()
        except httpx.HTTPError as exc:
            return {
                "available": False,
                "grounded": None,
                "label": None,
                "model": self.model,
                "error": str(exc),
            }
        # Guardian scores "yes" when the text is ungrounded/unfaithful, "no" when it is grounded.
        score = _parse_score(raw)
        return {
            "available": True,
            "grounded": None if score is None else score == "no",
            "label": score or raw,
            "risk": GROUNDEDNESS,
            "model": self.model,
        }

    def _groundedness_agent(self, *, context: str, response: str) -> dict:
        """Same groundedness judgement driven through the Claude Agent SDK (Claude Code session)
        instead of Granite Guardian, for runs where the writer is also Claude. Returns the same
        verdict shape; degrades to unavailable on any error."""
        from kassi.llm import ClaudeAgentLLM, LLMError

        system = (
            f"{_JUDGE_NOTHINK}\n\n### Criteria: {_CRITERIA}\n\n### Scoring Schema: {_SCHEMA}\n\n"
            "Reply with exactly one token wrapped in a score tag: <score>yes</score> or "
            "<score>no</score>. 'yes' means the text is ungrounded; 'no' means it is grounded."
        )
        user = f"### Document(s)\n{context}\n\n### Text to judge\n{response}"
        try:
            raw = ClaudeAgentLLM(model=self.model).generate(system=system, user=user)
        except LLMError as exc:
            return {
                "available": False,
                "grounded": None,
                "label": None,
                "model": self.model,
                "error": str(exc),
            }
        score = _parse_score(raw)
        return {
            "available": True,
            "grounded": None if score is None else score == "no",
            "label": score or raw,
            "risk": GROUNDEDNESS,
            "model": self.model,
        }


def make_guardian() -> Guardian:
    backend = os.environ.get("KASSI_LLM", "ollama").strip().lower()
    if backend in _AGENT_BACKENDS:
        model = os.environ.get("KASSI_GUARDIAN_MODEL", "")
        if not model or model == DEFAULT_GUARDIAN_MODEL:
            model = os.environ.get("KASSI_MODEL", "sonnet")
        return Guardian(model=model, use_agent=True)
    return Guardian(model=os.environ.get("KASSI_GUARDIAN_MODEL", DEFAULT_GUARDIAN_MODEL))
