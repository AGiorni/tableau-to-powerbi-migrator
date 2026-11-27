"""The refinement agent's brief, tools and run (scripted model; no LLM, no Desktop)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from t2pbi.graph import MigrationContext, migrate
from t2pbi.ingest import load_workbook
from t2pbi.parse import parse_workbook
from t2pbi.refine.agent import build_brief, make_tools, run_agent, tableau_facts
from t2pbi.refine.edits import ReportEditor
from t2pbi.refine.worklist import Problem, ProblemSource, WorkItem

BOOK1 = Path(__file__).resolve().parents[1] / "samples" / "Book1" / "Book1.twb"
TOOLTIP = "Tooltip: Profit Ratio by City"


@pytest.fixture(scope="module")
def book1_project(tmp_path_factory):
    out = tmp_path_factory.mktemp("book1")
    migrate(BOOK1, out, MigrationContext(run_output_validation=False))
    return out


@pytest.fixture
def editor(book1_project, tmp_path):
    shutil.copytree(book1_project, tmp_path, dirs_exist_ok=True)
    return ReportEditor(tmp_path)


@pytest.fixture(scope="module")
def workbook():
    return parse_workbook(load_workbook(BOOK1))


def test_tableau_facts_speak_the_tools_field_specs(workbook):
    facts = tableau_facts(workbook, "Sales by Category")
    assert facts["mark"] == "Area"
    assert "Order Date:month" in facts["columns_shelf"]
    assert facts["encodings"]["color"] == "Order Profitable?"  # a dimension calc: a column
    tooltip = tableau_facts(workbook, TOOLTIP)
    assert tooltip["shown_in_tooltip_of"] == ["profit ratio by geography"]
    assert tooltip["sort"] == [{"field": "profit ratio:measure", "sorts": "City", "direction": "asc"}]
    dashboard = tableau_facts(workbook, "Executive Overview")
    assert "Key Performance Indicators" in dashboard["shows_sheets"]
    assert tableau_facts(workbook, "No such sheet")["note"].startswith("not found")


def test_the_brief_carries_problems_feedback_and_images(tmp_path):
    item = WorkItem(
        page="P",
        problems=[Problem(source=ProblemSource.DERIVED, check="title", detail="expected 'T'")],
        attempts=1,
        feedback=["now failing: P::sort"],
    )
    image = tmp_path / "ref.png"
    image.write_bytes(b"\x89PNG fake")
    content = build_brief(item, {"sheet": "P"}, {"page": "P"}, [("Image: the Tableau original.", image)])
    text = content[0]["text"]
    assert 'Fix the Power BI page "P".' in text
    assert "- [derived] title: expected 'T'" in text
    assert "refused by the review" in text and "now failing: P::sort" in text
    assert content[1]["text"] == "Image: the Tableau original."
    assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")


def test_tools_are_bound_to_one_page_and_refusals_come_back_as_text(editor):
    tools = {t.name: t for t in make_tools(editor, TOOLTIP, lambda: "checked")}
    assert set(tools) == {
        "describe_page", "list_model_fields", "allowed_roles", "set_visual_type", "bind_field",
        "remove_field", "set_title", "set_sort", "make_tooltip_page", "check_page",
    }
    # Arguments are typed in each tool's schema (what the model sees).
    assert set(tools["bind_field"].args) == {"role", "field", "visual"}
    refused = tools["bind_field"].invoke({"role": "Nope", "field": "Profit:sum"})
    assert refused.startswith("Refused:") and "roles are" in refused
    done = tools["set_title"].invoke({"title": "Profit Ratio by City"})
    assert "title set" in done
    assert editor.describe(TOOLTIP)["title"] == "Profit Ratio by City"
    assert tools["check_page"].invoke({}) == "checked"


def test_a_run_reports_its_summary_and_tool_calls(editor, scripted_agent_chat):
    chat = scripted_agent_chat(scripts=[[("describe_page", {}), ("set_title", {"title": "X"}), "CHANGED: set the title"]])
    run = run_agent(chat, make_tools(editor, TOOLTIP, lambda: ""), [{"type": "text", "text": "brief"}])
    assert (run.summary, run.tool_calls, run.error) == ("set the title", 2, None)


def test_a_run_stops_at_its_budget(editor, scripted_agent_chat):
    looping = scripted_agent_chat(scripts=[[("describe_page", {})]])  # never finishes
    run = run_agent(looping, make_tools(editor, TOOLTIP, lambda: ""), [{"type": "text", "text": "b"}], max_steps=3)
    assert run.error == "stopped after 3 model calls (budget)"


def test_a_provider_error_ends_the_run_without_raising(editor, scripted_agent_chat):
    class Failing(scripted_agent_chat):
        def _generate(self, *args, **kwargs):
            raise RuntimeError("Error code: 400 - content filter")

    run = run_agent(Failing(scripts=[]), make_tools(editor, TOOLTIP, lambda: ""), [{"type": "text", "text": "b"}])
    assert run.error and "400" in run.error


def test_several_tool_calls_in_one_message_all_land(editor, scripted_agent_chat):
    # The agent's tool node runs one message's calls on parallel threads; these three all rewrite
    # the same visual.json, so without the tools' lock they would read each other's partial writes.
    edits = [
        ("set_title", {"title": "Profit Ratio by City"}),
        ("set_sort", {"field": "profit ratio:measure", "direction": "ascending"}),
        ("bind_field", {"role": "Tooltips", "field": "Profit:sum"}),
    ]
    for _ in range(5):
        chat = scripted_agent_chat(scripts=[[edits, "CHANGED: three edits"]])
        run = run_agent(chat, make_tools(editor, TOOLTIP, lambda: ""), [{"type": "text", "text": "b"}])
        assert run.error is None and run.tool_calls == 3
        assert not any(r.startswith("Refused") or "Error" in r for r in chat.tool_results[:2])
    page = editor.describe(TOOLTIP)
    assert page["title"] == "Profit Ratio by City"
    assert page["sort"] == ["profit ratio:measure ascending"]
    assert page["roles"]["Tooltips"] == ["Profit:sum"]
