"""Tests for mapping and TMDL emission."""

from __future__ import annotations

from t2pbi.emit.tmdl_writer import render_semantic_model
from t2pbi.ingest import load_workbook
from t2pbi.ir.powerbi_ir import PBIDataType, SummarizeBy
from t2pbi.mapping.model_mapper import UNTRANSLATED_DAX, map_model
from t2pbi.parse import parse_workbook


def _model(path):
    return map_model(parse_workbook(load_workbook(path)))


def test_map_model_structure(superstore_twb):
    result = _model(superstore_twb)
    model = result.model

    assert model.name == "superstore"
    assert len(model.tables) == 1

    table = model.tables[0]
    assert table.name == "Superstore"

    sales = next(c for c in table.columns if c.name == "Sales")
    assert sales.data_type is PBIDataType.DOUBLE
    assert sales.summarize_by is SummarizeBy.SUM

    category = next(c for c in table.columns if c.name == "Category")
    assert category.data_type is PBIDataType.STRING
    assert category.summarize_by is SummarizeBy.NONE

    measure_names = {m.name for m in table.measures}
    assert measure_names == {"Profit Ratio", "Sales per Customer"}


def test_pending_expressions_flagged(superstore_twb):
    result = _model(superstore_twb)

    assert set(result.pending_expressions) == {
        "Superstore::Profit Ratio",
        "Superstore::Sales per Customer",
    }
    # Untranslated measures keep a valid placeholder body.
    for measure in result.model.tables[0].measures:
        assert measure.expression == UNTRANSLATED_DAX


def test_render_tmdl(superstore_twb):
    model = _model(superstore_twb).model
    files = render_semantic_model(model)

    assert "database.tmdl" in files
    assert "model.tmdl" in files
    assert "tables/Superstore.tmdl" in files

    # The model must be marked as V3 (enhanced metadata) or Desktop refuses modeling changes.
    assert "defaultPowerBIDataSourceVersion: powerBI_V3" in files["model.tmdl"]

    table_tmdl = files["tables/Superstore.tmdl"]
    assert "table Superstore" in table_tmdl
    assert "column Sales" in table_tmdl
    assert "summarizeBy: sum" in table_tmdl
    # Names with spaces are single-quoted.
    assert "column 'Order Date'" in table_tmdl
    assert "measure 'Profit Ratio' = BLANK()" in table_tmdl
    # Partition uses tab indentation and an M source.
    assert "\tpartition" in table_tmdl
