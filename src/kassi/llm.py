"""LLM wrappers behind the narrow ``LLM`` Protocol.

Interchangeable backends:

* :class:`OllamaLLM` (default) calls the local Ollama HTTP API at ``OLLAMA_HOST``, default
  model IBM ``granite4.1:8b``. ``documents`` are passed as Granite's native grounding role so
  the model answers strictly from the supplied facts (used for the cited run analysis).
* :class:`AnthropicLLM` calls the Claude Messages API over HTTP (``ANTHROPIC_API_KEY``);
  ``documents`` are inlined into the prompt since the API has no native grounding role.
* :class:`ClaudeAgentLLM` drives Claude (default Sonnet) through the Claude Agent SDK, which
  uses the logged-in Claude Code session rather than an API key, so it runs on the Claude
  subscription instead of metered API credits.

:func:`make_llm` picks the backend from ``KASSI_LLM`` (``ollama`` | ``anthropic`` | ``claude_agent``).
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Protocol

import httpx

DEFAULT_MODEL = "granite4.1:8b"
# Context window for every Granite call. 32K comfortably holds the largest prompt kassi builds
# (the scaffold script plus grounding documents) with room to spare, so nothing is truncated, while
# staying small enough that the KV cache does not thrash a 16GB host. Keep this consistent across
# the driver and the per-phase worker: a mismatched num_ctx makes Ollama reload the model between
# calls, which on the M4 timed the worker out. Raise with KASSI_NUM_CTX where memory allows.
DEFAULT_NUM_CTX = int(os.environ.get("KASSI_NUM_CTX", "32768"))
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_AGENT_MODEL = "sonnet"


class LLMError(Exception):
    """Non-recoverable LLM failure."""


class LLM(Protocol):
    def generate(
        self,
        *,
        system: str,
        user: str,
        stop: list[str] | None = None,
        format: str | None = None,
        documents: list[tuple[str, str]] | None = None,
    ) -> str: ...


class OllamaLLM:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str | None = None,
        temperature: float = 0.1,
        num_ctx: int = DEFAULT_NUM_CTX,
        timeout: float = 300.0,
    ) -> None:
        self.model = model
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.timeout = timeout

    def generate(
        self,
        *,
        system: str,
        user: str,
        stop: list[str] | None = None,
        format: str | None = None,
        documents: list[tuple[str, str]] | None = None,
    ) -> str:
        messages: list[dict] = [{"role": "system", "content": system}]
        # Granite grounds on messages whose role starts with "document"; the part after
        # "document_" becomes the document title (used for source citations). The model is
        # instructed by its own template to answer strictly from these documents.
        for title, text in documents or []:
            messages.append({"role": f"document_{title}", "content": text})
        messages.append({"role": "user", "content": user})
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
            },
        }
        if stop:
            payload["options"]["stop"] = stop
        if format:
            payload["format"] = format

        try:
            resp = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc

        data = resp.json()
        content = data.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMError(f"Ollama returned empty content: {data!r}")
        return content


class AnthropicLLM:
    """Claude Messages API over HTTP. ``format='json'`` forces a JSON object via an
    assistant prefill, which Haiku-class models support."""

    def __init__(
        self,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        api_key: str | None = None,
        max_tokens: int = 1024,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.max_tokens = max_tokens
        self.timeout = timeout

    def generate(
        self,
        *,
        system: str,
        user: str,
        stop: list[str] | None = None,
        format: str | None = None,
        documents: list[tuple[str, str]] | None = None,
    ) -> str:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")

        if documents:
            docs = "\n".join(f"[{title}] {text}" for title, text in documents)
            user = (
                f"Documents (answer strictly from these, cite the [source] of each fact):\n{docs}\n\n{user}"
            )
        messages: list[dict] = [{"role": "user", "content": user}]
        prefix = ""
        if format == "json":
            # Prefill the assistant turn with the opening brace so the model emits a
            # bare JSON object (no markdown fence, no preamble). Re-prepended below.
            messages.append({"role": "assistant", "content": "{"})
            prefix = "{"

        payload: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if stop:
            payload["stop_sequences"] = stop

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        try:
            resp = httpx.post(ANTHROPIC_API_URL, json=payload, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc

        data = resp.json()
        if data.get("stop_reason") == "refusal":
            raise LLMError("Anthropic declined the request (stop_reason=refusal)")
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if not (prefix + text).strip():
            raise LLMError(f"Anthropic returned empty content: {data!r}")
        return prefix + text


def _run_blocking(coro: Any) -> Any:
    """Run an async coroutine to completion from sync code, even when a loop is already
    running on the calling thread. The Agent SDK is async; kassi's ``generate`` is sync and
    is invoked from both sync and asyncio (Burr ``arun``) contexts, so the coroutine runs on
    its own thread with a fresh event loop."""
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


class ClaudeAgentLLM:
    """Claude via the Claude Agent SDK, authenticated by the logged-in Claude Code session
    (no ``ANTHROPIC_API_KEY``; runs on the Claude subscription). Each ``generate`` is a
    single-turn, tool-free completion. ``format='json'`` is requested in the system prompt
    since the SDK has no assistant prefill; the response is fence-stripped defensively."""

    def __init__(self, model: str = DEFAULT_AGENT_MODEL, timeout: float = 300.0) -> None:
        self.model = model
        self.timeout = timeout

    def generate(
        self,
        *,
        system: str,
        user: str,
        stop: list[str] | None = None,
        format: str | None = None,
        documents: list[tuple[str, str]] | None = None,
    ) -> str:
        if documents:
            docs = "\n".join(f"[{title}] {text}" for title, text in documents)
            user = (
                f"Documents (answer strictly from these, cite the [source] of each fact):\n{docs}\n\n{user}"
            )
        sys_prompt = system
        if format == "json":
            sys_prompt = (
                f"{system}\n\nRespond with a single valid JSON object and nothing else: no "
                "markdown code fences, no prose before or after the object."
            )

        text = _run_blocking(self._agenerate(sys_prompt, user))

        for marker in stop or []:
            idx = text.find(marker)
            if idx != -1:
                text = text[:idx]
        if format == "json":
            text = _strip_json_fence(text)
        text = text.strip()
        if not text:
            raise LLMError("Claude Agent SDK returned empty content")
        return text

    async def _agenerate(self, system: str, user: str) -> str:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        options = ClaudeAgentOptions(
            system_prompt=system,
            allowed_tools=[],
            max_turns=1,
            model=self.model,
            permission_mode="bypassPermissions",
            setting_sources=[],
        )
        out: list[str] = []
        try:
            async for message in query(prompt=user, options=options):
                if isinstance(message, AssistantMessage):
                    out.extend(b.text for b in message.content if isinstance(b, TextBlock))
                elif isinstance(message, ResultMessage) and getattr(message, "is_error", False):
                    raise LLMError(f"Claude Agent SDK error: {getattr(message, 'result', None)}")
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface SDK/transport failures uniformly
            raise LLMError(f"Claude Agent SDK request failed: {exc}") from exc
        return "".join(out)


def make_llm() -> LLM:
    """Build the configured LLM backend from ``KASSI_LLM``: ``claude_agent`` selects Claude via
    the Claude Code session (Agent SDK), ``anthropic`` the Claude Messages API (``ANTHROPIC_API_KEY``),
    anything else (default) Ollama. ``KASSI_MODEL`` overrides the model tag."""
    backend = os.environ.get("KASSI_LLM", "ollama").strip().lower()
    if backend in ("claude_agent", "claude-agent", "agent", "claude_code", "claude-code"):
        return ClaudeAgentLLM(model=os.environ.get("KASSI_MODEL", DEFAULT_AGENT_MODEL))
    if backend == "anthropic":
        return AnthropicLLM(model=os.environ.get("KASSI_MODEL", DEFAULT_ANTHROPIC_MODEL))
    return OllamaLLM(model=os.environ.get("KASSI_MODEL", DEFAULT_MODEL))
