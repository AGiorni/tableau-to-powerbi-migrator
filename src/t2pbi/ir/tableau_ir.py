"""Tableau-side Intermediate Representation.

These models capture the structural facts parsed from a ``.twb`` XML document that are
relevant to a Power BI migration. They are intentionally lossy: only fields we know how to
map (or want to report on) are retained.
"""

from __future__ import annotations

from enum import Enum

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
    type: str = "table"
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
        return self.caption or _strip_brackets(self.name)


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
        return self.caption or _strip_brackets(self.name)


class TableauParameter(BaseModel):
    """A workbook parameter."""

    name: str
    caption: str | None = None
    datatype: TableauDataType = TableauDataType.UNKNOWN
    current_value: str | None = None
    allowable_values: list[str] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.caption or _strip_brackets(self.name)


class TableauDatasource(BaseModel):
    """A Tableau datasource: connections plus the fields it exposes."""

    name: str
    caption: str | None = None
    is_parameters: bool = Field(
        default=False, description="True for the special 'Parameters' datasource."
    )
    connections: list[TableauConnection] = Field(default_factory=list)
    columns: list[TableauColumn] = Field(default_factory=list)
    calculated_fields: list[TableauCalculatedField] = Field(default_factory=list)
    relations: list[TableauRelation] = Field(default_factory=list)
    joins: list[TableauJoin] = Field(default_factory=list)
    column_origins: dict[str, ColumnOrigin] = Field(
        default_factory=dict,
        description="Raw logical column name (e.g. '[Sales]') -> physical origin.",
    )

    @property
    def display_name(self) -> str:
        return self.caption or self.name


class TableauWorksheet(BaseModel):
    """A worksheet. Only high-level facts are captured in Phase 1."""

    name: str
    datasource_names: list[str] = Field(default_factory=list)


class TableauDashboard(BaseModel):
    """A dashboard. Only high-level facts are captured in Phase 1."""

    name: str
    worksheet_names: list[str] = Field(default_factory=list)


class TableauWorkbook(BaseModel):
    """Root of the Tableau IR."""

    source_name: str = Field(description="Original workbook file name (without extension).")
    datasources: list[TableauDatasource] = Field(default_factory=list)
    parameters: list[TableauParameter] = Field(default_factory=list)
    worksheets: list[TableauWorksheet] = Field(default_factory=list)
    dashboards: list[TableauDashboard] = Field(default_factory=list)


def _strip_brackets(name: str) -> str:
    """Remove Tableau's surrounding square brackets from an internal field name."""

    return name[1:-1] if name.startswith("[") and name.endswith("]") else name
