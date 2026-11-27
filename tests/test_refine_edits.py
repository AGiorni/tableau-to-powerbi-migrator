"""The refinement agent's edit tools, applied to Book1's migrated report.

Each edit must fix what it targets (the derived checks pass), refuse bad requests with a useful
message, and leave a project Microsoft's validators still accept.
"""

from __future__ import annotations

import json

import pytest

from t2pbi.fidelity import read_project, score_structural
from t2pbi.fidelity.derived import derive_expectations
from t2pbi.graph import MigrationContext, migrate
from t2pbi.ingest import load_workbook
from t2pbi.parse import parse_workbook
from t2pbi.refine.edits import EditError, ReportEditor
from t2pbi.validate.pbir_cli import run_pbir_validation
from t2pbi.validate.tmdl_cli import definition_dir, run_tmdl_validation

TOOLTIP = "Tooltip: Profit Ratio by City"


@pytest.fixture
def book1(book1_twb, tmp_path):
    result = migrate(book1_twb, tmp_path, MigrationContext(run_output_validation=False))
    return result.pbip_path, ReportEditor(tmp_path), parse_workbook(load_workbook(book1_twb))


def _failing(pbip_path, workbook):
    expected = derive_expectations(workbook, "Book1.twb")
    return {(r.sheet, r.check) for r in score_structural(expected, read_project(pbip_path.parent)) if not r.passed}


def test_describe_shows_what_the_agent_needs(book1):
    _, editor, _ = book1
    info = editor.describe("Sales by Category")
    assert info["visual_type"] == "areaChart"
    assert info["roles"] == {"Category": ["Order Date"], "Y": ["Sales:sum"], "Rows": ["Category"]}
    assert "Tooltips" in info["allowed_roles"]
    assert "Profit" in editor.model_fields()["Orders"]["columns"]


def test_edits_fix_the_derived_checks_they_target(book1):
    pbip, editor, workbook = book1
    before = _failing(pbip, workbook)

    editor.set_title("Key Performance Indicators", "Executive Overview - Profitability")
    editor.set_title(TOOLTIP, "Profit Ratio by City")
    editor.set_sort(TOOLTIP, "profit ratio:measure", "ascending")
    editor.bind_field("Sales by Category", "Tooltips", "Profit:sum")
    editor.make_tooltip_page(TOOLTIP, host_page="profit ratio by geography")

    fixed = before - _failing(pbip, workbook)
    assert fixed == {
        ("Key Performance Indicators", "title"),
        (TOOLTIP, "title"),
        (TOOLTIP, "sort"),
        (TOOLTIP, "tooltip-page"),
        (TOOLTIP, "hidden"),
        ("Sales by Category", "fields[Profit:sum]"),
    }
    assert not _failing(pbip, workbook) - before  # nothing that passed now fails


def test_tooltip_page_is_linked_from_the_host_visual(book1):
    pbip, editor, _ = book1
    editor.make_tooltip_page(TOOLTIP, host_page="profit ratio by geography")
    project = read_project(pbip.parent)
    tip = project.page(TOOLTIP)
    assert (tip.page_type, tip.visibility) == ("Tooltip", "HiddenInViewMode")

    host_dir = next(p.parent for p in pbip.parent.glob("*.Report/definition/pages/*/page.json")
                    if json.loads(p.read_text(encoding="utf-8"))["displayName"] == "profit ratio by geography")
    host = json.loads(next(host_dir.glob("visuals/*/visual.json")).read_text(encoding="utf-8"))
    props = host["visual"]["visualContainerObjects"]["visualTooltip"][0]["properties"]
    assert props["type"]["expr"]["Literal"]["Value"] == "'Canvas'"
    assert props["section"]["expr"]["Literal"]["Value"] == f"'{tip.name}'"


def test_set_visual_type_keeps_compatible_roles(book1):
    _, editor, _ = book1
    result = editor.set_visual_type("Sales by Category", "stackedAreaChart")
    assert not result.warnings  # Category, Y and Rows all exist on stackedAreaChart
    assert editor.describe("Sales by Category")["visual_type"] == "stackedAreaChart"

    dropped = editor.set_visual_type("Sales by segment", "tableEx")
    assert any("rebind" in w for w in dropped.warnings)


def test_remove_field_also_drops_a_sort_on_it(book1):
    _, editor, _ = book1
    result = editor.remove_field(TOOLTIP, "Y", "profit ratio:measure")
    assert result.warnings and "sort" in result.warnings[0]
    assert "Y" not in editor.describe(TOOLTIP)["roles"]


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda e: e.bind_field("Sales by Category", "SmallMultiples", "Category"), "no role 'SmallMultiples'"),
        (lambda e: e.bind_field("Sales by Category", "Rows", "Sales:sum"), "takes columns"),
        (lambda e: e.bind_field("Sales by Category", "Y", "Region"), "takes measures"),
        (lambda e: e.bind_field("Sales by Category", "Tooltips", "Nope:sum"), "no column 'Nope'"),
        (lambda e: e.bind_field("Sales by Category", "Category", "Order Date:month"), "needs a model change"),
        (lambda e: e.bind_field("Sales by Category", "Y", "Sales:sum"), "already in role"),
        (lambda e: e.set_visual_type("Sales by Category", "notAVisual"), "unknown visual type"),
        (lambda e: e.set_visual_type("profit ratio by geography", "filledMap"), "being retired.*use azureMap"),
        (lambda e: e.set_sort(TOOLTIP, "Sales:sum", "ascending"), "doesn't show"),
        (lambda e: e.set_title("Nowhere", "x"), "no page named"),
        (lambda e: e.set_title("Executive Overview", "x"), "several visuals"),
    ],
)
def test_bad_requests_are_refused_with_a_useful_message(book1, call, message):
    _, editor, _ = book1
    with pytest.raises(EditError, match=message):
        call(editor)


@pytest.mark.external("tmdl-validate", "powerbi-report-author")
def test_edited_report_still_passes_microsofts_validators(book1):
    pbip, editor, _ = book1
    editor.set_title("Key Performance Indicators", "Executive Overview - Profitability")
    editor.set_sort(TOOLTIP, "profit ratio:measure", "ascending")
    editor.bind_field("Sales by Category", "Tooltips", "Profit:sum")
    editor.set_visual_type("Sales by Category", "stackedAreaChart")
    editor.make_tooltip_page(TOOLTIP, host_page="profit ratio by geography")

    pbir = run_pbir_validation(pbip)
    assert pbir["result"] == "succeeded", pbir.get("diagnostics")
    assert run_tmdl_validation(definition_dir(pbip))["result"] == "succeeded"
