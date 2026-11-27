"""Visual fidelity: does the migrated page look like the Tableau original? An LLM judges.

The judge gets the user's screenshots of the Tableau sheet (one or more views of it) and the Power
BI page rendered by Desktop, and scores a fixed rubric. Sheets without a screenshot aren't judged. It's one
structured call per sheet (LLM-as-judge), not an agent. Advisory: a judgement, never the only gate;
the structural and numeric layers are the hard checks.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from t2pbi.fidelity.expectations import SheetExpectation
from t2pbi.llm import message_text, provider_error_message

RUBRIC = {
    "chart_type": "Same kind of chart (bar / area / line / map / table / …), orientation and stacking.",
    "fields": "Same dimensions and measures on the axes, rows and columns (or table columns).",
    "date_granularity": "Dates at the same level (e.g. by month, not by day). n/a if no dates.",
    "colour": "Same colour split or colour encoding (legend series, gradient). n/a if none.",
    "layout": "Same panels / small multiples, and for dashboards the same visuals arranged alike.",
    "title": "Same title text (dynamic parts such as filter values may differ).",
}

_SYSTEM_PROMPT = (
    "You check a migration from Tableau to Power BI. You get images of the same report sheet: first "
    "the ORIGINAL in Tableau (one or more views of it, such as the whole sheet and a close-up of a "
    "legend or a hovered tooltip), then the MIGRATED page in Power BI. Judge whether the migrated "
    "page communicates the same thing.\n"
    "Ignore: Tableau's own UI around the view (Columns/Rows/Marks/Filters panes, toolbars, sheet "
    "tabs), Power BI's filter pane, fonts, exact colour shades, gridlines, padding, and small "
    "differences in totals caused by filters. Do not ignore: a different chart type, missing or "
    "extra fields, a different date level, a missing colour split, a missing panel or visual.\n"
    "Return ONLY JSON, no prose, no code fences:\n"
    '{"criteria": {"<name>": {"match": true | false | null, "note": "<short reason>"}, ...}, '
    '"score": <integer 0-10, overall fidelity>, "summary": "<one sentence>"}\n'
    "Use match null when a criterion doesn't apply. Criteria:\n"
    + "\n".join(f"- {name}: {text}" for name, text in RUBRIC.items())
)


@dataclass
class VisualVerdict:
    sheet: str
    reference: str  # which Tableau screenshots were used (file names)
    score: int | None = None
    criteria: dict[str, dict[str, Any]] = field(default_factory=dict)
    summary: str = ""
    error: str | None = None

    @property
    def mismatches(self) -> list[str]:
        return [name for name, c in self.criteria.items() if c.get("match") is False]


def judge_sheet(
    chat_model: Any, sheet: SheetExpectation, tableau_images: Sequence[Path], powerbi_image: Path
) -> VisualVerdict:
    """Ask the chat model to compare one sheet's Tableau screenshots with its Power BI page."""

    from langchain_core.messages import HumanMessage, SystemMessage

    verdict = VisualVerdict(sheet=sheet.name, reference=", ".join(p.name for p in tableau_images))
    count = len(tableau_images)
    content: list[dict] = [{"type": "text", "text": f'Sheet "{sheet.name}" ({sheet.kind}).'}]
    for number, image in enumerate(tableau_images, 1):
        view = f" (view {number} of {count}: {image.name})" if count > 1 else ""
        content += [{"type": "text", "text": f"Image {number}: the Tableau original{view}."}, image_block(image)]
    content += [
        {"type": "text", "text": f"Image {count + 1}: the migrated Power BI page."},
        image_block(powerbi_image),
    ]
    try:
        response = chat_model.invoke([SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=content)])
    except Exception as exc:  # noqa: BLE001 - a failed judgement is reported, never raised
        verdict.error = provider_error_message(exc)
        return verdict
    return parse_verdict(verdict, message_text(response))


def parse_verdict(verdict: VisualVerdict, text: Any) -> VisualVerdict:
    """Fill ``verdict`` from the judge's JSON answer; sets ``error`` if it can't be read."""

    raw = text if isinstance(text, str) else ""
    start, end = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[start : end + 1]) if start != -1 and end > start else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        verdict.error = f"unreadable judgement: {raw[:200]!r}"
        return verdict
    criteria = data.get("criteria") if isinstance(data.get("criteria"), dict) else {}
    verdict.criteria = {
        name: {"match": c.get("match"), "note": str(c.get("note", ""))}
        for name, c in criteria.items()
        if name in RUBRIC and isinstance(c, dict)
    }
    score = data.get("score")
    verdict.score = max(0, min(10, int(score))) if isinstance(score, (int, float)) else None
    verdict.summary = str(data.get("summary", ""))
    return verdict


def image_block(path: Path) -> dict:
    """An image file as a chat-message content block (a data URI with the file's type)."""

    mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
