"""The review gate: decide whether the agent's change to a page is kept.

The agent only proposes; this decides, with checks the agent can't influence:

1. **Validators (hard).** Microsoft's PBIR and TMDL checkers must report no errors.
2. **Derived checks (hard).** No check that passed before may fail now, and at least one of the
   page's own failing checks must now pass — unless the page had none (its problems came from the
   report or the judge only), in which case the judge must see an improvement.
3. **Picture (soft).** With Desktop running, the judge's score for the page must not drop by
   more than ``JUDGE_TOLERANCE`` (the judge is an LLM: the same pair of images can score a point
   apart on two calls).

Kept changes become a git commit; refused ones are reverted (see ``refine.workspace``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from t2pbi.fidelity import read_project, score_structural
from t2pbi.fidelity.derived import derive_expectations
from t2pbi.fidelity.structural import CheckResult
from t2pbi.ir.tableau_ir import TableauWorkbook
from t2pbi.mapping.report import IssueSeverity
from t2pbi.refine.worklist import ProblemSource, WorkItem
from t2pbi.validate.pbir_cli import iter_findings as pbir_findings
from t2pbi.validate.pbir_cli import run_pbir_validation
from t2pbi.validate.tmdl_cli import definition_dir, run_tmdl_validation
from t2pbi.validate.tmdl_cli import iter_findings as tmdl_findings

# Points the judge's score may drop before a change counts as making the picture worse.
JUDGE_TOLERANCE = 1


def failures(results: list[CheckResult]) -> dict[str, str]:
    """Failing checks keyed ``"<page>::<check>"`` (the key the gate compares) -> detail. A
    sheet seen on another sheet's page is keyed by that page, its check naming the visual."""

    return {f"{r.page_name}::{r.page_check}": r.detail for r in results if not r.passed}


def derived_failures(workbook: TableauWorkbook, workbook_file: str, project_dir: Path) -> dict[str, str]:
    """Every failing derived check for the project as it is on disk now."""

    return failures(score_structural(derive_expectations(workbook, workbook_file), read_project(project_dir)))


def validator_errors(pbip_path: str | Path) -> list[str]:
    """Errors from Microsoft's validators; a validator that isn't installed counts as clean."""

    errors = []
    pbir = run_pbir_validation(pbip_path)
    if pbir is not None:
        errors += [f"PBIR {code}: {msg}" for sev, code, msg in pbir_findings(pbir) if sev is IssueSeverity.ERROR]
    tmdl = run_tmdl_validation(definition_dir(pbip_path))
    if tmdl is not None:
        errors += [f"{where}: {msg}" for sev, where, msg in tmdl_findings(tmdl) if sev is IssueSeverity.ERROR]
    return errors


@dataclass
class ReviewVerdict:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)


def judge_verdict(
    item: WorkItem,
    before: dict[str, str],
    after: dict[str, str],
    validator_problems: list[str],
    score_before: int | None = None,
    score_after: int | None = None,
) -> ReviewVerdict:
    """Apply the gates to one page's change (pure: every input is already measured)."""

    reasons: list[str] = []
    if validator_problems:
        reasons.append("validators reject it: " + "; ".join(validator_problems[:3]))
    broke = sorted(set(after) - set(before))
    if broke:
        reasons.append("now failing: " + "; ".join(f"{k} ({after[k]})" for k in broke[:5]))

    targets = {f"{item.page}::{p.check}" for p in item.problems if p.source is ProblemSource.DERIVED}
    fixed = sorted(t for t in targets if t in before and t not in after)
    judged = score_before is not None and score_after is not None
    if targets and not fixed:
        reasons.append("none of the page's failing checks passes now")
    if not targets and not (judged and score_after > score_before):
        reasons.append("no measurable improvement (the judge's score didn't rise)")
    if judged and score_after < score_before - JUDGE_TOLERANCE:
        reasons.append(f"the picture got worse (judge {score_before}/10 -> {score_after}/10)")

    if reasons:
        return ReviewVerdict(accepted=False, reasons=reasons, fixed=fixed)
    accepted = [f"fixed: {', '.join(k.split('::', 1)[1] for k in fixed)}"] if fixed else []
    if judged:
        accepted.append(f"judge {score_before}/10 -> {score_after}/10")
    return ReviewVerdict(accepted=True, reasons=accepted, fixed=fixed)
