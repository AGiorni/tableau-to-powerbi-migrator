"""Frozen snapshot of Power BI visual role capabilities.

The data in ``data/visual_catalog.json`` is generated from ``powerbi-report-author catalog
describe`` (Microsoft's authoritative capability metadata) by ``scripts/refresh_visual_catalog.py``.
Regenerate it with that script when targeting a newer PBIR/Desktop version. Used as a guardrail:
before a visual is emitted, its role assignment is checked against the roles the visual actually
supports, so an unknown or over-filled role is caught here rather than producing a blank visual.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from t2pbi.ir.powerbi_ir import FieldKind, PBIFieldRef

_CATALOG_PATH = Path(__file__).parent / "data" / "visual_catalog.json"

# Which field kinds a role's declared kind accepts (mirrors the validator's role-kind check).
_ROLE_KIND_OK = {
    "Grouping": {FieldKind.COLUMN},
    "Measure": {FieldKind.MEASURE, FieldKind.AGGREGATION},
    "GroupingOrMeasure": {FieldKind.COLUMN, FieldKind.MEASURE, FieldKind.AGGREGATION},
}


class VisualSpec(BaseModel):
    """The query roles a visual type supports and their cardinality limits."""

    roles: dict[str, str] = Field(description="Role name -> kind (Grouping/Measure/GroupingOrMeasure).")
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    max_per_role: dict[str, int] = Field(default_factory=dict)


def _load_catalog() -> dict[str, VisualSpec]:
    data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return {name: VisualSpec(**spec) for name, spec in data["visuals"].items()}


# Frozen at import from the generated snapshot; refresh via scripts/refresh_visual_catalog.py.
VISUAL_CATALOG: dict[str, VisualSpec] = _load_catalog()


def enforce_wells(
    visual_type: str, wells: dict[str, list[PBIFieldRef]]
) -> tuple[dict[str, list[PBIFieldRef]], list[str]]:
    """Return a wells dict trimmed to what ``visual_type`` supports, plus human-readable warnings.

    Drops fields in unknown roles and trims roles that exceed their cardinality limit. An unknown
    visual type is passed through unchanged (nothing to validate against).
    """

    spec = VISUAL_CATALOG.get(visual_type)
    if spec is None:
        return wells, [f'No catalog entry for visual type "{visual_type}"; roles not validated.']

    cleaned: dict[str, list[PBIFieldRef]] = {}
    warnings: list[str] = []
    for role, fields in wells.items():
        if not fields:
            continue
        if role not in spec.roles:
            names = ", ".join(f.property for f in fields)
            warnings.append(f'Dropped unknown role "{role}" ({names}) for visual "{visual_type}".')
            continue
        allowed = _ROLE_KIND_OK.get(spec.roles[role])
        if allowed is not None:
            kept = [f for f in fields if f.kind in allowed]
            if len(kept) != len(fields):
                mismatched = ", ".join(f.property for f in fields if f.kind not in allowed)
                warnings.append(
                    f'Dropped field(s) {mismatched} from role "{role}" on "{visual_type}" '
                    "(role/field kind mismatch)."
                )
            fields = kept
            if not fields:
                continue
        cap = spec.max_per_role.get(role)
        if cap is not None and len(fields) > cap:
            warnings.append(
                f'Role "{role}" on "{visual_type}" allows {cap} field(s); kept the first of {len(fields)}.'
            )
            fields = fields[:cap]
        cleaned[role] = fields

    missing = [r for r in spec.required if not cleaned.get(r)]
    if missing:
        warnings.append(f'Visual "{visual_type}" is missing required role(s): {", ".join(missing)}.')
    return cleaned, warnings
