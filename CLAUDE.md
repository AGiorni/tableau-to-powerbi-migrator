# Working in this repo

`x2pbi` migrates workbooks from other BI tools into Power BI Projects (`.pbip`). Tableau is
supported; Spotfire has a registered adapter whose parser is not written yet.

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

Source file → **source adapter** (the only tool-specific code) → **source IR** → shared mapping
→ **Power BI IR** → emit. Adding a BI tool means writing one adapter and registering it in
[sources/\_\_init\_\_.py](src/x2pbi/sources/__init__.py); nothing else should need to change.

If a change to shared code (`mapping/`, `emit/`, `validate/`, `graph/`) requires knowing which
tool the input came from, that is a signal the normalization in
[ir/source_ir.py](src/x2pbi/ir/source_ir.py) is missing something — extend the enums and keep the
tool's own string in a `native_*` field, rather than leaking the tool name downstream.

## Conventions

- Deterministic code parses and generates; LLMs are used **only** where the source is genuinely
  ambiguous. If the input already answers the question, read the input. Every LLM path must fall
  back safely to the deterministic result.
- Unmappable input degrades with a report warning; it does not raise. A partial migration that
  opens beats a clean failure.
- The visual catalog JSON is generated, not hand-edited — refresh it with
  `scripts/refresh_visual_catalog.py`.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
```

Both must pass. For refactors that should not change output, diff the generated `.pbip` against
the previous revision rather than trusting the tests alone — the technique is described in the
2026-09-03 DEVLOG entry.
