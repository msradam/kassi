"""Fetch k6's own script-generation guidance from the k6 MCP server.

theodosia's upstream API calls tools only, so the `generate_script` MCP prompt and
the `docs://k6/best_practices` resource are fetched here with a short-lived fastmcp
client to the same k6 upstream config. Best-effort: returns None on any failure, and
the caller falls back to a built-in prompt.
"""

from __future__ import annotations

from kassi.upstream import k6_upstream_config


def _text(obj: object) -> str:
    return getattr(obj, "text", "") or ""


async def fetch_k6_generation_guidance(description: str) -> str | None:
    """k6's script-generation methodology + best practices as one guidance string."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    cfg = k6_upstream_config()
    transport = StdioTransport(command=cfg["command"], args=cfg.get("args", []))
    try:
        async with Client(transport) as client:
            prompt = await client.get_prompt("generate_script", {"description": description})
            methodology = "\n\n".join(_text(m.content) for m in prompt.messages).strip()
            try:
                practices = "\n\n".join(
                    _text(r) for r in await client.read_resource("docs://k6/best_practices")
                )
            except Exception:
                practices = ""
    except Exception:
        return None
    if not methodology:
        return None
    return "\n\n".join(p for p in (methodology, practices.strip()) if p)
