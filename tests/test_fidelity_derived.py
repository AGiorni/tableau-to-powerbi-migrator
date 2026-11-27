"""Expectations derived from the workbook: the parser's titles, tooltip links and sorts, and how
close the generator gets to the hand-written oracle."""

from __future__ import annotations

from pathlib import Path

import pytest

from t2pbi.fidelity import FieldSpec, load_expectations
from t2pbi.fidelity.derived import derive_expectations, field_spec
from t2pbi.ingest import load_workbook
from t2pbi.ir.tableau_ir import TableauCalculatedField, TableauFieldRef, TableauRole
from t2pbi.parse import parse_workbook

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def book1():
    return parse_workbook(load_workbook(_ROOT / "samples" / "Book1" / "Book1.twb"))


def test_parser_reads_titles_tooltip_links_and_sorts(book1):
    sheets = {ws.name: ws for ws in book1.worksheets}
    kpi = sheets["Key Performance Indicators"]
    assert kpi.title == "Executive Overview - Profitability"
    assert kpi.title_fields == ["State/Province"]
    assert sheets["Sales by Category"].title == "Sales by Category"  # "<Sheet Name> <[...]>"
    assert sheets["profit ratio by geography"].title is None  # Tableau's default title
    assert sheets["profit ratio by geography"].tooltip_sheets == ["Tooltip: Profit Ratio by City"]

    (sort,) = sheets["Tooltip: Profit Ratio by City"].sorts
    assert (sort.dimension.field, sort.direction, sort.by.aggregation) == ("City", "asc", "usr")


def test_derived_expectations_agree_with_the_oracle(book1):
    derived = {s.name: s for s in derive_expectations(book1, "Book1.twb").sheets}
    oracle = load_expectations(_ROOT / "samples" / "Book1" / "expected.toml")

    assert set(derived) == {s.name for s in oracle.sheets}
    for expected in oracle.sheets:
        got = derived[expected.name]
        assert got.kind == expected.kind, expected.name
        assert got.sort == expected.sort, expected.name
        if expected.kind == "dashboard":
            assert got.contains == expected.contains
            assert got.slicers == expected.slicers
            continue
        assert got.title == expected.title, expected.name
        # Coverage: every field the oracle places somewhere must be expected by the generator.
        oracle_fields = {f for specs in expected.roles.values() for f in specs} | set(expected.fields)
        assert oracle_fields <= set(got.fields), (expected.name, oracle_fields - set(got.fields))


def test_derived_expectations_never_state_mapper_choices(book1):
    for sheet in derive_expectations(book1, "Book1.twb").sheets:
        assert not sheet.visual_types and not sheet.roles and not sheet.known_gaps


def _ref(field, agg="none", role=TableauRole.DIMENSION):
    return TableauFieldRef(field=field, aggregation=agg, role=role)


@pytest.mark.parametrize(
    ("ref", "spec"),
    [
        (_ref("Orders_4666C054B71148B2B6470A7342081B95", "cnt"), FieldSpec("Orders", "rowcount")),
        (_ref("Order Date", "tmn"), FieldSpec("Order Date", "month")),
        (_ref("Order Date", "yr"), FieldSpec("Order Date", "year")),
        (_ref("Sales", "sum"), FieldSpec("Sales", "sum")),
        (_ref("Customer Name", "cntd"), FieldSpec("Customer Name", "countd")),
        (_ref("Category"), FieldSpec("Category")),
        (_ref("Calc_1", "usr"), FieldSpec("profit ratio", "measure")),
        (_ref("Calc_2"), FieldSpec("Order Profitable?")),  # a dimension calc stays a column
    ],
)
def test_field_spec(ref, spec):
    calcs = {
        "Calc_1": TableauCalculatedField(name="[Calc_1]", caption="profit ratio", formula="x"),
        "Calc_2": TableauCalculatedField(
            name="[Calc_2]", caption="Order Profitable?", formula="y", role=TableauRole.DIMENSION
        ),
    }
    assert field_spec(ref, calcs) == spec
