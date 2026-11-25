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
        "validate_pbir",
        "__end__",
    }
    edges = {(e.source, e.target, e.data, e.conditional) for e in graph.edges}
    # Decisions are conditional edges with named routes, not branches hidden inside nodes.
    assert ("map_model", "translate_expressions", "translate", True) in edges
    assert ("map_model", "flag_untranslated", "no_llm", True) in edges
    assert ("map_model", "map_visuals", "nothing_to_translate", True) in edges
    assert ("validate_expressions", "translate_expressions", "repair", True) in edges
    assert ("validate_expressions", "map_visuals", "done", True) in edges
    assert ("assemble", "validate_pbir", "validate", True) in edges
    assert ("assemble", "__end__", "skip", True) in edges


def test_no_llm_path(superstore_twb, tmp_path):
    visited, _ = _run(superstore_twb, tmp_path, MigrationContext(run_pbir_validation=False))
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
        superstore_twb, tmp_path, MigrationContext(chat_model=chat, run_pbir_validation=False)
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
    context = MigrationContext(chat_model=chat, run_pbir_validation=False, max_translation_attempts=3)

    visited, state = _run(superstore_twb, tmp_path, context)

    assert visited.count("translate_expressions") == 3
    assert visited.count("validate_expressions") == 3
    for measure in state.model.tables[0].measures:
        assert measure.expression == UNTRANSLATED_DAX
    errors = [i for i in state.issues if i.severity is IssueSeverity.ERROR]
    assert len(errors) == 2
    assert all("Could not produce valid DAX" in i.message for i in errors)


def test_pbir_validation_runs_when_enabled(superstore_twb, tmp_path, monkeypatch):
    payload = {"result": "succeeded", "errorCount": 0, "warningCount": 0, "diagnostics": {}}
    monkeypatch.setattr(nodes, "run_pbir_validation", lambda _path: payload)

    visited, state = _run(superstore_twb, tmp_path, MigrationContext(run_pbir_validation=True))

    assert visited[-1] == "validate_pbir"
    assert any(i.stage == "preflight" and "succeeded" in i.message for i in state.issues)


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
