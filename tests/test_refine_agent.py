"""The refinement agent's brief, tools and run (scripted model; no LLM, no Desktop)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from t2pbi.graph import MigrationContext, migrate
from t2pbi.ingest import load_workbook
from t2pbi.parse import parse_workbook
from t2pbi.refine import agent
from t2pbi.refine.agent import build_brief, make_tools, run_agent, tableau_facts
from t2pbi.refine.report_files import ReportFiles
from t2pbi.refine.worklist import Problem, ProblemSource, WorkItem

BOOK1 = Path(__file__).resolve().parents[1] / "samples" / "Book1" / "Book1.twb"
TOOLTIP = "Tooltip: Profit Ratio by City"
DASHBOARD = "Executive Overview"


@pytest.fixture(scope="module")
def book1_project(tmp_path_factory):
    out = tmp_path_factory.mktemp("book1")
    migrate(BOOK1, out, MigrationContext(run_output_validation=False))
    return out


@pytest.fixture(scope="module")
def workbook():
    return parse_workbook(load_workbook(BOOK1))


@pytest.fixture
def files(book1_project, tmp_path, workbook):
    shutil.copytree(book1_project, tmp_path, dirs_exist_ok=True)
    # As fix_item does: a visual can be named by the sheet it shows (its title is the sheet's own).
    return ReportFiles(tmp_path, {ws.name: ws.title or ws.name for ws in workbook.worksheets})


def _tools(files, page=TOOLTIP, errors=()):
    return {t.name: t for t in make_tools(files, page, lambda: list(errors), lambda: "checked")}


def _retitled(text: str, title: str) -> str:
    """A visual.json with its title replaced, as an agent would write it back."""

    data = json.loads(text)
    data["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"] = f"'{title}'"
    return json.dumps(data)


def test_tableau_facts_speak_field_specs(workbook):
    facts = tableau_facts(workbook, "Sales by Category")
    assert facts["mark"] == "Area"
    assert "Order Date:month" in facts["columns_shelf"]
    assert facts["encodings"]["color"] == "Order Profitable?"  # a dimension calc: a column
    tooltip = tableau_facts(workbook, TOOLTIP)
    assert tooltip["shown_in_tooltip_of"] == ["profit ratio by geography"]
    assert tooltip["sort"] == [{"field": "profit ratio:measure", "sorts": "City", "direction": "asc"}]
    dashboard = tableau_facts(workbook, DASHBOARD)
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


def test_the_agent_reads_and_writes_the_reports_own_files(files):
    tools = _tools(files)
    assert set(tools) == {
        "list_pages", "read_page", "read_visual", "write_page", "write_visual", "list_model_fields",
        "lookup_visual_type", "lookup_formatting", "encode_literal", "check_page",
    }
    assert set(tools["write_visual"].args) == {"visual", "content", "page_name"}  # what the model sees

    pages = json.loads(tools["list_pages"].invoke({}))
    assert {p["page"] for p in pages} >= {TOOLTIP, DASHBOARD}
    original = tools["read_visual"].invoke({"visual": TOOLTIP})  # by the sheet it shows
    assert tools["read_visual"].invoke({"visual": "Profit Ratio by City"}) == original  # by its title
    saved = tools["write_visual"].invoke({"visual": TOOLTIP, "content": _retitled(original, "Profit ratio, by city")})
    assert saved == f"Saved visual {json.loads(original)['name']!r} on page {TOOLTIP!r}. Validation: no errors."
    assert files.overview(TOOLTIP)["visuals"][0]["title"] == "Profit ratio, by city"
    # On the dashboard, the KPI table shows its custom title; the overview says which sheet it is.
    kpi = next(v for v in files.overview(DASHBOARD)["visuals"] if v["title"] == "Executive Overview - Profitability")
    assert kpi["sheet"] == "Key Performance Indicators"
    assert "Profit" in json.loads(tools["list_model_fields"].invoke({}))["Orders"]["columns"]
    assert tools["check_page"].invoke({}) == "checked"


def test_a_save_reports_the_validators_errors(files):
    tools = _tools(files, errors=["REFERENCE page 'P', visual 'V': 'Orders'[Proffit] isn't a column of Orders"])
    original = tools["read_visual"].invoke({"visual": TOOLTIP})
    result = tools["write_visual"].invoke({"visual": TOOLTIP, "content": original})
    assert "the report now has 1 error(s); fix them" in result and "Proffit" in result


def test_other_pages_can_be_read_and_written_by_name(files):
    # A viz-in-tooltip link is set on the host page's visual, not on the tooltip page itself.
    tools = _tools(files)
    host = json.loads(tools["read_visual"].invoke({"visual": "profit ratio by geography", "page_name": "profit ratio by geography"}))
    host["visual"].setdefault("visualContainerObjects", {})["visualTooltip"] = [{"properties": {}}]
    result = tools["write_visual"].invoke({
        "visual": host["name"], "content": json.dumps(host), "page_name": "profit ratio by geography",
    })
    assert result.startswith("Saved visual")
    page = json.loads(tools["read_page"].invoke({}))
    page.update({"type": "Tooltip", "visibility": "HiddenInViewMode"})
    assert tools["write_page"].invoke({"content": json.dumps(page)}).startswith("Saved page")
    assert {p["page"]: p["type"] for p in files.pages()}[TOOLTIP] == "Tooltip"


def test_a_new_name_creates_a_visual_on_the_page(files):
    tools = _tools(files)
    template = json.loads(tools["read_visual"].invoke({"visual": TOOLTIP}))
    template["name"] = "subtitleBox01"
    result = tools["write_visual"].invoke({"visual": "subtitleBox01", "content": json.dumps(template)})
    assert result.startswith("Created visual 'subtitleBox01'")
    assert "subtitleBox01" in {v["name"] for p in files.pages() if p["page"] == TOOLTIP for v in p["visuals"]}


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda t, v: t["write_visual"].invoke({"visual": TOOLTIP, "content": "{not json"}), "not valid JSON"),
        (lambda t, v: t["write_visual"].invoke({"visual": TOOLTIP, "content": "[1, 2]"}), "one JSON object"),
        (lambda t, v: t["write_visual"].invoke({"visual": TOOLTIP, "content": json.dumps({**v, "name": "other"})}),
         "can't be renamed"),
        (lambda t, v: t["write_visual"].invoke({"visual": "../../escape", "content": json.dumps(v)}), "new name of letters"),
        (lambda t, v: t["write_visual"].invoke({"visual": "newOne", "content": json.dumps(v)}), 'must have "name"'),
        (lambda t, v: t["read_visual"].invoke({"visual": "x", "page_name": "Nowhere"}), "no page named"),
        (lambda t, v: t["write_page"].invoke({"content": json.dumps({"name": "renamed"})}), "can't be renamed"),
    ],
)
def test_writes_stay_inside_the_reports_files(files, call, message):
    tools = _tools(files)
    visual = json.loads(tools["read_visual"].invoke({"visual": TOOLTIP}))
    before = sorted(p.as_posix() for p in files.root.rglob("*.json"))
    result = call(tools, visual)
    assert result.startswith("Refused:") and message in result
    assert sorted(p.as_posix() for p in files.root.rglob("*.json")) == before  # nothing new written


def test_lookups_say_when_microsofts_cli_is_missing(files, monkeypatch):
    monkeypatch.setattr(agent, "run_report_author", lambda *args: None)
    assert _tools(files)["lookup_visual_type"].invoke({"visual_type": "areaChart"}).startswith("Lookup unavailable")


@pytest.mark.external("powerbi-report-author")
def test_lookups_answer_from_microsofts_cli(files):
    tools = _tools(files)
    roles = json.loads(tools["lookup_visual_type"].invoke({"visual_type": "stackedAreaChart"}))["roles"]
    assert roles["Tooltips"]["kind"] == "Measure"
    literal = json.loads(tools["encode_literal"].invoke({"value": "Profit Ratio by City", "kind": "string"}))
    assert "'Profit Ratio by City'" in json.dumps(literal)
    assert "legend" in tools["lookup_formatting"].invoke({"visual_type": "areaChart"})


def test_a_run_reports_its_summary_and_tool_calls(files, scripted_agent_chat):
    chat = scripted_agent_chat(scripts=[[
        ("read_visual", {"visual": TOOLTIP}),
        ("write_visual", lambda results: {"visual": TOOLTIP, "content": _retitled(results[-1], "X")}),
        "CHANGED: set the title",
    ]])
    run = run_agent(chat, list(_tools(files).values()), [{"type": "text", "text": "brief"}])
    assert (run.summary, run.tool_calls, run.error) == ("set the title", 2, None)
    assert files.overview(TOOLTIP)["visuals"][0]["title"] == "X"


def test_a_run_stops_at_its_budget(files, scripted_agent_chat):
    looping = scripted_agent_chat(scripts=[[("list_pages", {})]])  # never finishes
    run = run_agent(looping, list(_tools(files).values()), [{"type": "text", "text": "b"}], max_steps=3)
    assert run.error == "stopped after 3 model calls (budget)"


def test_a_provider_error_ends_the_run_without_raising(files, scripted_agent_chat):
    class Failing(scripted_agent_chat):
        def _generate(self, *args, **kwargs):
            raise RuntimeError("Error code: 400 - content filter")

    run = run_agent(Failing(scripts=[]), list(_tools(files).values()), [{"type": "text", "text": "b"}])
    assert run.error and "400" in run.error


def test_several_saves_in_one_message_all_land(files, scripted_agent_chat):
    # The agent's tool node runs one message's calls on parallel threads; the tools' lock keeps
    # each read-and-save whole.
    visuals = [v for p in files.pages() if p["page"] == DASHBOARD for v in p["visuals"] if v["visualType"] != "slicer"]
    texts = {v["name"]: files.read_visual(DASHBOARD, v["name"]) for v in visuals}
    saves = [("write_visual", {"visual": name, "content": _retitled(text, f"T{i}"), "page_name": DASHBOARD})
             for i, (name, text) in enumerate(texts.items())]
    chat = scripted_agent_chat(scripts=[[saves, "CHANGED: retitled"]])
    run = run_agent(chat, list(_tools(files).values()), [{"type": "text", "text": "b"}])
    assert run.error is None and run.tool_calls == len(saves)
    titles = {v["title"] for v in files.overview(DASHBOARD)["visuals"]}
    assert {f"T{i}" for i in range(len(saves))} <= titles
