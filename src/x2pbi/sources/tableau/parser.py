"""Turn a loaded Tableau workbook XML tree into the neutral source IR.

This is a deterministic, best-effort parser. It reads the ``<datasources>`` block (columns,
calculated fields, connections, parameters) and a lightweight view of worksheets/dashboards.
Unknown or unsupported constructs are skipped rather than raising, so partial workbooks still
produce a usable IR.

Everything Tableau-specific stops here. Tableau's own vocabulary — connection classes, mark
names, aggregation codes, bracketed field names — is normalized to the enums in
``x2pbi.ir.source_ir`` so the rest of the pipeline is source-agnostic.
"""

from __future__ import annotations

import re

from lxml import etree

from x2pbi.ir.source_ir import (
    Aggregation,
    ColumnOrigin,
    ConnectionKind,
    DataType,
    FieldRole,
    MarkType,
    SourceCalculatedField,
    SourceColumn,
    SourceConnection,
    SourceDashboard,
    SourceDashboardZone,
    SourceDatasource,
    SourceEncoding,
    SourceFieldRef,
    SourceJoin,
    SourceParameter,
    SourceRelation,
    SourceView,
    SourceWorkbook,
)
from x2pbi.sources.tableau.loader import LoadedWorkbook

_PARAMETERS_DATASOURCE = "Parameters"
_LOD_PATTERN = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.IGNORECASE)

# --- Tableau vocabulary -> neutral IR enums --------------------------------------------------

_CONNECTION_KINDS: dict[str, ConnectionKind] = {
    "textscan": ConnectionKind.CSV,
    "excel-direct": ConnectionKind.EXCEL,
    "sqlserver": ConnectionKind.SQL_SERVER,
    "postgres": ConnectionKind.POSTGRES,
    "mysql": ConnectionKind.MYSQL,
}

_MARK_TYPES: dict[str, MarkType] = {
    "bar": MarkType.BAR,
    "line": MarkType.LINE,
    "area": MarkType.AREA,
    "text": MarkType.TEXT,
    "square": MarkType.SQUARE,
    "automatic": MarkType.AUTOMATIC,
}

# Aggregation codes as they appear inside a column-instance name, e.g. 'sum' in [sum:Sales:qk].
_SHELF_AGGREGATIONS: dict[str, Aggregation] = {
    "none": Aggregation.NONE,
    "sum": Aggregation.SUM,
    "avg": Aggregation.AVG,
    "min": Aggregation.MIN,
    "max": Aggregation.MAX,
    "cnt": Aggregation.COUNT,
    "count": Aggregation.COUNT,
    "cntd": Aggregation.COUNT_DISTINCT,
    "median": Aggregation.MEDIAN,
}

# A field's declared default aggregation, e.g. aggregation="Sum" on a <column>.
_DEFAULT_AGGREGATIONS: dict[str, Aggregation] = {
    "none": Aggregation.NONE,
    "sum": Aggregation.SUM,
    "avg": Aggregation.AVG,
    "min": Aggregation.MIN,
    "max": Aggregation.MAX,
    "count": Aggregation.COUNT,
    "countd": Aggregation.COUNT_DISTINCT,
    "median": Aggregation.MEDIAN,
}


def parse_workbook(loaded: LoadedWorkbook) -> SourceWorkbook:
    """Build a :class:`SourceWorkbook` IR from a loaded Tableau workbook."""

    root = loaded.root
    datasources: list[SourceDatasource] = []
    parameters: list[SourceParameter] = []

    for ds_el in root.findall("./datasources/datasource"):
        if _is_parameters_datasource(ds_el):
            parameters.extend(_parse_parameters(ds_el))
            continue
        datasources.append(_parse_datasource(ds_el))

    views = [_parse_view(ws) for ws in root.findall("./worksheets/worksheet")]
    dashboards = [_parse_dashboard(db) for db in root.findall("./dashboards/dashboard")]

    return SourceWorkbook(
        source_name=loaded.source_name,
        dialect="tableau",
        datasources=datasources,
        parameters=parameters,
        views=views,
        dashboards=dashboards,
    )


def _is_parameters_datasource(ds_el: etree._Element) -> bool:
    return ds_el.get("name") == _PARAMETERS_DATASOURCE


def _parse_datasource(ds_el: etree._Element) -> SourceDatasource:
    name = ds_el.get("name") or "datasource"
    caption = ds_el.get("caption")

    connections = _parse_connections(ds_el)
    relations, joins = _parse_relations(ds_el)
    logical_tables = {r.name for r in relations}

    overrides, calculated = _parse_column_overrides(ds_el)
    columns, column_origins = _build_columns(ds_el, logical_tables, overrides)
    joins.extend(_parse_logical_relationships(ds_el, column_origins))

    return SourceDatasource(
        name=name,
        caption=caption,
        connections=connections,
        columns=columns,
        calculated_fields=calculated,
        relations=relations,
        joins=joins,
        column_origins=column_origins,
    )


def _parse_connections(ds_el: etree._Element) -> list[SourceConnection]:
    connections: list[SourceConnection] = []
    # Named connections live under <connection><named-connections>; older workbooks put the
    # connection attributes directly on a <connection> element.
    for named in ds_el.iter("named-connection"):
        conn_el = named.find("./connection")
        if conn_el is None or not conn_el.get("class"):
            continue
        connections.append(_build_connection(conn_el, name=named.get("name")))

    if connections:
        return connections

    # Fallback: a bare <connection> carrying the class directly (older single-source workbooks).
    for conn_el in ds_el.iter("connection"):
        cls = conn_el.get("class")
        if not cls or cls == "federated":
            continue
        connections.append(_build_connection(conn_el, name=conn_el.get("name")))
    return connections


def _build_connection(conn_el: etree._Element, name: str | None) -> SourceConnection:
    cls = conn_el.get("class") or ""
    return SourceConnection(
        kind=_CONNECTION_KINDS.get(cls, ConnectionKind.UNKNOWN),
        native_class=cls,
        name=name,
        server=conn_el.get("server"),
        dbname=conn_el.get("dbname"),
        schema=conn_el.get("schema"),
        filename=conn_el.get("filename"),
        directory=conn_el.get("directory"),
        username=conn_el.get("username"),
    )


def _parse_relations(
    ds_el: etree._Element,
) -> tuple[list[SourceRelation], list[SourceJoin]]:
    relations: dict[str, SourceRelation] = {}
    joins: list[SourceJoin] = []
    for rel_el in ds_el.findall("./connection/relation"):
        _walk_relation(rel_el, relations, joins)
    return list(relations.values()), joins


def _walk_relation(
    rel_el: etree._Element,
    relations: dict[str, SourceRelation],
    joins: list[SourceJoin],
) -> None:
    rel_type = rel_el.get("type")
    if rel_type == "join":
        joins.extend(_parse_join_clauses(rel_el))
        for child in rel_el.findall("./relation"):
            _walk_relation(child, relations, joins)
    elif rel_type == "collection":
        # A logical-model container that simply groups table relations.
        for child in rel_el.findall("./relation"):
            _walk_relation(child, relations, joins)
    elif rel_el.get("name"):
        table = rel_el.get("table")
        # Skip the shadow extract copies ('[Extract].[Orders_<guid>]') of logical tables.
        if table and table.startswith("[Extract]"):
            return
        name = rel_el.get("name") or ""
        relations.setdefault(
            name,
            SourceRelation(
                name=name,
                kind=rel_type or "table",
                table=table,
                connection_name=rel_el.get("connection"),
            ),
        )


def _parse_join_clauses(join_el: etree._Element) -> list[SourceJoin]:
    join_type = join_el.get("join") or "inner"
    joins: list[SourceJoin] = []
    for eq in join_el.findall("./clause/expression[@op='=']"):
        operands = eq.findall("./expression")
        if len(operands) != 2:
            continue
        left = _split_qualified(operands[0].get("op"))
        right = _split_qualified(operands[1].get("op"))
        if left and right:
            joins.append(
                SourceJoin(
                    left_table=left[0],
                    left_column=left[1],
                    right_table=right[0],
                    right_column=right[1],
                    join_type=join_type,
                )
            )
    return joins


def _parse_column_origins(ds_el: etree._Element) -> dict[str, ColumnOrigin]:
    origins: dict[str, ColumnOrigin] = {}
    for map_el in ds_el.findall("./connection/cols/map"):
        key = map_el.get("key")
        value = map_el.get("value")
        parsed = _split_qualified(value)
        if key and parsed:
            origins[_strip_brackets(key)] = ColumnOrigin(table=parsed[0], source_column=parsed[1])
    return origins


def _parse_column_overrides(
    ds_el: etree._Element,
) -> tuple[dict[str, SourceColumn], list[SourceCalculatedField]]:
    """Parse datasource-level <column> elements: field overrides and calculated fields.

    Tableau only emits a <column> for a field that has an override (caption, role, aggregation)
    or a calculation; the full physical column list comes from <metadata-records>. The returned
    dict stays keyed on Tableau's raw bracketed name so it can be matched against metadata.
    """

    overrides: dict[str, SourceColumn] = {}
    calculated: list[SourceCalculatedField] = []
    for col_el in ds_el.findall("./column"):
        calc_el = col_el.find("./calculation")
        if calc_el is not None and calc_el.get("formula") is not None:
            calculated.append(_parse_calculated_field(col_el, calc_el))
        elif col_el.get("name"):
            overrides[col_el.get("name") or ""] = _parse_column(col_el)
    return overrides, calculated


def _build_columns(
    ds_el: etree._Element,
    logical_tables: set[str],
    overrides: dict[str, SourceColumn],
) -> tuple[list[SourceColumn], dict[str, ColumnOrigin]]:
    """Build the full physical column set, preferring <metadata-records> over <column>.

    Falls back to <column> elements + the <cols> map when no metadata is present (as in simple
    single-source workbooks).
    """

    metadata = _parse_metadata_columns(ds_el, logical_tables)
    if not metadata:
        return list(overrides.values()), _parse_column_origins(ds_el)

    columns: list[SourceColumn] = []
    origins: dict[str, ColumnOrigin] = {}
    seen: set[tuple[str, str]] = set()
    for local, dtype, table, remote in metadata:
        if (table, local) in seen:
            continue
        seen.add((table, local))
        override = overrides.get(local)
        columns.append(_merge_column(local, dtype, override))
        origins[_strip_brackets(local)] = ColumnOrigin(table=table, source_column=remote)
    return columns, origins


def _merge_column(local: str, dtype: str, override: SourceColumn | None) -> SourceColumn:
    if override is not None:
        return override
    data_type = _map_datatype(dtype)
    role = (
        FieldRole.MEASURE
        if data_type in {DataType.INTEGER, DataType.REAL}
        else FieldRole.DIMENSION
    )
    return SourceColumn(name=_strip_brackets(local), datatype=data_type, role=role)


def _parse_metadata_columns(
    ds_el: etree._Element, logical_tables: set[str]
) -> list[tuple[str, str, str, str]]:
    """Return (local_name, datatype, table, source_column) for each real physical column."""

    out: list[tuple[str, str, str, str]] = []
    for rec in ds_el.findall('.//metadata-record[@class="column"]'):
        parent = _strip_brackets(rec.findtext("parent-name") or "")
        if parent not in logical_tables:
            continue
        local = rec.findtext("local-name") or ""
        if not local:
            continue
        dtype = rec.findtext("local-type") or ""
        remote = rec.findtext("remote-name") or _strip_brackets(local)
        out.append((local, dtype, parent, remote))
    return out


def _parse_logical_relationships(
    ds_el: etree._Element, origins: dict[str, ColumnOrigin]
) -> list[SourceJoin]:
    """Parse Tableau's logical model relationships (<relationships>) into joins."""

    col_to_table = {name: origin.table for name, origin in origins.items()}
    joins: list[SourceJoin] = []
    for rel in ds_el.findall(".//relationships/relationship"):
        eq = rel.find('./expression[@op="="]')
        if eq is None:
            continue
        operands = eq.findall("./expression")
        if len(operands) != 2:
            continue
        left_ref, right_ref = operands[0].get("op"), operands[1].get("op")
        left_col = _strip_brackets(left_ref or "")
        right_col = _strip_brackets(right_ref or "")
        left_table = col_to_table.get(left_col) or _endpoint_table(rel, "first-end-point")
        right_table = col_to_table.get(right_col) or _endpoint_table(rel, "second-end-point")
        if left_table and right_table and left_table != right_table:
            joins.append(
                SourceJoin(
                    left_table=left_table,
                    left_column=left_col,
                    right_table=right_table,
                    right_column=right_col,
                    join_type="relationship",
                )
            )
    return joins


def _endpoint_table(rel_el: etree._Element, tag: str) -> str | None:
    end = rel_el.find("./" + tag)
    if end is None:
        return None
    return _strip_extract_guid(end.get("object-id") or "")


def _strip_extract_guid(name: str) -> str:
    """'Orders_4666C054...B95' (a 32-hex extract suffix) -> 'Orders'."""

    match = re.match(r"^(.*)_[0-9A-Fa-f]{32}$", name)
    return match.group(1) if match else name


def _split_qualified(ref: str | None) -> tuple[str, str] | None:
    """Split a '[Table].[Column]' reference into ('Table', 'Column')."""

    if not ref or "].[" not in ref:
        return None
    table_part, _, col_part = ref.partition("].[")
    return _strip_brackets(table_part + "]"), _strip_brackets("[" + col_part)


def _strip_brackets(name: str) -> str:
    """Remove Tableau's surrounding square brackets from an internal field name."""

    return name[1:-1] if name.startswith("[") and name.endswith("]") else name


def _parse_column(col_el: etree._Element) -> SourceColumn:
    return SourceColumn(
        name=_strip_brackets(col_el.get("name") or ""),
        caption=col_el.get("caption"),
        datatype=_map_datatype(col_el.get("datatype")),
        role=_map_role(col_el.get("role")),
        aggregation=_map_default_aggregation(
            col_el.get("aggregation") or col_el.get("default-aggregation")
        ),
        hidden=col_el.get("hidden") == "true",
    )


def _parse_calculated_field(
    col_el: etree._Element, calc_el: etree._Element
) -> SourceCalculatedField:
    formula = calc_el.get("formula") or ""
    return SourceCalculatedField(
        name=_strip_brackets(col_el.get("name") or ""),
        caption=col_el.get("caption"),
        formula=formula,
        datatype=_map_datatype(col_el.get("datatype")),
        role=_map_role(col_el.get("role")),
        aggregation=_map_default_aggregation(
            col_el.get("aggregation") or col_el.get("default-aggregation")
        ),
        is_scoped_aggregation=bool(_LOD_PATTERN.search(formula)),
    )


def _parse_parameters(ds_el: etree._Element) -> list[SourceParameter]:
    params: list[SourceParameter] = []
    for col_el in ds_el.findall("./column"):
        allowable = [
            m.get("value")
            for m in col_el.findall("./members/member")
            if m.get("value") is not None
        ]
        params.append(
            SourceParameter(
                name=_strip_brackets(col_el.get("name") or ""),
                caption=col_el.get("caption"),
                datatype=_map_datatype(col_el.get("datatype")),
                current_value=col_el.get("value"),
                allowable_values=[a for a in allowable if a is not None],
            )
        )
    return params


def _parse_view(ws_el: etree._Element) -> SourceView:
    """Parse one <worksheet> into a neutral view."""

    ds_names = [
        ds.get("name")
        for ds in ws_el.findall("./table/view/datasources/datasource")
        if ds.get("name")
    ]
    mark = ws_el.find(".//mark")
    native_mark = (mark.get("class") if mark is not None else None) or "Automatic"

    rows = _parse_shelf(ws_el.findtext(".//rows"))
    cols = _parse_shelf(ws_el.findtext(".//cols"))
    encodings = [
        SourceEncoding(kind=etree.QName(enc).localname, field=ref)
        for enc in ws_el.findall(".//encodings/*")
        if (ref := _parse_field_ref(enc.get("column"))) is not None
    ]

    return SourceView(
        name=ws_el.get("name") or "",
        datasource_names=[n for n in ds_names if n is not None],
        mark=_MARK_TYPES.get(native_mark.lower(), MarkType.OTHER),
        native_mark=native_mark,
        rows=rows,
        cols=cols,
        encodings=encodings,
        measure_names=_parse_measure_names(ws_el),
        is_map=_is_map_worksheet(ws_el),
    )


def _is_map_worksheet(ws_el: etree._Element) -> bool:
    """A worksheet is geographic if it has a map source, geometry encoding, or generated lat/long."""

    if ws_el.find(".//mapsources/mapsource") is not None:
        return True
    if ws_el.find(".//encodings/geometry") is not None:
        return True
    for shelf in (ws_el.findtext(".//rows"), ws_el.findtext(".//cols")):
        if shelf and "(generated)" in shelf and ("Latitude" in shelf or "Longitude" in shelf):
            return True
    return False


def _parse_measure_names(ws_el: etree._Element) -> list[SourceFieldRef]:
    """Extract the measures listed by a ``[:Measure Names]`` filter (a Measure Values table)."""

    refs: list[SourceFieldRef] = []
    for flt in ws_el.findall(".//filter"):
        if not (flt.get("column") or "").endswith("[:Measure Names]"):
            continue
        for member_el in flt.findall(".//groupfilter[@function='member']"):
            member = (member_el.get("member") or "").strip().strip('"')
            if (ref := _parse_field_ref(member)) is not None:
                refs.append(ref)
        break
    return refs


def _parse_shelf(text: str | None) -> list[SourceFieldRef]:
    """Parse a rows/cols shelf expression into field refs.

    Shelves nest fields with ``*``, e.g. ``([ds].[none:Category:nk] * [ds].[sum:Sales:qk])``.
    """

    if not text:
        return []
    refs: list[SourceFieldRef] = []
    for token in text.split("*"):
        ref = _parse_field_ref(token.strip().strip("()").strip())
        if ref is not None:
            refs.append(ref)
    return refs


def _parse_field_ref(ref: str | None) -> SourceFieldRef | None:
    """Parse a column-instance reference like ``[ds].[sum:Sales:qk]`` into a field ref.

    Returns None for generated/internal fields (Latitude, Measure Names, object ids, etc.).
    """

    if not ref:
        return None
    # Take the last bracketed segment (drop the datasource qualifier).
    inner = ref.rsplit(".[", 1)[-1]
    inner = _strip_brackets(inner if inner.startswith("[") else "[" + inner)
    if not inner or inner.startswith((":", "__tableau", "Multiple Values")):
        return None
    if "(generated)" in inner:
        return None

    parts = inner.split(":")
    if len(parts) >= 3:
        agg, role_code = parts[0], parts[-1]
        field = ":".join(parts[1:-1])
    else:
        agg, field, role_code = "none", inner, "nk"

    role = FieldRole.MEASURE if role_code == "qk" else FieldRole.DIMENSION
    return SourceFieldRef(
        field=field,
        aggregation=_SHELF_AGGREGATIONS.get(agg.lower(), Aggregation.OTHER),
        role=role,
        raw=inner,
    )


def _parse_dashboard(db_el: etree._Element) -> SourceDashboard:
    zones: list[SourceDashboardZone] = []
    # Only the primary layout (skip device/phone layouts under ./devicelayouts).
    primary = db_el.find("./zones")
    if primary is not None:
        for z in primary.iter("zone"):
            type_v2 = z.get("type-v2")
            name = z.get("name")
            geom = {
                "x": int(z.get("x") or 0),
                "y": int(z.get("y") or 0),
                "w": int(z.get("w") or 0),
                "h": int(z.get("h") or 0),
            }
            if type_v2 == "filter":
                zones.append(
                    SourceDashboardZone(
                        kind="filter", view=name, field=_parse_field_ref(z.get("param")), **geom
                    )
                )
            elif name and type_v2 is None:  # worksheet zones carry a name and no type-v2
                zones.append(SourceDashboardZone(kind="view", view=name, **geom))

    view_names = list(dict.fromkeys(z.view for z in zones if z.kind == "view" and z.view))
    return SourceDashboard(
        name=db_el.get("name") or "",
        view_names=view_names,
        zones=zones,
    )


def _map_datatype(value: str | None) -> DataType:
    try:
        return DataType(value) if value else DataType.UNKNOWN
    except ValueError:
        return DataType.UNKNOWN


def _map_role(value: str | None) -> FieldRole:
    return FieldRole.MEASURE if value == "measure" else FieldRole.DIMENSION


def _map_default_aggregation(value: str | None) -> Aggregation | None:
    """Map a declared default aggregation ('Sum', 'CountD', 'Year', ...) to the neutral enum."""

    if not value:
        return None
    return _DEFAULT_AGGREGATIONS.get(value.lower(), Aggregation.OTHER)
