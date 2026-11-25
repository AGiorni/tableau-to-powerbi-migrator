# Reference screenshots: Book1

Screenshots of `samples/input/Book1.twb` as Tableau shows it. Visual fidelity checks (step 1c)
compare the migrated Power BI pages against these; without them, the workbook's embedded 192px
thumbnails are used.

One PNG per worksheet and dashboard, named after it. Names are matched ignoring case, spaces and
punctuation (`src/t2pbi/fidelity/references.py`), so `tooltip profit ratio by city.png` is found
for "Tooltip: Profit Ratio by City". Suggested names:

| Tableau sheet | File |
|---|---|
| Key Performance Indicators | `Key Performance Indicators.png` |
| Sales by Category | `Sales by Category.png` |
| Sales by segment | `Sales by segment.png` |
| Tooltip: Profit Ratio by City | `Tooltip_ Profit Ratio by City.png` |
| profit ratio by geography | `profit ratio by geography.png` |
| Executive Overview (dashboard) | `Executive Overview.png` |

How to capture:
- Open the workbook as saved and **don't change filters or selections** — the migration sees the
  saved state, so the reference should too.
- Capture the view with its title, axes and legends. Including Tableau's Columns / Rows / Marks /
  Filters panes is useful: they show how the sheet is built. Windows Snipping Tool
  (`Win + Shift + S`) works.
- PNG, at normal screen size; no need to resize.

Book1 uses Tableau's public Superstore sample data, so these images can be committed. Screenshots
of real client workbooks contain client data: keep those out of git.
