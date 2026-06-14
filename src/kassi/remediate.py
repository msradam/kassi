"""Turn a correlated root cause into a validated remediation.

LLMs are unreliable at emitting unified diffs (they invent line numbers), but reliable at
SEARCH/REPLACE edits. So the model proposes edits as search/replace blocks grounded in the
file, the root cause, and the recommendation; kassi applies them deterministically, validates
that the result still parses (Python AST), and emits a real unified diff with correct line
numbers via difflib. The model does the semantic edit; the tooling does application,
validation, and diffing, which is the part models get wrong. A patch is only ever returned if
it applied cleanly and still parses.
"""

from __future__ import annotations

import ast
import difflib
import re
from typing import Any

SEARCH_REPLACE_SYSTEM = (
    "You are a site-reliability engineer proposing the smallest code fix for a load-induced "
    "regression, applying the recommended approach. Return ONLY one or more edit blocks in "
    "EXACTLY this format, nothing else:\n"
    "<<<<<<< SEARCH\n"
    "<lines copied verbatim from the current file>\n"
    "=======\n"
    "<the replacement lines>\n"
    ">>>>>>> REPLACE\n"
    "The SEARCH text must match the current file exactly, character for character. Keep the "
    "change minimal and do not remove the commit or the error handling. No prose, no markdown."
)

_BLOCK = re.compile(r"<{5,}\s*SEARCH\s*\n(.*?)\n={5,}\s*\n(.*?)\n>{5,}\s*REPLACE", re.DOTALL)


def parse_blocks(text: str) -> list[tuple[str, str]]:
    """Pull (search, replace) pairs out of the model's reply (fences stripped)."""
    cleaned = (text or "").replace("```", "")
    return [(m.group(1), m.group(2)) for m in _BLOCK.finditer(cleaned)]


def changed_file(diff_text: str) -> str | None:
    """The file the introducing diff touched, from its `+++ b/<path>` header."""
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            for prefix in ("a/", "b/"):
                if path.startswith(prefix):
                    path = path[2:]
            return path if path and path != "/dev/null" else None
    return None


def apply_blocks(source: str, blocks: list[tuple[str, str]]) -> str | None:
    """Apply each search/replace to the source by exact match. Returns the patched source, or
    None if any block does not match exactly (so a partial or hallucinated edit is rejected)."""
    out = source
    applied = False
    for old, new in blocks:
        if old and old in out:
            out = out.replace(old, new, 1)
            applied = True
        else:
            return None
    return out if applied and out != source else None


def valid_python(source: str) -> bool:
    try:
        ast.parse(source)
        return True
    except SyntaxError:
        return False


def unified(old: str, new: str, path: str) -> str:
    """A real unified diff with correct line numbers, computed from before/after."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff).rstrip()


def documents(source: str, findings: dict[str, Any], recommendation: str) -> list[tuple[str, str]]:
    """Grounding for the edit: the file to fix, the correlated root cause, and the approach.

    Two root-cause shapes: a 5xx regression names a dominant server error; a latency degradation
    has no error at all, so the cause is the rising server-side time itself. Grounding the model on
    a fake "server error" for a degradation is what kept it from proposing a fix, so describe the
    latency cause directly when there is no error string."""
    te = findings.get("top_error") or {}
    wp = findings.get("worst_path") or {}
    path = wp.get("path") or "the changed endpoint"
    p95 = wp.get("p95_ms") or findings.get("p95_ms")
    docs: list[tuple[str, str]] = [("current file", source[:6000])]
    if te.get("error_message"):
        cause = (
            f"dominant server error '{te['error_message']}'"
            + (f" x{te.get('count')}" if te.get("count") else "")
            + f" on {path} under concurrency (only appears under load)"
        )
    else:
        cause = (
            f"latency degradation on {path}: server-side p95"
            + (f" {p95} ms" if p95 else "")
            + " climbs under sustained load with zero errors, because the per-request server-side "
            "work grows with the traffic already sent. Fix the algorithmic/work-per-request growth."
        )
    docs.append(("root cause", cause))
    if recommendation:
        docs.append(("recommended approach (apply this)", recommendation))
    return docs
