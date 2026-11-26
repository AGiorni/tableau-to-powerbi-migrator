"""Numeric fidelity: do the migrated visuals show the numbers Tableau shows?

For each sheet with reference ``values``, the field bound in the emitted visual that matches each
expected spec is turned into a DAX expression (``SUM('Orders'[Sales])``, ``'Orders'[profit
ratio]``), all of a sheet's expressions are evaluated in one query against the model running in
Power BI Desktop, and each result is compared with Tableau's number at the precision Tableau
displayed it ("29" means anything that rounds to 29). Needs Desktop with data loaded
(:mod:`t2pbi.desktop.session`); never runs in CI.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from t2pbi.fidelity.expectations import FieldSpec, Reference, SheetExpectation, WorkbookExpectations
from t2pbi.fidelity.project import Project
from t2pbi.fidelity.structural import CheckResult, describe, matches

# QueryAggregateFunction code -> DAX aggregation (5 is CountNonNull, Tableau's CNT).
DAX_AGGREGATIONS = {0: "SUM", 1: "AVERAGE", 2: "DISTINCTCOUNT", 3: "MIN", 4: "MAX", 5: "COUNTA", 6: "MEDIAN"}

Query = Callable[[str], Awaitable[list[dict[str, str]]]]


def dax_for(expr: dict) -> str | None:
    """A DAX scalar expression for one bound field, or None if it isn't a value (a plain column)."""

    if "Measure" in expr:
        node = expr["Measure"]
        return f"{_table(node)}[{_name(node.get('Property', ''))}]"
    if "Aggregation" in expr:
        agg = expr["Aggregation"]
        column = agg.get("Expression", {}).get("Column")
        function = DAX_AGGREGATIONS.get(agg.get("Function"))
        if column and function:
            return f"{function}({_table(column)}[{_name(column.get('Property', ''))}])"
    return None


def _table(node: dict) -> str:
    entity = node.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
    return "'" + entity.replace("'", "''") + "'"


def _name(name: str) -> str:
    return name.replace("]", "]]")


async def score_numeric(expected: WorkbookExpectations, project: Project, query: Query) -> list[CheckResult]:
    """Check every reference value of every sheet; one DAX query per sheet."""

    results: list[CheckResult] = []
    for sheet in expected.sheets:
        if not sheet.values:
            continue
        unknown = {k for k in sheet.known_gaps if k.startswith("value[")} - {
            f"value[{spec}]" for spec in sheet.values
        }
        if unknown:
            raise ValueError(f"sheet {sheet.name!r}: known_gaps name unknown checks {sorted(unknown)}")
        for check, passed, detail in await _sheet_values(sheet, project, query):
            results.append(CheckResult(sheet.name, check, passed, detail, sheet.known_gaps.get(check)))
    return results


async def _sheet_values(sheet: SheetExpectation, project: Project, query: Query):
    page = project.page(sheet.page_name)
    visual = next((v for v in page.visuals if not v.visual_type.endswith("licer")), None) if page else None
    bound = [f for fields in visual.roles.values() for f in fields] if visual else []

    plans: list[tuple[FieldSpec, str | None, str]] = []
    for spec in sheet.values:
        hit = next((f for f in bound if matches(spec, f, project)), None)
        if hit is None:
            plans.append((spec, None, f"not bound in the visual (bound: {[describe(f) for f in bound]})"))
        else:
            dax = dax_for(hit)
            plans.append((spec, dax, "" if dax else f"{describe(hit)} is not a value"))

    queried = [(i, dax) for i, (_, dax, _) in enumerate(plans) if dax]
    actual: dict[int, str] = {}
    error = ""
    if queried:
        columns = ", ".join(f'"v{i}", {dax}' for i, dax in queried)
        try:
            rows = await query(f"EVALUATE ROW({columns})")
            actual = {i: (rows[0].get(f"v{i}", "") if rows else "") for i, _ in queried}
        except Exception as exc:  # noqa: BLE001 - report the engine's error on every check
            error = " ".join(str(exc).split())[:300]

    outcomes = []
    for i, (spec, dax, why) in enumerate(plans):
        reference = Reference.parse(sheet.values[spec])
        check = f"value[{spec}]"
        if dax is None:
            outcomes.append((check, False, why))
        elif error:
            outcomes.append((check, False, f"query failed: {error}"))
        else:
            outcomes.append((check, *_compare(reference, actual.get(i, ""), dax)))
    return outcomes


def _compare(reference: Reference, raw: str, dax: str) -> tuple[bool, str]:
    if raw.strip() == "":
        return False, f"{dax} is BLANK; Tableau shows {reference.text}"
    try:
        value = float(raw)
    except ValueError:
        return False, f"{dax} returned {raw!r}; Tableau shows {reference.text}"
    ok = reference.accepts(value)
    return ok, f"{dax} = {value:,.6g}; Tableau shows {reference.text}"
