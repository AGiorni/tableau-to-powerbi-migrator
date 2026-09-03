"""Source-side Intermediate Representation — the neutral shape every source adapter produces.

This module is the seam of the migrator. A source adapter (Tableau, Spotfire, ...) turns its
native file format into these models; nothing downstream of here knows which tool the workbook
came from. Adding a source means writing a parser that fills these in — the model mapper, the
expression translator, the visual mapper and the whole Power BI emit side come for free.

The models are intentionally lossy: only the facts we know how to map to Power BI (or want to
report on) are retained. Where a source has vocabulary of its own — a mark type, a connection
class, an aggregation code — the adapter normalizes it to the enums below and keeps the original
string in a ``native_*`` field, which stays available for prompts and diagnostics.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class FieldRole(str, Enum):
    """Whether a field behaves as a dimension or a measure in the source tool."""

    DIMENSION = "dimension"
    MEASURE = "measure"


class DataType(str, Enum):
    """Normalized column data types."""

    INTEGER = "integer"
    REAL = "real"
    STRING = "string"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    UNKNOWN = "unknown"


class Aggregation(str, Enum):
    """Normalized aggregation, covering both default (column-level) and applied (shelf) ones.

    ``OTHER`` is the escape hatch for a source aggregation with no Power BI equivalent — a date
    truncation such as Tableau's ``year``/``qtr``, for instance. It is deliberately distinct from
    ``NONE``: the field carries an aggregation we chose not to translate, so downstream code
    treats it as a plain column rather than an aggregated one.
    """

    NONE = "none"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    MEDIAN = "median"
    OTHER = "other"


class ConnectionKind(str, Enum):
    """Normalized data-source technology, which decides the Power Query function we emit."""

    CSV = "csv"
    EXCEL = "excel"
    SQL_SERVER = "sql_server"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    UNKNOWN = "unknown"


class MarkType(str, Enum):
    """Normalized mark/geometry of a view, i.e. how its data is drawn.

    ``AUTOMATIC`` means the source let the tool choose; ``OTHER`` covers marks we have no
    deterministic rule for (pie, circle, gantt, ...). Both are routed to the visual router rather
    than guessed at. The adapter keeps the source's own word in ``SourceView.native_mark``.
    """

    BAR = "bar"
    LINE = "line"
    AREA = "area"
    TEXT = "text"
    SQUARE = "square"
    AUTOMATIC = "automatic"
    OTHER = "other"


class SourceConnection(BaseModel):
    """A physical connection belonging to a datasource."""

    kind: ConnectionKind = ConnectionKind.UNKNOWN
    native_class: str = Field(
        default="", description="The source tool's own class string, e.g. 'sqlserver'."
    )
    name: str | None = Field(default=None, description="Named-connection id relations refer to.")
    server: str | None = None
    dbname: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    filename: str | None = None
    directory: str | None = None
    username: str | None = None

    model_config = {"populate_by_name": True}


class SourceRelation(BaseModel):
    """A physical table referenced by a datasource."""

    name: str = Field(description="Relation alias used in join clauses and column origins.")
    kind: str = "table"
    table: str | None = Field(default=None, description="Physical object id, e.g. '[dbo].[Orders]'.")
    connection_name: str | None = Field(
        default=None, description="Named connection this relation uses."
    )


class SourceJoin(BaseModel):
    """A join between two relations."""

    left_table: str
    left_column: str
    right_table: str
    right_column: str
    join_type: str = "inner"


class ColumnOrigin(BaseModel):
    """Where a logical field physically comes from."""

    table: str = Field(description="Owning relation name, e.g. 'Orders'.")
    source_column: str = Field(description="Physical column name, e.g. 'Sales'.")


class SourceColumn(BaseModel):
    """A physical or logical column exposed by a datasource."""

    name: str = Field(description="Field name as the source tool identifies it, e.g. 'Sales'.")
    caption: str | None = Field(default=None, description="Human-friendly display name.")
    datatype: DataType = DataType.UNKNOWN
    role: FieldRole = FieldRole.DIMENSION
    aggregation: Aggregation | None = Field(
        default=None, description="Default aggregation applied when the field is used."
    )
    hidden: bool = False

    @property
    def display_name(self) -> str:
        return self.caption or self.name


class SourceCalculatedField(BaseModel):
    """A field defined by a formula in the source tool's expression language."""

    name: str
    caption: str | None = None
    formula: str = Field(description="Raw formula, in the source dialect.")
    datatype: DataType = DataType.UNKNOWN
    role: FieldRole = FieldRole.MEASURE
    aggregation: Aggregation | None = None
    is_scoped_aggregation: bool = Field(
        default=False,
        description=(
            "True if the formula aggregates at a grouping other than the view's own — Tableau's "
            "LOD ({FIXED}/{INCLUDE}/{EXCLUDE}), Spotfire's OVER(). These need CALCULATE-shaped "
            "DAX rather than a plain aggregation, so translation is prompted differently."
        ),
    )

    @property
    def display_name(self) -> str:
        return self.caption or self.name


class SourceParameter(BaseModel):
    """A workbook-level parameter."""

    name: str
    caption: str | None = None
    datatype: DataType = DataType.UNKNOWN
    current_value: str | None = None
    allowable_values: list[str] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.caption or self.name


class SourceDatasource(BaseModel):
    """A datasource: its connections plus the fields it exposes."""

    name: str
    caption: str | None = None
    connections: list[SourceConnection] = Field(default_factory=list)
    columns: list[SourceColumn] = Field(default_factory=list)
    calculated_fields: list[SourceCalculatedField] = Field(default_factory=list)
    relations: list[SourceRelation] = Field(default_factory=list)
    joins: list[SourceJoin] = Field(default_factory=list)
    column_origins: dict[str, ColumnOrigin] = Field(
        default_factory=dict, description="Column name -> physical origin."
    )

    @property
    def display_name(self) -> str:
        return self.caption or self.name


class SourceFieldRef(BaseModel):
    """A field placed on a view's shelf or encoding."""

    field: str = Field(description="Field name, e.g. 'Sales'.")
    aggregation: Aggregation = Aggregation.NONE
    role: FieldRole = FieldRole.DIMENSION
    raw: str = Field(default="", description="Original source reference, for debugging.")


class SourceEncoding(BaseModel):
    """A mark encoding (color, size, text, tooltip, detail, ...) bound to a field."""

    kind: str = Field(description="Encoding type: color/size/text/label/tooltip/detail/...")
    field: SourceFieldRef


class SourceView(BaseModel):
    """One chart-bearing view: its mark type and the fields on its shelves and encodings.

    A Tableau worksheet is a view; so is a single Spotfire visualization. Each becomes one Power
    BI visual on its own page, and may additionally be placed on a dashboard page.
    """

    name: str
    datasource_names: list[str] = Field(default_factory=list)
    mark: MarkType = MarkType.AUTOMATIC
    native_mark: str = Field(
        default="", description="The source tool's own mark name, e.g. 'Pie'. Used in LLM prompts."
    )
    # Rows/cols follow the shelf convention: cols drive the X axis, rows the Y axis.
    rows: list[SourceFieldRef] = Field(default_factory=list)
    cols: list[SourceFieldRef] = Field(default_factory=list)
    encodings: list[SourceEncoding] = Field(default_factory=list)
    # Measures the view lists side by side (a Measure Names / multi-measure table).
    measure_names: list[SourceFieldRef] = Field(default_factory=list)
    is_map: bool = Field(default=False, description="Geographic view (map source/geometry).")


class SourceDashboardZone(BaseModel):
    """One placed item on a dashboard: an embedded view or a filter card."""

    kind: str = Field(description="'view' or 'filter'.")
    view: str | None = Field(default=None, description="Embedded/target view name.")
    field: SourceFieldRef | None = Field(default=None, description="Filtered field (filter zones).")
    # Position/size, normalized to a 0..GRID box (see ``DASHBOARD_GRID``).
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0


class SourceDashboard(BaseModel):
    """A dashboard: its layout zones (embedded views and filter cards)."""

    name: str
    view_names: list[str] = Field(default_factory=list)
    zones: list[SourceDashboardZone] = Field(default_factory=list)


# Dashboard zone coordinates are expressed on a square grid of this size, so adapters for tools
# with different native canvases normalize once and the visual mapper scales from a single basis.
DASHBOARD_GRID = 100_000


class SourceWorkbook(BaseModel):
    """Root of the source IR — everything one input file contributes to a migration."""

    source_name: str = Field(description="Original file name (without extension).")
    dialect: str = Field(
        default="", description="Adapter name that produced this IR, e.g. 'tableau'."
    )
    datasources: list[SourceDatasource] = Field(default_factory=list)
    parameters: list[SourceParameter] = Field(default_factory=list)
    views: list[SourceView] = Field(default_factory=list)
    dashboards: list[SourceDashboard] = Field(default_factory=list)
