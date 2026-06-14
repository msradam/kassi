"""``drive_granite``: let a local Granite model drive the kassi FSM, step by step.

Theodosia mounts the workflow as an MCP server whose only control surface is the ``step`` tool.
The scripted demos (``verify_*.py``) advance that graph with Burr's executor; here the *model*
drives it: at each turn Granite reads the reachable actions and calls ``step`` with the next one,
the action does its per-phase work (authoring the script, correlating, writing the analysis), the
result comes back, and Granite decides the next move, until the FSM reaches ``report``.

So the same local model that drives the walk also does the work inside each phase. The one
hand-off is ``screen``: that phase calls a separate Granite Guardian model to audit the analysis.
Driver, writer, and auditor are all local, which is the point: the whole loop runs on the Mini
with no cloud brain.

This is the Ollama/tool-calling analogue of ``theodosia.drive_claude``; Granite 4.1 emits native
tool calls over Ollama's ``/api/chat`` ``tools=`` surface.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from kassi.llm import DEFAULT_MODEL, DEFAULT_NUM_CTX

DRIVER_SYSTEM = (
    "You are driving a state machine over MCP. The run has already started. Take exactly ONE "
    "action per turn by calling the `step` tool with the single appropriate `action` from the "
    "reachable actions you are given (most phases need no inputs). Never invent an action; every "
    "refusal carries `valid_next_actions` to recover from. Continue until the workflow reaches its "
    "terminal `report` action. Do not write prose; just call the tool."
)


def _mcp_tools_to_ollama(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        }
        for t in mcp_tools
    ]


async def _resource(client: Any, uri: str) -> str:
    try:
        result = await client.read_resource(uri)
    except Exception:
        return ""
    return (getattr(result[0], "text", "") if result else "") or ""


async def _ollama_chat(
    host: str, model: str, messages: list[dict], tools: list[dict], temperature: float, timeout: float
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": False,
        # Match the worker's context size so Ollama keeps one model instance loaded across the
        # driver's tool-calling turns and the per-phase worker generations (no reload thrash).
        "options": {"temperature": temperature, "num_ctx": DEFAULT_NUM_CTX},
    }
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.post(f"{host}/api/chat", json=payload)
        resp.raise_for_status()
    return resp.json().get("message", {}) or {}


def _reachable(next_text: str) -> list[str]:
    try:
        data = json.loads(next_text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [d.get("action", d) if isinstance(d, dict) else d for d in data]
    if isinstance(data, dict):
        return data.get("valid_next_actions") or list(data.keys())
    return []


async def drive_granite(
    server: Any,
    *,
    prompt: str,
    prelude: tuple[str, dict] | None = None,
    model: str = DEFAULT_MODEL,
    host: str | None = None,
    max_turns: int = 40,
    temperature: float = 0.0,
    timeout: float = 600.0,
    on_step: Any = None,
) -> dict[str, Any]:
    """Run Granite against a mounted kassi server until the FSM is terminal or the cap is hit.
    Returns a transcript: ``turns`` (one per executed action), ``final_state``, ``stopped_on``.

    ``prelude`` is an optional ``(action, inputs)`` executed deterministically before the model
    takes over. The run's entry parameters (which repo, which target, which index) are operator
    config, not a model decision, so the pilot seeds ``select_mode`` with them and Granite drives
    every workflow phase from there."""
    from fastmcp import Client

    host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    transcript: dict[str, Any] = {"turns": [], "final_state": None, "stopped_on": "cap"}

    async with Client(server) as client:
        tools = _mcp_tools_to_ollama(await client.list_tools())
        graph = await _resource(client, "theodosia://graph")

        if prelude is not None:
            action, inputs = prelude
            r = await client.call_tool("step", {"action": action, "inputs": inputs})
            payload = r.structured_content or {"content": str(r.content)}
            transcript["turns"].append({"action": action, "result": payload})
            if on_step is not None:
                await on_step(action, payload)
        reachable = _reachable(await _resource(client, "theodosia://next"))

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{DRIVER_SYSTEM}\n\n## FSM graph\n{graph}"},
            {"role": "user", "content": f"{prompt}\n\nReachable actions now: {reachable}"},
        ]

        nudges = 0
        for _ in range(max_turns):
            msg = await _ollama_chat(host, model, messages, tools, temperature, timeout)
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                # The model answered with prose instead of a tool call. While the FSM still has
                # legal moves, nudge it to continue rather than giving up mid-run (a small model
                # occasionally stops driving); only fall through to terminal when truly stuck.
                if reachable and nudges < 4:
                    nudges += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Continue: call the step tool now with one of {reachable}.",
                        }
                    )
                    continue
                transcript["stopped_on"] = "terminal" if not reachable else "text_only"
                break
            nudges = 0

            for call in calls:
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                try:
                    r = await client.call_tool(fn.get("name", "step"), args)
                    payload = r.structured_content or {"content": str(r.content)}
                except Exception as exc:
                    payload = {"error": "tool_invocation_failed", "detail": str(exc)}
                action = args.get("action") or fn.get("name")
                transcript["turns"].append({"action": action, "result": payload})
                if on_step is not None:
                    await on_step(action, payload)
                messages.append({"role": "tool", "content": json.dumps(payload, default=str)})

            reachable = _reachable(await _resource(client, "theodosia://next"))
            if not reachable:
                transcript["stopped_on"] = "terminal"
                break
            # Re-feed the reachable actions each turn so a small model stays on rails.
            messages.append(
                {"role": "user", "content": f"Reachable actions now: {reachable}. Call step once."}
            )

        state_text = await _resource(client, "theodosia://state")
        try:
            transcript["final_state"] = json.loads(state_text)
        except json.JSONDecodeError:
            transcript["final_state"] = state_text

    return transcript
