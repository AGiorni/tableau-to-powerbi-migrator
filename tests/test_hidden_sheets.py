"""Which Tableau sheets become pages: what a Tableau viewer can open as a tab.

``Book1_hidden`` is Book1 after "Hide All Sheets" on its dashboard: the four worksheets the
dashboard shows are hidden, the viz-in-tooltip sheet is not. So its report should have two pages,
the dashboard and the tooltip sheet, where Book1 (nothing hidden) has six.
"""

from __future__ import annotations

from t2pbi.fidelity import read_project, score_structural
from t2pbi.fidelity.derived import derive_expectations
from t2pbi.graph import MigrationContext, migrate
from t2pbi.ingest import load_workbook
from t2pbi.mapping.model_mapper import map_model
from t2pbi.mapping.report import IssueSeverity, MigrationReport
from t2pbi.mapping.visual_mapper import map_visuals
from t2pbi.parse import parse_workbook
from t2pbi.refine.agent import tableau_facts
from t2pbi.refine.review import failures
from t2pbi.refine.worklist import build_work_list

DASHBOARD_SHEETS = {"Key Performance Indicators", "Sales by Category", "Sales by segment", "profit ratio by geography"}
TOOLTIP = "Tooltip: Profit Ratio by City"


def test_the_hidden_flag_is_read_from_the_sheets_window(book1_hidden_twb, book1_twb):
    hidden = {ws.name for ws in parse_workbook(load_workbook(book1_hidden_twb)).worksheets if ws.hidden}
    assert hidden == DASHBOARD_SHEETS
    assert not any(ws.hidden for ws in parse_workbook(load_workbook(book1_twb)).worksheets)


def test_hidden_sheets_on_a_dashboard_get_no_page_of_their_own(book1_hidden_twb, tmp_path):
    result = migrate(book1_hidden_twb, tmp_path, MigrationContext(run_output_validation=False))

    project = read_project(tmp_path)
    assert {p.display_name for p in project.pages} == {"Executive Overview", TOOLTIP}
    # The dashboard still shows all four, as visuals.
    titles = {v.title for v in project.page("Executive Overview").visuals}
    workbook = parse_workbook(load_workbook(book1_hidden_twb))
    assert {ws.title or ws.name for ws in workbook.worksheets if ws.name in DASHBOARD_SHEETS} <= titles
    infos = {i.object_name for i in result.report.issues if "no page of its own" in i.message}
    assert infos == DASHBOARD_SHEETS


def test_a_hidden_sheet_on_no_dashboard_keeps_its_page(book1_hidden_twb):
    # E.g. a sheet used only in a story, which isn't migrated: a page beats losing it.
    workbook = parse_workbook(load_workbook(book1_hidden_twb)).model_copy(update={"dashboards": []})
    report = MigrationReport()
    pages = map_visuals(workbook, map_model(workbook, MigrationReport()).model, report)

    assert DASHBOARD_SHEETS <= {p.display_name for p in pages}
    warned = {i.object_name for i in report.issues if i.severity is IssueSeverity.WARNING and "stories" in i.message}
    assert warned == DASHBOARD_SHEETS


def test_derived_checks_look_for_hidden_sheets_on_their_dashboard(book1_hidden_twb, tmp_path):
    migrate(book1_hidden_twb, tmp_path, MigrationContext(run_output_validation=False))
    workbook = parse_workbook(load_workbook(book1_hidden_twb))
    results = score_structural(derive_expectations(workbook, "Book1_hidden.twb"), read_project(tmp_path))

    by_check = {(r.sheet, r.check): r for r in results}
    for sheet in DASHBOARD_SHEETS:
        assert by_check[(sheet, "page")].passed and by_check[(sheet, "page")].page == "Executive Overview"
        assert by_check[(sheet, "no-own-page")].passed
        assert by_check[(sheet, "visual")].passed  # found on the dashboard page
    # The same gap as in Book1, now found on the dashboard's copy of the visual.
    month = by_check[("Sales by Category", "fields[Order Date:month]")]
    assert not month.passed
    assert month.page_check == "visual 'Sales by Category': fields[Order Date:month]"
    assert "Executive Overview::visual 'Sales by Category': fields[Order Date:month]" in failures(results)


def test_no_own_page_fails_when_a_hidden_sheet_gets_a_page(book1_hidden_twb, book1_twb, tmp_path):
    # Book1's output has a page per worksheet: judged by Book1_hidden's rule, those are extra.
    migrate(book1_twb, tmp_path, MigrationContext(run_output_validation=False))
    workbook = parse_workbook(load_workbook(book1_hidden_twb))
    results = score_structural(derive_expectations(workbook, "Book1_hidden.twb"), read_project(tmp_path))
    extra = {r.sheet for r in results if r.check == "no-own-page" and not r.passed}
    assert extra == DASHBOARD_SHEETS


def test_a_hidden_sheets_problems_go_to_the_dashboard_page(book1_hidden_twb, tmp_path):
    result = migrate(book1_hidden_twb, tmp_path, MigrationContext(run_output_validation=False))
    workbook = parse_workbook(load_workbook(book1_hidden_twb))
    project = read_project(tmp_path)
    results = score_structural(derive_expectations(workbook, "Book1_hidden.twb"), project)

    items = {item.page: item for item in build_work_list(results, result.report.issues, project)}

    assert set(items) <= {"Executive Overview", TOOLTIP}
    checks = {p.check for p in items["Executive Overview"].problems}
    assert "visual 'Sales by Category': fields[Order Date:month]" in checks


def test_the_agent_gets_the_facts_of_hidden_sheets_on_a_dashboard(book1_hidden_twb, book1_twb):
    facts = tableau_facts(parse_workbook(load_workbook(book1_hidden_twb)), "Executive Overview")
    assert set(facts["hidden_sheets_shown_here"]) == DASHBOARD_SHEETS
    assert facts["hidden_sheets_shown_here"]["Sales by Category"]["mark"] == "Area"
    assert "hidden_sheets_shown_here" not in tableau_facts(parse_workbook(load_workbook(book1_twb)), "Executive Overview")
