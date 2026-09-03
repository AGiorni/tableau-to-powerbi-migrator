"""Tests for the source-adapter seam: registry resolution, the contract, and the stub adapter."""

from __future__ import annotations

import pytest

from x2pbi.graph.state import PipelineState, ingest_node
from x2pbi.ir.source_ir import SourceWorkbook
from x2pbi.sources import (
    ADAPTERS,
    SourceAdapter,
    UnknownSourceError,
    adapter_for_path,
    get_adapter,
    supported_extensions,
)
from x2pbi.sources.spotfire import SPOTFIRE
from x2pbi.sources.tableau import TABLEAU


def test_every_registered_adapter_satisfies_the_contract():
    for adapter in ADAPTERS:
        assert isinstance(adapter, SourceAdapter)
        assert adapter.name and adapter.display_name
        assert adapter.extensions
        assert all(ext.startswith(".") and ext.islower() for ext in adapter.extensions)
        assert adapter.dialect.name


def test_no_two_adapters_claim_the_same_extension():
    extensions = supported_extensions()
    assert len(extensions) == len(set(extensions))


def test_get_adapter_by_name():
    assert get_adapter("tableau") is TABLEAU
    assert get_adapter("Tableau") is TABLEAU  # case-insensitive
    assert get_adapter("spotfire") is SPOTFIRE


def test_get_adapter_rejects_unknown_name():
    with pytest.raises(UnknownSourceError, match="tableau"):
        get_adapter("qlik")


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("book.twb", TABLEAU), ("book.twbx", TABLEAU), ("BOOK.TWB", TABLEAU), ("a.dxp", SPOTFIRE)],
)
def test_adapter_resolved_from_extension(filename, expected):
    assert adapter_for_path(filename) is expected


def test_unknown_extension_names_what_is_supported():
    with pytest.raises(UnknownSourceError, match=r"\.twb"):
        adapter_for_path("report.pbix")


def test_tableau_adapter_parses_to_the_neutral_ir(superstore_twb):
    workbook = TABLEAU.parse(superstore_twb)

    assert isinstance(workbook, SourceWorkbook)
    assert workbook.dialect == "tableau"
    assert workbook.datasources


def test_ingest_node_selects_the_adapter_by_extension(superstore_twb):
    state = ingest_node(PipelineState(input_path=str(superstore_twb), out_dir="out"))

    assert state.adapter is TABLEAU
    assert state.workbook is not None


def test_explicit_source_overrides_extension_inference(superstore_twb):
    # A .twb forced through the Spotfire adapter must fail in Spotfire's loader, proving the
    # explicit --source wins over the extension.
    state = PipelineState(input_path=str(superstore_twb), out_dir="out", source="spotfire")
    with pytest.raises(ValueError, match="Expected .dxp"):
        ingest_node(state)


def test_spotfire_dialect_differs_from_tableau():
    # The translator prompts per dialect; a shared dialect would silently mistranslate.
    assert SPOTFIRE.dialect.name != TABLEAU.dialect.name
    assert SPOTFIRE.dialect.rules


def test_spotfire_parse_is_a_clear_not_implemented(tmp_path):
    analysis = tmp_path / "analysis.dxp"
    analysis.write_bytes(b"not a real dxp")

    with pytest.raises(NotImplementedError, match="not implemented"):
        SPOTFIRE.parse(analysis)


def test_spotfire_missing_file_still_reports_the_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        SPOTFIRE.parse(tmp_path / "nope.dxp")
