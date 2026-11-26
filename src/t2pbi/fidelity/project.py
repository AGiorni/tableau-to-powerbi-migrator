"""Read an emitted Power BI project back from disk: report pages and visuals (PBIR) and measures (TMDL).

Fidelity is judged on the files the user receives, not on t2pbi's in-memory IR, so a bug in the
writers counts against the score.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_MEASURE_LINE = re.compile(r"^\tmeasure (?:'((?:[^']|'')+)'|(\S+)) =(.*)$")


@dataclass
class Visual:
    name: str
    visual_type: str
    title: str | None
    # Role -> the field expression of each projection (the PBIR "field" object).
    roles: dict[str, list[dict]] = field(default_factory=dict)
    # The visual's sort, in order: (field expression, "Ascending" | "Descending").
    sort: list[tuple[dict, str]] = field(default_factory=list)


@dataclass
class Page:
    name: str
    display_name: str
    page_type: str | None
    visibility: str | None
    visuals: list[Visual] = field(default_factory=list)


@dataclass
class Project:
    pages: list[Page]
    # Measure name -> (table, DAX expression), from the TMDL table files.
    measures: dict[str, tuple[str, str]]

    def page(self, display_name: str) -> Page | None:
        return next((p for p in self.pages if p.display_name == display_name), None)


def read_project(project_dir: str | Path) -> Project:
    """Read the ``*.Report`` and ``*.SemanticModel`` folders under ``project_dir``."""

    root = Path(project_dir)
    report = next(root.glob("*.Report"), None)
    model = next(root.glob("*.SemanticModel"), None)
    return Project(
        pages=_read_pages(report / "definition" / "pages") if report else [],
        measures=_read_measures(model / "definition" / "tables") if model else {},
    )


def _read_pages(pages_dir: Path) -> list[Page]:
    order = []
    pages_json = pages_dir / "pages.json"
    if pages_json.is_file():
        order = json.loads(pages_json.read_text(encoding="utf-8")).get("pageOrder", [])
    pages = []
    for page_json in pages_dir.glob("*/page.json"):
        raw = json.loads(page_json.read_text(encoding="utf-8"))
        visuals = [_read_visual(v) for v in sorted(page_json.parent.glob("visuals/*/visual.json"))]
        pages.append(
            Page(
                name=raw["name"],
                display_name=raw.get("displayName", raw["name"]),
                page_type=raw.get("type"),
                visibility=raw.get("visibility"),
                visuals=visuals,
            )
        )
    pages.sort(key=lambda p: order.index(p.name) if p.name in order else len(order))
    return pages


def _read_visual(path: Path) -> Visual:
    raw = json.loads(path.read_text(encoding="utf-8"))
    body = raw.get("visual", {})
    query = body.get("query", {})
    roles = {
        role: [p["field"] for p in state.get("projections", [])]
        for role, state in query.get("queryState", {}).items()
    }
    sort = [
        (s["field"], s.get("direction", "Ascending"))
        for s in query.get("sortDefinition", {}).get("sort", [])
        if "field" in s
    ]
    return Visual(
        name=raw.get("name", path.parent.name),
        visual_type=body.get("visualType", ""),
        title=_title(body),
        roles=roles,
        sort=sort,
    )


def _title(body: dict) -> str | None:
    """The visual's title text, from its title literal (``'Sales by Category'``)."""

    try:
        value = body["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"]
    except (KeyError, IndexError, TypeError):
        return None
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _read_measures(tables_dir: Path) -> dict[str, tuple[str, str]]:
    measures: dict[str, tuple[str, str]] = {}
    for tmdl in sorted(tables_dir.glob("*.tmdl")):
        lines = tmdl.read_text(encoding="utf-8").splitlines()
        table = tmdl.stem
        for i, line in enumerate(lines):
            match = _MEASURE_LINE.match(line)
            if not match:
                continue
            name = (match.group(1) or "").replace("''", "'") or match.group(2)
            expression = match.group(3).strip()
            if not expression:  # multi-line expression: the more-indented lines that follow
                body = []
                for following in lines[i + 1 :]:
                    if following.startswith("\t\t\t"):
                        body.append(following.strip())
                    elif following.strip():
                        break
                expression = " ".join(body)
            measures[name] = (table, expression)
    return measures
