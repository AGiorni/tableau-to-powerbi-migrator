"""Typed edits to a migrated report's PBIR files: the refinement agent's hands.

Each edit finds its page (by display name) and visual, checks its arguments — visual types and
role names against the visual catalog, fields against the semantic model, role kinds (grouping vs
measure) and capacities — and writes the JSON the way t2pbi's own writer does (``emit.pbir_writer``
builds every projection and literal). A bad request raises :class:`EditError` with a message meant
for the agent: what was wrong and what is allowed instead.

Edits change files only. Whether a change is kept is decided after the agent's turn by the review
step (validators, derived checks, the visual judge): it commits or reverts the workspace.

Not yet: date levels (``Order Date:month``) and row counts (``Orders:rowcount``) need a model
change first — a column or measure — which comes with the model-side tools (step 4.5).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from t2pbi.emit.pbir_writer import (
    field_expression,
    literal,
    projection,
    string_literal,
    title_object,
)
from t2pbi.fidelity.expectations import AGGREGATIONS, DATE_LEVELS, FieldSpec
from t2pbi.fidelity.project import read_project
from t2pbi.fidelity.structural import describe, matches
from t2pbi.ir.powerbi_ir import FieldKind, PBIFieldRef
from t2pbi.mapping.visual_catalog import VISUAL_CATALOG

# Power BI Desktop's size for a report-page tooltip.
_TOOLTIP_SIZE = (320, 240)


class EditError(ValueError):
    """The edit was refused; the message says why and what to do instead."""


@dataclass
class EditResult:
    message: str
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.message + "".join(f"\nWarning: {w}" for w in self.warnings)


@dataclass
class _Target:
    page_dir: Path
    page: dict
    visual_path: Path
    visual: dict


class ReportEditor:
    """Edits one migrated project folder (the one holding ``<name>.pbip``)."""

    def __init__(self, project_dir: str | Path) -> None:
        self.root = Path(project_dir)
        reports = list(self.root.glob("*.Report"))
        if len(reports) != 1:
            raise EditError(f"expected one .Report folder in {self.root}, found {len(reports)}")
        self.pages_dir = reports[0] / "definition" / "pages"

    # ---------------------------------------------------------------- reading

    def page_names(self) -> list[str]:
        return [json.loads(p.read_text(encoding="utf-8")).get("displayName", p.parent.name)
                for p in sorted(self.pages_dir.glob("*/page.json"))]

    def describe(self, page: str, visual: str | None = None) -> dict:
        """What a page shows now: type, title, fields per role, sort, tooltip settings."""

        t = self._target(page, visual)
        body = t.visual.get("visual", {})
        query = body.get("query", {})
        roles = {
            role: [describe(p["field"]) for p in state.get("projections", [])]
            for role, state in query.get("queryState", {}).items()
        }
        sort = [f"{describe(s['field'])} {s.get('direction', 'Ascending').lower()}"
                for s in query.get("sortDefinition", {}).get("sort", [])]
        title = _title_text(body)
        return {
            "page": page,
            "page_type": t.page.get("type"),
            "visual": t.visual.get("name"),
            "visual_type": body.get("visualType"),
            "title": title,
            "roles": roles,
            "allowed_roles": VISUAL_CATALOG[body["visualType"]].roles if body.get("visualType") in VISUAL_CATALOG else {},
            "sort": sort,
        }

    def model_fields(self) -> dict[str, dict[str, list[str]]]:
        """Table -> its columns and measures, as the agent may bind them."""

        project = read_project(self.root)
        tables: dict[str, dict[str, list[str]]] = {t: {"columns": cols, "measures": []} for t, cols in project.columns.items()}
        for name, (table, _) in project.measures.items():
            tables.setdefault(table, {"columns": [], "measures": []})["measures"].append(name)
        return tables

    # ---------------------------------------------------------------- edits

    def set_visual_type(self, page: str, visual_type: str, visual: str | None = None) -> EditResult:
        spec = VISUAL_CATALOG.get(visual_type)
        if spec is None:
            raise EditError(f"unknown visual type {visual_type!r}; it must be a type in the visual catalog")
        t = self._target(page, visual)
        body = t.visual["visual"]
        old = body.get("visualType")
        warnings = []
        state = body.get("query", {}).get("queryState", {})
        for role in list(state):
            if role not in spec.roles:
                fields = [describe(p["field"]) for p in state[role]["projections"]]
                warnings.append(f"role {role!r} doesn't exist on {visual_type}; removed {fields} — rebind them")
                del state[role]
        body["visualType"] = visual_type
        missing = [r for r in spec.required if r not in state]
        if missing:
            warnings.append(f"{visual_type} needs role(s) {missing}; bind fields to them")
        self._write(t.visual_path, t.visual)
        return EditResult(f"{page}: visual type {old} -> {visual_type}", warnings)

    def bind_field(self, page: str, role: str, field_spec: str, visual: str | None = None) -> EditResult:
        """Add a field to a role, e.g. role "Tooltips", field "Profit:sum"."""

        t = self._target(page, visual)
        body = t.visual["visual"]
        vt = body.get("visualType")
        spec = VISUAL_CATALOG.get(vt)
        if spec is None:
            raise EditError(f"visual type {vt!r} isn't in the catalog; can't check roles")
        if role not in spec.roles:
            raise EditError(f"{vt} has no role {role!r}; its roles are {sorted(spec.roles)}")
        ref = self._field_ref(FieldSpec.parse(field_spec))
        kind = spec.roles[role]
        if kind == "Grouping" and ref.kind is not FieldKind.COLUMN:
            raise EditError(f"role {role!r} on {vt} takes columns (categories), not {field_spec!r}")
        if kind == "Measure" and ref.kind is FieldKind.COLUMN:
            raise EditError(f"role {role!r} on {vt} takes measures or aggregations (e.g. 'Sales:sum'), not a plain column")
        state = body.setdefault("query", {}).setdefault("queryState", {})
        projections = state.setdefault(role, {"projections": []})["projections"]
        expr = field_expression(ref)
        if any(p["field"] == expr for p in projections):
            raise EditError(f"{field_spec!r} is already in role {role!r}")
        cap = spec.max_per_role.get(role)
        if cap is not None and len(projections) >= cap:
            raise EditError(f"role {role!r} on {vt} takes at most {cap} field(s); remove one first")
        projections.append(projection(ref, active=(role == "Category" and not projections)))
        self._write(t.visual_path, t.visual)
        return EditResult(f"{page}: bound {field_spec} to {role}")

    def remove_field(self, page: str, role: str, field_spec: str, visual: str | None = None) -> EditResult:
        t = self._target(page, visual)
        state = t.visual["visual"].get("query", {}).get("queryState", {})
        projections = state.get(role, {}).get("projections", [])
        spec = FieldSpec.parse(field_spec)
        hit = next((p for p in projections if matches(spec, p["field"], None)), None)
        if hit is None:
            bound = [describe(p["field"]) for p in projections]
            raise EditError(f"{field_spec!r} isn't in role {role!r} (bound: {bound})")
        projections.remove(hit)
        if not projections:
            del state[role]
        warnings = []
        sort = t.visual["visual"].get("query", {}).get("sortDefinition", {}).get("sort", [])
        if any(s.get("field") == hit["field"] for s in sort):
            t.visual["visual"]["query"].pop("sortDefinition", None)
            warnings.append("the visual was sorted by that field; its sort was removed")
        self._write(t.visual_path, t.visual)
        return EditResult(f"{page}: removed {field_spec} from {role}", warnings)

    def set_title(self, page: str, title: str, visual: str | None = None) -> EditResult:
        if not title.strip():
            raise EditError("a title can't be empty")
        t = self._target(page, visual)
        body = t.visual["visual"]
        body.setdefault("visualContainerObjects", {})["title"] = title_object(title)
        self._write(t.visual_path, t.visual)
        return EditResult(f"{page}: title set to {title!r}")

    def set_sort(self, page: str, field_spec: str, direction: str, visual: str | None = None) -> EditResult:
        """Sort the visual by a field it shows, ``ascending`` or ``descending``."""

        if direction.lower() not in ("ascending", "descending"):
            raise EditError("direction must be 'ascending' or 'descending'")
        t = self._target(page, visual)
        query = t.visual["visual"].setdefault("query", {})
        bound = [p["field"] for state in query.get("queryState", {}).values() for p in state.get("projections", [])]
        spec = FieldSpec.parse(field_spec)
        hit = next((f for f in bound if matches(spec, f, None)), None)
        if hit is None:
            raise EditError(f"the visual doesn't show {field_spec!r}; sort by one of {[describe(f) for f in bound]}")
        query["sortDefinition"] = {
            "sort": [{"field": hit, "direction": direction.capitalize()}],
            "isDefaultSort": False,
        }
        self._write(t.visual_path, t.visual)
        return EditResult(f"{page}: sorted by {field_spec} {direction.lower()}")

    def make_tooltip_page(self, page: str, host_page: str, host_visual: str | None = None) -> EditResult:
        """Turn ``page`` into a hidden report-page tooltip shown when hovering the host visual."""

        tip = self._target(page, None, allow_any=True)
        if tip.page_dir == self._target(host_page, host_visual).page_dir:
            raise EditError("a page can't be its own tooltip")
        width, height = _TOOLTIP_SIZE
        old_w, old_h = tip.page.get("width", 1280) or 1280, tip.page.get("height", 720) or 720
        tip.page.update({
            "type": "Tooltip",
            "visibility": "HiddenInViewMode",
            "displayOption": "ActualSize",
            "width": width,
            "height": height,
            "pageBinding": {"name": uuid.uuid4().hex[:20], "type": "Tooltip"},
        })
        self._write(tip.page_dir / "page.json", tip.page)
        # Scale the page's visuals to the tooltip canvas.
        for path in tip.page_dir.glob("visuals/*/visual.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            pos = raw.get("position", {})
            for key, scale in (("x", width / old_w), ("width", width / old_w), ("y", height / old_h), ("height", height / old_h)):
                if key in pos:
                    pos[key] = round(pos[key] * scale, 2)
            self._write(path, raw)

        host = self._target(host_page, host_visual)
        host.visual["visual"].setdefault("visualContainerObjects", {})["visualTooltip"] = [{
            "properties": {
                "show": literal("true"),
                "type": string_literal("Canvas"),
                "section": string_literal(tip.page["name"]),
            }
        }]
        self._write(host.visual_path, host.visual)
        return EditResult(f"{page} is now a hidden tooltip page, shown on hover over {host_page}")

    # ---------------------------------------------------------------- internals

    def _target(self, page: str, visual: str | None, allow_any: bool = False) -> _Target:
        page_json = None
        for path in sorted(self.pages_dir.glob("*/page.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("displayName") == page:
                page_json, page_raw = path, raw
                break
        if page_json is None:
            raise EditError(f"no page named {page!r}; pages are {self.page_names()}")
        candidates = []
        for path in sorted(page_json.parent.glob("visuals/*/visual.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if visual is not None:
                if visual in (raw.get("name"), _title_text(raw.get("visual", {}))):
                    candidates = [(path, raw)]
                    break
            elif not raw.get("visual", {}).get("visualType", "").endswith("licer"):
                candidates.append((path, raw))
        if not candidates and not allow_any:
            raise EditError(f"no visual {visual!r} on page {page!r}" if visual else f"page {page!r} has no chart visual")
        if len(candidates) > 1:
            titles = [_title_text(raw.get("visual", {})) or raw.get("name") for _, raw in candidates]
            raise EditError(f"page {page!r} has several visuals; name one with `visual`: {titles}")
        path, raw = candidates[0] if candidates else (page_json, {})
        return _Target(page_dir=page_json.parent, page=page_raw, visual_path=path, visual=raw)

    def _field_ref(self, spec: FieldSpec) -> PBIFieldRef:
        project = read_project(self.root)
        if spec.qualifier in DATE_LEVELS or spec.qualifier == "rowcount":
            raise EditError(
                f"{spec} needs a model change first (a date-level column or a row-count measure); "
                "not available yet"
            )
        if spec.qualifier == "measure":
            if spec.name not in project.measures:
                raise EditError(f"no measure {spec.name!r}; measures are {sorted(project.measures)}")
            return PBIFieldRef(entity=project.measures[spec.name][0], property=spec.name, kind=FieldKind.MEASURE)
        tables = [t for t, cols in project.columns.items() if spec.name in cols]
        if not tables:
            known = sorted({c for cols in project.columns.values() for c in cols})
            raise EditError(f"no column {spec.name!r}; known columns: {known}")
        if len(tables) > 1:
            raise EditError(f"column {spec.name!r} is ambiguous: it's in tables {tables}")
        if spec.qualifier is None:
            return PBIFieldRef(entity=tables[0], property=spec.name, kind=FieldKind.COLUMN)
        return PBIFieldRef(entity=tables[0], property=spec.name, kind=FieldKind.AGGREGATION,
                           function=AGGREGATIONS[spec.qualifier])

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="")


def _title_text(body: dict) -> str | None:
    try:
        value = body["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"]
    except (KeyError, IndexError, TypeError):
        return None
    return value[1:-1].replace("''", "'") if len(value) >= 2 and value[0] == value[-1] == "'" else value
