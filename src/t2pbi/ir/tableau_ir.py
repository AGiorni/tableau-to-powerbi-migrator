"""Tableau-side Intermediate Representation.

These models capture the structural facts parsed from a ``.twb`` XML document that are
relevant to a Power BI migration. They are intentionally lossy: only fields we know how to
map (or want to report on) are retained.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class TableauRole(str, Enum):
    """Whether a field behaves as a dimension or a measure in Tableau."""

    DIMENSION = "dimension"
    MEASURE = "measure"


class TableauDataType(str, Enum):
    """Tableau column data types as they appear in workbook XML."""

    INTEGER = "integer"
    REAL = "real"
    STRING = "string"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    UNKNOWN = "unknown"


class TableauConnection(BaseModel):
    """A physical connection inside a datasource (``<connection>`` element)."""

    cls: str = Field(description="Connection class, e.g. 'sqlserver', 'excel-direct', 'textscan'.")
    name: str | None = Field(default=None, description="Named-connection id relations refer to.")
    server: str | None = None
    dbname: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    filename: str | None = None
    directory: str | None = None
    username: str | None = None

    model_config = {"populate_by_name": True}


class TableauRelation(BaseModel):
    """A physical table referenced by a datasource (``<relation type='table'/'text'>``)."""

    name: str = Field(description="Relation alias used in join clauses and the <cols> map.")
    table: str | None = Field(default=None, description="Physical object id, e.g. '[dbo].[Orders]'.")
    connection_name: str | None = Field(default=None, description="Named-connection this relation uses.")


class TableauJoin(BaseModel):
    """A join clause between two relations (``<relation type='join'>``)."""

    left_table: str
    left_column: str
    right_table: str
    right_column: str
    join_type: str = "inner"


class ColumnOrigin(BaseModel):
    """Where a logical field physically comes from, parsed from the ``<cols>`` map."""

    table: str = Field(description="Owning relation name, e.g. 'Orders'.")
    source_column: str = Field(description="Physical column name, e.g. 'Sales'.")


class TableauColumn(BaseModel):
    """A physical or logical column exposed by a datasource."""

    name: str = Field(description="Internal Tableau name, e.g. '[Sales]'.")
    caption: str | None = Field(default=None, description="Human-friendly display name.")
    datatype: TableauDataType = TableauDataType.UNKNOWN
    role: TableauRole = TableauRole.DIMENSION
    aggregation: str | None = Field(
        default=None, description="Default aggregation, e.g. 'Sum', 'Count', 'Year'."
    )
    hidden: bool = False

    @property
    def display_name(self) -> str:
        return self.caption or strip_brackets(self.name)


class TableauCalculatedField(BaseModel):
    """A calculated field (``<column>`` with a ``<calculation>`` child)."""

    name: str
    caption: str | None = None
    formula: str = Field(description="Raw Tableau calculation formula.")
    datatype: TableauDataType = TableauDataType.UNKNOWN
    role: TableauRole = TableauRole.MEASURE
    aggregation: str | None = None
    is_lod: bool = Field(
        default=False, description="True if the formula contains an LOD expression ({FIXED/INCLUDE/EXCLUDE})."
    )

    @property
    def display_name(self) -> str:
        return self.caption or strip_brackets(self.name)


class TableauParameter(BaseModel):
    """A workbook parameter."""

    name: str
    caption: str | None = None
    datatype: TableauDataType = TableauDataType.UNKNOWN
    current_value: str | None = None
    allowable_values: list[str] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.caption or strip_brackets(self.name)


class TableauExtract(BaseModel):
    """The data snapshot of a datasource: a Hyper database (``.hyper``) Tableau keeps for it.

    In a packaged workbook (``.twbx``) the file is inside the package, at ``path``; that's all the
    data a workbook downloaded from Tableau Public carries, since the source it was built from
    stays on its author's machine. A plain ``.twb`` names a file elsewhere (often a temp file).
    """

    path: str = Field(description="The .hyper file: a path inside the package, e.g. 'Data/Extracts/x.hyper'.")
    tables: list[str] = Field(
        default_factory=list,
        description="Its tables in schema 'Extract': 'Extract' for a one-table source, or one per "
        "logical table named '<table>_<32 hex>'.",
    )


class TableauDatasource(BaseModel):
    """A Tableau datasource: connections plus the fields it exposes."""

    name: str
    caption: str | None = None
    connections: list[TableauConnection] = Field(default_factory=list)
    columns: list[TableauColumn] = Field(default_factory=list)
    calculated_fields: list[TableauCalculatedField] = Field(default_factory=list)
    relations: list[TableauRelation] = Field(default_factory=list)
    joins: list[TableauJoin] = Field(default_factory=list)
    column_origins: dict[str, ColumnOrigin] = Field(
        default_factory=dict,
        description="Raw logical column name (e.g. '[Sales]') -> physical origin.",
    )
    extract: TableauExtract | None = None

    @property
    def display_name(self) -> str:
        return self.caption or self.name


class TableauFieldRef(BaseModel):
    """A field placed on a worksheet shelf or encoding (a Tableau column-instance).

    Parsed from the internal ``[agg:Field Name:role]`` form, e.g. ``sum:Sales:qk``, which may
    also carry a table calculation in front (``pcto:sum:Sales:qk``), a visual-total suffix after
    the field (``sum:Sales:vtmin:qk``) and an instance number at the end (``usr:Calc_1:qk:7``).
    """

    field: str = Field(description="Field display name, e.g. 'Sales'.")
    aggregation: str = Field(default="none", description="Tableau agg code: none/sum/avg/cnt/...")
    role: TableauRole = TableauRole.DIMENSION
    raw: str = Field(default="", description="Original column-instance name for debugging.")
    table_calc: str | None = Field(
        default=None,
        description="A quick table calculation applied on top: pcto (percent of total), pcdf "
        "(percent difference), rsum (running total), rank, ... Not migrated yet.",
    )


class TableauEncoding(BaseModel):
    """A mark encoding (color, size, text, tooltip, detail, ...) bound to a field."""

    kind: str = Field(description="Encoding type: color/size/text/label/tooltip/detail/...")
    field: TableauFieldRef


class TableauSort(BaseModel):
    """A sort on a shelf: ``dimension`` ordered by ``by`` (a measure), or by itself when None."""

    dimension: TableauFieldRef
    direction: Literal["asc", "desc"] = "asc"
    by: TableauFieldRef | None = None


class TableauWorksheet(BaseModel):
    """A worksheet: its mark type and the fields on its shelves and encodings."""

    name: str
    datasource_names: list[str] = Field(default_factory=list)
    mark_class: str = Field(default="Automatic", description="Tableau mark type, e.g. 'Bar'.")
    rows: list[TableauFieldRef] = Field(default_factory=list)
    cols: list[TableauFieldRef] = Field(default_factory=list)
    encodings: list[TableauEncoding] = Field(default_factory=list)
    # Measures listed by a Measure Names filter (a Measure Names/Measure Values table).
    measure_names: list[TableauFieldRef] = Field(default_factory=list)
    is_map: bool = Field(default=False, description="Geographic worksheet (map source/geometry).")
    title: str | None = Field(
        default=None,
        description="Static text of a custom title, with <Sheet Name> filled in and field inserts "
        "removed; None when the sheet shows Tableau's default title (its name).",
    )
    title_fields: list[str] = Field(
        default_factory=list, description="Fields inserted into the title (shown as their filter values)."
    )
    tooltip_sheets: list[str] = Field(
        default_factory=list, description="Sheets embedded in this sheet's tooltip (viz in tooltip)."
    )
    sorts: list[TableauSort] = Field(default_factory=list)
    hidden: bool = Field(
        default=False,
        description="Hidden in Tableau: no tab of its own, so viewers see it only inside the "
        "dashboards (or stories) that show it. Tableau only lets a sheet used in one be hidden.",
    )


class TableauDashboardZone(BaseModel):
    """One placed item on a dashboard: an embedded worksheet or a filter (slicer) card."""

    kind: str = Field(description="'worksheet' or 'filter'.")
    worksheet: str | None = Field(default=None, description="Embedded/target worksheet name.")
    field: TableauFieldRef | None = Field(default=None, description="Filtered field (filter zones).")
    # Position/size in Tableau's 100000 x 100000 dashboard grid.
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0


class TableauDashboard(BaseModel):
    """A dashboard: its layout zones (embedded worksheets and filter cards)."""

    name: str
    worksheet_names: list[str] = Field(default_factory=list)
    zones: list[TableauDashboardZone] = Field(default_factory=list)


class TableauWorkbook(BaseModel):
    """Root of the Tableau IR."""

    source_name: str = Field(description="Original workbook file name (without extension).")
    datasources: list[TableauDatasource] = Field(default_factory=list)
    parameters: list[TableauParameter] = Field(default_factory=list)
    worksheets: list[TableauWorksheet] = Field(default_factory=list)
    dashboards: list[TableauDashboard] = Field(default_factory=list)

    def display_names(self) -> dict[str, str]:
        """Internal field name -> the name Tableau shows (and the migrated model uses), for every
        column and calculated field whose two names differ. Shelves and formulas reference
        fields by internal name (``job_ads``, ``Calculation_1862…``); users see captions ("Job
        Ads", "Profit Ratio"), and the model's columns and measures are named after those."""

        names: dict[str, str] = {}
        for ds in self.datasources:
            for field in [*ds.columns, *ds.calculated_fields]:
                internal = strip_brackets(field.name)
                if field.display_name != internal:
                    names.setdefault(internal, field.display_name)
        return names


# Tableau's per-table row count ("Orders (Count)" in the data pane) is a column of datatype 'table'
# named "<logical table>_<32 hex digits>", placed on a shelf with a count aggregation.
_TABLE_COUNT = re.compile(r"^(.*)_[0-9A-Fa-f]{32}$")
_COUNT_AGGREGATIONS = {"cnt", "cntd"}


def counted_table(ref: TableauFieldRef) -> str | None:
    """The logical table whose rows ``ref`` counts, or None when it isn't a row count."""

    match = _TABLE_COUNT.match(ref.field)
    return match.group(1) if match and ref.aggregation in _COUNT_AGGREGATIONS else None


def strip_brackets(name: str) -> str:
    """Remove the surrounding square brackets from a Tableau name ('[Sales]' -> 'Sales')."""

    return name[1:-1] if name.startswith("[") and name.endswith("]") else name
