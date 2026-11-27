"""Tests for worksheet parsing: mark type, shelves, encodings, field references."""

from __future__ import annotations

import pytest

from t2pbi.ingest import load_workbook
from t2pbi.ir.tableau_ir import TableauRole
from t2pbi.parse import parse_workbook
from t2pbi.parse.tableau_parser import _parse_field_ref, _parse_shelf


def test_field_ref_parsing():
    ref = _parse_field_ref("[federated.abc].[sum:Sales:qk]")
    assert ref is not None
    assert ref.field == "Sales"
    assert ref.aggregation == "sum"
    assert ref.role is TableauRole.MEASURE

    dim = _parse_field_ref("[ds].[none:Category:nk]")
    assert dim is not None
    assert dim.field == "Category"
    assert dim.role is TableauRole.DIMENSION


def test_field_ref_skips_generated_and_internal():
    assert _parse_field_ref("[ds].[Latitude (generated)]") is None
    assert _parse_field_ref("[ds].[:Measure Names]") is None
    assert _parse_field_ref("[ds].[Multiple Values]") is None
    assert _parse_field_ref(None) is None


def test_shelf_splits_nested_fields():
    refs = _parse_shelf("([ds].[none:Category:nk] * [ds].[sum:Sales:qk])")
    assert [(r.field, r.aggregation) for r in refs] == [("Category", "none"), ("Sales", "sum")]


def test_worksheet_parse_book1_if_present(tmp_path):
    from pathlib import Path

    book1 = Path(__file__).resolve().parents[1] / "samples" / "Book1" / "Book1.twb"
    if not book1.exists():
        return  # sample optional

    wb = parse_workbook(load_workbook(book1))
    sheet = next(w for w in wb.worksheets if w.name == "Sales by Category")

    assert sheet.mark_class == "Area"
    row_fields = {(r.field, r.role) for r in sheet.rows}
    assert ("Category", TableauRole.DIMENSION) in row_fields
    assert ("Sales", TableauRole.MEASURE) in row_fields
    assert any(e.kind == "color" for e in sheet.encodings)
    assert any(e.kind == "tooltip" for e in sheet.encodings)


@pytest.mark.parametrize(
    ("ref", "field", "aggregation", "role", "table_calc"),
    [
        ("[ds].[sum:Sales:qk]", "Sales", "sum", "measure", None),
        ("[ds].[none:Region:nk]", "Region", "none", "dimension", None),
        # An instance number: the same field placed twice (e.g. with different table calcs).
        ("[ds].[usr:Calculation_1059753339666751488:qk:7]", "Calculation_1059753339666751488", "usr", "measure", None),
        # A quick table calculation on top of the aggregation.
        ("[ds].[pcto:sum:Sales:qk]", "Sales", "sum", "measure", "pcto"),
        ("[ds].[pcdf:avg:Order Transit Time:qk:1]", "Order Transit Time", "avg", "measure", "pcdf"),
        # A visual-total variant after the field.
        ("[ds].[sum:EmployeeCount:vtmin:qk]", "EmployeeCount", "sum", "measure", None),
    ],
)
def test_column_instance_forms(ref, field, aggregation, role, table_calc):
    from t2pbi.parse.tableau_parser import _parse_field_ref

    parsed = _parse_field_ref(ref)
    assert (parsed.field, parsed.aggregation, parsed.role.value, parsed.table_calc) == (field, aggregation, role, table_calc)
