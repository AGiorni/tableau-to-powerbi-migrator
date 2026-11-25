"""Structural fidelity: the expectation files, the PBIR reader and the field matcher.

The sample test is a ratchet: every check that passes today must keep passing, and every known gap
must keep failing until someone fixes it and deletes the gap from the expectations file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from t2pbi.fidelity import FieldSpec, load_expectations, read_project, score_structural
from t2pbi.fidelity.expectations import WorkbookExpectations, _parse_sheet
from t2pbi.fidelity.project import Page, Project, Visual
from t2pbi.fidelity.references import find_reference_image
from t2pbi.fidelity.structural import matches
from t2pbi.graph import MigrationContext, migrate

_ROOT = Path(__file__).resolve().parents[1]
_EXPECTATION_FILES = sorted((_ROOT / "samples" / "expected").glob("*.toml"))


@pytest.mark.parametrize("expectations", _EXPECTATION_FILES, ids=lambda p: p.stem)
def test_sample_fidelity_matches_expectations(expectations, tmp_path):
    expected = load_expectations(expectations)
    migrate(_ROOT / "samples" / "input" / expected.workbook, tmp_path, MigrationContext(run_pbir_validation=False))

    results = score_structural(expected, read_project(tmp_path))

    regressions = [f"{r.sheet} :: {r.check} — {r.detail}" for r in results if r.outcome == "fail"]
    fixed = [f"{r.sheet} :: {r.check}" for r in results if r.outcome == "fixed"]
    assert not regressions, "Fidelity regressions:\n" + "\n".join(regressions)
    assert not fixed, "Known gaps now pass — delete them from known_gaps:\n" + "\n".join(fixed)


# --- Field specs and expectation files -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "name", "qualifier"),
    [
        ("Category", "Category", None),
        ("Sales:sum", "Sales", "sum"),
        ("profit ratio:measure", "profit ratio", "measure"),
        ("Order Date:month", "Order Date", "month"),
        ("Orders:rowcount", "Orders", "rowcount"),
        ("Ratio: weird:name", "Ratio: weird:name", None),  # an unknown suffix stays part of the name
    ],
)
def test_field_spec_parse(text, name, qualifier):
    assert FieldSpec.parse(text) == FieldSpec(name, qualifier)


def test_role_names_must_come_from_the_catalog():
    with pytest.raises(ValueError, match="SmallMultiples"):
        _parse_sheet({"name": "s", "visual": ["areaChart"], "roles": {"SmallMultiples": ["x"]}})
    with pytest.raises(ValueError, match="not in the visual catalog"):
        _parse_sheet({"name": "s", "visual": ["notAVisual"]})


def test_unknown_keys_are_rejected():
    with pytest.raises(ValueError, match="visuals"):
        _parse_sheet({"name": "s", "visuals": ["tableEx"]})  # typo for "visual"


def test_every_book1_sheet_has_a_reference_image():
    expected = load_expectations(_ROOT / "samples" / "expected" / "Book1.toml")
    folder = _ROOT / "samples" / "reference" / "Book1"
    missing = [s.name for s in expected.sheets if find_reference_image(folder, s.name) is None]
    assert not missing


def test_reference_names_match_loosely(tmp_path):
    (tmp_path / "tooltip profit ratio by city.png").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("not an image", encoding="utf-8")
    assert find_reference_image(tmp_path, "Tooltip: Profit Ratio by City").name == (
        "tooltip profit ratio by city.png"
    )
    assert find_reference_image(tmp_path, "Sales by Category") is None
    assert find_reference_image(tmp_path / "absent", "x") is None


def test_known_gap_must_name_a_real_check(tmp_path):
    path = tmp_path / "x.toml"
    path.write_text(
        'workbook = "x.twb"\n[[sheet]]\nname = "s"\nvisual = ["tableEx"]\n'
        'known_gaps."visual-typo" = "oops"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="visual-typo"):
        score_structural(load_expectations(path), Project(pages=[], measures={}))


def test_title_allows_a_dynamic_suffix_and_dashboards_find_visuals_either_way():
    sheet = _parse_sheet({"name": "KPI", "title": "Executive Overview - Profitability"})
    dash = _parse_sheet({"name": "Dash", "kind": "dashboard", "contains": ["KPI"]})
    expected = WorkbookExpectations(workbook="x.twb", sheets=[sheet, dash])

    def project(title):
        visual = Visual(name="v", visual_type="tableEx", title=title)
        return Project(
            pages=[
                Page(name="p1", display_name="KPI", page_type=None, visibility=None, visuals=[visual]),
                Page(name="p2", display_name="Dash", page_type=None, visibility=None, visuals=[visual]),
            ],
            measures={},
        )

    def outcome(title, check):
        return next(r.passed for r in score_structural(expected, project(title)) if r.check == check)

    assert outcome("Executive Overview - Profitability All", "title")
    assert not outcome("KPI", "title")
    # The dashboard finds the visual by sheet name today and by its title once titles migrate.
    assert outcome("KPI", "contains[KPI]")
    assert outcome("Executive Overview - Profitability All", "contains[KPI]")


# --- Matching PBIR field expressions ---------------------------------------------------------


def _col(entity, prop):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def test_matches_columns_measures_and_aggregations():
    assert matches(FieldSpec("City"), _col("Orders", "City"), None)
    assert not matches(FieldSpec("City"), _col("Orders", "Region"), None)
    measure = {"Measure": {"Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "profit ratio"}}
    assert matches(FieldSpec("profit ratio", "measure"), measure, None)
    total = {"Aggregation": {"Expression": _col("Orders", "Sales"), "Function": 0}}
    assert matches(FieldSpec("Sales", "sum"), total, None)
    assert not matches(FieldSpec("Sales", "avg"), total, None)


def test_matches_date_hierarchy_level():
    month = {
        "HierarchyLevel": {
            "Expression": {
                "Hierarchy": {
                    "Expression": {
                        "PropertyVariationSource": {
                            "Expression": {"SourceRef": {"Entity": "Orders"}},
                            "Name": "Variation",
                            "Property": "Order Date",
                        }
                    },
                    "Hierarchy": "Date Hierarchy",
                }
            },
            "Level": "Month",
        }
    }
    assert matches(FieldSpec("Order Date", "month"), month, None)
    assert not matches(FieldSpec("Order Date", "year"), month, None)
    assert not matches(FieldSpec("Order Date", "month"), _col("Orders", "Order Date"), None)


def test_matches_row_count_as_aggregation_or_countrows_measure():
    count = {"Aggregation": {"Expression": _col("Orders", "Order ID"), "Function": 5}}
    assert matches(FieldSpec("Orders", "rowcount"), count, None)
    project = Project(pages=[], measures={"Order Count": ("Orders", "COUNTROWS('Orders')")})
    measure = {"Measure": {"Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Order Count"}}
    assert matches(FieldSpec("Orders", "rowcount"), measure, project)
    assert not matches(FieldSpec("Returns", "rowcount"), measure, project)


def test_read_project_reads_titles_page_types_and_multiline_measures(tmp_path):
    page_dir = tmp_path / "X.Report" / "definition" / "pages" / "p1"
    (page_dir / "visuals" / "v1").mkdir(parents=True)
    (page_dir / "page.json").write_text(
        json.dumps({"name": "p1", "displayName": "Tip", "type": "Tooltip", "visibility": "HiddenInViewMode"}),
        encoding="utf-8",
    )
    title = {"title": [{"properties": {"text": {"expr": {"Literal": {"Value": "'It''s'"}}}}}]}
    visual = {
        "name": "v1",
        "visual": {
            "visualType": "tableEx",
            "query": {"queryState": {"Values": {"projections": [{"field": _col("T", "a")}]}}},
            "visualContainerObjects": title,
        },
    }
    (page_dir / "visuals" / "v1" / "visual.json").write_text(json.dumps(visual), encoding="utf-8")
    tables = tmp_path / "X.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "T.tmdl").write_text(
        "table T\n\n\tmeasure 'One' = 1\n\n\tmeasure Multi =\n\t\t\tVAR x = 1\n\t\t\tRETURN x\n"
        "\t\tformatString: 0\n",
        encoding="utf-8",
    )

    project = read_project(tmp_path)

    page = project.page("Tip")
    assert page is not None and page.page_type == "Tooltip" and page.visibility == "HiddenInViewMode"
    assert page.visuals[0].title == "It's"
    assert project.measures == {"One": ("T", "1"), "Multi": ("T", "VAR x = 1 RETURN x")}
