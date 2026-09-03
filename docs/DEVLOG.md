# Development log

Why things are the way they are. Decisions, the reasoning behind them, and the traps found the
hard way — the things a `git diff` shows *what* changed but never *why*.

**Format:** newest entry first. Add an entry when a decision is made, reversed, or a
non-obvious constraint is discovered — not for routine changes, which commit messages already
cover. Keep [BACKLOG.md](../BACKLOG.md) in sync when an entry opens or closes work.

---

## 2026-09-03 — Split at a neutral source IR instead of forking for Spotfire

**Context.** Spotfire support was wanted alongside Tableau. The obvious options were a GitHub
fork, a copied repo, or one repo with pluggable frontends.

**Decision: one repo, source adapters.** A fork was rejected outright — GitHub won't fork into
the account that already owns the repo, fork semantics exist for contributing *back upstream*
(this divergence is permanent, no PR will ever flow Tableau→Spotfire), and sharing fixes across
a fork means cherry-picking anyway.

The deciding argument was the shape of the codebase. Of ~2.5k lines of `src`, roughly 900 are
pure target-side (`powerbi_ir`, `emit/`, `validate/`, the visual catalog, the LLM plumbing) —
and that is also the part that *churns most*: PBIR format changes, catalog refreshes, DAX
validation. Copying it into a second repo means doing every one of those fixes twice, forever.

**How the seam works.** `ir/source_ir.py` is the contract. An adapter's only jobs are
`parse(path) -> SourceWorkbook` and an `ExpressionDialect` describing its formula language.
Everything downstream reads normalized enums (`ConnectionKind`, `MarkType`, `Aggregation`,
`DataType`, `FieldRole`) and never learns which tool the input came from.

**The `native_*` fields are load-bearing.** Normalizing to enums loses information the LLM
needs — collapsing Tableau's `"Pie"` to `MarkType.OTHER` would strip the single strongest hint
the visual router has. So every normalized value keeps the source's own string alongside it
(`native_mark`, `native_class`), and those are what go into prompts and warning messages.
Unmapped values degrade rather than fail: an unknown connection kind emits an empty-table stub
plus a report warning, an unknown mark routes to the LLM router.

**Verification technique worth reusing.** A refactor this wide needs proof it changed nothing.
Approach: `git worktree` of pre-refactor `main`, run both versions over every sample via
`sys.path` injection (no install needed), then diff the outputs — comparing the report by
*logical identity* (page display name → canonical visuals) rather than file path, since page
and visual folder names are random per run. First attempt collapsed all pages onto one dict key
and produced fake diffs; keying by display name fixed it. Result: identical output on all three
samples.

**Deferred.** Extracting the shared target side as a `pbip-core` package with two thin repos
depending on it. Only worth the three-repo overhead if release cadences genuinely diverge (e.g.
different clients) — not for tidiness.

---

## 2026-09-01 — Trellis vs legend is deterministic, so don't ask the LLM

The LLM router was being handed a choice it had no business making: whether a dimension becomes
small multiples (`Rows`) or an overlaid legend series (`Series`). It flipped small multiples to
legend often enough to matter.

The insight: **the shelf structure already answers this.** A dimension sharing the measure's row
shelf is a trellis; a dimension on the colour encoding is a legend. Both are read
deterministically from the source, so the decision was moved out of the ambiguous path entirely
and the mapping now reports full confidence.

General principle this established: the LLM resolves cases where the *source itself* is
ambiguous, never cases where we simply hadn't looked hard enough at the input.

---

## 2026-08-31 — Guardrails: catalog, router fallback, and a real validation gate

Three defences added the same week, all aimed at the same failure mode — emitting a `.pbip` that
opens but renders blank.

**Visual catalog as a hard guardrail.** `mapping/data/visual_catalog.json` is generated from
`powerbi-report-author catalog describe`, Microsoft's authoritative capability metadata, via
`scripts/refresh_visual_catalog.py`. Every mapping is checked against it before emission, so an
unknown role, an over-filled role, or a measure placed in a `Grouping` role is caught here
rather than becoming a silently blank visual. Regenerate when targeting a newer PBIR/Desktop
version — do not hand-edit the JSON.

This is why role names must come from the catalog and not from intuition: the small-multiples
role is `Rows`, *not* `SmallMultiples`. That cost a debugging cycle.

**Router can only ever improve on the guess.** `LLMVisualRouter` reconstructs its answer from
the catalog and re-validates it; an unknown visual type, an empty assignment, or one missing a
required role is rejected in favour of the deterministic guess. Provider errors, unparseable
JSON and worse-than-baseline answers all fall back. It cannot produce an invalid visual.

**PBIR validation as a post-build gate.** `powerbi-report-author validate` runs after assembly
and its diagnostics fold into the migration report. Environment-only failures (e.g. schema
unreachable offline) are downgraded to INFO so a disconnected run never fails spuriously.

---

## 2026-08-28 — Shelf semantics drive orientation, not mark type alone

Mapping mark type → visual type isn't enough. Tableau's convention is `cols` = X axis, `rows` =
Y axis, and a measure on `cols` means the author wanted **horizontal** bars. Without reading
that, a horizontal bar chart came out as a transposed column chart and time axes went missing
on sheets like *Sales by Category/Segment*.

The rule now: measure on cols alone → horizontal; otherwise vertical, with the axis dimension
taken from the opposite shelf.

---

## 2026-08-27 — Strict column typing, and why errors are surfaced not swallowed

CSV and Excel sources load every column as text after `PromoteHeaders`, so model columns
rendered as text despite carrying the correct TMDL `dataType`. Partitions now append a
`Table.TransformColumnTypes` step derived from the model's own column types.

**Deliberately strict.** Bad values are *not* coerced to null — that matches native Power BI
import behaviour and keeps data-quality problems discoverable. Each file-sourced table gets an
INFO note pointing at the `Errors in <table>` query, so a postal code typed as a number surfaces
instead of vanishing.

Ordering constraint: partitions are built **after** hidden join-key columns are added, so those
get typed too.

---

## 2026-08-27 — Initial architecture: staged pipeline over a central IR

A LangGraph pipeline over a central IR, with deterministic code doing parsing and generation and
LLMs used surgically for the hard reasoning only (calc → DAX/M translation, ambiguous visual
mapping, repair). The pipeline degrades to a fully deterministic path with no LLM configured,
producing an openable model with untranslated measures flagged in the report.

---

## Reference: format constraints Power BI Desktop enforces

Each of these was found by a rendering or load failure, not by documentation. Changing any of
them is almost certainly a regression — the tests in `test_pipeline.py` guard several.

| Constraint | Consequence if wrong |
| --- | --- |
| `.pbip` `$schema` must match Desktop's exact pattern | Desktop refuses the project |
| PBIR `version.json` must be `2.0.0` (not `1.0.0`) | Report does not render |
| `.platform` files required for Report *and* SemanticModel | Validator errors |
| Base theme resource must be emitted, not just referenced | Rendering fails |
| `defaultPowerBIDataSourceVersion: powerBI_V3` in `model.tmdl` | Desktop refuses modeling changes |
| Files written without a BOM | Load failure |
| Visual container format `2.12.0` | Visual does not render |
| Role names come from the visual catalog (`Rows`, not `SmallMultiples`) | Blank visual, no error |
