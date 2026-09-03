# Backlog

Open work and unresolved questions. Rationale for how things got this way lives in
[docs/DEVLOG.md](docs/DEVLOG.md).

Items marked **(verified)** were confirmed against the code at the date shown; the rest are
judgement calls. Remove an item when it ships, and add a DEVLOG entry if closing it involved a
decision worth remembering.

## Next up

- [ ] **Implement the Spotfire parser.** Everything downstream of the source IR already works —
  this is the one remaining piece. The concept-by-concept mapping (data table → `SourceDatasource`,
  visualization → `SourceView`, `OVER()` → `is_scoped_aggregation`, page → `SourceDashboard`) is
  written out in the docstring of
  [sources/spotfire/parser.py](src/x2pbi/sources/spotfire/parser.py). The container reader in
  [sources/spotfire/loader.py](src/x2pbi/sources/spotfire/loader.py) comes first; `.dxp` is a
  container, unlike Tableau's plain-XML `.twb`.

- [ ] **Rename the GitHub repo.** It is `tableau-to-powerbi-migrator` but now holds a
  multi-source tool. GitHub redirects the old URL, so this is cheap. The local package and CLI
  are already `x2pbi`.

## Known gaps in Tableau migration

- [ ] **Parameters are parsed but never migrated.** *(verified 2026-09-03)* The Tableau parser
  fills `SourceWorkbook.parameters` — including `current_value` and `allowable_values` — and
  nothing consumes it. They should become Power BI what-if parameters or M parameters. Dead data
  until then.

- [ ] **Sort direction is never derived from the source.** *(verified 2026-09-03)*
  `PBIVisual.sort_descending` defaults to `True` and is never set from the input;
  `visual_mapper` only picks the sort *field* (the first measure). Noted as a gap since the
  shelf-aware mapping work on 2026-08-28.

- [ ] **Unsupported connection kinds emit an empty-table stub.** Only CSV, Excel, SQL Server,
  Postgres and MySQL produce real Power Query. Anything else (Oracle, Snowflake, BigQuery,
  extracts-as-source) migrates structure but no data, with a report warning. Add kinds to
  `ConnectionKind` and a branch in [mapping/powerquery.py](src/x2pbi/mapping/powerquery.py) as
  they come up.

- [ ] **`datasource_names` and `view_names` are parsed but unused.** *(verified 2026-09-03)*
  Harmless provenance today. Either use them for multi-datasource disambiguation or drop them.

## Open questions

- [ ] **Is `reference_pbip/` a fixture or scratch?** It is gitignored but present in the working
  tree. If tests or a contributor would want it as a reference for Desktop-parity output, it
  should be tracked; if it is scratch, leave it ignored and say so here.

- [ ] **How much does LOD → DAX translation actually get right?** The repair loop catches
  *syntax* failures, and reference errors are only warnings. Nothing measures semantic
  correctness. A small set of known-answer calcs with expected DAX would turn this from a guess
  into a number.

## Deferred (decided, not doing yet)

- **`pbip-core` as a shared package with thin per-source repos.** Only worth the three-repo
  overhead if release cadences genuinely diverge — see the 2026-09-03 DEVLOG entry.
