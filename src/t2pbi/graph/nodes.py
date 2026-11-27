"""Graph nodes and routers.

Nodes are thin: each reads the state, delegates the real work to the ingest / parse / mapping /
validate / emit layers, and returns a partial state update. Issues a node finds are returned in
``issues`` (appended by the state's reducer), never written anywhere else.

Routers decide which edge to follow. They read the state and the run context and return a route
label; the labels are mapped to nodes in :mod:`t2pbi.graph.pipeline`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from langgraph.runtime import Runtime

from t2pbi.desktop import bridge
from t2pbi.desktop.modeling import eula_accepted
from t2pbi.desktop.session import load_data
from t2pbi.emit import assemble_pbip
from t2pbi.fidelity import read_project, score_structural
from t2pbi.fidelity.derived import derive_expectations
from t2pbi.fidelity.expectations import SheetExpectation
from t2pbi.fidelity.project import Project
from t2pbi.fidelity.references import match_screenshots
from t2pbi.graph.state import MigrationContext, MigrationState
from t2pbi.ingest import load_workbook
from t2pbi.mapping import expression_translator, model_mapper, visual_mapper
from t2pbi.mapping.report import IssueSeverity, MigrationReport
from t2pbi.mapping.visual_router_llm import LLMVisualRouter
from t2pbi.parse import parse_workbook
from t2pbi.refine import pictures
from t2pbi.refine.agent import build_brief, make_tools, run_agent, tableau_facts
from t2pbi.refine.report_files import ReportFileError, ReportFiles
from t2pbi.refine.review import (
    derived_failures,
    failures,
    judge_verdict,
    report_errors,
    validator_failures,
)
from t2pbi.refine.worklist import RefineOutcome, build_work_list
from t2pbi.refine.workspace import Workspace
from t2pbi.validate import model_validate, tmdl_cli
from t2pbi.validate.pbir_cli import iter_findings, run_pbir_validation
from t2pbi.validate.report_refs import check_report_references

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

    assert state.model is not None and state.workbook is not None
    chat = runtime.context.chat_model
    schema = expression_translator.schema_context(state.model, state.workbook.display_names())
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
    """Worksheets -> visuals; dashboards and the worksheets Tableau viewers can open -> report
    pages. Unmatched worksheets go to the LLM router."""

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
    """Check the written report: Microsoft's powerbi-report-author validator (schemas, catalog),
    then t2pbi's reference check against the semantic model, which that validator never opens."""

    assert state.pbip_path is not None
    report = MigrationReport()
    for problem in check_report_references(Path(state.pbip_path).parent):
        report.add(IssueSeverity.ERROR, "pbir", "REFERENCE", str(problem))

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


def open_desktop(state: MigrationState, runtime: Runtime[MigrationContext]) -> dict:
    """Open the project in Power BI Desktop with its data loaded, so the review can compare pages
    with the user's Tableau screenshots.

    Pictures are an extra signal, never a requirement. Only pages the user gave a screenshot of
    are compared; with no screenshots, Desktop isn't opened. Without the Desktop Bridge, the
    Authoring MCP's EULA (needed to load the data) or a working Desktop, refinement goes on with
    the checks alone.
    """

    assert state.pbip_path is not None and state.workbook is not None
    report = MigrationReport()

    def checks_only(why: str, severity: IssueSeverity = IssueSeverity.INFO) -> dict:
        report.add(severity, "refine", "desktop", f"Reviewing with checks only: {why}.")
        return {"issues": report.issues}

    folder = runtime.context.reference_images
    if not folder:
        return checks_only("no Tableau screenshots given (--reference-images DIR), so no page is compared with a picture")
    if not Path(folder).is_dir():
        return checks_only(f"the screenshots folder {folder} doesn't exist", IssueSeverity.WARNING)
    sheets = [ws.name for ws in state.workbook.worksheets] + [d.name for d in state.workbook.dashboards]
    shots = match_screenshots(folder, sheets)
    # Only pages are judged. A sheet hidden in Tableau has none (it's a visual on a dashboard), so
    # its screenshots are left out, but not reported as misnamed.
    pages = [p.display_name for p in read_project(Path(state.pbip_path).parent).pages]
    no_page = [s for s in shots.images if s not in pages]
    if no_page:
        report.add(
            IssueSeverity.INFO, "refine", "desktop",
            f"Screenshots of sheets without a page of their own, not judged: {', '.join(no_page)}.",
        )
    shots.images = {s: paths for s, paths in shots.images.items() if s in pages}
    for sheet, folders in shots.ambiguous.items():
        report.add(
            IssueSeverity.WARNING, "refine", sheet,
            f"Not compared with a picture: several screenshot folders match it ({', '.join(folders)}).",
        )
    if shots.unmatched:
        report.add(
            IssueSeverity.WARNING, "refine", "desktop",
            f"Screenshot folders named after no sheet, not used: {', '.join(shots.unmatched)}.",
        )
    if shots.loose:
        report.add(
            IssueSeverity.WARNING, "refine", "desktop",
            f"Screenshots outside a sheet's folder, not used: {', '.join(shots.loose)} "
            "(put each sheet's images in a folder named after the sheet).",
        )
    if not shots.images:
        return checks_only("no page has screenshots (one folder per sheet, named after it)", IssueSeverity.WARNING)
    if not bridge.available():
        return checks_only("the Desktop Bridge CLI isn't installed (npm ci in tools/)")
    if not eula_accepted():
        return checks_only(
            "the Authoring MCP's EULA isn't accepted (PBI_MODELING_MCP_ACCEPT_EULA), so data can't be loaded"
        )

    pbip = Path(state.pbip_path).resolve()
    pid = None
    try:
        pid = bridge.open_report(pbip)
        bridge.wait_ready(pid, pbip)
        asyncio.run(load_data(pbip))
    except Exception as exc:  # noqa: BLE001 - Desktop trouble degrades to checks only
        if pid is not None:
            bridge.close(pid)
        return checks_only(f"Power BI Desktop couldn't show the project ({exc})", IssueSeverity.WARNING)
    report.add(
        IssueSeverity.INFO, "refine", "desktop",
        f"Opened in Power BI Desktop (pid {pid}) with data loaded; {len(shots.images)} of {len(pages)} "
        "page(s) have Tableau screenshots to be compared with.",
        detail="Not compared: " + ", ".join(p for p in pages if p not in shots.images)
        if len(shots.images) < len(pages) else None,
    )
    references = {sheet: [str(p) for p in paths] for sheet, paths in shots.images.items()}
    return {"desktop_pid": pid, "references": references, "issues": report.issues}


def assess(state: MigrationState, runtime: Runtime[MigrationContext]) -> dict:
    """Build the work list: what's wrong with each page, from checks derived from the workbook,
    problems already in the report and — with Desktop open — the visual judge. No hand-written
    expectations are needed."""

    assert state.workbook is not None and state.pbip_path is not None
    project = read_project(Path(state.pbip_path).parent)
    expected = derive_expectations(state.workbook, Path(state.input_path).name)
    results = score_structural(expected, project)
    report = MigrationReport()

    scores: dict[str, int] = {}
    mismatches: dict[str, list[tuple[str, str]]] = {}
    chat = runtime.context.chat_model
    if state.desktop_pid is not None and chat is not None:
        sheets = {sheet.name: sheet for sheet in expected.sheets}
        work = pictures.work_dir(state.out_dir)
        for page in project.pages:
            references = [Path(p) for p in state.references.get(page.display_name, [])]
            sheet = sheets.get(page.display_name)
            if not references or sheet is None:
                continue
            judged = pictures.judge_page(
                chat, state.desktop_pid, page.name, sheet, references, pictures.shot_path(work, page.name)
            )
            if judged.score is None:
                report.add(IssueSeverity.INFO, "refine", page.display_name, f"Not judged: {judged.error}.")
                continue
            scores[page.display_name] = judged.score
            mismatches[page.display_name] = judged.mismatches
            report.add(
                IssueSeverity.INFO, "refine", page.display_name, f"Judge: {judged.score}/10 against Tableau.",
                detail="; ".join(f"{c}: {note}" for c, note in judged.mismatches) or None,
            )

    errors = validator_failures(state.pbip_path)
    items = build_work_list(results, state.issues, project, mismatches, errors)
    failing = {**failures(results), **errors}
    if not items:
        report.add(
            IssueSeverity.INFO, "refine", "work list",
            "Nothing to refine: every derived check passes and the validators report no errors.",
        )
    for item in items:
        report.add(
            IssueSeverity.INFO, "refine", item.page,
            f"To refine: {len(item.problems)} problem(s).",
            detail="; ".join(f"{p.check}: {p.detail}" for p in item.problems),
        )
    return {
        "work_items": items,
        "failing_checks": failing,
        "failing_at_start": len(failing),
        "page_scores": scores,
        "issues": report.issues,
    }


def pick_item(state: MigrationState) -> dict:
    """Take the next page off the work list (a refused page comes back first, for its retry)."""

    item, rest = state.work_items[0], state.work_items[1:]
    return {
        "current_item": item,
        "work_items": rest,
        "refine_picks": state.refine_picks + 1,
        "agent_summary": "",
        "agent_error": None,
    }


def fix_item(state: MigrationState, runtime: Runtime[MigrationContext]) -> dict:
    """Run the refinement agent on one page. It edits the report's PBIR files directly, each save
    validated; the review decides whether the change is kept."""

    item = state.current_item
    assert item is not None and state.workbook is not None and state.pbip_path is not None
    workbook, pbip_path = state.workbook, state.pbip_path
    project_dir = Path(pbip_path).parent
    files = ReportFiles(project_dir)
    try:
        current: dict[str, Any] = files.overview(item.page)
    except ReportFileError as exc:
        current = {"error": str(exc)}

    references = state.references.get(item.page, [])
    images = [
        (f"Image: the Tableau original{f' (view {n} of {len(references)})' if len(references) > 1 else ''}.", Path(p))
        for n, p in enumerate(references, 1)
    ]
    page_id = _page_id(read_project(project_dir), item.page)
    shot = pictures.shot_path(pictures.work_dir(state.out_dir), page_id) if page_id else None
    if shot is not None and shot.is_file():
        images.append(("Image: the Power BI page now.", shot))
    brief = build_brief(item, tableau_facts(workbook, item.page), current, images)

    workbook_file = Path(state.input_path).name

    def check_page() -> str:
        now = {**derived_failures(workbook, workbook_file, project_dir), **validator_failures(pbip_path)}
        mine = [f"- {k.split('::', 1)[1]}: {v}" for k, v in now.items() if k.startswith(f"{item.page}::")]
        broke = [f"- {k}: {v}" for k, v in now.items() if k not in state.failing_checks]
        lines = ["Still failing on this page (checks and validator errors):", *(mine or ["- nothing"])]
        if broke:
            lines += ["Newly failing (the review will refuse the change until these pass again):", *broke]
        if item.page in state.page_scores:
            lines.append("The picture is judged against Tableau after you finish.")
        return "\n".join(lines)

    context = runtime.context
    tools = make_tools(files, item.page, lambda: report_errors(pbip_path), check_page)
    run = run_agent(context.chat_model, tools, brief, context.max_agent_steps)
    report = MigrationReport()
    if run.error:
        report.add(IssueSeverity.WARNING, "refine", item.page, f"Agent run ended early: {run.error}.")
    else:
        report.add(IssueSeverity.INFO, "refine", item.page, f"Agent ({run.tool_calls} tool call(s)): {run.summary}")
    return {"agent_summary": run.summary, "agent_error": run.error, "issues": report.issues}


def review_item(state: MigrationState, runtime: Runtime[MigrationContext]) -> dict:
    """The gate: keep the agent's change as a commit, or revert it (and give the page one retry,
    with the reasons). Nothing the agent does can influence the checks used here."""

    item = state.current_item
    assert item is not None and state.workbook is not None and state.pbip_path is not None
    workspace = Workspace(state.pbip_path)
    project_dir = Path(state.pbip_path).parent
    report = MigrationReport()
    summary = state.agent_summary or (f"partial change, agent stopped: {state.agent_error}" if state.agent_error else "")

    if not workspace.diff().strip():
        outcome = RefineOutcome(page=item.page, outcome="failed" if state.agent_error else "unchanged", summary=summary)
        report.add(IssueSeverity.INFO, "refine", item.page, f"No change made: {summary or 'no reason given'}.")
        return {"current_item": None, "refine_log": [*state.refine_log, outcome], "issues": report.issues}

    after = {
        **derived_failures(state.workbook, Path(state.input_path).name, project_dir),
        **validator_failures(state.pbip_path),
    }
    page_id = _page_id(read_project(project_dir), item.page)
    work = pictures.work_dir(state.out_dir)
    score_before, score_after = state.page_scores.get(item.page), None
    chat = runtime.context.chat_model
    if state.desktop_pid is not None and score_before is not None and page_id and chat is not None:
        try:
            bridge.reload(state.desktop_pid)
        except bridge.BridgeError as exc:
            report.add(IssueSeverity.WARNING, "refine", item.page, f"Desktop didn't reload the change: {exc}")
        else:
            sheet = SheetExpectation(name=item.page, kind=_page_kind(state, item.page))
            judged = pictures.judge_page(
                chat, state.desktop_pid, page_id, sheet, [Path(p) for p in state.references[item.page]],
                pictures.shot_path(work, page_id, candidate=True),
            )
            score_after = judged.score
            if judged.error:
                report.add(IssueSeverity.INFO, "refine", item.page, f"Change not judged: {judged.error}.")

    verdict = judge_verdict(item, state.failing_checks, after, score_before, score_after)
    if verdict.accepted:
        commit = workspace.commit(_commit_message(item.page, summary, verdict.reasons))
        if page_id:
            pictures.keep_candidate(work, page_id)
        scores = dict(state.page_scores)
        if score_after is not None:
            scores[item.page] = score_after
        outcome = RefineOutcome(
            page=item.page, outcome="accepted", commit=commit, summary=summary, reasons=verdict.reasons
        )
        report.add(
            IssueSeverity.INFO, "refine", item.page, f"Kept as commit {commit}: {summary}",
            detail="; ".join(verdict.reasons),
        )
        return {
            "current_item": None,
            "failing_checks": after,
            "page_scores": scores,
            "refine_log": [*state.refine_log, outcome],
            "issues": report.issues,
        }

    workspace.revert()
    retry = [item.model_copy(update={"attempts": 1, "feedback": verdict.reasons})] if item.attempts == 0 else []
    outcome = RefineOutcome(page=item.page, outcome="rejected", summary=summary, reasons=verdict.reasons)
    report.add(
        IssueSeverity.INFO, "refine", item.page,
        f"Change refused and reverted{'; retrying once' if retry else ''}: {summary}",
        detail="; ".join(verdict.reasons),
    )
    return {
        "current_item": None,
        "work_items": retry + state.work_items,
        "refine_log": [*state.refine_log, outcome],
        "issues": report.issues,
    }


def finalize_refine(state: MigrationState, runtime: Runtime[MigrationContext]) -> dict:
    """Close Desktop and report what refinement did."""

    if state.desktop_pid is not None:
        bridge.close(state.desktop_pid)
    report = MigrationReport()
    kept = [o for o in state.refine_log if o.outcome == "accepted"]
    report.add(
        IssueSeverity.INFO, "refine", "summary",
        f"Refinement kept {len(kept)} of {len(state.refine_log)} agent attempt(s); "
        f"failing checks and validator errors {state.failing_at_start} -> {len(state.failing_checks)}.",
        detail="; ".join(f"{o.page}: {o.outcome}{f' ({o.commit})' if o.commit else ''}" for o in state.refine_log)
        or None,
    )
    if state.work_items:
        report.add(
            IssueSeverity.INFO, "refine", "budget",
            f"Not attempted (budget of {runtime.context.max_refine_items} agent runs): "
            + ", ".join(item.page for item in state.work_items) + ".",
        )
    return {"desktop_pid": None, "issues": report.issues}


def _commit_message(page: str, summary: str, reasons: list[str]) -> str:
    """Subject ``Refine <page>: <summary>``, cut at a word to fit git's 72 characters (the whole
    summary then opens the body); the review's reasons follow in the body."""

    subject = f"Refine {page}: {summary or 'agent change'}"
    body = list(reasons)
    if len(subject) > 72:
        subject = subject[:71].rsplit(" ", 1)[0] + "…"
        body.insert(0, summary)
    return subject + "\n\n" + "\n".join(body)


def _page_id(project: Project, display_name: str) -> str | None:
    return next((page.name for page in project.pages if page.display_name == display_name), None)


def _page_kind(state: MigrationState, page: str) -> str:
    assert state.workbook is not None
    if any(d.name == page for d in state.workbook.dashboards):
        return "dashboard"
    tooltip_sheets = {name for ws in state.workbook.worksheets for name in ws.tooltip_sheets}
    return "tooltip" if page in tooltip_sheets else "worksheet"


# --------------------------------------------------------------------------------------------
# Routers
# --------------------------------------------------------------------------------------------

ExpressionRoute = Literal["translate", "no_llm", "nothing_to_translate"]
RepairRoute = Literal["repair", "done"]
OutputValidationRoute = Literal["validate", "skip"]
RefineRoute = Literal["refine", "done"]
DesktopRoute = Literal["desktop", "checks_only"]
WorkRoute = Literal["work", "done"]


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


def route_desktop(state: MigrationState, runtime: Runtime[MigrationContext]) -> DesktopRoute:
    """Before assessing: open Power BI Desktop for pictures, unless the run turned that off."""

    return "desktop" if runtime.context.use_desktop else "checks_only"


def route_work(state: MigrationState, runtime: Runtime[MigrationContext]) -> WorkRoute:
    """After assessing or reviewing: the next page, while there's work and agent runs are left."""

    if state.work_items and state.refine_picks < runtime.context.max_refine_items:
        return "work"
    return "done"
