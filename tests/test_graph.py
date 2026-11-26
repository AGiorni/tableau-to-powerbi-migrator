"""Tests for the migration graph: its shape, its routing, and its loops."""

from __future__ import annotations

from types import SimpleNamespace

from t2pbi.graph import MigrationContext, MigrationState, build_graph, nodes
from t2pbi.ir.tableau_ir import TableauCalculatedField
from t2pbi.mapping.model_mapper import UNTRANSLATED_DAX
from t2pbi.mapping.report import IssueSeverity

_BROKEN = "SUM('Superstore'[Sales]"
_VALID = "SUM('Superstore'[Sales])"


def _run(twb, out_dir, context: MigrationContext) -> tuple[list[str], MigrationState]:
    """Run the graph once; return the node names in execution order and the final state."""

    visited: list[str] = []
    final: dict = {}
    for mode, chunk in build_graph().stream(
        MigrationState(input_path=str(twb), out_dir=str(out_dir)),
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
        "assess",
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
    assert ("prepare_workspace", "assess", None, False) in edges


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
    context = MigrationContext(chat_model=fake_chat("BLANK()"), refine=True, max_translation_attempts=1)

    visited, state = _run(book1_twb, tmp_path, context)

    assert visited[-2:] == ["prepare_workspace", "assess"]
    assert (tmp_path / ".git").is_dir() and state.refine_baseline
    pages = {item.page: item for item in state.work_items}
    # From the workbook alone (no oracle): the known Book1 gaps show up as work.
    assert "fields[Order Date:month]" in {p.check for p in pages["Sales by Category"].problems}
    assert "tooltip-page" in {p.check for p in pages["Tooltip: Profit Ratio by City"].problems}
    # Untranslated calcs are problems on the pages that show them.
    assert any(p.check == "report:measure[profit ratio]" for p in pages["Tooltip: Profit Ratio by City"].problems)
    assert any(i.stage == "refine" and i.message.startswith("To refine") for i in state.issues)
