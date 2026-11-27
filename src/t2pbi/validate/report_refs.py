"""Check a report against its own semantic model: what Microsoft's PBIR validator can't.

``powerbi-report-author validate`` checks a report's files against the PBIR schemas and the
visual catalog, but never opens the semantic model: a visual bound to ``Orders[Proffit]``
validates, then shows an error box in Desktop. This walks every page and visual file and checks:

- every field reference (``Column``, ``Measure``, and the column under a date hierarchy) names a
  table in the model and a column or measure of that table, suggesting the nearest names when
  it doesn't;
- every visual type is in the visual catalog. The validator only warns about an unknown type,
  since it could be a custom visual; t2pbi writes none, so for us it's a typo;
- no visual uses a type Microsoft is retiring (the Bing-based Map and Filled map).

Messages are written for whoever fixes the file, a person or the refinement agent: where, what's
wrong, and what exists instead.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from t2pbi.fidelity.project import read_project
from t2pbi.mapping.visual_catalog import VISUAL_CATALOG

# Visual types in the catalog that Microsoft is retiring, with what to use instead. The Bing-based
# Map and Filled map are scheduled for deprecation in favour of Azure Maps, which draws filled
# (choropleth) regions as a layer (learn.microsoft.com, "Convert Map and Filled map visuals to an
# Azure Maps visual").
RETIRING_VISUALS = {
    "map": "azureMap",
    "filledMap": "azureMap (its filled-map layer draws shaded regions)",
}


@dataclass(frozen=True)
class RefProblem:
    where: str  # "page 'Sales by Category', visual 'Sales by Category'"
    message: str
    file: Path

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


def check_report_references(project_dir: str | Path) -> list[RefProblem]:
    """Every reference and visual-type problem in the report under ``project_dir``."""

    root = Path(project_dir)
    report = next(root.glob("*.Report"), None)
    if report is None:
        return []
    project = read_project(root)
    columns = project.columns
    measures: dict[str, list[str]] = {}
    for name, (table, _) in project.measures.items():
        measures.setdefault(table, []).append(name)
    tables = sorted(set(columns) | set(measures))

    problems: list[RefProblem] = []
    for page_json in sorted((report / "definition" / "pages").glob("*/page.json")):
        page = _load(page_json)
        page_name = str(page.get("displayName") or page_json.parent.name) if page else page_json.parent.name
        files = [(page_json, f"page '{page_name}'")]
        for visual_json in sorted(page_json.parent.glob("visuals/*/visual.json")):
            visual = _load(visual_json)
            label = _title(visual) or (visual or {}).get("name") or visual_json.parent.name
            files.append((visual_json, f"page '{page_name}', visual '{label}'"))
        for path, where in files:
            data = _load(path)
            if data is None:
                problems.append(RefProblem(where, "the file isn't valid JSON", path))
                continue
            seen: set[str] = set()
            for message in _check_file(data, tables, columns, measures):
                if message not in seen:
                    seen.add(message)
                    problems.append(RefProblem(where, message, path))
    return problems


def _check_file(data: dict, tables, columns, measures) -> Iterator[str]:
    visual_type = (data.get("visual") or {}).get("visualType") if isinstance(data.get("visual"), dict) else None
    if visual_type in RETIRING_VISUALS:
        yield f"visual type {visual_type!r} is being retired by Microsoft; use {RETIRING_VISUALS[visual_type]}"
    elif visual_type is not None and visual_type not in VISUAL_CATALOG:
        yield f"unknown visual type {visual_type!r}{_suggest(visual_type, VISUAL_CATALOG)}"

    aliases = {
        item["Name"]: item["Entity"]
        for node in _nodes(data)
        for item in (node.get("From") if isinstance(node.get("From"), list) else [])
        if isinstance(item, dict) and "Name" in item and "Entity" in item
    }
    for node in _nodes(data):
        for key, kind in (("Column", "column"), ("Measure", "measure"), ("PropertyVariationSource", "column")):
            ref = node.get(key)
            if isinstance(ref, dict) and isinstance(ref.get("Property"), str):
                yield from _check_ref(kind, _entity(ref, aliases), ref["Property"], tables, columns, measures)


def _check_ref(kind: str, table: str | None, name: str, tables, columns, measures) -> Iterator[str]:
    if table is None:
        return  # an alias we can't resolve: leave it to Desktop
    if table not in tables:
        yield f"table {table!r} isn't in the model{_suggest(table, tables)}"
        return
    table_columns, table_measures = columns.get(table, []), measures.get(table, [])
    wanted, other = (table_columns, table_measures) if kind == "column" else (table_measures, table_columns)
    if name in wanted:
        return
    ref = f"'{table}'[{name}]"
    if name in other:
        actual = "a measure" if kind == "column" else "a column"
        shape = '{"Measure": ...}' if kind == "column" else '{"Column": ...} (or an Aggregation of it)'
        yield f"{ref} is {actual} of {table}, not {'a column' if kind == 'column' else 'a measure'}: reference it as {shape}"
        return
    yield f"{ref} isn't {'a column' if kind == 'column' else 'a measure'} of {table}{_suggest(name, wanted)}"


def _entity(ref: dict, aliases: dict[str, str]) -> str | None:
    source = (ref.get("Expression") or {}).get("SourceRef") or {}
    return source.get("Entity") or aliases.get(source.get("Source"))


def _nodes(value) -> Iterator[dict]:
    """Every dict nested anywhere in ``value``, ``value`` included."""

    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


def _suggest(name: str, options) -> str:
    options = sorted(options)
    close = difflib.get_close_matches(name, options, n=3, cutoff=0.6)
    if close:
        return "; did you mean " + " or ".join(repr(c) for c in close) + "?"
    shown = options[:12]
    return f"; it has {shown}{' …' if len(options) > len(shown) else ''}" if options else ""


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _title(data: dict | None) -> str | None:
    try:
        value = data["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"]
    except (KeyError, IndexError, TypeError):
        return None
    return value[1:-1].replace("''", "'") if len(value) >= 2 and value[0] == value[-1] == "'" else value
