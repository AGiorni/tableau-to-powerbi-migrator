"""Graph nodes and routers.

Nodes are thin: each reads the state, delegates the real work to the ingest / parse / mapping /
validate / emit layers, and returns a partial state update. Issues a node finds are returned in
``issues`` (appended by the state's reducer), never written anywhere else.

Routers decide which edge to follow. They read the state and the run context and return a route
label; the labels are mapped to nodes in :mod:`t2pbi.graph.pipeline`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from langgraph.runtime import Runtime

from t2pbi.emit import assemble_pbip
from t2pbi.fidelity import read_project, score_structural
from t2pbi.fidelity.derived import derive_expectations
from t2pbi.graph.state import MigrationContext, MigrationState
from t2pbi.ingest import load_workbook
from t2pbi.mapping import expression_translator, model_mapper, visual_mapper
from t2pbi.mapping.report import IssueSeverity, MigrationReport
from t2pbi.mapping.visual_router_llm import LLMVisualRouter
from t2pbi.parse import parse_workbook
from t2pbi.refine.worklist import build_work_list
from t2pbi.refine.workspace import Workspace
from t2pbi.validate import model_validate, tmdl_cli
from t2pbi.validate.pbir_cli import iter_findings, run_pbir_validation

# --------------------------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------------------------


def ingest(state: MigrationState) -> dict:
    """Read the .twb/.twbx and parse it into the Tableau IR."""

    return {"workbook": parse_workbook(load_workbook(state.input_path))}


def map_model(state: MigrationState) -> dict:
    """Tableau IR -> Power BI semantic model; calculated fields become pending measures."""

    assert state.workbook is not None
    report = MigrationReport()
    result = model_mapper.map_model(state.workbook, report)
    return {
        "model": result.model,
        "pending_expressions": result.pending_expressions,
        "issues": report.issues,
    }


def translate_expressions(state: MigrationState, runtime: Runtime[MigrationContext]) -> dict:
    """Ask the LLM for DAX for every pending calculated field.

    On a repair pass, each field's previous rejection reasons are sent along as feedback.
    """

    assert state.model is not None
    chat = runtime.context.chat_model
    schema = expression_translator.schema_context(state.model)
    translations = {
        key: expression_translator.translate_calc(
            calc,
            owning_table=key.split("::", 1)[0],
            schema_context=schema,
            chat_model=chat,
            feedback=state.dax_feedback.get(key),
        )
        for key, calc in state.pending_expressions.items()
    }
    return {
        "dax_candidates": {key: t.dax for key, t in translations.items()},
        "provider_errors": {key: t.error for key, t in translations.items() if t.error},
        "translation_attempts": state.translation_attempts + 1,
    }


def validate_expressions(state: MigrationState, runtime: Runtime[MigrationContext]) -> dict:
    """Accept valid candidates into the model; keep rejected ones pending with feedback.

    When the attempt budget is spent, whatever is still pending is reported as an error and
    stays ``BLANK()``.
    """

    assert state.model is not None
    model = state.model.model_copy(deep=True)
    measures = {
        f"{table.name}::{measure.name}": measure
        for table in model.tables
        for measure in table.measures
    }
    report = MigrationReport()
    pending, feedback = {}, {}

    for key, calc in state.pending_expressions.items():
        measure = measures.get(key)
        if measure is None:
            continue
        dax = state.dax_candidates.get(key)
        check = expression_translator.check_dax(dax, model)
        if not check.accepted:
            # A failed model call is the real reason, not "empty answer": say so in the report.
            error = state.provider_errors.get(key)
            reasons = [f"The model call failed: {error}"] if error else check.rejections
            pending[key], feedback[key] = calc, reasons
            continue

        measure.expression = dax or ""
        measure.description = None
        report.add(
            IssueSeverity.INFO,
            "expression",
            calc.display_name,
            "Translated Tableau calc to DAX.",
            detail=f"{calc.formula}  =>  {measure.expression}",
        )
        for warning in check.reference_warnings:
            report.add(
                IssueSeverity.WARNING,
                "expression",
                calc.display_name,
                f"Translated DAX references something not in the model: {warning}",
            )

    if pending and state.translation_attempts >= runtime.context.max_translation_attempts:
        for key, calc in pending.items():
            report.add(
                IssueSeverity.ERROR,
                "expression",
                calc.display_name,
                "Could not produce valid DAX after retries; left as BLANK().",
                detail=f"{calc.formula} | last errors: {'; '.join(feedback[key])}",
            )
        pending, feedback = {}, {}

    return {
        "model": model,
        "pending_expressions": pending,
        "dax_feedback": feedback,
        "dax_candidates": {},
        "provider_errors": {},
        "issues": report.issues,
    }


def flag_untranslated(state: MigrationState) -> dict:
    """No LLM configured: leave every calculated field as BLANK() and say so in the report."""

    report = MigrationReport()
    for calc in state.pending_expressions.values():
        report.add(
            IssueSeverity.WARNING,
            "expression",
            calc.display_name,
            "Calculated field awaiting DAX translation (no LLM configured); left as BLANK()."
            + (" Contains an LOD expression." if calc.is_lod else ""),
            detail=calc.formula,
        )
    return {"pending_expressions": {}, "issues": report.issues}


def map_visuals(state: MigrationState, runtime: Runtime[MigrationContext]) -> dict:
    """Worksheets and dashboards -> report pages. Unmatched worksheets go to the LLM router."""

    assert state.workbook is not None and state.model is not None
    chat = runtime.context.chat_model
    router = LLMVisualRouter(chat) if chat is not None else None
    report = MigrationReport()
    pages = visual_mapper.map_visuals(state.workbook, state.model, report, router=router)
    return {"pages": pages, "issues": report.issues}


def validate_model(state: MigrationState) -> dict:
    """Structural model checks (tables, partitions, relationship keys). Reported, not fatal.

    DAX is not re-checked here: ``validate_expressions`` only lets syntactically valid DAX into the
    model, and everything else stays ``BLANK()``.
    """

    assert state.model is not None
    report = MigrationReport()
    for finding in model_validate.validate_model(state.model):
        report.add(finding.severity, "validate", finding.object_name, finding.message)
    return {"issues": report.issues}


def assemble(state: MigrationState) -> dict:
    """Write the .pbip project (TMDL semantic model + PBIR report) to disk."""

    assert state.model is not None
    path = assemble_pbip(state.model, state.out_dir, state.pages)
    return {"pbip_path": str(path)}


def validate_tmdl(state: MigrationState) -> dict:
    """Load the written semantic model with Microsoft's TMDL serializer (TOM) and report problems.

    Catches writer bugs ``validate_model`` can't see, because that one checks the IR before writing.
    """

    assert state.pbip_path is not None
    report = MigrationReport()
    result = tmdl_cli.run_tmdl_validation(tmdl_cli.definition_dir(state.pbip_path))
    if result is None:
        report.add(
            IssueSeverity.INFO,
            "tmdl",
            "validator",
            "tmdl-validate not available (build tools/tmdl-validate; needs .NET); skipped TMDL validation.",
        )
        return {"issues": report.issues}

    findings = list(tmdl_cli.iter_findings(result))
    for severity, where, message in findings:
        report.add(severity, "tmdl", where, message)
    report.add(
        IssueSeverity.INFO,
        "tmdl",
        "validator",
        f"TMDL validation: {result.get('result', 'unknown')} ({len(findings)} error(s)).",
    )
    return {"issues": report.issues}


def validate_pbir(state: MigrationState) -> dict:
    """Run the external powerbi-report-author validator and fold its diagnostics into issues."""

    assert state.pbip_path is not None
    report = MigrationReport()
    result = run_pbir_validation(state.pbip_path)
    if result is None:
        report.add(
            IssueSeverity.INFO,
            "pbir",
            "validator",
            "powerbi-report-author not available (npm ci in tools/); skipped PBIR validation.",
        )
        return {"issues": report.issues}

    for severity, code, message in iter_findings(result):
        report.add(severity, "pbir", code, message)

    report.add(
        IssueSeverity.INFO,
        "pbir",
        "validator",
        f"PBIR validation: {result.get('result', 'unknown')} "
        f"({result.get('errorCount', 0)} error(s), {result.get('warningCount', 0)} warning(s)).",
    )
    return {"issues": report.issues}


def prepare_workspace(state: MigrationState) -> dict:
    """Turn the written project into a git repository with the first pass as its baseline commit,
    so every refinement is a reviewable, reversible commit on top."""

    assert state.pbip_path is not None
    baseline = Workspace(state.pbip_path).init()
    report = MigrationReport()
    report.add(
        IssueSeverity.INFO, "refine", "workspace",
        f"Project is a git repository; first-pass baseline is commit {baseline}.",
    )
    return {"refine_baseline": baseline, "issues": report.issues}


def assess(state: MigrationState) -> dict:
    """Build the work list: what's wrong with each page, from checks derived from the workbook and
    from problems already in the report. No hand-written expectations are needed."""

    assert state.workbook is not None and state.pbip_path is not None
    project = read_project(Path(state.pbip_path).parent)
    expected = derive_expectations(state.workbook, Path(state.input_path).name)
    items = build_work_list(score_structural(expected, project), state.issues, project)

    report = MigrationReport()
    if not items:
        report.add(IssueSeverity.INFO, "refine", "work list", "Nothing to refine: every derived check passes.")
    for item in items:
        report.add(
            IssueSeverity.INFO, "refine", item.page,
            f"To refine: {len(item.problems)} problem(s).",
            detail="; ".join(f"{p.check}: {p.detail}" for p in item.problems),
        )
    return {"work_items": items, "issues": report.issues}


# --------------------------------------------------------------------------------------------
# Routers
# --------------------------------------------------------------------------------------------

ExpressionRoute = Literal["translate", "no_llm", "nothing_to_translate"]
RepairRoute = Literal["repair", "done"]
OutputValidationRoute = Literal["validate", "skip"]
RefineRoute = Literal["refine", "done"]


def route_expressions(
    state: MigrationState, runtime: Runtime[MigrationContext]
) -> ExpressionRoute:
    """After model mapping: translate with the LLM, flag for manual work, or move on."""

    if not state.pending_expressions:
        return "nothing_to_translate"
    return "translate" if runtime.context.chat_model is not None else "no_llm"


def route_after_expression_validation(state: MigrationState) -> RepairRoute:
    """Loop back for a repair pass while any field is still pending (validation clears the
    pending set once the attempt budget is spent, so this always terminates)."""

    return "repair" if state.pending_expressions else "done"


def route_output_validation(
    state: MigrationState, runtime: Runtime[MigrationContext]
) -> OutputValidationRoute:
    """After assembly: run the external validators (TMDL, then PBIR) unless the run disabled them."""

    return "validate" if runtime.context.run_output_validation else "skip"


def route_refine(state: MigrationState, runtime: Runtime[MigrationContext]) -> RefineRoute:
    """After validation: refine only when asked to and an LLM is configured."""

    context = runtime.context
    return "refine" if context.refine and context.chat_model is not None else "done"
