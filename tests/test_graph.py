"""Tests for the migration graph: its shape, its routing, and its loops."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from t2pbi.graph import (
    MigrationContext,
    MigrationState,
    build_graph,
    migrate,
    nodes,
    recursion_limit,
)
from t2pbi.ingest import load_workbook
from t2pbi.ir.tableau_ir import TableauCalculatedField
from t2pbi.mapping.model_mapper import UNTRANSLATED_DAX
from t2pbi.mapping.report import IssueSeverity
from t2pbi.parse import parse_workbook
from t2pbi.refine.worklist import WorkItem
from t2pbi.refine.workspace import Workspace

_BROKEN = "SUM('Superstore'[Sales]"
_VALID = "SUM('Superstore'[Sales])"


def _run(twb, out_dir, context: MigrationContext) -> tuple[list[str], MigrationState]:
    """Run the graph once; return the node names in execution order and the final state."""

    visited: list[str] = []
    final: dict = {}
    for mode, chunk in build_graph().stream(
        MigrationState(input_path=str(twb), out_dir=str(out_dir)),
        {"recursion_limit": recursion_limit(context)},
        context=context,
        stream_mode=["updates", "values"],
    ):
        if mode == "updates":
            visited.extend(chunk)
        else:
            final = chunk
    return visited, MigrationState.model_validate(final)


def test_graph_shape():
    graph = build_graph().get_graph()
    assert set(graph.nodes) == {
        "__start__",
        "ingest",
        "map_model",
        "translate_expressions",
        "validate_expressions",
        "flag_untranslated",
        "map_visuals",
        "validate_model",
        "assemble",
        "validate_tmdl",
        "validate_pbir",
        "prepare_workspace",
        "open_desktop",
        "assess",
        "pick_item",
        "fix_item",
        "review_item",
        "finalize_refine",
        "__end__",
    }
    edges = {(e.source, e.target, e.data, e.conditional) for e in graph.edges}
    # Decisions are conditional edges with named routes, not branches hidden inside nodes.
    assert ("map_model", "translate_expressions", "translate", True) in edges
    assert ("map_model", "flag_untranslated", "no_llm", True) in edges
    assert ("map_model", "map_visuals", "nothing_to_translate", True) in edges
    assert ("validate_expressions", "translate_expressions", "repair", True) in edges
    assert ("validate_expressions", "map_visuals", "done", True) in edges
    assert ("assemble", "validate_tmdl", "validate", True) in edges
    assert ("assemble", "__end__", "skip", True) in edges
    assert ("validate_tmdl", "validate_pbir", None, False) in edges
    assert ("validate_pbir", "prepare_workspace", "refine", True) in edges
    assert ("validate_pbir", "__end__", "done", True) in edges
    assert ("prepare_workspace", "open_desktop", "desktop", True) in edges
    assert ("prepare_workspace", "assess", "checks_only", True) in edges
    assert ("open_desktop", "assess", None, False) in edges
    # The refinement loop: the agent (fix_item) proposes, review_item keeps or reverts.
    assert ("assess", "pick_item", "work", True) in edges
    assert ("assess", "finalize_refine", "done", True) in edges
    assert ("pick_item", "fix_item", None, False) in edges
    assert ("fix_item", "review_item", None, False) in edges
    assert ("review_item", "pick_item", "work", True) in edges
    assert ("review_item", "finalize_refine", "done", True) in edges
    assert ("finalize_refine", "__end__", None, False) in edges


def test_no_llm_path(superstore_twb, tmp_path):
    visited, _ = _run(superstore_twb, tmp_path, MigrationContext(run_output_validation=False))
    assert visited == [
        "ingest",
        "map_model",
        "flag_untranslated",
        "map_visuals",
        "validate_model",
        "assemble",
    ]


def test_repair_loop_fixes_unbalanced_dax(superstore_twb, tmp_path, fake_chat):
    # First pass: the first field gets broken DAX, the second valid. The repair pass fixes the first.
    chat = fake_chat([_BROKEN, _VALID])
    visited, state = _run(
        superstore_twb, tmp_path, MigrationContext(chat_model=chat, run_output_validation=False)
    )

    assert visited[2:6] == [
        "translate_expressions",
        "validate_expressions",
        "translate_expressions",
        "validate_expressions",
    ]
    assert all(m.expression == _VALID for m in state.model.tables[0].measures)
    assert not [i for i in state.issues if i.severity is IssueSeverity.ERROR]

    # Only the rejected field is retried, and its prompt carries the rejection reason.
    repairs = [p for p in chat.prompts if "previous attempt was invalid" in p]
    assert len(chat.prompts) == 3
    assert len(repairs) == 1
    assert "Unbalanced parenthesis" in repairs[0]


def test_attempt_budget_ends_loop_with_errors(superstore_twb, tmp_path, fake_chat):
    chat = fake_chat(_BROKEN)  # always unbalanced
    context = MigrationContext(chat_model=chat, run_output_validation=False, max_translation_attempts=3)

    visited, state = _run(superstore_twb, tmp_path, context)

    assert visited.count("translate_expressions") == 3
    assert visited.count("validate_expressions") == 3
    for measure in state.model.tables[0].measures:
        assert measure.expression == UNTRANSLATED_DAX
    errors = [i for i in state.issues if i.severity is IssueSeverity.ERROR]
    assert len(errors) == 2
    assert all("Could not produce valid DAX" in i.message for i in errors)


def test_output_validation_runs_both_validators_when_enabled(superstore_twb, tmp_path, monkeypatch):
    pbir = {"result": "succeeded", "errorCount": 0, "warningCount": 0, "diagnostics": {}}
    tmdl = {
        "result": "failed",
        "diagnostics": [
            {"severity": "error", "kind": "format", "file": "./tables/T", "line": 4,
             "message": "TMDL Format Error:\r\n\tInvalid indentation was detected!"}
        ],
    }
    seen_paths = []
    monkeypatch.setattr(nodes, "run_pbir_validation", lambda _path: pbir)
    monkeypatch.setattr(nodes.tmdl_cli, "run_tmdl_validation", lambda path: seen_paths.append(path) or tmdl)

    visited, state = _run(superstore_twb, tmp_path, MigrationContext(run_output_validation=True))

    assert visited[-2:] == ["validate_tmdl", "validate_pbir"]
    assert seen_paths == [tmp_path / "superstore.SemanticModel" / "definition"]
    assert any(i.stage == "pbir" and "succeeded" in i.message for i in state.issues)
    tmdl_errors = [i for i in state.issues if i.stage == "tmdl" and i.severity is IssueSeverity.ERROR]
    assert [(i.object_name, i.message) for i in tmdl_errors] == [
        ("./tables/T:4", "TMDL format error: TMDL Format Error: Invalid indentation was detected!")
    ]


def test_output_validators_are_skipped_not_failed_when_tools_are_missing(superstore_twb, tmp_path, monkeypatch):
    monkeypatch.setattr(nodes, "run_pbir_validation", lambda _path: None)
    monkeypatch.setattr(nodes.tmdl_cli, "run_tmdl_validation", lambda _path: None)

    _, state = _run(superstore_twb, tmp_path, MigrationContext(run_output_validation=True))

    validator_issues = [i for i in state.issues if i.stage in ("tmdl", "pbir")]
    assert {i.stage for i in validator_issues if "skipped" in i.message} == {"tmdl", "pbir"}
    assert all(i.severity is IssueSeverity.INFO for i in validator_issues)


def test_route_expressions():
    no_llm = SimpleNamespace(context=MigrationContext())
    with_llm = SimpleNamespace(context=MigrationContext(chat_model=object()))
    empty = MigrationState(input_path="x", out_dir="y")
    pending = empty.model_copy(
        update={"pending_expressions": {"T::m": TableauCalculatedField(name="[m]", formula="1")}}
    )

    assert nodes.route_expressions(empty, with_llm) == "nothing_to_translate"
    assert nodes.route_expressions(pending, no_llm) == "no_llm"
    assert nodes.route_expressions(pending, with_llm) == "translate"


def test_route_refine():
    state = MigrationState(input_path="x", out_dir="y")
    assert nodes.route_refine(state, SimpleNamespace(context=MigrationContext())) == "done"
    asked_no_llm = SimpleNamespace(context=MigrationContext(refine=True))
    assert nodes.route_refine(state, asked_no_llm) == "done"  # the graph completes without an LLM
    asked = SimpleNamespace(context=MigrationContext(refine=True, chat_model=object()))
    assert nodes.route_refine(state, asked) == "refine"


def test_refine_builds_a_workspace_and_a_work_list(book1_twb, tmp_path, monkeypatch, fake_chat):
    monkeypatch.setattr(nodes, "run_pbir_validation", lambda _path: None)
    monkeypatch.setattr(nodes.tmdl_cli, "run_tmdl_validation", lambda _path: None)
    context = MigrationContext(
        chat_model=fake_chat("BLANK()"), refine=True, max_translation_attempts=1,
        use_desktop=False, max_refine_items=0,
    )

    visited, state = _run(book1_twb, tmp_path, context)

    assert visited[-3:] == ["prepare_workspace", "assess", "finalize_refine"]
    assert (tmp_path / ".git").is_dir() and state.refine_baseline
    pages = {item.page: item for item in state.work_items}
    # From the workbook alone (no oracle): the known Book1 gaps show up as work.
    assert "fields[Order Date:month]" in {p.check for p in pages["Sales by Category"].problems}
    assert "tooltip-page" in {p.check for p in pages["Tooltip: Profit Ratio by City"].problems}
    # Untranslated calcs are problems on the pages that show them.
    assert any(p.check == "report:measure[profit ratio]" for p in pages["Tooltip: Profit Ratio by City"].problems)
    assert any(i.stage == "refine" and i.message.startswith("To refine") for i in state.issues)


def test_route_desktop_and_route_work():
    state = MigrationState(input_path="x", out_dir="y", work_items=[WorkItem(page="A"), WorkItem(page="B")])
    assert nodes.route_desktop(state, SimpleNamespace(context=MigrationContext())) == "desktop"
    assert nodes.route_desktop(state, SimpleNamespace(context=MigrationContext(use_desktop=False))) == "checks_only"
    budget = SimpleNamespace(context=MigrationContext(max_refine_items=2))
    assert nodes.route_work(state, budget) == "work"
    assert nodes.route_work(state.model_copy(update={"refine_picks": 2}), budget) == "done"
    assert nodes.route_work(state.model_copy(update={"work_items": []}), budget) == "done"


def test_open_desktop_compares_only_pages_with_a_user_screenshot(book1_twb, tmp_path, monkeypatch):
    """No thumbnail fallback: without the user's screenshots Desktop isn't even opened, and with
    some, only those pages get a picture to be judged against."""

    opened: list = []

    async def no_data(_pbip):
        return None

    monkeypatch.setattr(nodes.bridge, "available", lambda: True)
    monkeypatch.setattr(nodes, "eula_accepted", lambda: True)
    monkeypatch.setattr(nodes.bridge, "open_report", lambda pbip: opened.append(pbip) or 4242)
    monkeypatch.setattr(nodes.bridge, "wait_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(nodes, "load_data", no_data)
    project = migrate(book1_twb, tmp_path / "project", MigrationContext(run_output_validation=False))
    state = MigrationState(
        input_path=str(book1_twb), out_dir=str(tmp_path), pbip_path=str(project.pbip_path),
        workbook=parse_workbook(load_workbook(book1_twb)),
    )

    def open_with(folder):
        return nodes.open_desktop(state, SimpleNamespace(context=MigrationContext(reference_images=folder)))

    none_given = open_with(None)
    assert "desktop_pid" not in none_given and not opened
    assert "no Tableau screenshots given" in none_given["issues"][0].message

    shots = tmp_path / "screenshots"
    for name in ("Sales by category/view.png", "Sales by category/legend.png", "Sales by segment/view.png",
                 "sales_by_segment/view.png", "Sales/view.png"):
        (shots / name).parent.mkdir(parents=True, exist_ok=True)
        (shots / name).write_bytes(b"")
    (shots / "loose.png").write_bytes(b"")
    some = open_with(str(shots))
    assert some["desktop_pid"] == 4242 and len(opened) == 1
    assert {s: [Path(p).name for p in ps] for s, ps in some["references"].items()} == {
        "Sales by Category": ["legend.png", "view.png"]
    }
    warnings = [i.message for i in some["issues"] if i.severity is IssueSeverity.WARNING]
    assert any("several screenshot folders match" in w for w in warnings)  # Sales by segment: not judged
    assert any("named after no sheet, not used: Sales." in w for w in warnings)
    assert any("outside a sheet's folder, not used: loose.png" in w for w in warnings)

    for image in (shots / "Sales by category").iterdir():
        image.unlink()  # left: an ambiguous pair, a misnamed folder, a loose file
    left = open_with(str(shots))
    assert "desktop_pid" not in left and len(opened) == 1


def test_open_desktop_judges_pages_only(book1_hidden_twb, tmp_path, monkeypatch):
    """A sheet hidden in Tableau has no page: its screenshot is set aside, not called misnamed."""

    async def no_data(_pbip):
        return None

    monkeypatch.setattr(nodes.bridge, "available", lambda: True)
    monkeypatch.setattr(nodes, "eula_accepted", lambda: True)
    monkeypatch.setattr(nodes.bridge, "open_report", lambda _pbip: 4242)
    monkeypatch.setattr(nodes.bridge, "wait_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(nodes, "load_data", no_data)
    project = migrate(book1_hidden_twb, tmp_path / "project", MigrationContext(run_output_validation=False))
    shots = tmp_path / "screenshots"
    for sheet in ("Executive Overview", "Sales by Category"):  # a dashboard, a hidden sheet on it
        (shots / sheet).mkdir(parents=True)
        (shots / sheet / "view.png").write_bytes(b"")
    state = MigrationState(
        input_path=str(book1_hidden_twb), out_dir=str(tmp_path), pbip_path=str(project.pbip_path),
        workbook=parse_workbook(load_workbook(book1_hidden_twb)),
    )

    update = nodes.open_desktop(state, SimpleNamespace(context=MigrationContext(reference_images=str(shots))))

    assert set(update["references"]) == {"Executive Overview"}
    messages = [i.message for i in update["issues"]]
    assert any("without a page of their own, not judged: Sales by Category" in m for m in messages)
    assert not any("named after no sheet" in m for m in messages)
    assert any("1 of 2 page(s)" in m for m in messages)


def test_refine_loop_keeps_good_changes_and_reverts_bad_ones(book1_twb, tmp_path, monkeypatch, scripted_agent_chat):
    monkeypatch.setattr(nodes, "run_pbir_validation", lambda _path: None)
    monkeypatch.setattr(nodes.tmdl_cli, "run_tmdl_validation", lambda _path: None)
    monkeypatch.setattr(nodes, "validator_errors", lambda _path: [])
    monkeypatch.setattr(nodes, "report_errors", lambda _path: [])
    kpi, tip, geo = "Key Performance Indicators", "Tooltip: Profit Ratio by City", "profit ratio by geography"
    chat = scripted_agent_chat(scripts=[
        # Run 1, "Key Performance Indicators" (most problems): the title Tableau displays.
        [("read_visual", {"visual": kpi}),
         ("write_visual", lambda r: {"visual": kpi, "content": _edit(r[-1], _title("Executive Overview - Profitability"))}),
         "CHANGED: set the title Tableau shows"],
        # Run 2, the tooltip sheet: removes a field the page needs -> refused, reverted.
        [("read_visual", {"visual": tip}),
         ("write_visual", lambda r: {"visual": tip, "content": _edit(r[-1], _drop_role("Y"))}),
         "CHANGED: removed the measure"],
        # Run 3, the retry with the review's reasons: make it the map's hover tooltip, which takes
        # its own page.json and the map's visual.json on the host page.
        [("check_page", {}), ("read_page", {}),
         ("write_page", lambda r: {"content": _edit(r[-1], _tooltip_page)}),
         ("read_visual", {"visual": geo, "page_name": geo}),
         ("write_visual", lambda r: {"visual": geo, "page_name": geo,
                                     "content": _edit(r[-1], _shows_tooltip(_page_named(r, tip)))}),
         "CHANGED: made it the map's tooltip"],
    ])
    context = MigrationContext(
        chat_model=chat, refine=True, use_desktop=False, max_refine_items=3, max_translation_attempts=1
    )

    visited, state = _run(book1_twb, tmp_path, context)

    assert visited[visited.index("assess"):] == [
        "assess", *["pick_item", "fix_item", "review_item"] * 3, "finalize_refine",
    ]
    assert [(o.page, o.outcome) for o in state.refine_log] == [
        ("Key Performance Indicators", "accepted"),
        ("Tooltip: Profit Ratio by City", "rejected"),
        ("Tooltip: Profit Ratio by City", "accepted"),
    ]
    assert any("now failing" in r for r in state.refine_log[1].reasons)
    # The retry was told why the first attempt was refused.
    assert "refused by the review" in chat.briefs[2] and "now failing" in chat.briefs[2]
    assert any(r.startswith("Still failing on this page") for r in chat.tool_results)  # check_page ran

    # One commit per kept change on top of the baseline; the refused edit left no trace.
    workspace = Workspace(state.pbip_path)
    log = [subject for _sha, subject in workspace.log()]
    assert log == [
        "Refine Tooltip: Profit Ratio by City: made it the map's tooltip",
        "Refine Key Performance Indicators: set the title Tableau shows",
        "Baseline: deterministic migration",
    ]
    assert workspace.diff() == ""
    assert "Tooltip: Profit Ratio by City::fields[profit ratio:measure]" not in state.failing_checks
    for fixed in ("Key Performance Indicators::title", "Tooltip: Profit Ratio by City::tooltip-page",
                  "Tooltip: Profit Ratio by City::hidden"):
        assert fixed not in state.failing_checks
    assert len(state.failing_checks) == state.failing_at_start - 3

    summary = [i for i in state.issues if i.stage == "refine" and i.object_name == "summary"]
    assert summary and "kept 2 of 3" in summary[0].message
    assert any(i.object_name == "budget" for i in state.issues)  # pages left for lack of agent runs


def _edit(text: str, change) -> str:
    """A file the agent read, changed by ``change`` (which edits the parsed JSON in place)."""

    data = json.loads(text)
    change(data)
    return json.dumps(data)


def _literal(value: str) -> dict:
    return {"expr": {"Literal": {"Value": value}}}


def _title(title: str):
    def change(visual: dict) -> None:
        visual["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"] = _literal(f"'{title}'")
    return change


def _drop_role(role: str):
    return lambda visual: visual["visual"]["query"]["queryState"].pop(role)


def _tooltip_page(page: dict) -> None:
    page.update({
        "type": "Tooltip", "visibility": "HiddenInViewMode", "displayOption": "ActualSize",
        "width": 320, "height": 240, "pageBinding": {"name": "tip0000000000000001", "type": "Tooltip"},
    })


def _page_named(results: list[str], display_name: str) -> str:
    """The ``name`` of the page whose page.json the agent read earlier in the run."""

    for text in results:
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("displayName") == display_name:
            return data["name"]
    raise AssertionError(f"page {display_name!r} wasn't read")


def _shows_tooltip(page_name: str):
    def change(visual: dict) -> None:
        visual["visual"].setdefault("visualContainerObjects", {})["visualTooltip"] = [{"properties": {
            "show": _literal("true"), "type": _literal("'Canvas'"), "section": _literal(f"'{page_name}'"),
        }}]
    return change


def test_commit_subjects_fit_git_conventions():
    short = nodes._commit_message("Page", "set the title", ["fixed: title"])
    assert short == "Refine Page: set the title\n\nfixed: title"
    summary = "switched to a stacked area chart, set the title and added Profit to the tooltips of the visual"
    subject, _, body = nodes._commit_message("Sales by Category", summary, ["fixed: x"]).partition("\n\n")
    assert len(subject) <= 72 and subject.endswith("…")
    assert body == f"{summary}\nfixed: x"
