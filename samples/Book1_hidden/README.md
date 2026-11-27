# Book1_hidden

`Book1_hidden.twb` is [Book1](../Book1/) after "Hide All Sheets" on its dashboard, saved in
Tableau Public 2026.2. It tests which Tableau sheets become Power BI pages.

- The four worksheets on "Executive Overview" are hidden: Tableau viewers see them only inside
  the dashboard. The workbook records this on each sheet's window:
  `<window class='worksheet' hidden='true' name='Sales by Category'>`.
- "Tooltip: Profit Ratio by City" stays visible: it's shown in the map's tooltip, not on the
  dashboard, so "Hide All Sheets" leaves it alone.
- Tableau also tidied the dashboard layout when it re-saved: one nested container is gone and
  zone coordinates moved by a few units. The sheets themselves are unchanged.

Expected migration: two pages, "Executive Overview" (with all four worksheets as visuals) and
the tooltip sheet; Book1, with nothing hidden, has six. Pinned by `tests/test_hidden_sheets.py`.

No screenshots or `expected.toml` of its own: it looks like Book1. For `--refine`, pass Book1's
screenshots (`--reference-images samples\Book1\screenshots`); the hidden sheets' folders are set
aside, since those sheets have no page to judge. Same data as Book1 (see its README).
