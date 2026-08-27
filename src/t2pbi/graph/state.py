"""Shared pipeline state and the individual stage node functions.

Each node takes the mutable :class:`PipelineState` and returns it, so the same functions power
both the LangGraph graph and the plain-Python fallback orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from t2pbi.ingest import load_workbook
from t2pbi.ir.powerbi_ir import PBIModel
from t2pbi.ir.tableau_ir import TableauCalculatedField, TableauWorkbook
from t2pbi.mapping.expression_translator import translate_expressions
from t2pbi.mapping.model_mapper import map_model
from t2pbi.mapping.report import MigrationReport
from t2pbi.parse import parse_workbook


@dataclass
class PipelineState:
    """Everything that flows through the migration pipeline."""

    input_path: str
    out_dir: str
    chat_model: Any | None = None

    workbook: TableauWorkbook | None = None
    model: PBIModel | None = None
    pending_expressions: dict[str, TableauCalculatedField] = field(default_factory=dict)
    report: MigrationReport = field(default_factory=MigrationReport)
    pbip_path: str | None = None


def ingest_node(state: PipelineState) -> PipelineState:
    loaded = load_workbook(state.input_path)
    state.workbook = parse_workbook(loaded)
    return state


def map_model_node(state: PipelineState) -> PipelineState:
    assert state.workbook is not None, "ingest must run before mapping"
    result = map_model(state.workbook, state.report)
    state.model = result.model
    state.pending_expressions = result.pending_expressions
    state.report = result.report
    return state


def translate_expressions_node(state: PipelineState) -> PipelineState:
    assert state.model is not None, "mapping must run before translation"
    state.model = translate_expressions(
        state.model, state.pending_expressions, state.chat_model, state.report
    )
    return state


def validate_node(state: PipelineState) -> PipelineState:
    assert state.model is not None, "mapping must run before validation"
    from t2pbi.mapping.report import IssueSeverity
    from t2pbi.validate import validate_dax, validate_model

    for finding in validate_model(state.model):
        severity = IssueSeverity.ERROR if finding.severity == "error" else IssueSeverity.WARNING
        state.report.add(severity, "validate", finding.object_name, finding.message)

    # Safety net for the emitted DAX: only syntax breakage is fatal here (reference issues are
    # handled during translation, where they are legitimately just warnings).
    for table in state.model.tables:
        for measure in table.measures:
            for issue in validate_dax(measure.expression, state.model):
                if issue.kind == "delimiter":
                    state.report.add(IssueSeverity.ERROR, "validate", measure.name, issue.message)
    return state


def assemble_node(state: PipelineState) -> PipelineState:
    assert state.model is not None, "mapping must run before assembly"
    from t2pbi.emit import assemble_pbip

    path = assemble_pbip(state.model, state.out_dir)
    state.pbip_path = str(path)
    return state


STAGES = (
    ingest_node,
    map_model_node,
    translate_expressions_node,
    validate_node,
    assemble_node,
)
