"""Build PBIR report JSON (pages and visuals) from the Power BI report IR.

The JSON shapes mirror a Power BI Desktop-generated report exactly (see the visualContainer
2.12.0 and page 2.1.0 schemas).
"""

from __future__ import annotations

from t2pbi.ir.powerbi_ir import FieldKind, PBIFieldRef, PBIReportPage, PBIVisual

# Root of every Fabric/PBIR JSON schema URL the emitter writes.
SCHEMA_BASE = "https://developer.microsoft.com/json-schemas/fabric"

# QueryAggregateFunction enum -> (queryRef short name, nativeQueryRef label), per the PBIR docs.
# Function 2 is the "Count" enum (distinct count in the UI); 5 is "CountNonNull" (plain count).
_FUNC_NAMES = {
    0: ("Sum", "Sum"),
    1: ("Avg", "Average"),
    2: ("Count", "Distinct Count"),
    3: ("Min", "Min"),
    4: ("Max", "Max"),
    5: ("CountNonNull", "Count"),
    6: ("Median", "Median"),
}


def build_visual_json(visual: PBIVisual) -> dict:
    query_state = {
        well: {"projections": [projection(f, active=(well == "Category" and i == 0))
                               for i, f in enumerate(fields)]}
        for well, fields in visual.wells.items()
    }
    query: dict = {"queryState": query_state}
    if visual.sort_field is not None:
        query["sortDefinition"] = {
            "sort": [
                {
                    "field": field_expression(visual.sort_field),
                    "direction": "Descending" if visual.sort_descending else "Ascending",
                }
            ],
            "isDefaultSort": True,
        }
    return {
        "$schema": f"{SCHEMA_BASE}/item/report/definition/visualContainer/2.12.0/schema.json",
        "name": visual.name,
        "position": {
            "x": visual.x,
            "y": visual.y,
            "z": visual.z,
            "height": visual.height,
            "width": visual.width,
            "tabOrder": visual.tab_order,
        },
        "visual": _visual_body(visual, query),
    }


def _visual_body(visual: PBIVisual, query: dict) -> dict:
    body: dict = {
        "visualType": visual.visual_type,
        "query": query,
        "drillFilterOtherVisuals": True,
    }
    if visual.title:
        body["visualContainerObjects"] = {"title": title_object(visual.title)}
    return body


def title_object(title: str) -> list[dict]:
    """The ``title`` visual container object showing ``title``."""

    return [{"properties": {"show": literal("true"), "text": string_literal(title)}}]


def string_literal(text: str) -> dict:
    """A PBIR string literal, ``'text'`` (an embedded quote is doubled)."""

    return literal(f"'{text.replace(chr(39), chr(39) * 2)}'")


def literal(value: str) -> dict:
    return {"expr": {"Literal": {"Value": value}}}


def build_page_json(page: PBIReportPage) -> dict:
    return {
        "$schema": f"{SCHEMA_BASE}/item/report/definition/page/2.1.0/schema.json",
        "name": page.name,
        "displayName": page.display_name,
        "displayOption": "FitToPage",
        "height": 1080,
        "width": 1920,
    }


def projection(field: PBIFieldRef, active: bool) -> dict:
    query_ref, native_ref = _refs(field)
    projection = {
        "field": field_expression(field),
        "queryRef": query_ref,
        "nativeQueryRef": native_ref,
    }
    if active:
        projection["active"] = True
    return projection


def field_expression(field: PBIFieldRef) -> dict:
    source = {"Expression": {"SourceRef": {"Entity": field.entity}}, "Property": field.property}
    if field.kind is FieldKind.MEASURE:
        return {"Measure": source}
    if field.kind is FieldKind.AGGREGATION:
        return {"Aggregation": {"Expression": {"Column": source}, "Function": field.function}}
    return {"Column": source}


def _refs(field: PBIFieldRef) -> tuple[str, str]:
    if field.kind is FieldKind.AGGREGATION:
        short, label = _FUNC_NAMES.get(field.function or 0, ("Sum", "Sum"))
        return f"{short}({field.entity}.{field.property})", f"{label} of {field.property}"
    return f"{field.entity}.{field.property}", field.property
