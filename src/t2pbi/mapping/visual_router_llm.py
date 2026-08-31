"""LLM-backed router that resolves ambiguous visual mappings.

Implements the ``VisualRouter`` protocol. Given an ambiguous :class:`MappingDecision`, it asks the
configured chat model to choose the visual type and role assignment from the valid options in the
catalog, then reconstructs the decision from the model's field names and re-validates it. Any
failure (no model, provider error, unparseable or worse answer) falls back to the deterministic
guess, so this can never produce an invalid visual.
"""

from __future__ import annotations

import json
from typing import Any

from t2pbi.ir.powerbi_ir import PBIFieldRef
from t2pbi.mapping.visual_catalog import VISUAL_CATALOG, enforce_wells
from t2pbi.mapping.visual_decision import DecisionSource, MappingDecision

_SYSTEM_PROMPT = (
    "You are an expert in Tableau and Power BI report design. A Tableau worksheet maps ambiguously "
    "to a Power BI visual. Choose the visual type and assign each field to a query role so the "
    "result best preserves the author's intent. Rules:\n"
    "- Choose visual_type from the provided candidates only.\n"
    "- Use ONLY the roles listed for the chosen visual type. Grouping roles take dimensions; "
    "measure roles take measures.\n"
    "- A dimension that splits a measure into a grid of separate sub-charts is small multiples "
    "(role 'Rows'); a dimension that overlays coloured series in one chart is the legend "
    "(role 'Series').\n"
    '- Return ONLY JSON: {"visual_type": str, "roles": {role: [field names]}, "rationale": str, '
    '"confidence": number between 0 and 1}. No prose, no code fences.'
)


class LLMVisualRouter:
    """Resolve ambiguous mapping decisions with a chat model, grounded on the visual catalog."""

    def __init__(self, chat_model: Any) -> None:
        self._chat = chat_model

    def resolve(self, decision: MappingDecision) -> MappingDecision:
        if decision.ambiguity is None or self._chat is None:
            return decision

        index = {f.property: f for fields in decision.wells.values() for f in fields}
        for field in decision.ambiguity.available_fields:
            index.setdefault(field.property, field)
        parsed = self._ask(decision, index)
        if parsed is None:
            return _as_default(decision)

        visual_type, wells, rationale, confidence = parsed
        wells, _ = enforce_wells(visual_type, wells)
        spec = VISUAL_CATALOG.get(visual_type)
        # Reject an unknown, empty, or under-specified answer; keep the deterministic guess instead.
        if spec is None or not wells or any(not wells.get(r) for r in spec.required):
            return _as_default(decision)

        return decision.model_copy(
            update={
                "visual_type": visual_type,
                "wells": wells,
                "source": DecisionSource.LLM,
                "confidence": confidence,
                "rationale": rationale or decision.rationale,
            }
        )

    def _ask(
        self, decision: MappingDecision, index: dict[str, PBIFieldRef]
    ) -> tuple[str, dict[str, list[PBIFieldRef]], str, float] | None:
        from langchain_core.messages import HumanMessage, SystemMessage

        assert decision.ambiguity is not None
        candidates = decision.ambiguity.candidates or [decision.visual_type]
        catalog_view = {
            vt: {"roles": VISUAL_CATALOG[vt].roles, "required": VISUAL_CATALOG[vt].required}
            for vt in candidates
            if vt in VISUAL_CATALOG
        }
        current = {role: [f.property for f in fields] for role, fields in decision.wells.items()}
        user = (
            f"Ambiguity: {'; '.join(decision.ambiguity.reasons)}\n"
            f"Worksheet features: {json.dumps(decision.ambiguity.features)}\n"
            f"Candidate visual types and their roles: {json.dumps(catalog_view)}\n"
            f"Current rule-based assignment: {json.dumps(current)}\n"
            f"Available field names: {list(index)}"
        )
        try:
            response = self._chat.invoke(
                [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=user)]
            )
        except Exception:  # noqa: BLE001 - degrade gracefully on any provider error
            return None
        content = getattr(response, "content", None)
        if not isinstance(content, str):
            return None
        return _parse(content, index)


def _parse(
    content: str, index: dict[str, PBIFieldRef]
) -> tuple[str, dict[str, list[PBIFieldRef]], str, float] | None:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except (ValueError, TypeError):
        return None

    visual_type = data.get("visual_type")
    roles = data.get("roles")
    if not isinstance(visual_type, str) or not isinstance(roles, dict):
        return None

    wells: dict[str, list[PBIFieldRef]] = {}
    for role, names in roles.items():
        if not isinstance(names, list):
            continue
        refs = [index[n] for n in names if n in index]
        if refs:
            wells[role] = refs

    rationale = data.get("rationale") if isinstance(data.get("rationale"), str) else ""
    raw_conf = data.get("confidence")
    confidence = float(raw_conf) if isinstance(raw_conf, (int, float)) else 0.6
    return visual_type, wells, rationale, max(0.0, min(1.0, confidence))


def _as_default(decision: MappingDecision) -> MappingDecision:
    return decision.model_copy(
        update={"source": DecisionSource.DEFAULT, "confidence": min(decision.confidence, 0.5)}
    )
