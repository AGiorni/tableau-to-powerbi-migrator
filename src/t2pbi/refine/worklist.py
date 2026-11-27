"""The refinement agent's work list: what's wrong with each page of the migrated report.

Built by the ``assess`` node from three sources (no hand-written oracle needed):

- ``derived`` — failures of the coverage checks derived from the workbook (fidelity.derived);
- ``report``  — problems the first pass already reported for a page (fields it couldn't place,
  calculations left untranslated);
- ``judge``   — the visual judge's mismatches, screenshot against the Tableau reference (from 4.4,
  when Desktop is running).

One item per page, not per check: fixes on one visual interact (binding Order Date by month also
removes the "unexpected Order Date" failure), so the agent sees a page's problems together.
Everything here is plain data, so the work list lives in the graph state.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from pydantic import BaseModel, Field

from t2pbi.fidelity.project import Project
from t2pbi.fidelity.structural import CheckResult
from t2pbi.mapping.report import IssueSeverity, MigrationIssue


class ProblemSource(str, Enum):
    DERIVED = "derived"
    REPORT = "report"
    JUDGE = "judge"


class Problem(BaseModel):
    source: ProblemSource
    check: str = Field(description="Stable id, e.g. 'fields[Order Date:month]' or 'judge:colour'.")
    detail: str


class WorkItem(BaseModel):
    """Everything wrong with one report page, for one agent run."""

    page: str = Field(description="Report page display name (the Tableau sheet or dashboard name).")
    problems: list[Problem] = Field(default_factory=list)
    attempts: int = 0
    # Why earlier attempts on this page were rejected; sent back to the agent on a retry.
    feedback: list[str] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"{self.page}: {len(self.problems)} problem(s) — " + "; ".join(p.check for p in self.problems)


class RefineOutcome(BaseModel):
    """What happened to one agent attempt on one page."""

    page: str
    outcome: str = Field(description="accepted | rejected | unchanged | failed")
    commit: str | None = None
    summary: str = Field(default="", description="The agent's own one-line account of its change.")
    reasons: list[str] = Field(default_factory=list, description="Why the review kept or refused it.")


# Report stages whose issues describe a fixable problem on a page.
_PAGE_STAGES = {"visual", "dashboard"}


def build_work_list(
    derived: Iterable[CheckResult],
    issues: Iterable[MigrationIssue],
    project: Project,
    judge_mismatches: dict[str, list[tuple[str, str]]] | None = None,
) -> list[WorkItem]:
    """Group every problem by page; pages with the most problems first."""

    by_page: dict[str, list[Problem]] = {}

    def add(page: str, problem: Problem) -> None:
        problems = by_page.setdefault(page, [])
        if all(p.check != problem.check for p in problems):
            problems.append(problem)

    for result in derived:
        if not result.passed:
            add(result.sheet, Problem(source=ProblemSource.DERIVED, check=result.check, detail=result.detail))

    page_names = {p.display_name for p in project.pages}
    for issue in issues:
        if issue.severity is IssueSeverity.INFO:
            continue
        if issue.stage in _PAGE_STAGES and issue.object_name in page_names:
            add(issue.object_name, Problem(source=ProblemSource.REPORT, check=f"report:{issue.stage}", detail=issue.message))
        elif issue.stage == "expression":
            # An untranslated calculation is a problem on every page that shows it.
            for page in _pages_using_measure(project, issue.object_name):
                add(page, Problem(
                    source=ProblemSource.REPORT,
                    check=f"report:measure[{issue.object_name}]",
                    detail=f"{issue.message} {issue.detail or ''}".strip(),
                ))

    for page, mismatches in (judge_mismatches or {}).items():
        for criterion, note in mismatches:
            add(page, Problem(source=ProblemSource.JUDGE, check=f"judge:{criterion}", detail=note))

    items = [WorkItem(page=page, problems=problems) for page, problems in by_page.items()]
    return sorted(items, key=lambda item: (-len(item.problems), item.page))


def _pages_using_measure(project: Project, measure: str) -> list[str]:
    pages = []
    for page in project.pages:
        for visual in page.visuals:
            bound = [f for fields in visual.roles.values() for f in fields]
            if any(f.get("Measure", {}).get("Property") == measure for f in bound):
                pages.append(page.display_name)
                break
    return pages
