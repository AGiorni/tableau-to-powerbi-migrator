# Working in this repo

`t2pbi` migrates Tableau workbooks (`.twb` / `.twbx`) into Power BI Projects (`.pbip`).

## Read these first

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
- Propose these updates as part of the change; don't wait to be asked.

## Architecture in one line

`.twb`/`.twbx` → ingest → parse → **Tableau IR** ([ir/tableau_ir.py](src/t2pbi/ir/tableau_ir.py))
→ mapping → **Power BI IR** ([ir/powerbi_ir.py](src/t2pbi/ir/powerbi_ir.py)) → emit (TMDL + PBIR)
→ validate. Orchestrated as a LangGraph pipeline in [graph/](src/t2pbi/graph/).

## Conventions

- Deterministic code parses and generates; LLMs are used **only** where the workbook is genuinely
  ambiguous. If the input already answers the question, read the input. Every LLM path must fall
  back safely to the deterministic result.
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
