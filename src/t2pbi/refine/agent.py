"""The refinement agent: one page, one short conversation, typed tools.

For each work item the agent gets a fresh conversation: a fixed system prompt, then a brief with
the Tableau facts for the page (from the Tableau IR), what the Power BI page shows now, the
problems to fix, any reasons an earlier attempt was refused, and — when Desktop is running — the
Tableau reference image and the current Power BI screenshot. Its tools are the typed edits of
:mod:`t2pbi.refine.edits`, limited to its page. It proposes; the review step decides.
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
from t2pbi.mapping.visual_catalog import VISUAL_CATALOG
from t2pbi.refine.edits import EditError, ReportEditor
from t2pbi.refine.worklist import WorkItem

SYSTEM_PROMPT = """\
You repair one page of a Power BI report that was migrated automatically from Tableau, so that it
shows what the Tableau original shows. You work only through the tools.

How to work:
- Start with describe_page to see the page as it is now.
- Make the smallest changes that fix the listed problems. Don't restyle or rearrange anything else.
- After editing, call check_page to see which problems remain; continue until they're fixed or you
  can't fix them with these tools.
- A refused tool call explains why; adjust and try again. Don't repeat a refused call unchanged.
- Some problems need a change to the data model (a date-level column, a row-count measure); you
  can't make those yet — say so instead of working around them.

Fields are written as specs: "Category" (a column), "Sales:sum" (an aggregation: sum, avg, count,
countd, min, max, median), "profit ratio:measure" (a model measure). Role names must come from
describe_page's allowed_roles (or allowed_roles for another visual type) — never guess them.

Tableau → Power BI correspondences that often matter:
- Tableau stacks area marks by default: stackedAreaChart, not areaChart.
- A dimension on Tableau's Colour shelf splits the series: the "Series" role (Legend).
- Fields on Tableau's Tooltip shelf go in the "Tooltips" role.
- A sheet embedded in another sheet's tooltip ("viz in tooltip") is a hidden report-page tooltip:
  make_tooltip_page with the page that hovers it as host.
- The title Tableau displays is the visual's title; filter values Tableau inserts into it can be left out.
- Maps stay azureMap (Microsoft is retiring map and filledMap). Shading regions instead of
  bubbles is an Azure Maps layer setting these tools can't change yet: report it, don't swap types.

Finish with ONE line, which becomes the commit message:
"CHANGED: <what you changed>" or "NO CHANGE: <why nothing could be done>".
"""


@dataclass
class AgentRun:
    summary: str
    tool_calls: int = 0
    error: str | None = None


def tableau_facts(workbook: TableauWorkbook, sheet: str) -> dict[str, Any]:
    """What the Tableau sheet shows, as field specs the tools understand."""

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
            return {
                "dashboard": dash.name,
                "shows_sheets": [z.worksheet for z in dash.zones if z.kind == "worksheet"],
                "filter_cards": [spec(z.field) for z in dash.zones if z.kind == "filter" and z.field],
            }
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


def make_tools(editor: ReportEditor, page: str, run_checks: Callable[[], str]) -> list:
    """The agent's tools, bound to one page. Refusals come back as text the agent can act on.

    One lock serializes them: when the model asks for several tool calls in one message, the
    agent's tool node runs them on parallel threads, and two edits of the same visual.json would
    otherwise read each other's half-written file.
    """

    from langchain_core.tools import StructuredTool

    lock = threading.Lock()

    def guarded(fn: Callable[..., Any]) -> Callable[..., str]:
        @functools.wraps(fn)  # keeps the signature: the tool's argument schema comes from it
        def run(*args: Any, **kwargs: Any) -> str:
            with lock:
                try:
                    return str(fn(*args, **kwargs))
                except EditError as exc:
                    return f"Refused: {exc}"
        return run

    def describe_page(visual: str | None = None) -> str:
        """What the page shows now: visual type, title, fields per role, sort, allowed roles.
        On a dashboard page with several visuals, name one with `visual` (its title)."""
        return json.dumps(editor.describe(page, visual), ensure_ascii=False)

    def list_model_fields() -> str:
        """Every table's columns and measures in the semantic model."""
        return json.dumps(editor.model_fields(), ensure_ascii=False)

    def allowed_roles(visual_type: str) -> str:
        """The roles (field wells) a visual type accepts, and whether each takes columns
        (Grouping) or measures (Measure)."""
        spec = VISUAL_CATALOG.get(visual_type)
        if spec is None:
            return f"Unknown visual type {visual_type!r}."
        return json.dumps({"roles": spec.roles, "required": spec.required, "max_per_role": spec.max_per_role})

    def set_visual_type(visual_type: str, visual: str | None = None) -> str:
        """Change the visual's type (e.g. 'stackedAreaChart'). Fields in roles the new type
        lacks are removed and reported, so you can rebind them."""
        return editor.set_visual_type(page, visual_type, visual)

    def bind_field(role: str, field: str, visual: str | None = None) -> str:
        """Add a field (a spec such as 'Profit:sum') to a role (e.g. 'Tooltips')."""
        return editor.bind_field(page, role, field, visual)

    def remove_field(role: str, field: str, visual: str | None = None) -> str:
        """Remove a field (spec) from a role."""
        return editor.remove_field(page, role, field, visual)

    def set_title(title: str, visual: str | None = None) -> str:
        """Set the visual's title text."""
        return editor.set_title(page, title, visual)

    def set_sort(field: str, direction: str, visual: str | None = None) -> str:
        """Sort the visual by a field it shows; direction 'ascending' or 'descending'."""
        return editor.set_sort(page, field, direction, visual)

    def make_tooltip_page(host_page: str, host_visual: str | None = None) -> str:
        """Make THIS page a hidden report-page tooltip, shown when hovering the visual on
        `host_page` (name the visual with `host_visual` if that page has several)."""
        return editor.make_tooltip_page(page, host_page, host_visual)

    def check_page() -> str:
        """Re-run the checks for this page and list what's still wrong."""
        return run_checks()

    tools = [describe_page, list_model_fields, allowed_roles, set_visual_type, bind_field,
             remove_field, set_title, set_sort, make_tooltip_page, check_page]
    return [StructuredTool.from_function(guarded(fn), name=fn.__name__, description=fn.__doc__) for fn in tools]


def run_agent(chat_model: Any, tools: list, brief: list[dict[str, Any]], max_steps: int = 15) -> AgentRun:
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
        result = agent.invoke({"messages": [HumanMessage(content=brief)]})
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
