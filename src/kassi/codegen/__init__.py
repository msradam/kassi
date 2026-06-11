"""Deterministic k6 codegen.

The LLM fills a closed-enum :class:`~kassi.codegen.plan.Plan`; pure Python
composes a single self-contained k6 script from that plan plus the OpenAPI spec.
A single file (no imported client, no aux files) is a hard requirement: the k6 MCP
server runs one script string and cannot resolve local imports.
"""

from kassi.codegen.compose import compose
from kassi.codegen.plan import DEFAULT_PLAN, Plan, fill_plan
from kassi.codegen.slots import EmphasisFlag, Parameterization, TestTaxonomy

__all__ = [
    "DEFAULT_PLAN",
    "EmphasisFlag",
    "Parameterization",
    "Plan",
    "TestTaxonomy",
    "compose",
    "fill_plan",
]
