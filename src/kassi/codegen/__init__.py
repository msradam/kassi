"""Deterministic k6 codegen.

Pure Python composes a single self-contained k6 scaffold from a :class:`Plan` plus
the OpenAPI spec. A single file (no imported client, no aux files) is a hard
requirement: the k6 MCP server runs one script string and cannot resolve local
imports. The scaffold is the deterministic baseline; the FSM's ``generate_script``
phase then has the model author the final script on top of it.
"""

from kassi.codegen.compose import compose
from kassi.codegen.slots import (
    DEFAULT_PLAN,
    EmphasisFlag,
    EndpointEmphasis,
    Parameterization,
    Plan,
    TestTaxonomy,
    default_plan,
)

__all__ = [
    "DEFAULT_PLAN",
    "EmphasisFlag",
    "EndpointEmphasis",
    "Parameterization",
    "Plan",
    "TestTaxonomy",
    "compose",
    "default_plan",
]
