"""The report's references to its semantic model, checked (Microsoft's PBIR validator doesn't)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from t2pbi.graph import MigrationContext, migrate
from t2pbi.validate.report_refs import check_report_references

_SAMPLES = sorted((Path(__file__).resolve().parents[1] / "samples").glob("*/*.twb"))


@pytest.mark.parametrize("workbook", _SAMPLES, ids=lambda p: p.stem)
def test_every_sample_references_only_what_its_model_has(workbook, tmp_path):
    migrate(workbook, tmp_path, MigrationContext(run_output_validation=False))
    assert [str(p) for p in check_report_references(tmp_path)] == []


@pytest.fixture
def book1(book1_twb, tmp_path):
    migrate(book1_twb, tmp_path, MigrationContext(run_output_validation=False))
    return tmp_path


def _change(project: Path, page: str, change) -> None:
    for page_json in project.glob("*.Report/definition/pages/*/page.json"):
        if json.loads(page_json.read_text(encoding="utf-8"))["displayName"] == page:
            visual_json = next(page_json.parent.glob("visuals/*/visual.json"))
            data = json.loads(visual_json.read_text(encoding="utf-8"))
            change(data["visual"])
            visual_json.write_text(json.dumps(data), encoding="utf-8")
            return
    raise AssertionError(page)


def _first_field(visual: dict) -> dict:
    return next(iter(visual["query"]["queryState"].values()))["projections"][0]["field"]


@pytest.mark.parametrize(
    ("page", "change", "message"),
    [
        ("Sales by Category", lambda v: v.update(visualType="stackedAreaChrt"),
         "unknown visual type 'stackedAreaChrt'; did you mean 'stackedAreaChart'"),
        ("profit ratio by geography", lambda v: v.update(visualType="filledMap"),
         "'filledMap' is being retired by Microsoft; use azureMap"),
        ("Key Performance Indicators",
         lambda v: _first_field(v).update({"Column": {"Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Proffit"}}),
         "'Orders'[Proffit] isn't a column of Orders; did you mean 'Profit'?"),
        ("Key Performance Indicators",
         lambda v: _first_field(v).update({"Column": {"Expression": {"SourceRef": {"Entity": "Order"}}, "Property": "Sales"}}),
         "table 'Order' isn't in the model; did you mean 'Orders'"),
        ("Key Performance Indicators",
         lambda v: _first_field(v).update({"Measure": {"Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Sales"}}),
         "'Orders'[Sales] is a column of Orders, not a measure"),
    ],
)
def test_reference_problems_say_where_and_what_exists(book1, page, change, message):
    _change(book1, page, change)
    problems = check_report_references(book1)
    assert any(message in str(p) and str(p).startswith(f"page '{page}', visual ") for p in problems), [str(p) for p in problems]


def test_a_field_inside_an_aggregation_or_a_filter_alias_is_checked(book1):
    def broken(visual: dict) -> None:
        visual["query"]["queryState"]["Tooltips"] = {"projections": [{"field": {"Aggregation": {
            "Expression": {"Column": {"Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Proft"}},
            "Function": 0}}}]}
        visual["filter"] = {"From": [{"Name": "o", "Entity": "Orders", "Type": 0}], "Where": [{"Condition": {
            "Comparison": {"Left": {"Column": {"Expression": {"SourceRef": {"Source": "o"}}, "Property": "Regon"}}}}}]}

    _change(book1, "Sales by Category", broken)
    messages = [str(p) for p in check_report_references(book1)]
    assert any("[Proft]" in m for m in messages) and any("[Regon]" in m for m in messages)
