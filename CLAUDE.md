# Working in this repo

`t2pbi` migrates Tableau workbooks (`.twb` / `.twbx`) into Power BI Projects (`.pbip`).

## Read these first

- **[docs/architecture.html](docs/architecture.html)** — open in a browser: pipeline stages, the
  worksheet → visual decision order, Tableau → Power BI mapping rules, LLM touchpoints, gotchas.
- **[docs/DEVLOG.md](docs/DEVLOG.md)** — why the code is shaped the way it is, and a reference
  table of format constraints Power BI Desktop enforces. Check it before changing anything in
  `emit/`; several of those constraints look arbitrary and are not.
- **[BACKLOG.md](BACKLOG.md)** — open work and unresolved questions.

## Keeping them current

- Add a **DEVLOG** entry (newest first) when a decision is made or reversed, or when a
  non-obvious constraint is discovered. Not for routine changes — commit messages cover those.
  Record the reasoning and how it was verified, not just the outcome.
- Update **BACKLOG** in the same commit as the work that opens or closes an item. Mark an item
  **(verified)** with the date when it has been confirmed against the code.
- Update **docs/architecture.html** when a change moves what it shows: a stage added or
  reordered, a visual-mapping rule, an LLM touchpoint, a gotcha fixed. Bump the commit and date in
  its footer.
- Propose these updates as part of the change; don't wait to be asked.

## Architecture in one line

`.twb`/`.twbx` → ingest → parse → **Tableau IR** ([ir/tableau_ir.py](src/t2pbi/ir/tableau_ir.py))
→ mapping → **Power BI IR** ([ir/powerbi_ir.py](src/t2pbi/ir/powerbi_ir.py)) → emit (TMDL + PBIR)
→ validate. Orchestrated by one LangGraph `StateGraph` in
[graph/pipeline.py](src/t2pbi/graph/pipeline.py); its topology is drawn in that file's docstring.

## The graph is the only runner

This is an agentic accelerator: validation, repair and LLM steps belong in the graph, where they
are visible, testable and checkpointable. There is exactly one way to run a migration —
`build_graph()` / `migrate()` — used by the CLI and the tests alike. Do not add a second
orchestration path.

- **New stage → new node** in [graph/nodes.py](src/t2pbi/graph/nodes.py). Nodes stay thin: they
  delegate to `ingest/`, `parse/`, `mapping/`, `validate/`, `emit/` and return a partial state
  update. They never mutate the state they were given (copy first, as `validate_expressions` does).
- **New decision, loop or gate → conditional edge** with a named route, not an `if` or retry
  loop inside a node. The DAX repair loop (`validate_expressions` → `repair` →
  `translate_expressions`) is the model to follow.
- **State vs context.** `MigrationState` ([graph/state.py](src/t2pbi/graph/state.py)) holds data
  only, so it stays serializable. Clients, flags and budgets go in `MigrationContext` and are
  read through `runtime.context`.
- **Issues** are returned as `{"issues": [...]}`; the reducer appends them. Mapping functions
  still take a `MigrationReport`; a node creates a fresh one and returns its `issues`.
- `tests/test_graph.py` pins the topology and routing — update it with any graph change.

## Conventions

- Deterministic code parses and generates; LLMs are used **only** where the workbook is genuinely
  ambiguous. If the input already answers the question, read the input. Every LLM path must fall
  back safely to the deterministic result, and the graph must still complete with
  `MigrationContext(chat_model=None)`.
- Unmappable input degrades with a report warning; it does not raise. A partial migration that
  opens beats a clean failure.
- The visual catalog JSON is generated, not hand-edited — refresh it with
  `scripts/refresh_visual_catalog.py`. Visual role names come from the catalog, never intuition.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
```

Both must pass. For refactors that should not change output, diff the generated `.pbip` against
the previous revision rather than trusting the tests alone: run both revisions (e.g. via
`git worktree`) over every sample and compare report pages by display name, not by folder path —
page and visual folder names are random per run.

## Parked work

`refactor/source-pluggable` (Spotfire / multi-source refactor) is parked — see BACKLOG.md. Don't
merge it or port its changes unless asked.
