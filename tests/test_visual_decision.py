"""Tests for the visual catalog guardrail and the mapping-decision router seam."""

from __future__ import annotations

from t2pbi.ir.powerbi_ir import FieldKind, PBIFieldRef
from t2pbi.mapping.visual_catalog import VISUAL_CATALOG, enforce_wells
from t2pbi.mapping.visual_decision import Ambiguity, DecisionSource, DefaultRouter, MappingDecision


def _col(name: str) -> PBIFieldRef:
    return PBIFieldRef(entity="Orders", property=name, kind=FieldKind.COLUMN)


def _agg(name: str) -> PBIFieldRef:
    return PBIFieldRef(entity="Orders", property=name, kind=FieldKind.AGGREGATION, function=0)


def test_visual_catalog_loads_from_generated_json():
    # Guards against a missing/corrupt data/visual_catalog.json snapshot.
    assert len(VISUAL_CATALOG) > 20
    for vt in ("areaChart", "clusteredColumnChart", "lineChart", "pivotTable", "tableEx"):
        assert vt in VISUAL_CATALOG
    assert VISUAL_CATALOG["clusteredColumnChart"].roles["Rows"] == "Grouping"


def test_enforce_wells_drops_unknown_role():
    wells = {"Category": [_col("Order Date")], "Y": [_agg("Sales")], "SmallMultiples": [_col("Category")]}
    cleaned, warnings = enforce_wells("areaChart", wells)

    assert "SmallMultiples" not in cleaned
    assert "Rows" not in cleaned
    assert any("SmallMultiples" in w for w in warnings)
    assert set(cleaned) == {"Category", "Y"}


def test_enforce_wells_trims_over_capacity_role():
    wells = {"Category": [_col("City")], "Y": [_agg("Sales")], "Series": [_col("A"), _col("B")]}
    cleaned, warnings = enforce_wells("clusteredBarChart", wells)

    assert len(cleaned["Series"]) == 1
    assert any("Series" in w for w in warnings)


def test_enforce_wells_flags_missing_required():
    cleaned, warnings = enforce_wells("clusteredColumnChart", {"Category": [_col("City")]})

    assert cleaned == {"Category": [_col("City")]}
    assert any("missing required" in w.lower() for w in warnings)


def test_default_router_flags_ambiguous_decision():
    decision = MappingDecision(
        visual_type="areaChart",
        wells={"Category": [_col("Order Date")], "Y": [_agg("Sales")], "Rows": [_col("Category")]},
        ambiguity=Ambiguity(reasons=["trellis vs legend"], candidates=["areaChart"]),
    )
    resolved = DefaultRouter().resolve(decision)

    assert resolved.source is DecisionSource.DEFAULT
    assert resolved.confidence <= 0.5
    assert resolved.wells == decision.wells


def test_default_router_passes_through_confident_decision():
    decision = MappingDecision(visual_type="tableEx", wells={"Values": [_col("City")]})
    resolved = DefaultRouter().resolve(decision)

    assert resolved.source is DecisionSource.RULES
    assert resolved.confidence == 1.0
