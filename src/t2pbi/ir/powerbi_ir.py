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
