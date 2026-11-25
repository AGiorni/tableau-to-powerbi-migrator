# Backlog

Open work and unresolved questions. Rationale for how things got this way lives in
[docs/DEVLOG.md](docs/DEVLOG.md).

Items marked **(verified)** were confirmed against the code at the date shown; the rest are
judgement calls. Remove an item when it ships, and add a DEVLOG entry if closing it involved a
decision worth remembering.

## Known gaps in Tableau migration

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

## Open questions

- [ ] **Is `reference_pbip/` a fixture or scratch?** It is gitignored but present in the working
  tree. If tests or a contributor would want it as a reference for Desktop-parity output, it
  should be tracked; if it is scratch, leave it ignored and say so here.

- [ ] **How much does LOD → DAX translation actually get right?** The repair loop catches
  *syntax* failures, and reference errors are only warnings. Nothing measures semantic
  correctness. A small set of known-answer calcs with expected DAX would turn this from a guess
  into a number.

## Parked

- **Source-pluggable refactor (Spotfire support)** — branch `refactor/source-pluggable`, pushed,
  not merged. Splits the code at a tool-neutral source IR, renames the package `t2pbi` → `x2pbi`,
  and adds a Spotfire adapter stub. Its own `docs/DEVLOG.md` records the reasoning. Parked
  2026-11-24 to focus on Tableau; it branched from `main` at `f691613`, so expect conflicts
  with any `main` work done since.
