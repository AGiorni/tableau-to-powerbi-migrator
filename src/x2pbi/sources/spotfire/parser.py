"""Turn a loaded Spotfire analysis into the neutral source IR.

Not yet implemented. This module is the *only* thing standing between the migrator and Spotfire
support: once ``parse_analysis`` returns a populated ``SourceWorkbook``, the existing model
mapper, Power Query generator, expression translator, visual mapper, validator and PBIP emitter
all apply unchanged.

Concept mapping to fill in
--------------------------
Spotfire concept                     -> source IR
    data table                        -> SourceDatasource (one per table)
    data connection / linked source    -> SourceConnection; set ``kind`` from the neutral
                                         ConnectionKind enum and keep Spotfire's own name in
                                         ``native_class`` (an unmapped kind emits an
                                         empty-table stub plus a report warning, which is the
                                         intended degradation, not a failure)
    column                            -> SourceColumn (``role`` measure vs dimension,
                                         ``aggregation`` = its default aggregation)
    calculated column / custom expr    -> SourceCalculatedField; set
                                         ``is_scoped_aggregation`` for OVER()/node-navigation
                                         expressions so they are prompted for CALCULATE-shaped
                                         DAX rather than a plain aggregation
    document property                 -> SourceParameter
    relation between data tables       -> SourceRelation + SourceJoin
    visualization                     -> SourceView (see the mark note below)
    page                              -> SourceDashboard, one SourceDashboardZone per
                                         visualization, plus one ``kind="filter"`` zone per
                                         filter that should become a slicer

Marks
-----
``SourceView.mark`` drives the deterministic chart rules, so map each visualization type onto
the closest :class:`~x2pbi.ir.source_ir.MarkType` (bar chart -> BAR, line chart -> LINE, area
chart -> AREA, cross table -> TEXT) and use ``MarkType.OTHER`` for anything without a clean
equivalent (scatter plot, box plot, treemap, ...). ``OTHER`` is not a failure: it routes the
view to the LLM visual router, which picks from the full visual catalog. Always keep
Spotfire's own name in ``native_mark`` — the router puts it in the prompt.

Axes
----
``cols`` is the X axis and ``rows`` is the Y axis, matching the shelf convention the visual
mapper reads: a measure on ``cols`` alone produces a horizontal bar chart, a dimension sharing
the measure's ``rows`` shelf becomes small multiples, and a dimension on the colour encoding
becomes a legend series.

Layout
------
Zone ``x``/``y``/``w``/``h`` are expressed on a square grid of
:data:`~x2pbi.ir.source_ir.DASHBOARD_GRID`; scale Spotfire's own layout coordinates onto it and
the visual mapper handles the rest.
"""

from __future__ import annotations

from x2pbi.ir.source_ir import SourceWorkbook
from x2pbi.sources.spotfire.loader import LoadedAnalysis


def parse_analysis(loaded: LoadedAnalysis) -> SourceWorkbook:
    """Build a :class:`SourceWorkbook` IR from a loaded Spotfire analysis.

    The result must set ``dialect="spotfire"`` so the expression translator picks the Spotfire
    prompt rules from ``SPOTFIRE_DIALECT``.
    """

    raise NotImplementedError(
        "Spotfire parsing is not implemented yet. Fill in this module (see the module docstring "
        "for the Spotfire concept -> source IR mapping); everything downstream already works."
    )
