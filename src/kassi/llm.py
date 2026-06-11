"""Ollama-backed LLM wrapper.

Calls the local Ollama HTTP API at ``OLLAMA_HOST`` (default
``http://localhost:11434``). The narrow ``LLM`` Protocol lets a llama.cpp / hosted
backend drop in later. The model only ever fills a closed-enum plan; it never
authors k6 source.
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx

DEFAULT_MODEL = "qwen2.5-coder:7b"


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
    ) -> str: ...


class OllamaLLM:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str | None = None,
        temperature: float = 0.1,
        num_ctx: int = 8192,
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
    ) -> str:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
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
