"""Mapping decisions and the routing seam for ambiguous worksheets.

A worksheet is mapped to a :class:`MappingDecision` — a chosen visual type plus a role->fields
assignment. Clear-cut worksheets are decided by the deterministic rules in ``visual_mapper``.
Ambiguous ones carry an :class:`Ambiguity` describing the competing interpretations; a
:class:`VisualRouter` may then refine the choice. The built-in :class:`DefaultRouter` keeps the
rule-based guess and marks it low-confidence so it surfaces for a reviewer. A future LLM-backed
router can implement the same protocol without touching the deterministic path.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from pydantic import BaseModel, Field

from t2pbi.ir.powerbi_ir import PBIFieldRef


class DecisionSource(str, Enum):
    """Who made the final mapping choice."""

    RULES = "rules"  # deterministic rules were confident
    LLM = "llm"  # an LLM router resolved an ambiguous case
    DEFAULT = "default"  # ambiguous case left at the rule-based fallback


class Ambiguity(BaseModel):
    """Why a worksheet's mapping is not clear-cut, for a router or a reviewer to act on."""

    reasons: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(
        default_factory=list, description="Alternative visual types worth considering."
    )
    available_fields: list[PBIFieldRef] = Field(
        default_factory=list, description="Resolved fields a router may assign when no rule matched."
    )
    features: dict = Field(
        default_factory=dict, description="Normalized worksheet features a router can reason over."
    )


class MappingDecision(BaseModel):
    """The logical choice for one worksheet: a visual type and its role->fields assignment."""

    visual_type: str
    wells: dict[str, list[PBIFieldRef]] = Field(default_factory=dict)
    sort_field: PBIFieldRef | None = None
    sort_descending: bool = True
    source: DecisionSource = DecisionSource.RULES
    confidence: float = 1.0
    rationale: str = ""
    ambiguity: Ambiguity | None = None


class VisualRouter(Protocol):
    """Resolves an ambiguous :class:`MappingDecision` into a final one."""

    def resolve(self, decision: MappingDecision) -> MappingDecision: ...


class DefaultRouter:
    """Keeps the deterministic guess, flagging ambiguous cases as low-confidence defaults."""

    def resolve(self, decision: MappingDecision) -> MappingDecision:
        if decision.ambiguity is None:
            return decision
        return decision.model_copy(
            update={"source": DecisionSource.DEFAULT, "confidence": min(decision.confidence, 0.5)}
        )
