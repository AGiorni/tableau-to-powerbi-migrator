"""Tests for Tableau worksheet parsing: mark type, shelves, encodings, field references."""

from __future__ import annotations

from x2pbi.ir.source_ir import Aggregation, FieldRole, MarkType
from x2pbi.sources.tableau import load_workbook, parse_workbook
from x2pbi.sources.tableau.parser import _parse_field_ref, _parse_shelf


def test_field_ref_parsing():
    ref = _parse_field_ref("[federated.abc].[sum:Sales:qk]")
    assert ref is not None
    assert ref.field == "Sales"
    assert ref.aggregation is Aggregation.SUM
    assert ref.role is FieldRole.MEASURE

    dim = _parse_field_ref("[ds].[none:Category:nk]")
    assert dim is not None
    assert dim.field == "Category"
    assert dim.role is FieldRole.DIMENSION


def test_untranslatable_aggregation_becomes_other():
    # A date truncation has no Power BI aggregate function; OTHER keeps it a plain column.
    ref = _parse_field_ref("[ds].[year:Order Date:ok]")
    assert ref is not None
    assert ref.aggregation is Aggregation.OTHER


def test_field_ref_skips_generated_and_internal():
    assert _parse_field_ref("[ds].[Latitude (generated)]") is None
    assert _parse_field_ref("[ds].[:Measure Names]") is None
    assert _parse_field_ref("[ds].[Multiple Values]") is None
    assert _parse_field_ref(None) is None


def test_shelf_splits_nested_fields():
    refs = _parse_shelf("([ds].[none:Category:nk] * [ds].[sum:Sales:qk])")
    assert [(r.field, r.aggregation) for r in refs] == [
        ("Category", Aggregation.NONE),
        ("Sales", Aggregation.SUM),
    ]


def test_worksheet_parse_book1_if_present(tmp_path):
    from pathlib import Path

    book1 = Path(__file__).resolve().parents[1] / "samples" / "input" / "Book1.twb"
    if not book1.exists():
        return  # sample optional

    wb = parse_workbook(load_workbook(book1))
    sheet = next(v for v in wb.views if v.name == "Sales by Category")

    assert sheet.mark is MarkType.AREA
    assert sheet.native_mark == "Area"
    row_fields = {(r.field, r.role) for r in sheet.rows}
    assert ("Category", FieldRole.DIMENSION) in row_fields
    assert ("Sales", FieldRole.MEASURE) in row_fields
    assert any(e.kind == "color" for e in sheet.encodings)
    assert any(e.kind == "tooltip" for e in sheet.encodings)
