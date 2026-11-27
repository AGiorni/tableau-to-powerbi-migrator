"""Power BI screenshots for the refinement loop, and the visual judge's verdict on a page against
the user's Tableau screenshot of it. Used only while Power BI Desktop is open, and only for pages
the user gave a screenshot of (:func:`t2pbi.fidelity.references.match_screenshots`).

Files live in the run's work folder (``<out>/.refine/``), outside the project the workspace tracks:

- ``shots/<page id>.png`` — the page as it is at the last accepted commit;
- ``shots/<page id>.candidate.png`` — the page with the change under review.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from t2pbi.desktop import bridge
from t2pbi.fidelity.expectations import SheetExpectation
from t2pbi.fidelity.visual import judge_sheet


def work_dir(out_dir: str | Path) -> Path:
    return Path(out_dir) / ".refine"


def shot_path(work: Path, page_id: str, candidate: bool = False) -> Path:
    return work / "shots" / f"{page_id}{'.candidate' if candidate else ''}.png"


def keep_candidate(work: Path, page_id: str) -> None:
    """The change was accepted: its screenshot becomes the page's current one."""

    candidate = shot_path(work, page_id, candidate=True)
    if candidate.is_file():
        shutil.move(candidate, shot_path(work, page_id))


@dataclass
class PageJudgement:
    score: int | None = None
    mismatches: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None


def judge_page(
    chat_model: Any, pid: int, page_id: str, sheet: SheetExpectation, references: Sequence[Path], out: Path
) -> PageJudgement:
    """Screenshot one page in Desktop and have the judge compare it with the user's Tableau
    screenshots of the sheet."""

    try:
        bridge.screenshot_page(pid, page_id, out)
    except bridge.BridgeError as exc:
        return PageJudgement(error=f"screenshot failed: {exc}")
    verdict = judge_sheet(chat_model, sheet, references, out)
    if verdict.error:
        return PageJudgement(error=verdict.error)
    notes = [(name, verdict.criteria[name].get("note", "")) for name in verdict.mismatches]
    return PageJudgement(score=verdict.score, mismatches=notes)
