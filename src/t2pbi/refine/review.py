"""The review gate: decide whether the agent's change to a page is kept.

The agent only proposes; this decides, with checks the agent can't influence:

1. **Validators and derived checks (hard), compared before and after.** Validator errors
   (Microsoft's PBIR and TMDL checkers, and the reference check of the report against its model)
   are failures keyed by page like the derived checks, so the same rule covers both: nothing that
   passed before may fail now (a new validator error is refused), and at least one of the page's
   own failing checks or validator errors must now pass, unless the page had none (its problems
   came from the report or the judge only), in which case the judge must see an improvement. An
   error the first pass left is a problem to fix, not a veto on every change.
3. **Picture (soft).** With Desktop running, the judge's score for the page must not drop by
   more than ``JUDGE_TOLERANCE`` (the judge is an LLM: the same pair of images can score a point
   apart on two calls).

Kept changes become a git commit; refused ones are reverted (see ``refine.workspace``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from t2pbi.fidelity import read_project, score_structural
from t2pbi.fidelity.derived import derive_expectations
from t2pbi.fidelity.structural import CheckResult
from t2pbi.ir.tableau_ir import TableauWorkbook
from t2pbi.mapping.report import IssueSeverity
from t2pbi.refine.report_files import title_text
from t2pbi.refine.worklist import ProblemSource, WorkItem
from t2pbi.validate.pbir_cli import iter_diagnostics, run_pbir_validation
from t2pbi.validate.pbir_cli import iter_findings as pbir_findings
from t2pbi.validate.report_refs import check_report_references
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


def report_errors(pbip_path: str | Path) -> list[str]:
    """Errors in the report files: Microsoft's PBIR validator and the reference check against the
    model (wrong fields, unknown or retiring visual types). What the agent's saves are checked with."""

    errors = [f"REFERENCE {p}" for p in check_report_references(Path(pbip_path).parent)]
    pbir = run_pbir_validation(pbip_path)
    if pbir is not None:
        errors += [f"PBIR {code}: {msg}" for sev, code, msg in pbir_findings(pbir) if sev is IssueSeverity.ERROR]
    return errors


# A validator failure's check starts with this, after the page: "<page>::validator: <what>".
VALIDATOR_CHECK = "validator: "
# Where an error that isn't on any page is keyed: the model (TMDL) or another report file.
MODEL, REPORT = "(model)", "(report)"


def validator_failures(pbip_path: str | Path) -> dict[str, str]:
    """Every validator error for the project on disk now, keyed like derived failures:
    ``"<page>::validator: <where>: <what>" -> detail``. A tool that isn't installed counts as clean."""

    project_dir = Path(pbip_path).parent
    pages = _page_names(project_dir)
    found: dict[str, str] = {}
    for problem in check_report_references(project_dir):
        page = pages.get(_page_folder(problem.file), REPORT)
        where = problem.where.split(", ", 1)[1] if ", " in problem.where else "page"
        found[f"{page}::{VALIDATOR_CHECK}{where}: {problem.message}"] = problem.message
    pbir = run_pbir_validation(pbip_path)
    for diag in iter_diagnostics(pbir) if pbir is not None else []:
        if diag["severity"] is not IssueSeverity.ERROR:
            continue
        file = Path(diag["file"]) if diag["file"] else None
        message = diag["message"]
        if file is not None and message.endswith(f": {diag['file']}"):
            message = message[: -len(diag["file"]) - 2]  # the absolute path, already in the key
        page = pages.get(_page_folder(file), REPORT) if file else REPORT
        found[f"{page}::{VALIDATOR_CHECK}{_location(file)}: {diag['code']} {message}"] = message
    tmdl = run_tmdl_validation(definition_dir(pbip_path))
    for severity, where, message in tmdl_findings(tmdl) if tmdl is not None else []:
        if severity is IssueSeverity.ERROR:
            found[f"{MODEL}::{VALIDATOR_CHECK}{where}: {message}"] = message
    return found


def _page_names(project_dir: Path) -> dict[str, str]:
    """Page folder name -> page display name."""

    names = {}
    for page_json in project_dir.glob("*.Report/definition/pages/*/page.json"):
        try:
            names[page_json.parent.name] = json.loads(page_json.read_text(encoding="utf-8")).get("displayName", "")
        except (OSError, ValueError):
            continue
    return names


def _page_folder(file: Path | None) -> str | None:
    """The page folder a report file is in (``pages/<folder>/...``), or None."""

    parts = file.parts if file is not None else ()
    return parts[parts.index("pages") + 1] if "pages" in parts and parts.index("pages") + 1 < len(parts) - 1 else None


def _location(file: Path | None) -> str:
    if file is None or file.name != "visual.json":
        return "page" if file is not None and file.name == "page.json" else "report"
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"visual '{file.parent.name}'"
    return f"visual '{title_text(raw.get('visual') or {}) or raw.get('name') or file.parent.name}'"


@dataclass
class ReviewVerdict:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)


def judge_verdict(
    item: WorkItem,
    before: dict[str, str],
    after: dict[str, str],
    score_before: int | None = None,
    score_after: int | None = None,
) -> ReviewVerdict:
    """Apply the gates to one page's change (pure: every input is already measured).
    ``before`` and ``after`` hold every failing derived check and validator error."""

    reasons: list[str] = []
    broke = sorted(set(after) - set(before))
    new_errors = [k for k in broke if _is_validator(k)]
    if new_errors:
        reasons.append("validators reject it: " + "; ".join(k.split("::", 1)[1] for k in new_errors[:3]))
    newly_failing = [k for k in broke if not _is_validator(k)]
    if newly_failing:
        reasons.append("now failing: " + "; ".join(f"{k} ({after[k]})" for k in newly_failing[:5]))

    targets = {
        f"{item.page}::{p.check}" for p in item.problems
        if p.source in (ProblemSource.DERIVED, ProblemSource.VALIDATOR)
    }
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


def _is_validator(key: str) -> bool:
    return key.split("::", 1)[-1].startswith(VALIDATOR_CHECK)
