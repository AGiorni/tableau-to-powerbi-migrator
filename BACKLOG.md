# Backlog

Open work and unresolved questions. Rationale for how things got this way lives in
[docs/DEVLOG.md](docs/DEVLOG.md).

Items marked **(verified)** were confirmed against the code at the date shown; the rest are
judgement calls. Remove an item when it ships, and add a DEVLOG entry if closing it involved a
decision worth remembering.

## Roadmap: agentic refinement (decided 2026-11-25)

Deterministic first pass, then an agentic refinement stage; see the DEVLOG entries of 2026-11-25.
Decided for this roadmap: Azure OpenAI as the LLM, Book1 as the only data-backed workbook for
now, GitHub Actions for CI, and an agent that edits PBIR/TMDL files directly. In order:

0. [x] **Pinned toolchain and feasibility spikes** *(done 2026-11-25)*. All four spikes passed:
   unattended rendering (after one-time machine setup), data refresh through the Authoring MCP
   server, DAX validation by the engine, and Python/LangGraph driving the server. Details in
   DEVLOG.
1. [ ] **Fidelity harness.** For each sample: Tableau visuals (embedded thumbnails, or exports
   when available) and data, the Power BI output rendered through the Desktop Bridge (page PNGs)
   and queried with DAX through the Authoring MCP server, and a fidelity score. Comes first:
   without a measurement we can't tell whether the agent helps.
2. [ ] **`validate_tmdl` node.** A small .NET tool in the repo (`tools/`) that loads the written
   TMDL with TOM's `TmdlSerializer` and prints JSON diagnostics; a report-only graph node after
   `assemble`, skipped with an INFO issue when .NET is unavailable, mirroring `validate_pbir`.
3. [ ] **Run both validators over every sample in the tests**, so writer bugs fail CI instead
   of reaching users.
4. [ ] **Agentic refinement subgraph.** A tool-using agent inside the graph that works through
   report issues and fidelity gaps on the baseline `.pbip`. Tools: edit PBIR/TMDL files, the
   Authoring MCP server, `powerbi-report-author validate`, Desktop Bridge reload and screenshot,
   Tableau thumbnails. Keeps a change only if it validates and fidelity doesn't drop; one git
   commit per change; bounded by a budget.

Supporting work, to be designed with step 4: a structured `diagnostics` field in the state (the
agent's work list, separate from the report's `issues`); the output project as a git repository
(baseline commit, one commit per accepted change), so a failed run never leaves a half-broken
project; and a baseline normalized through Microsoft's TMDL serializer, so the first model edit
doesn't diff every file.

- [ ] **Use the engine's DAX validation in the repair loop.** When the Authoring MCP server and a
  Desktop instance are available, `dax_query_operations Validate` catches unknown columns and
  real syntax errors that `validate/dax.py`'s bracket check misses; feed its message back to the
  LLM on the `repair` route, keeping the bracket check as the offline fallback.
- [ ] **Grow the test corpus beyond Book1.** Only Book1 has real data; the other samples are
  synthetic. More data-backed workbooks are what turn the fidelity harness from a smoke test into
  a measure.

## Known gaps in Tableau migration

- [ ] **Date parts on shelves are ignored, so time axes plot by day.** *(verified 2026-11-25)*
  Tableau's `tmn:` (truncated month), `yr:`, `qr:` and similar prefixes reach `_resolve_field` in
  [visual_mapper.py](src/t2pbi/mapping/visual_mapper.py) as an aggregation code that
  `_AGG_FUNCTION` doesn't know, so the date becomes a plain column. Book1's "Sales by Category" is
  monthly in Tableau and a flat daily line in Power BI (seen in the step-0 screenshots).

- [ ] **Dimension-role calculated fields become measures, so colour splits are dropped.**
  *(verified 2026-11-25)* Every calc maps to a measure in
  [model_mapper.py](src/t2pbi/mapping/model_mapper.py); Book1's "Order Profitable?" (a dimension,
  on the colour shelf) can't go in a `Series` role, so `enforce_wells` drops it and the chart loses
  its two-colour split.

- [ ] **Parameters are parsed but never migrated.** *(verified 2026-11-24)*
  [tableau_parser.py](src/t2pbi/parse/tableau_parser.py) fills `TableauWorkbook.parameters` —
  including `current_value` and `allowable_values` — and nothing consumes it. They should become
  Power BI what-if parameters or M parameters. Dead data until then.

- [ ] **Sort direction is never derived from the source.** *(verified 2026-11-24)*
  `sort_descending` defaults to `True` in [visual_decision.py](src/t2pbi/mapping/visual_decision.py)
  and is never set from the input; [visual_mapper.py](src/t2pbi/mapping/visual_mapper.py) only
  picks the sort *field* (the first measure). A gap since the shelf-aware mapping work on
  2026-08-28.

- [ ] **Unsupported connection kinds emit an empty-table stub.** *(verified 2026-11-24)* Only
  `textscan` (CSV), `excel-direct`, `sqlserver`, `postgres` and `mysql` produce real Power Query.
  Anything else (Oracle, Snowflake, BigQuery, extracts-as-source) migrates structure but no data,
  with a report warning. Add a branch in [powerquery.py](src/t2pbi/mapping/powerquery.py) as
  they come up.

- [ ] **`TableauWorksheet.datasource_names` and `TableauDashboard.worksheet_names` are parsed but
  unused.** *(verified 2026-11-24)* Harmless provenance today. Either use them for
  multi-datasource disambiguation or drop them from [tableau_ir.py](src/t2pbi/ir/tableau_ir.py).

- [ ] **Number formats are not migrated.** *(verified 2026-11-25)* `PBIColumn.format_string` and
  `PBIMeasure.format_string` are emitted by [tmdl_writer.py](src/t2pbi/emit/tmdl_writer.py) but
  the mapper never sets them, so currency and percentages lose their formatting. Tableau keeps
  the format on the `<column>` element (`default-format`); it needs reading and translating to
  a .NET format string.

- [ ] **Table-name duplicates are checked case-sensitively.** *(verified 2026-11-25)*
  `_check_duplicate_tables` in [model_validate.py](src/t2pbi/validate/model_validate.py)
  compares names exactly, but Power BI object names are case-insensitive (to our knowledge) and so
  are Windows file names: `Orders` and `orders` pass the check, then overwrite each other's
  `tables/<name>.tmdl` without any warning.

- [ ] **`validate_pbir` reports under the stage name `preflight`, but it runs after the
  build.** *(verified 2026-11-25)* The name is left over from earlier code. Renaming it changes
  report output, so bundle it with the "should validation stop the run?" decision.

## Decisions needed

- [ ] **Should validation errors stop the run?** *(verified 2026-11-25)* Today `validate_model`
  and `validate_pbir` only report: the `.pbip` is always written and the CLI always exits 0, so
  a script can't tell a clean run from a broken one. Options: a non-zero exit code when
  `MigrationReport.has_errors` (kept for this; currently unused), or a router after
  `validate_model` that skips `assemble`, or both. Choosing is a behaviour change, not a
  refactor; the graph makes either a one-edge change.

- [ ] **Surface visual-mapping confidence and rationale, or drop them?** *(verified 2026-11-25)*
  `MappingDecision.confidence` and `.rationale` are filled by the rules, `DefaultRouter` and
  `LLMVisualRouter`, but nothing reads them: the "Review recommended" INFO issue in
  [visual_mapper.py](src/t2pbi/mapping/visual_mapper.py) says only who decided. Recommended:
  put both in that issue's `detail`, so a reviewer can triage low-confidence visuals first.
  With the agentic stage on the roadmap, low confidence is also a natural entry for the agent's
  work list.

## Open questions

- [ ] **Is `reference_pbip/` a fixture or scratch?** It is gitignored but present in the working
  tree. If tests or a contributor would want it as a reference for Desktop-parity output, it
  should be tracked; if it is scratch, leave it ignored and say so here.

- [ ] **How much does LOD → DAX translation actually get right?** The repair loop catches
  *syntax* failures, and reference errors are only warnings. Nothing measures semantic
  correctness. A small set of known-answer calcs with expected DAX would turn this from a guess
  into a number.

## Parked

- **Turning recurring agent fixes into reusable rules** — at run time (approved fixes retrieved as
  examples, or declarative rules the deterministic pass reads) or as code through CI/CD.
  Discussed 2026-11-25 and deferred as too complex for now. If it is picked up, the first piece
  is a structured record of every change the agent makes, since every tier is built from those
  records.

- **Source-pluggable refactor (Spotfire support)** — branch `refactor/source-pluggable`, pushed,
  not merged. Splits the code at a tool-neutral source IR, renames the package `t2pbi` → `x2pbi`,
  and adds a Spotfire adapter stub. Its own `docs/DEVLOG.md` records the reasoning. Parked
  2026-11-24 to focus on Tableau; it branched from `main` at `f691613`, so expect conflicts
  with any `main` work done since.
