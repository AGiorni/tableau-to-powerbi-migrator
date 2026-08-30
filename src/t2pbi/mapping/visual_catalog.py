"""Frozen snapshot of Power BI visual role capabilities.

Captured from ``powerbi-report-author catalog describe <type>`` (Microsoft's authoritative
capability metadata). Regenerate when targeting a newer PBIR/Desktop version. Used as a guardrail:
before a visual is emitted, its role assignment is checked against the roles the visual actually
supports, so an unknown or over-filled role is caught here rather than producing a blank visual.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from t2pbi.ir.powerbi_ir import PBIFieldRef


class VisualSpec(BaseModel):
    """The query roles a visual type supports and their cardinality limits."""

    roles: dict[str, str] = Field(description="Role name -> kind (Grouping/Measure/GroupingOrMeasure).")
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    max_per_role: dict[str, int] = Field(default_factory=dict)


# Only the visual types the mapper currently emits. Extend by running the CLI for more types.
VISUAL_CATALOG: dict[str, VisualSpec] = {
    "areaChart": VisualSpec(
        roles={"Category": "Grouping", "Series": "Grouping", "Y": "Measure",
               "Y2": "Measure", "Rows": "Grouping", "Tooltips": "Measure"},
        required=["Category", "Y"], optional=["Series", "Y2", "Rows", "Tooltips"],
        max_per_role={"Series": 1},
    ),
    "barChart": VisualSpec(
        roles={"Category": "Grouping", "Series": "Grouping", "Y": "Measure",
               "Rows": "Grouping", "Tooltips": "Measure"},
        required=["Category", "Y"], optional=["Series", "Rows", "Tooltips"],
        max_per_role={"Series": 1},
    ),
    "clusteredBarChart": VisualSpec(
        roles={"Category": "Grouping", "Series": "Grouping", "Y": "Measure",
               "Rows": "Grouping", "Tooltips": "Measure"},
        required=["Category", "Y"], optional=["Series", "Rows", "Tooltips"],
        max_per_role={"Series": 1},
    ),
    "columnChart": VisualSpec(
        roles={"Category": "Grouping", "Series": "Grouping", "Y": "Measure",
               "Rows": "Grouping", "Tooltips": "Measure"},
        required=["Category", "Y"], optional=["Series", "Rows", "Tooltips"],
        max_per_role={"Series": 1},
    ),
    "clusteredColumnChart": VisualSpec(
        roles={"Category": "Grouping", "Series": "Grouping", "Y": "Measure",
               "Rows": "Grouping", "Tooltips": "Measure"},
        required=["Category", "Y"], optional=["Series", "Rows", "Tooltips"],
        max_per_role={"Series": 1},
    ),
    "lineChart": VisualSpec(
        roles={"Category": "Grouping", "Series": "Grouping", "Y": "Measure",
               "Y2": "Measure", "Rows": "Grouping", "Tooltips": "Measure"},
        required=["Category", "Y"], optional=["Series", "Y2", "Rows", "Tooltips"],
        max_per_role={"Series": 1},
    ),
    "pivotTable": VisualSpec(
        roles={"Rows": "Grouping", "Columns": "Grouping", "Values": "Measure"},
        required=["Values"], optional=["Rows", "Columns"],
    ),
    "scatterChart": VisualSpec(
        roles={"Category": "Grouping", "Series": "Grouping", "X": "GroupingOrMeasure",
               "Y": "GroupingOrMeasure", "Size": "Measure", "Play": "Grouping", "Tooltips": "Measure"},
        required=["X", "Y"], optional=["Category", "Series", "Size", "Play", "Tooltips"],
        max_per_role={"Category": 1, "Series": 1, "Y": 1, "Size": 1, "Play": 1},
    ),
    "tableEx": VisualSpec(
        roles={"Values": "GroupingOrMeasure"}, required=["Values"],
    ),
}


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
