"""The report's PBIR files as the refinement agent may read and write them.

The agent edits the report the way Microsoft's report-authoring skill does: it reads a page's or a
visual's JSON, looks up what it needs (visual roles, formatting properties, literal encodings),
and writes the file back whole. After each save it gets the validators' errors and repairs them.
This module only finds files and keeps writes inside bounds:

- only ``page.json`` and ``visuals/<name>/visual.json`` under the report's ``definition/pages``
  folder, found by page display name and visual name (or title), never by a path the agent gives;
- the content must be a JSON object whose ``name`` is the file's own, so a page or visual can't be
  renamed, nor a write land on another file;
- a new visual gets a new folder on an existing page; pages aren't created or deleted.

Whether a change is kept is decided after the agent's turn by the review step, which commits or
reverts the workspace.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from t2pbi.fidelity.project import read_project
from t2pbi.fidelity.structural import describe

_VISUAL_NAME = re.compile(r"^[0-9A-Za-z_-]{1,50}$")


class ReportFileError(ValueError):
    """A read or write was refused; the message says why and what to do instead."""


class ReportFiles:
    """The page and visual files of one migrated project folder (the one holding ``<name>.pbip``)."""

    def __init__(self, project_dir: str | Path) -> None:
        self.root = Path(project_dir)
        reports = list(self.root.glob("*.Report"))
        if len(reports) != 1:
            raise ReportFileError(f"expected one .Report folder in {self.root}, found {len(reports)}")
        self.pages_dir = reports[0] / "definition" / "pages"

    # ---------------------------------------------------------------- reading

    def pages(self) -> list[dict]:
        """Every page with its visuals: names, types and titles."""

        found = []
        for page_json in sorted(self.pages_dir.glob("*/page.json")):
            page = _load(page_json)
            visuals = []
            for path in sorted(page_json.parent.glob("visuals/*/visual.json")):
                raw = _load(path)
                body = raw.get("visual", {}) if isinstance(raw.get("visual"), dict) else {}
                visuals.append({"name": raw.get("name"), "visualType": body.get("visualType"), "title": title_text(body)})
            found.append({
                "page": page.get("displayName"),
                "name": page.get("name"),
                "type": page.get("type", "Default"),
                "visibility": page.get("visibility", "AlwaysVisible"),
                "visuals": visuals,
            })
        return found

    def overview(self, page: str) -> dict:
        """What a page shows now, compactly: each visual's type, title, fields per role, sort."""

        page_json, page_raw = self._page(page)
        visuals = []
        for path in sorted(page_json.parent.glob("visuals/*/visual.json")):
            raw = _load(path)
            body = raw.get("visual", {}) if isinstance(raw.get("visual"), dict) else {}
            query = body.get("query", {})
            visuals.append({
                "name": raw.get("name"),
                "visual_type": body.get("visualType"),
                "title": title_text(body),
                "roles": {
                    role: [describe(p["field"]) for p in state.get("projections", []) if "field" in p]
                    for role, state in query.get("queryState", {}).items()
                },
                "sort": [f"{describe(s['field'])} {s.get('direction', 'Ascending').lower()}"
                         for s in query.get("sortDefinition", {}).get("sort", []) if "field" in s],
            })
        return {"page": page, "page_type": page_raw.get("type", "Default"), "visuals": visuals}

    def model_fields(self) -> dict[str, dict[str, list[str]]]:
        """Table -> its columns and measures: the names a field reference may use."""

        project = read_project(self.root)
        tables: dict[str, dict[str, list[str]]] = {
            t: {"columns": cols, "measures": []} for t, cols in project.columns.items()
        }
        for name, (table, _) in project.measures.items():
            tables.setdefault(table, {"columns": [], "measures": []})["measures"].append(name)
        return tables

    def read_page(self, page: str) -> str:
        path, _ = self._page(page)
        return path.read_text(encoding="utf-8")

    def read_visual(self, page: str, visual: str) -> str:
        return self._visual(page, visual).read_text(encoding="utf-8")

    # ---------------------------------------------------------------- writing

    def write_page(self, page: str, content: str) -> str:
        path, current = self._page(page)
        data = _parse(content)
        if data.get("name") != current.get("name"):
            raise ReportFileError(f"keep the page's name {current.get('name')!r}; pages can't be renamed")
        _write(path, data)
        return f"saved page {page!r}"

    def write_visual(self, page: str, visual: str, content: str) -> str:
        """Replace a visual's file, or create a visual when ``visual`` is a new name on the page."""

        data = _parse(content)
        try:
            path = self._visual(page, visual)
        except ReportFileError:
            if not _VISUAL_NAME.match(visual):
                raise ReportFileError(
                    f"no visual {visual!r} on page {page!r}; to create one, use a new name of letters, "
                    "digits, '-' or '_' (up to 50) and the same \"name\" in the JSON"
                ) from None
            page_json, _ = self._page(page)
            path = page_json.parent / "visuals" / visual / "visual.json"
            if data.get("name") != visual:
                raise ReportFileError(f'the new visual\'s JSON must have "name": {visual!r}')
            _write(path, data)
            return f"created visual {visual!r} on page {page!r}"
        current = _load(path)
        if data.get("name") != current.get("name"):
            raise ReportFileError(f"keep the visual's name {current.get('name')!r}; visuals can't be renamed")
        _write(path, data)
        return f"saved visual {current.get('name')!r} on page {page!r}"

    # ---------------------------------------------------------------- internals

    def _page(self, page: str) -> tuple[Path, dict]:
        for path in sorted(self.pages_dir.glob("*/page.json")):
            raw = _load(path)
            if raw.get("displayName") == page:
                return path, raw
        names = [p["page"] for p in self.pages()]
        raise ReportFileError(f"no page named {page!r}; pages are {names}")

    def _visual(self, page: str, visual: str) -> Path:
        page_json, _ = self._page(page)
        for path in sorted(page_json.parent.glob("visuals/*/visual.json")):
            raw = _load(path)
            body = raw.get("visual", {}) if isinstance(raw.get("visual"), dict) else {}
            if visual in (raw.get("name"), title_text(body)):
                return path
        known = [f"{v['name']} ({v['title']})" for p in self.pages() if p["page"] == page for v in p["visuals"]]
        raise ReportFileError(f"no visual {visual!r} on page {page!r}; its visuals are {known}")


def title_text(body: dict) -> str | None:
    """A visual's title text, from its title literal (``'Sales by Category'``)."""

    try:
        value = body["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"]
    except (KeyError, IndexError, TypeError):
        return None
    return value[1:-1].replace("''", "'") if len(value) >= 2 and value[0] == value[-1] == "'" else value


def _parse(content: str) -> dict:
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ReportFileError(f"not valid JSON ({exc}); nothing was saved") from None
    if not isinstance(data, dict):
        raise ReportFileError("the file must be one JSON object; nothing was saved")
    return data


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    """Indented, so each change is a readable git diff."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="")
