# Development log

Why things are the way they are. Decisions, the reasoning behind them, and the traps found the
hard way — the things a `git diff` shows *what* changed but never *why*.

**Format:** newest entry first. Add an entry when a decision is made, reversed, or a
non-obvious constraint is discovered — not for routine changes, which commit messages already
cover. Keep [BACKLOG.md](../BACKLOG.md) in sync when an entry opens or closes work.

---

## 2026-11-25 — Fidelity step 1a: structural expectations and a ratchet

**What.** `src/t2pbi/fidelity/` scores an emitted project against a hand-reviewed expectations
file per workbook (`samples/expected/<name>.toml`): for each Tableau sheet, the page, the
acceptable visual types and the fields per role a *faithful* Power BI report would have.
`scripts/fidelity.py` runs it; `tests/test_fidelity_structural.py` keeps it honest.

**Decisions.**
- *Expectations state the truth, not today's output.* What t2pbi fails today is listed per check
  under `known_gaps` with the reason. The test is a ratchet: an unexpected failure fails it, and so
  does a known gap that starts passing (delete it from the file), so the score can only be moved
  on purpose.
- *Judged on the files, not the IR.* The scorer reads `page.json` / `visual.json` and the TMDL
  measures, so writer bugs count against fidelity.
- *Role names are checked against the visual catalog* when the file is loaded: an expectation
  can't use a role that none of its allowed visual types has (the `SmallMultiples` trap again).
- *TOML, not YAML*: Python reads it with the standard library, so no new dependency.
- *Book1 only.* `superstore.twb`'s only worksheet has empty shelves and `sales_sql.twb` has none,
  so neither has anything to be faithful to; they stay model-mapping fixtures.
- *Reference screenshots come from the user*, once per workbook (`samples/reference/Book1/`,
  later a CLI option), with the embedded thumbnails as fallback. The user's screenshots included
  Tableau's shelf panes and confirmed every expectation drafted from the XML; they also showed
  things I hadn't looked for in the XML: custom titles with dynamic filter inserts, number
  formats, an ascending sort and value labels. The point for the app: a reference picture
  surfaces gaps nobody thought to write a rule for. Names are matched loosely (case, spaces and
  punctuation ignored), because people won't type `Tooltip_ Profit Ratio by City.png`.

**Baseline.** Book1 scores **45/62 (73%)**; all 17 failures are known gaps, each a verified
BACKLOG item: date parts ignored, dimension calcs lost from colour, tooltip encodings not
migrated, viz-in-tooltip sheets as normal pages, count-of-table fields dropped, area charts not
stacked, map colour measure used as bubble size, custom titles not migrated. The dashboard scores
7/7.

**Found on the way: a latent TMDL writer bug.** A multi-line measure is written with its body at
the same indentation as its properties, so TOM reads the next property (e.g. `formatString`) as
part of the DAX — silently: the file still loads. Single-line measures and today's output (no
format strings) are unaffected. Fixed separately; see the format-constraints table.

---

## 2026-11-25 — Step 0: pinned toolchain, and what the Power BI tools can really do

**Decisions taken while planning the roadmap.** LLM: Azure OpenAI through the existing client
(a tool-calling, vision-capable deployment, configured by the operator). Corpus: Book1 only for
now. CI: GitHub Actions. The refinement agent edits **PBIR/TMDL files directly**; after refinement
the files, not the IR, are the source of truth.

**Toolchain pinned.** `tools/package.json` pins `powerbi-report-authoring-cli` 0.5.0 (was 0.1.4 on
PATH), `powerbi-desktop-bridge-cli` 1.1.0 and `powerbi-modeling-mcp` 1.0.1; `npm ci` in `tools/`.
`t2pbi/toolchain.py` prefers the pinned copy over PATH. Under 0.5.0 all three samples still
validate with 0 errors and 0 warnings; the refreshed visual catalog changed a few roles
(`cardVisual` gained `TrendVisualCategory`, slicers lost their one-field cap,
`hundredPercentStackedBarChart` no longer requires `Category`) and migration output was
byte-identical before and after (`scripts/snapshot_outputs.py`, now in the repo).

**Spikes, run against Book1 in Power BI Desktop 2.160 (Store):**
- *Rendering works unattended, once the machine is set up.* Two one-time prerequisites: the Store
  app isn't found by the Bridge CLI, so `PBI_DESKTOP_PATH` must point at `PBIDesktop.exe` inside
  the Store package folder (the `WindowsApps` alias is a reparse point the CLI rejects), and
  Desktop must have been signed in once — until then a sign-in dialog blocks the bridge with
  "Host is not ready to accept operations". Short (8.3) paths such as `ANDREA~1.GIO` also failed
  to open; use long paths.
- *Data loads without a click.* A freshly migrated PBIP opens with an empty model ("Some of the
  tables have incomplete or no data"). The Authoring MCP server's `model_operations
  RefreshWithXMLA` (read-write mode) against Desktop's local engine refreshes it: 10,194 Orders
  rows, Sales by Category matching. The data survives a Bridge reload from disk, so editing report
  files and re-screenshotting is cheap. (Microsoft's external-tools page says processing commands
  aren't supported in Desktop; through this server it worked.)
- *DAX can be validated by the real engine, with or without data.* `dax_query_operations
  Validate` caught both a syntax error and an unknown column — far stronger than our bracket
  check in `validate/dax.py`.
- *The MCP server edits a PBIP folder offline*: `ConnectFolder` → `measure_operations Update` →
  `database_operations ExportToTmdlFolder`, and the result loads in TOM. But it re-serializes
  every file in Microsoft's style (CRLF, measures before columns, unquoted `sourceColumn`, deeper
  M indentation), so the first agent edit would diff the whole model. Normalize the baseline
  through the same serializer before the first commit, or make our writer emit that style.
- *Python drives it from LangGraph*: `langchain-mcp-adapters` loads the 21 tools as LangChain
  tools; calls must share one session, because the server keeps its connection per session.
- *EULA.* The server refuses every call until its EULA is accepted, and its own tool description
  forbids an agent from accepting it unprompted. The user accepted it per process for these
  spikes; t2pbi will only start the server when the operator sets
  `PBI_MODELING_MCP_ACCEPT_EULA=true`, never on its own.
- *Embedded Tableau thumbnails are good enough to judge.* Book1's 192px images clearly show what
  our output loses — see the new known gaps in BACKLOG (monthly vs daily dates, the missing
  colour split).

---

## 2026-11-25 — Direction: deterministic first pass, then an agentic refinement stage

**Context.** The pipeline is a deterministic Tableau → Power BI schema translation with LLM help
at two points (DAX translation, unmatched visuals). Every check we have measures *validity* —
does the project open, are the files well-formed — and nothing measures *fidelity*: does the
Power BI report show and mean what the Tableau one did. The long tail of Tableau features
(table calcs, sets, dual axes, actions, formatting) is where migrations fail, and more
deterministic rules alone won't cover it.

**Options considered.**
- *A — keep the deterministic pass and add an agentic refinement stage* that takes the baseline
  `.pbip`, the IR and the migration report, edits the project files with tools, and checks its
  work against validators, the Power BI engine and the original Tableau visuals.
- *B — rebuild as a fully agentic system* where each node is an agent that builds its component,
  reads documentation, validates and repairs itself, keeping a memory of learnings.

**Decision: A.** Parsing workbook XML and writing TMDL/PBIR have exact answers; an LLM does that
worse, slower and at higher cost than code, and would rediscover the format constraints in the
reference table below on every run. A migration product also has to be reproducible (same
workbook → same output) to be regression-tested, audited and costed. The agent's effort goes
where the input is genuinely ambiguous and where fidelity has to be judged.

**Shape of the agentic stage** (not built yet):
- A *subgraph* inside the existing LangGraph graph, after the deterministic pass — not a second
  system, so the graph stays the only runner.
- Validators become the agent's *tools*: every edit is followed by validation.
- It can only improve on the baseline: a change is kept only if it validates and fidelity
  doesn't drop, with each change committed to git so it can be rolled back. Same principle as
  `LLMVisualRouter` ("can only improve on the guess").
- Its work list is bounded: report issues plus fidelity gaps, under a budget, with a person
  approving semantic choices.
- This supersedes the idea of a graph-level `repair_ir` loop after the post-build validators;
  repair after writing becomes the agent's job.

**Tooling, checked against Microsoft Learn on 2026-11-25:**
- *Power BI Authoring MCP server* (local version generally available): edits semantic models in
  PBIP/TMDL folders on disk or in Desktop, and runs DAX queries — real DAX validation against the
  engine. It cannot edit report pages.
- *Power BI Desktop Bridge* and its `powerbi-desktop` CLI: open a PBIP in a running Desktop,
  reload after on-disk edits, capture each page as a PNG. Windows only, local named pipe, one
  operation at a time per Desktop window.
- *`powerbi-report-author`*: PBIR validation (already used). It ignores the semantic model
  entirely — tested by deleting the whole `SemanticModel` folder, which still validated.
- *TOM `TmdlSerializer`* (NuGet `Microsoft.AnalysisServices`): a throwaway spike loaded all three
  samples cleanly and caught 9 of 13 deliberate defects (indentation, unknown keywords, invalid
  enum and data-type values, unquoted names, duplicate columns, relationships to missing
  columns or tables, a deleted table file). It does not check DAX or M text, or a measure named like a column. Gotcha: the
  `dotnet` CLI hung on a background workload-update check after each build until
  `DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE=1` was set.
- *Report export to PDF/PNG via REST* needs Fabric/Premium capacity; Desktop Bridge screenshots
  cover local use.
- Tableau workbooks can embed sheet thumbnails: `Book1.twb` has 6, up to 192px — enough for a
  coarse chart-type and layout check.

**Constraints to design for.** Rendering needs the data (Desktop must refresh the model, so it
needs source access and credentials). Screenshot comparison is a weak signal; comparing numbers
(DAX query versus Tableau data) is stronger. Screenshots and query results carry customer data
to the model, so the LLM endpoint's data boundary is a decision, not a default.

**Deferred.** Turning recurring agent fixes into reusable rules — at run time (retrieved
examples, declarative rules) or as code via CI/CD — was discussed and left for later as too
complex for now.

---

## 2026-11-25 — Dead-code sweep after the graph refactor

A review of `main` (vulture plus reading every module) for code nothing reaches and logic written
twice. What was removed or merged, and why some of it is not obvious:

- **The DAX check in `validate_model` is gone.** It re-ran the bracket check over every measure
  as a "safety net", but since the graph refactor it could never fire: `validate_expressions`
  only lets a candidate into the model after the identical check, and everything else stays
  `BLANK()`. The expression loop is now the one place DAX is judged. If a future node writes DAX
  by another route, it must go through `check_dax` too, not rely on a later sweep.
- **IR fields that nothing filled or read were removed:** `PBIModel.expressions` /
  `PBIExpression` (never populated, and the TMDL writer never emitted it, so it implied support
  that didn't exist), `TableauDatasource.is_parameters`, `TableauRelation.type`,
  `MappingResult.report`. Re-add `expressions` together with an emitter when parameters are
  migrated.
- **`langgraph>=0.2` was a false floor.** The graph uses `context_schema` and `Runtime`, which
  arrived in LangGraph 0.6; an older install would have satisfied `pyproject.toml` and then
  failed at import. `langchain-core` is now declared, since the code imports it directly.
- **ImportError fallbacks in `llm/client.py` were removed.** `langchain-openai` and
  `azure-identity` are required dependencies, so "not installed" could not happen. The imports
  stay inside `build_chat_model` so the `--no-llm` path never loads them.
- **Merged duplicates:** one `strip_brackets` (was four copies), one `new_object_name` for
  page/visual ids, one `SCHEMA_BASE`, one `run_report_author` used by both the validation gate and
  `scripts/refresh_visual_catalog.py`, and severity as `IssueSeverity` end to end instead of
  strings mapped in each node. Tests share fixtures and a single `FakeChat` in `conftest.py`.

**Kept deliberately:** `MigrationReport.has_errors` and the visual-mapping `confidence` /
`rationale` look dead but are the hooks for two open decisions (see BACKLOG, *Decisions needed*).

**Verified** the same way as the refactor: every sample migrated with no LLM, with a fake LLM that
forces the repair loop, and with one whose visual-router answers are garbage. All emitted files
and report issues were snapshotted before and after, and were identical across all nine runs. The
shared CLI helper was checked against the real `powerbi-report-author` (its `catalog describe`
output matches the committed catalog). None of the samples reaches the LLM visual router, so its
fallback rests on the unit tests in `tests/test_visual_router_llm.py`.

---

## 2026-11-24 — The LangGraph graph is the only runner

**Context.** LangGraph was chosen at the start because this is an agentic accelerator:
validation stages, repair loops, LLM calls for the hard cases, and later human review. In
practice the CLI ran a plain `for stage in STAGES` loop; `build_graph()` wired the same stages
into a straight-line graph that nothing called or tested. The things LangGraph exists for had
been built *inside* nodes instead: the DAX retry loop lived in `_translate_and_repair`, the
"is there an LLM?" and "run the validator?" decisions were `if`s at the top of node functions.

**Decision.** One compiled `StateGraph` runs every migration (`graph/pipeline.py`), and the
hidden control flow became graph structure:

- *LLM or not* → router after `map_model`: `translate` / `no_llm` / `nothing_to_translate`.
- *DAX repair* → `translate_expressions` ⇄ `validate_expressions` loop on the `repair` route.
  Translation and checking are now pure helpers in `mapping/expression_translator.py`; the loop,
  the feedback and the attempt budget (`max_translation_attempts`) belong to the graph.
- *External PBIR validation* → router after `assemble`: `validate` / `skip`.

**State vs context.** `MigrationState` (Pydantic) holds data only; nodes return partial updates
and never mutate it. Issues use an append reducer (`Annotated[list, operator.add]`). The chat
model and run settings live in `MigrationContext`, injected via `runtime.context`, so the state
stays serializable. That is the precondition for checkpointing and human-in-the-loop interrupts,
neither of which is wired yet.

**Verified behaviour-preserving.** Before the change, every sample was migrated twice (no LLM,
and a fake LLM that answers each formula with unbalanced DAX first, forcing the repair path) and
every emitted file and report issue was snapshotted, with random ids masked and issues compared
as a sorted multiset. After the change the snapshot was identical on all six runs. One trap in
building that harness: Python's `hash()` of a string is randomized per process, so the fake LLM's
answers differed between two baseline runs until it used `zlib.crc32`.

**Not done here, deliberately.** Whether `validate_model` errors should block `assemble` (today
they are reported only, and the CLI exits 0) is a behaviour change, not a refactor; it is now a
one-edge change when decided.

---

## 2026-09-01 — Trellis vs legend is deterministic, so don't ask the LLM

The LLM router was being handed a choice it had no business making: whether a dimension becomes
small multiples (`Rows`) or an overlaid legend series (`Series`). It flipped small multiples to
legend often enough to matter.

The insight: **the shelf structure already answers this.** A dimension sharing the measure's row
shelf is a trellis; a dimension on the colour encoding is a legend. Both are read
deterministically from the workbook, so the decision was moved out of the ambiguous path
entirely and the mapping now reports full confidence.

General principle this established: the LLM resolves cases where the *workbook itself* is
ambiguous, never cases where we simply hadn't looked hard enough at the input.

---

## 2026-08-31 — Guardrails: catalog, router fallback, and a real validation gate

Three defences added the same week, all aimed at the same failure mode — emitting a `.pbip` that
opens but renders blank.

**Visual catalog as a hard guardrail.** `src/t2pbi/mapping/data/visual_catalog.json` is
generated from `powerbi-report-author catalog describe`, Microsoft's authoritative capability
metadata, via `scripts/refresh_visual_catalog.py`. Every mapping is checked against it before
emission, so an unknown role, an over-filled role, or a measure placed in a `Grouping` role is
caught here rather than becoming a silently blank visual. Regenerate when targeting a newer
PBIR/Desktop version — do not hand-edit the JSON.

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
them is almost certainly a regression — the tests in `tests/test_pipeline.py` guard several.

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
| Multi-line TMDL expressions indented one level deeper than the object's properties | The next property (e.g. `formatString`) is read as part of the DAX; the file still loads |
