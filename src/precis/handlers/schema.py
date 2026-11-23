"""Live PostgreSQL definitions and aggregate profiles for the public schema."""

from __future__ import annotations

from typing import Any, ClassVar

import psycopg
from psycopg import Connection, sql
from psycopg.rows import dict_row

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Upstream
from precis.format import render_agent_table
from precis.protocol import ArgsSpec, Handler, KindSpec
from precis.response import Response
from precis.store.pool import checkout_budget_cap_ms
from precis.utils.next_block import render_next_section

_LIST = "get(kind='schema')"
_RELATIONS = """
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
      AND has_schema_privilege(n.oid, 'USAGE')
      AND has_table_privilege(c.oid, 'SELECT')
"""
_COLUMNS = """
    SELECT a.attname AS "column", format_type(a.atttypid, a.atttypmod) AS type,
           NOT a.attnotnull AS nullable, pg_get_expr(d.adbin, d.adrelid) AS expression,
           NULLIF(a.attidentity, '') AS identity, NULLIF(a.attgenerated, '') AS generated,
           col_description(a.attrelid, a.attnum) AS comment
    FROM pg_attribute a
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
"""
_CONSTRAINTS = """
    SELECT conname AS name, pg_get_constraintdef(oid) AS definition
    FROM pg_constraint WHERE conrelid = %s AND contype IN ('p', 'u', 'c', 'x')
    ORDER BY conname
"""
_FOREIGN_KEYS = """
    SELECT con.conname AS name, src.relname AS source, dst.relname AS target,
           pg_get_constraintdef(con.oid) AS definition,
           con.conrelid = %(oid)s AS outgoing, con.confrelid = %(oid)s AS incoming
    FROM pg_constraint con
    JOIN pg_class src ON src.oid = con.conrelid
    JOIN pg_class dst ON dst.oid = con.confrelid
    JOIN pg_namespace src_ns ON src_ns.oid = src.relnamespace
    JOIN pg_namespace dst_ns ON dst_ns.oid = dst.relnamespace
    WHERE con.contype = 'f' AND (con.conrelid = %(oid)s OR con.confrelid = %(oid)s)
      AND src_ns.nspname = 'public' AND dst_ns.nspname = 'public'
      AND has_table_privilege(src.oid, 'SELECT') AND has_table_privilege(dst.oid, 'SELECT')
    ORDER BY src.relname, dst.relname, con.conname
"""
_TABLE_PROFILE = """
    SELECT pg_table_size(c.oid) AS table_bytes,
           pg_total_relation_size(c.oid) AS total_bytes,
           pg_indexes_size(c.oid) AS index_bytes,
           CASE WHEN c.reltuples >= 0 THEN round(c.reltuples)::bigint END AS estimated_rows,
           s.seq_scan, s.idx_scan, s.n_dead_tup AS estimated_dead_tuples,
           s.last_vacuum::text, s.last_autovacuum::text,
           s.last_analyze::text, s.last_autoanalyze::text
    FROM pg_class c LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
    WHERE c.oid = %s
"""
# pg_stats enforces column visibility and suppresses statistics under active RLS.
# Project numeric fields only. Negative n_distinct encodes a fraction of table rows.
_COLUMN_PROFILE = """
    SELECT a.attname AS "column", format_type(a.atttypid, a.atttypmod) AS type,
           CASE WHEN s.n_distinct >= 0 THEN round(s.n_distinct)::bigint
                WHEN c.reltuples >= 0 THEN round(-s.n_distinct * c.reltuples)::bigint
           END AS estimated_distinct_nonnull,
           s.null_frac AS null_fraction, s.avg_width AS average_bytes
    FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid
    LEFT JOIN pg_stats s ON s.schemaname = 'public' AND s.tablename = c.relname
         AND s.attname = a.attname AND s.inherited = (c.relkind = 'p')
    WHERE c.oid = %s AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
"""
_INDEXES = """
    SELECT ic.relname AS name, pg_get_indexdef(ic.oid) AS definition,
           i.indisunique AS "unique", i.indpred IS NOT NULL AS partial,
           i.indisvalid AS valid, pg_relation_size(ic.oid) AS bytes,
           si.idx_scan AS scans, si.idx_tup_fetch AS tuples_fetched,
           si.idx_scan = 0 AS no_recorded_scans,
           a.attname AS simple_column
    FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
    LEFT JOIN pg_stat_all_indexes si ON si.indexrelid = ic.oid
    LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
         AND i.indnkeyatts = 1 AND i.indpred IS NULL
    WHERE i.indrelid = %s
    ORDER BY ic.relname
"""
_JSON_KEYS = sql.SQL("""
    SELECT k.key COLLATE "C" AS key, count(*) AS rows_with_key
    FROM ONLY {} AS t
    CROSS JOIN LATERAL pg_catalog.jsonb_object_keys(
        CASE WHEN pg_catalog.jsonb_typeof(t.{}) = 'object' THEN t.{} END
    ) AS k(key)
    GROUP BY 1
    ORDER BY rows_with_key DESC, key
    LIMIT %s OFFSET %s
""")


class SchemaHandler(Handler):
    """Read a relation definition, statistics or one JSONB key inventory."""

    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        "get": ArgsSpec({"column", "page", "page_size"})
    }
    spec: ClassVar[KindSpec] = KindSpec(
        kind="schema",
        title="Database schema",
        description=(
            "Live read-only PostgreSQL introspection. get(kind='schema') lists public relations; "
            "id='refs' shows structure and foreign keys in both directions; view='profile' "
            "shows aggregate statistics; view='jsonb', args={'column': 'meta'} counts top-level "
            "keys on one JSONB column. List and key counts accept args page and page_size."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=False,
        placement="system",
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("schema: store required")
        self.store = hub.store

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        column: str | None = None,
        page: int = 1,
        page_size: int = 25,
        **_kw: Any,
    ) -> Response:
        detail_call = "get(kind='schema', id='refs')"
        json_call = (
            "get(kind='schema', id='refs', view='jsonb', args={'column': 'meta'})"
        )
        if view not in (None, "profile", "jsonb") or (id is None and view is not None):
            raise BadInput(
                "Choose a relation for profile or jsonb views", next=detail_call
            )
        if view == "jsonb":
            if not isinstance(column, str) or not column:
                raise BadInput("The jsonb view requires a column name", next=json_call)
        elif column is not None:
            raise BadInput("column belongs to the jsonb view", next=json_call)
        if (
            type(page) is not int
            or type(page_size) is not int
            or not 1 <= page <= 1_000_000
            or not 1 <= page_size <= 100
        ):
            raise BadInput("Use page 1–1000000 and page_size 1–100", next=_LIST)
        if id is not None and view != "jsonb" and (page != 1 or page_size != 25):
            raise BadInput(
                "Pagination applies to the relation list and JSONB keys", next=_LIST
            )
        try:
            with self.store.tx() as conn, conn.cursor(row_factory=dict_row) as cur:
                # Pool checkout may already have run a query to apply the
                # request budget. PostgreSQL still allows entry to read-only mode.
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("SET LOCAL search_path = pg_catalog")
                request_limit = checkout_budget_cap_ms()
                cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{min(10_000, request_limit or 10_000)}ms",),
                )
                cur.execute("SET LOCAL lock_timeout = '1s'")
                cur.execute("SET LOCAL TIME ZONE 'UTC'")
                bounds = (page_size + 1, (page - 1) * page_size)
                if id is None:
                    rows = cur.execute(
                        """
                        SELECT c.relname AS relation,
                               CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned table'
                                    WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' END AS kind
                        """
                        + _RELATIONS
                        + ' ORDER BY c.relname COLLATE "C" LIMIT %s OFFSET %s',
                        bounds,
                    ).fetchall()
                    return _page("Public relations", rows, _LIST, {}, page, page_size)
                relation = cur.execute(
                    "SELECT c.oid, c.relname, c.relkind, obj_description(c.oid, 'pg_class') AS comment, "
                    "CASE WHEN c.relkind IN ('v', 'm') THEN pg_get_viewdef(c.oid) END AS view_definition "
                    + _RELATIONS
                    + " AND c.relname = %s",
                    (str(id),),
                ).fetchone()
                if relation is None:
                    raise NotFound(
                        "No visible public relation with that name", next=_LIST
                    )
                if view == "jsonb" and column is not None:
                    return _json_profile(
                        conn, relation, column, page, page_size, bounds
                    )
                if view == "profile":
                    return _profile(conn, relation)
                return _definition(conn, relation)
        except psycopg.errors.QueryCanceled as exc:
            raise Upstream(
                "Schema query exceeded the statement time limit",
                next="Retry once or use get(kind='schema', id='refs', view='profile')",
            ) from exc
        except psycopg.Error as exc:
            # PostgreSQL diagnostics can contain row contents and connection details.
            raise Upstream(
                "Database schema query failed", next="Retry get(kind='schema')"
            ) from exc


def _section(title: str, rows: list[dict[str, Any]]) -> str:
    return f"\n\n## {title}\n" + (render_agent_table(rows) if rows else "(none)")


def _definition(conn: Connection, relation: dict[str, Any]) -> Response:
    name, oid = relation["relname"], relation["oid"]
    call = f"get(kind='schema', id={name!r}"
    with conn.cursor(row_factory=dict_row) as cur:
        columns = cur.execute(_COLUMNS, (oid,)).fetchall()
        constraints = cur.execute(_CONSTRAINTS, (oid,)).fetchall()
        fks = cur.execute(_FOREIGN_KEYS, {"oid": oid}).fetchall()
        indexes = cur.execute(
            "SELECT pg_get_indexdef(indexrelid) AS definition "
            "FROM pg_index WHERE indrelid = %s ORDER BY indexrelid",
            (oid,),
        ).fetchall()
    body = "# Public relation" + _section(
        "Relation",
        [
            {
                "name": name,
                "comment": relation["comment"],
                "view_definition": relation["view_definition"],
            }
        ],
    )
    body += _section("Columns (expression is default or generated expression)", columns)
    body += _section("Constraints", constraints) + _section("Indexes", indexes)
    for title, flag in (("References", "outgoing"), ("Referenced by", "incoming")):
        body += _section(
            title,
            [
                {key: fk[key] for key in ("name", "source", "target", "definition")}
                for fk in fks
                if fk[flag]
            ],
        )
    neighbors = sorted(
        {fk["target"] if fk["outgoing"] else fk["source"] for fk in fks} - {name}
    )
    nexts = [(call + ", view='profile')", "inspect table, column and index statistics")]
    nexts.extend(
        (f"get(kind='schema', id={other!r})", "follow a foreign key")
        for other in neighbors
    )
    return Response(body=body + render_next_section(nexts), result_count=1)


def _profile(conn: Connection, relation: dict[str, Any]) -> Response:
    oid, name = relation["oid"], relation["relname"]
    with conn.cursor(row_factory=dict_row) as cur:
        table = cur.execute(_TABLE_PROFILE, (oid,)).fetchall()
        columns = cur.execute(_COLUMN_PROFILE, (oid,)).fetchall()
        indexes = cur.execute(_INDEXES, (oid,)).fetchall()
    distinct = {c["column"]: c["estimated_distinct_nonnull"] for c in columns}
    for index in indexes:
        count = distinct.get(index.pop("simple_column"))
        index["low_cardinality_key"] = None if count is None else 1 <= count <= 10
    body = (
        "# Relation profile\n"
        "Missing statistics are unknown. Scan counters reflect PostgreSQL's current statistics "
        "window. Low cardinality flags simple nonpartial index keys with 1–10 estimated distinct "
        "non-null values. Sizes and table counters describe this relation's own storage."
    )
    body += _section("Table (timestamps UTC)", table)
    body += _section("Columns", columns) + _section("Indexes", indexes)
    nexts = [
        (f"get(kind='schema', id={name!r})", "show the definition and foreign keys")
    ]
    if relation["relkind"] == "r":
        nexts.extend(
            (
                f"get(kind='schema', id={name!r}, view='jsonb', args={{'column': {c['column']!r}}})",
                "count top-level JSONB keys",
            )
            for c in columns
            if c["type"] == "jsonb"
        )
    return Response(body=body + render_next_section(nexts), result_count=1)


def _json_profile(
    conn: Connection,
    relation: dict[str, Any],
    column: str,
    page: int,
    page_size: int,
    bounds: tuple[int, int],
) -> Response:
    name = relation["relname"]
    call = f"get(kind='schema', id={name!r}"
    if relation["relkind"] != "r":
        raise BadInput("Choose a JSONB column on an ordinary table", next=call + ")")
    table = sql.Identifier("public", name)
    with conn.cursor(row_factory=dict_row) as cur:
        # Keep validation and the identifier-based scan on the same relation
        # if DDL renames or replaces it concurrently.
        cur.execute(sql.SQL("LOCK TABLE ONLY {} IN ACCESS SHARE MODE").format(table))
        is_jsonb = cur.execute(
            """
            SELECT 1 FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.oid = %s AND n.nspname = 'public' AND c.relname = %s
              AND c.relkind = 'r' AND a.attname = %s AND a.attnum > 0
              AND NOT a.attisdropped AND a.atttypid = 'jsonb'::regtype
            """,
            (relation["oid"], name, column),
        ).fetchone()
        if is_jsonb is None:
            raise BadInput(
                "Choose a JSONB column on an ordinary table", next=call + ")"
            )
        identifier = sql.Identifier(column)
        rows = cur.execute(
            _JSON_KEYS.format(table, identifier, identifier),
            bounds,
        ).fetchall()
    return _page(
        "Top-level JSONB keys (exact counts of visible rows in this table)",
        rows,
        call + ", view='jsonb')",
        {"column": column},
        page,
        page_size,
    )


def _page(
    title: str,
    rows: list[dict[str, Any]],
    call: str,
    args: dict[str, Any],
    page: int,
    page_size: int,
) -> Response:
    visible = rows[:page_size]
    body = f"# {title} (page {page})\n" + (
        render_agent_table(visible) if visible else "(none)"
    )
    if len(rows) > page_size:
        next_args = {**args, "page": page + 1, "page_size": page_size}
        body += render_next_section(
            [(f"{call[:-1]}, args={next_args!r})", "show the next page")]
        )
    return Response(body=body, result_count=len(visible))
