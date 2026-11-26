"""The refinement workspace (git) and the work list builder."""

from __future__ import annotations

import subprocess

import pytest

from t2pbi.fidelity.project import Page, Project, Visual
from t2pbi.fidelity.structural import CheckResult
from t2pbi.mapping.report import IssueSeverity, MigrationIssue
from t2pbi.refine.worklist import ProblemSource, build_work_list
from t2pbi.refine.workspace import Workspace, WorkspaceError


@pytest.fixture
def project_dir(tmp_path):
    (tmp_path / "Book.pbip").write_text("{}", encoding="utf-8")
    page = tmp_path / "Book.Report" / "definition" / "pages" / "p1"
    page.mkdir(parents=True)
    (page / "page.json").write_text('{"name": "p1"}\n', encoding="utf-8")
    (tmp_path / "Book.SemanticModel").mkdir()
    (tmp_path / "unrelated.txt").write_text("not ours", encoding="utf-8")
    return tmp_path


def _tracked(root):
    out = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True)
    return set(out.stdout.split())


def test_baseline_commits_only_the_project(project_dir):
    ws = Workspace(project_dir / "Book.pbip")
    baseline = ws.init()

    assert baseline and ws.log() == [(baseline, "Baseline: deterministic migration")]
    assert _tracked(project_dir) == {"Book.pbip", "Book.Report/definition/pages/p1/page.json"}
    assert ws.init() == baseline  # idempotent: nothing new to commit


def test_commit_and_revert(project_dir):
    ws = Workspace(project_dir / "Book.pbip")
    ws.init()
    page = project_dir / "Book.Report" / "definition" / "pages" / "p1" / "page.json"

    page.write_text('{"name": "p1", "type": "Tooltip"}\n', encoding="utf-8")
    assert "Tooltip" in ws.diff()
    commit = ws.commit("Make p1 a tooltip page")
    assert commit and ws.log()[0] == (commit, "Make p1 a tooltip page")
    assert ws.commit("nothing changed") is None

    page.write_text("broken", encoding="utf-8")
    new_visual = project_dir / "Book.Report" / "definition" / "pages" / "p1" / "visuals" / "v9" / "visual.json"
    new_visual.parent.mkdir(parents=True)
    new_visual.write_text("{}", encoding="utf-8")
    ws.revert()
    assert "Tooltip" in page.read_text(encoding="utf-8")
    assert not new_visual.exists()
    assert (project_dir / "unrelated.txt").exists()  # never touches what isn't ours


def test_git_commands_refuse_to_run_without_our_repository(project_dir):
    with pytest.raises(WorkspaceError, match="not a t2pbi workspace"):
        Workspace(project_dir / "Book.pbip").commit("x")


def test_work_list_groups_problems_by_page_most_first():
    measure = {"Measure": {"Expression": {"SourceRef": {"Entity": "T"}}, "Property": "profit ratio"}}
    project = Project(
        pages=[
            Page(name="a", display_name="Map", page_type=None, visibility=None,
                 visuals=[Visual(name="v", visual_type="azureMap", title=None, roles={"Size": [measure]})]),
            Page(name="b", display_name="Trend", page_type=None, visibility=None, visuals=[]),
        ],
        measures={},
    )
    derived = [
        CheckResult("Trend", "fields[Order Date:month]", False, "bound: ['Order Date']"),
        CheckResult("Trend", "title", True),
        CheckResult("Map", "sort", False, "no sort"),
    ]
    issues = [
        MigrationIssue(severity=IssueSeverity.ERROR, stage="expression", object_name="profit ratio",
                       message="Could not produce valid DAX", detail="sum(...)"),
        MigrationIssue(severity=IssueSeverity.WARNING, stage="visual", object_name="Trend",
                       message='Dropped unknown role "X"'),
        MigrationIssue(severity=IssueSeverity.INFO, stage="visual", object_name="Trend", message="fyi"),
    ]
    judge = {"Trend": [("colour", "no colour split")]}

    items = build_work_list(derived, issues, project, judge)

    assert [i.page for i in items] == ["Trend", "Map"]
    trend = {p.check: p.source for p in items[0].problems}
    assert trend == {
        "fields[Order Date:month]": ProblemSource.DERIVED,
        "report:visual": ProblemSource.REPORT,
        "judge:colour": ProblemSource.JUDGE,
    }
    assert {p.check for p in items[1].problems} == {"sort", "report:measure[profit ratio]"}
