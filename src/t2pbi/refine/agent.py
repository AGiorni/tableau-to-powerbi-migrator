"""The refinement agent: one page, one short conversation, the report's own files.

For each work item the agent gets a fresh conversation: a fixed system prompt, then a brief with
the Tableau facts for the page (from the Tableau IR), what the Power BI page shows now, the
problems to fix, any reasons an earlier attempt was refused, and — when Desktop is running — the
Tableau reference image and the current Power BI screenshot.

It edits the report's PBIR JSON directly, as Microsoft's report-authoring skill does: it reads a
file, looks up visual roles, formatting properties and literal encodings with Microsoft's
``powerbi-report-author`` CLI, and saves the file whole (:mod:`t2pbi.refine.report_files` keeps
writes inside the report's page files). Every save is validated and the errors go back to it, so
it repairs its own mistakes; it proposes, and the review step decides.
"""

from __future__ import annotations

import functools
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from t2pbi.fidelity.derived import field_spec
from t2pbi.fidelity.visual import image_block
from t2pbi.ir.tableau_ir import TableauWorkbook, strip_brackets
from t2pbi.llm import message_text, provider_error_message
from t2pbi.refine.report_files import ReportFileError, ReportFiles
from t2pbi.refine.worklist import WorkItem
from t2pbi.validate.pbir_cli import run_report_author

SYSTEM_PROMPT = """\
You repair one page of a Power BI report that was migrated automatically from Tableau, so that it
shows what the Tableau original shows. The report is in PBIR format: one page.json per page and
one visual.json per visual. You read those files, change them, and save them whole.

How to work:
- Read before you write: read_visual (or read_page) gives you the file as it is now.
- Look things up instead of guessing: lookup_visual_type for a visual type's data roles (which
  take columns, "Grouping", or measures, "Measure"), lookup_formatting for formatting objects and
  properties, encode_literal for literal values, list_model_fields for the tables, columns and
  measures a field may reference.
- Make the smallest change that fixes the listed problems. Keep "$schema", "name" and
  "position" as they are unless the fix needs otherwise; don't restyle anything else.
- Every save is validated. Fix each error it reports, then call check_page to see which of the
  page's problems remain; continue until they're fixed or can't be fixed with these files.
- A refused call says why; adjust instead of repeating it.
- Some problems need a change to the data model (a date-level column, a row-count measure, a
  column for a dimension calculation); you can't make those yet: say so instead of working around them.

PBIR shapes, copied from files this migrator writes (follow the files you read):
- A field in a role: {"field": <field>, "queryRef": "...", "nativeQueryRef": "..."} in
  visual.query.queryState.<Role>.projections. A column: {"Column": {"Expression": {"SourceRef":
  {"Entity": "Orders"}}, "Property": "Region"}}, queryRef "Orders.Region", nativeQueryRef
  "Region". A measure: the same with "Measure" instead of "Column". A sum of a column:
  {"Aggregation": {"Expression": {"Column": ...}, "Function": 0}}, queryRef "Sum(Orders.Sales)",
  nativeQueryRef "Sum of Sales" (Function: 0 Sum, 1 Average, 2 Distinct count, 3 Min, 4 Max,
  5 Count, 6 Median).
- Sort: visual.query.sortDefinition = {"sort": [{"field": <field>, "direction": "Ascending"}],
  "isDefaultSort": false}.
- Title: visual.visualContainerObjects.title = [{"properties": {"show": <true literal>,
  "text": <string literal>}}]; literals come from encode_literal.

Tableau -> Power BI correspondences that often matter (the facts use specs: "Sales:sum" is a sum
of Sales, "profit ratio:measure" a model measure, "Order Date:month" a date by month):
- Tableau stacks area marks by default: stackedAreaChart, not areaChart.
- A dimension on Tableau's Colour shelf splits the series: the "Series" role (Legend).
- Fields on Tableau's Tooltip shelf go in the "Tooltips" role.
- A sheet embedded in another sheet's tooltip ("viz in tooltip") is a report-page tooltip: its
  page.json gets "type": "Tooltip", "visibility": "HiddenInViewMode", "displayOption":
  "ActualSize", width 320, height 240 (scale its visuals' positions to fit) and "pageBinding":
  {"name": <a new id>, "type": "Tooltip"}; the hovered visual on the host page gets
  visualContainerObjects.visualTooltip = [{"properties": {"show": <true literal>,
  "type": <'Canvas' literal>, "section": <literal of the tooltip page's "name">}}].
- The title Tableau displays is the visual's title; filter values Tableau inserts into it can be left out.
- Maps stay azureMap (Microsoft is retiring map and filledMap, and the validator refuses them).
- A sheet hidden in Tableau has no page of its own: it's a visual on its dashboard's page.
  Problems named "visual '<sheet>': ..." are about that visual.

Finish with ONE line, which becomes the commit message:
"CHANGED: <what you changed>" or "NO CHANGE: <why nothing could be done>".
"""


@dataclass
class AgentRun:
    summary: str
    tool_calls: int = 0
    error: str | None = None


def tableau_facts(workbook: TableauWorkbook, sheet: str) -> dict[str, Any]:
    """What the Tableau sheet shows, as field specs ("Sales:sum", "Order Date:month")."""

    calcs = {strip_brackets(c.name): c for ds in workbook.datasources for c in ds.calculated_fields}
    spec = lambda ref: str(field_spec(ref, calcs))
    for ws in workbook.worksheets:
        if ws.name != sheet:
            continue
        return {
            "sheet": ws.name,
            "mark": ws.mark_class,
            "columns_shelf": [spec(r) for r in ws.cols],
            "rows_shelf": [spec(r) for r in ws.rows],
            "encodings": {e.kind: spec(e.field) for e in ws.encodings},
            "measure_values": [spec(r) for r in ws.measure_names],
            "map": ws.is_map,
            "title": ws.title or ws.name,
            "title_inserts_filter_values_of": ws.title_fields,
            "sort": [
                {"field": spec(s.by or s.dimension), "sorts": spec(s.dimension), "direction": s.direction}
                for s in ws.sorts
            ],
            "shown_in_tooltip_of": [w.name for w in workbook.worksheets if ws.name in w.tooltip_sheets],
            "embeds_in_its_tooltip": ws.tooltip_sheets,
        }
    for dash in workbook.dashboards:
        if dash.name == sheet:
            shown = [z.worksheet for z in dash.zones if z.kind == "worksheet"]
            hidden = [ws.name for ws in workbook.worksheets if ws.hidden and ws.name in shown]
            facts: dict[str, Any] = {
                "dashboard": dash.name,
                "shows_sheets": shown,
                "filter_cards": [spec(z.field) for z in dash.zones if z.kind == "filter" and z.field],
            }
            if hidden:
                # Hidden in Tableau: they have no page of their own, so this page is the only
                # place their visuals are fixed.
                facts["hidden_sheets_shown_here"] = {name: tableau_facts(workbook, name) for name in hidden}
            return facts
    return {"sheet": sheet, "note": "not found in the Tableau workbook"}


def build_brief(
    item: WorkItem,
    facts: dict[str, Any],
    current: dict[str, Any],
    images: list[tuple[str, Path]],
) -> list[dict[str, Any]]:
    """The first user message: text, then labelled images."""

    lines = [
        f'Fix the Power BI page "{item.page}".',
        "",
        "Tableau original (facts from the workbook):",
        json.dumps(facts, indent=1, ensure_ascii=False),
        "",
        "Power BI page now:",
        json.dumps(current, indent=1, ensure_ascii=False),
        "",
        "Problems found:",
        *[f"- [{p.source.value}] {p.check}: {p.detail}" for p in item.problems],
    ]
    if item.feedback:
        lines += ["", "An earlier attempt on this page was refused by the review:", *[f"- {f}" for f in item.feedback]]
    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}]
    for label, path in images:
        content += [{"type": "text", "text": label}, image_block(path)]
    return content


# Lookup answers longer than this are cut (a formatting search can run to several KB).
_LOOKUP_LIMIT = 6000


def make_tools(
    files: ReportFiles,
    page: str,
    validate: Callable[[], list[str]],
    run_checks: Callable[[], str],
) -> list:
    """The agent's tools. Reads and writes default to its own page but may name another (a
    viz-in-tooltip link is set on the host page's visual). Every save is followed by
    ``validate``, whose errors are returned with the save. Refusals come back as text.

    One lock serializes the tools: when the model asks for several tool calls in one message, the
    agent's tool node runs them on parallel threads, and two saves of one file would otherwise
    interleave.
    """

    from langchain_core.tools import StructuredTool

    lock = threading.Lock()

    def guarded(fn: Callable[..., Any]) -> Callable[..., str]:
        @functools.wraps(fn)  # keeps the signature: the tool's argument schema comes from it
        def run(*args: Any, **kwargs: Any) -> str:
            with lock:
                try:
                    return str(fn(*args, **kwargs))
                except ReportFileError as exc:
                    return f"Refused: {exc}"
        return run

    def saved(message: str) -> str:
        errors = validate()
        message = message[:1].upper() + message[1:]
        if not errors:
            return f"{message}. Validation: no errors."
        listed = "\n".join(f"- {e}" for e in errors[:15])
        more = f"\n(and {len(errors) - 15} more)" if len(errors) > 15 else ""
        return f"{message}, but the report now has {len(errors)} error(s); fix them:\n{listed}{more}"

    def lookup(*cli_args: str) -> str:
        result = run_report_author(*cli_args)
        if result is None:
            return "Lookup unavailable: Microsoft's powerbi-report-author CLI isn't installed."
        text = json.dumps(result, ensure_ascii=False)
        return text if len(text) <= _LOOKUP_LIMIT else text[:_LOOKUP_LIMIT] + " …(cut; narrow the lookup)"

    def list_pages() -> str:
        """Every report page with its visuals: name (for the file tools), type and title."""
        return json.dumps(files.pages(), ensure_ascii=False)

    def read_page(page_name: str | None = None) -> str:
        """The page.json of your page (or of `page_name`, a page's display name)."""
        return files.read_page(page_name or page)

    def read_visual(visual: str, page_name: str | None = None) -> str:
        """A visual's visual.json; `visual` is its name, its title, or the Tableau sheet it shows.
        On your page unless `page_name` names another."""
        return files.read_visual(page_name or page, visual)

    def write_page(content: str, page_name: str | None = None) -> str:
        """Save a page's page.json: the whole file, as one JSON object (keep its "name").
        Validated after saving."""
        return saved(files.write_page(page_name or page, content))

    def write_visual(visual: str, content: str, page_name: str | None = None) -> str:
        """Save a visual's visual.json: the whole file, as one JSON object (keep its "name"). A
        new `visual` name creates a visual, with that name in its JSON. Validated after saving."""
        return saved(files.write_visual(page_name or page, visual, content))

    def list_model_fields() -> str:
        """Every table's columns and measures in the semantic model: the only names a field may use."""
        return json.dumps(files.model_fields(), ensure_ascii=False)

    def lookup_visual_type(visual_type: str) -> str:
        """A visual type's data roles (Grouping = columns, Measure = measures), required roles,
        limits and formatting objects. visual_type "list" lists every type."""
        if visual_type == "list":
            return lookup("catalog", "list")
        return lookup("catalog", "describe", visual_type)

    def lookup_formatting(visual_type: str, object_name: str | None = None, search: str | None = None) -> str:
        """Formatting for a visual type: its formatting objects; with `object_name`, that
        object's properties; with `search`, properties matching a regex (e.g. "label|color")."""
        if search:
            return lookup("formatting", "search", visual_type, search)
        if object_name:
            return lookup("formatting", "describe-object", visual_type, object_name)
        return lookup("formatting", "list-objects", visual_type)

    def encode_literal(value: str, kind: str) -> str:
        """A value as a PBIR literal expression; kind is bool, number, integer, string, color or
        themeColor (e.g. "Profit Ratio by City", "string")."""
        return lookup("expr", "encode", value, "--kind", kind)

    def check_page() -> str:
        """Re-run the checks for your page and the validators; list what's still wrong."""
        return run_checks()

    tools = [list_pages, read_page, read_visual, write_page, write_visual, list_model_fields,
             lookup_visual_type, lookup_formatting, encode_literal, check_page]
    return [StructuredTool.from_function(guarded(fn), name=fn.__name__, description=fn.__doc__) for fn in tools]


def run_agent(chat_model: Any, tools: list, brief: list[dict[str, Any]], max_steps: int = 25) -> AgentRun:
    """Run one agent conversation of at most ``max_steps`` model calls. Never raises: a failed or
    cut-off run is reported, and whatever it changed still goes through the review."""

    from langchain.agents import create_agent
    from langchain.agents.middleware import ModelCallLimitMiddleware
    from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
    from langchain_core.messages import AIMessage, HumanMessage

    agent = create_agent(
        chat_model,
        tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=[ModelCallLimitMiddleware(run_limit=max_steps, exit_behavior="error")],
    )
    try:
        # A model call, its middleware hooks and its tool calls are several graph steps: the budget,
        # not LangGraph's recursion limit, must be what stops a long run.
        result = agent.invoke({"messages": [HumanMessage(content=brief)]}, {"recursion_limit": 6 * max_steps + 10})
    except ModelCallLimitExceededError:
        return AgentRun(summary="", error=f"stopped after {max_steps} model calls (budget)")
    except Exception as exc:  # noqa: BLE001 - provider errors and the like end this item, not the run
        return AgentRun(summary="", error=provider_error_message(exc))
    messages = result.get("messages", [])
    calls = sum(len(getattr(m, "tool_calls", []) or []) for m in messages if isinstance(m, AIMessage))
    final = next((m for m in reversed(messages) if isinstance(m, AIMessage) and not m.tool_calls), None)
    text = message_text(final) if final is not None else ""
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    for prefix in ("CHANGED:", "NO CHANGE:"):
        if line.upper().startswith(prefix):
            line = line[len(prefix):].strip()
    return AgentRun(summary=line[:300], tool_calls=calls)
