"""Typed ``Plan`` — the only thing the LLM is allowed to author.

If a field is here, the model picks from a closed set. Everything structural
(the k6 source itself) is decided by the composer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

TestTaxonomy = Literal["load", "regression_comparison", "smoke"]
Parameterization = Literal["static_examples", "faker", "csv_data", "response_extracted"]
EmphasisFlag = Literal[
    "risk_n_plus_one",
    "risk_unbounded_query",
    "risk_pagination",
    "auth_required",
]


class EndpointEmphasis(BaseModel):
    method: str
    path: str
    flags: list[EmphasisFlag] = Field(default_factory=list)


class Plan(BaseModel):
    test_taxonomy: TestTaxonomy
    parameterization: Parameterization
    endpoints: list[EndpointEmphasis] = Field(default_factory=list)

    @field_validator("test_taxonomy", "parameterization", mode="before")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return v.strip().lower() if isinstance(v, str) else v


DEFAULT_PLAN = Plan(test_taxonomy="load", parameterization="static_examples", endpoints=[])
