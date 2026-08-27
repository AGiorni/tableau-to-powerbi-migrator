"""Power BI-side Intermediate Representation.

These models mirror the subset of the Tabular Object Model (TOM) that we emit as TMDL for a
Power BI semantic model. Names and enum values follow TMDL/TOM conventions so the emitter is a
near-mechanical serialization step.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class PBIDataType(str, Enum):
    """TOM column data types (as written in TMDL ``dataType:``)."""

    INT64 = "int64"
    DOUBLE = "double"
    DECIMAL = "decimal"
    STRING = "string"
    BOOLEAN = "boolean"
    DATE_TIME = "dateTime"


class SummarizeBy(str, Enum):
    """Default aggregation for a column (TMDL ``summarizeBy:``)."""

    NONE = "none"
    SUM = "sum"
    COUNT = "count"
    MIN = "min"
    MAX = "max"
    AVERAGE = "average"


class Cardinality(str, Enum):
    MANY_TO_ONE = "manyToOne"
    ONE_TO_MANY = "oneToMany"
    ONE_TO_ONE = "oneToOne"
    MANY_TO_MANY = "manyToMany"


class CrossFilter(str, Enum):
    SINGLE = "oneDirection"
    BOTH = "bothDirections"


class PBIColumn(BaseModel):
    """A table column."""

    name: str
    data_type: PBIDataType
    source_column: str = Field(description="Column name in the underlying query/partition.")
    summarize_by: SummarizeBy = SummarizeBy.NONE
    is_hidden: bool = False
    format_string: str | None = None


class PBIMeasure(BaseModel):
    """A DAX measure."""

    name: str
    expression: str = Field(description="DAX expression body.")
    format_string: str | None = None
    display_folder: str | None = None
    is_hidden: bool = False
    description: str | None = Field(
        default=None, description="Emitted as a /// description; used for migration TODOs."
    )


class PBIPartition(BaseModel):
    """A table partition, i.e. the Power Query (M) source for the table's data."""

    name: str
    mode: str = "import"
    m_source: str = Field(description="Power Query (M) expression producing the table.")


class PBITable(BaseModel):
    """A model table."""

    name: str
    columns: list[PBIColumn] = Field(default_factory=list)
    measures: list[PBIMeasure] = Field(default_factory=list)
    partition: PBIPartition | None = None
    is_hidden: bool = False


class PBIRelationship(BaseModel):
    """A relationship between two tables."""

    name: str = Field(description="A GUID-like identifier used as the TMDL object name.")
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: Cardinality = Cardinality.MANY_TO_ONE
    cross_filter: CrossFilter = CrossFilter.SINGLE
    is_active: bool = True


class PBIExpression(BaseModel):
    """A model-level shared expression (e.g. a Power Query parameter)."""

    name: str
    expression: str
    kind: str = Field(default="m", description="'m' for Power Query expressions.")


class PBIModel(BaseModel):
    """Root of the Power BI IR — a single semantic model."""

    name: str
    culture: str = "en-US"
    compatibility_level: int = 1567
    tables: list[PBITable] = Field(default_factory=list)
    relationships: list[PBIRelationship] = Field(default_factory=list)
    expressions: list[PBIExpression] = Field(default_factory=list)


class FieldKind(str, Enum):
    """How a field is referenced in a visual's query."""

    COLUMN = "column"  # a table column used as a grouping/dimension
    MEASURE = "measure"  # a model measure
    AGGREGATION = "aggregation"  # an implicit aggregation of a raw column


class PBIFieldRef(BaseModel):
    """A single field placed in a visual field well (a query projection)."""

    entity: str = Field(description="Owning table name.")
    property: str = Field(description="Column or measure name.")
    kind: FieldKind = FieldKind.COLUMN
    # Aggregation function enum (0=Sum,1=Avg,2=Min,3=Max,4=Count) when kind is AGGREGATION.
    function: int | None = None


class PBIVisual(BaseModel):
    """A single report visual."""

    name: str = Field(description="20-char identifier used as the visual folder/name.")
    visual_type: str = Field(description="e.g. 'clusteredColumnChart', 'tableEx', 'lineChart'.")
    x: float = 0
    y: float = 0
    z: int = 0
    width: float = 600
    height: float = 400
    tab_order: int = 0
    # Field well name (e.g. 'Category', 'Y', 'Series', 'Values') -> its projected fields.
    wells: dict[str, list[PBIFieldRef]] = Field(default_factory=dict)


class PBIReportPage(BaseModel):
    """A report page (one per migrated Tableau worksheet)."""

    name: str = Field(description="20-char identifier used as the page folder/name.")
    display_name: str
    visuals: list[PBIVisual] = Field(default_factory=list)
