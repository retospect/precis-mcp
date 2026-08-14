"""Store ops for the ``component`` kind (``component-kind`` (git-only)).

Mirrors ``_material_ops.py``'s star schema, plus a category dimension:

- the **entity** is a slug-addressed ``refs`` row (``kind='component'``);
  ``title`` = canonical name, ``meta`` = ``{category, mpn, manufacturer,
  sku, uom, package, aliases, notes}``;
- the **category registry** (``component_categories``) is a growable, flat
  typed vocabulary, seeded ``core`` by migration 0093 and mintable
  ``proposed`` at entity-write time;
- the **spec registry** (``component_specs``) is a typed, growable
  vocabulary like ``material_properties``, plus a nullable
  ``category_id`` (NULL = universal, non-NULL = scoped to that category);
- the **values** (``component_spec_values``) are a plain fact table, one
  row per sourced measurement — no card, no embedding.

Mixin assumes the concrete Store provides ``self.pool`` / ``self.tx`` /
``self.add_link``.

Row mapping: every read goes through a cursor bound to psycopg's
``dict_row`` factory (never positional tuple indexing over the long
``_SPEC_COLS``/``_VALUE_COLS`` lists — a SELECT-list drift would
otherwise silently mis-assign a field), then ``cast`` to the matching
``TypedDict`` below so callers get named, typed access.
"""

from __future__ import annotations

from typing import Any, TypedDict, cast

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_CATEGORY_COLS = "category_id, name, status, description"

_SPEC_COLS = (
    "spec_id, name, canonical_unit, dimension, value_type, allowed_values, "
    "standard_ref, status, higher_is_better, description, category_id"
)

_VALUE_COLS = (
    "id, component_ref_id, spec_id, value_num, value_low, value_high, "
    "value_text, value_bool, input_unit, conditions, maturity, method, "
    "source_ref_id, source_chunk, source_url, as_of, set_by, created_at, notes"
)


class ComponentCategoryRow(TypedDict):
    category_id: str
    name: str
    status: str
    description: str | None


class ComponentSpecRow(TypedDict):
    spec_id: str
    name: str
    canonical_unit: str | None
    dimension: str | None
    value_type: str
    allowed_values: list[Any] | None
    standard_ref: str | None
    status: str
    higher_is_better: bool | None
    description: str | None
    category_id: str | None


class ComponentValueRow(TypedDict):
    """One ``component_spec_values`` row — a 5-way tagged union over which
    of ``value_num``/``value_low``/``value_high``/``value_text``/
    ``value_bool`` is populated, keyed by the owning spec's ``value_type``
    (quantity/ratio -> ``value_num`` [+ ``value_low``/``value_high``],
    boolean -> ``value_bool``, categorical/text -> ``value_text``)."""

    id: int
    component_ref_id: int
    spec_id: str
    value_num: float | None
    value_low: float | None
    value_high: float | None
    value_text: str | None
    value_bool: bool | None
    input_unit: str | None
    conditions: dict[str, Any] | None
    maturity: str
    method: str | None
    source_ref_id: int | None
    source_chunk: str | None
    source_url: str | None
    as_of: str | None
    set_by: str | None
    created_at: Any
    notes: str | None


class ComponentValueRowWithSource(ComponentValueRow):
    """A :class:`ComponentValueRow` joined to the source ref's kind (the
    ``component-page``/table reads, which need to format a source handle
    without a second query)."""

    source_kind: str | None


class ComponentValueSearchRow(ComponentValueRowWithSource):
    """A :class:`ComponentValueRowWithSource` joined to the owning
    component's title (``component_search_values``)."""

    component_title: str


def _fetchone(conn: Connection, sql: str, params: Any = ()) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _fetchall(conn: Connection, sql: str, params: Any = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


class ComponentMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any
    add_link: Any
    remove_link: Any
    links_for: Any

    # -- entity ----------------------------------------------------------

    def component_entity_upsert(
        self,
        *,
        slug: str,
        title: str,
        meta_patch: dict[str, Any],
    ) -> tuple[Any, bool]:
        """Create-or-update the component entity ``refs`` row.

        On create, ``meta_patch`` is the whole ``meta``. On update, it is
        shallow-merged onto the existing ``meta`` (adding an alias/note
        doesn't clobber an already-recorded ``category``/``mpn``). Returns
        ``(ref, created)``.
        """
        existing = self.get_ref(kind="component", id=slug)
        with self.tx() as conn:
            if existing is None:
                ref = self.insert_ref(
                    kind="component",
                    slug=slug,
                    title=title,
                    meta=dict(meta_patch),
                    conn=conn,
                )
                return ref, True
            merged = {**(existing.meta or {}), **meta_patch}
            conn.execute(
                "UPDATE refs SET title = %s, meta = %s WHERE ref_id = %s",
                (title, Jsonb(merged), existing.id),
            )
            updated = self.get_ref(kind="component", id=slug)
            assert updated is not None
            return updated, False

    # -- category registry ------------------------------------------------

    def component_category_get(
        self, category_id: str, *, conn: Connection | None = None
    ) -> ComponentCategoryRow | None:
        """Look up one category row by ``category_id``, or ``None``."""
        sql = (
            f"SELECT {_CATEGORY_COLS} FROM component_categories WHERE category_id = %s"
        )
        if conn is not None:
            row = _fetchone(conn, sql, (category_id,))
        else:
            with self.pool.connection() as c:
                row = _fetchone(c, sql, (category_id,))
        return None if row is None else cast(ComponentCategoryRow, row)

    def component_category_list(self) -> list[ComponentCategoryRow]:
        """The whole registry, core first then proposed, then alphabetical."""
        with self.pool.connection() as conn:
            rows = _fetchall(
                conn,
                f"SELECT {_CATEGORY_COLS} FROM component_categories "
                "ORDER BY (status = 'core') DESC, category_id",
            )
        return [cast(ComponentCategoryRow, r) for r in rows]

    def component_category_mint(
        self,
        *,
        category_id: str,
        name: str,
        description: str | None = None,
        conn: Connection | None = None,
    ) -> ComponentCategoryRow:
        """Insert a new ``proposed``-tier category. Caller has already
        checked ``category_id`` doesn't exist. Never mints ``core`` — that
        tier is curated by migration only."""
        sql = (
            "INSERT INTO component_categories "
            "(category_id, name, status, description) "
            "VALUES (%s, %s, 'proposed', %s) "
            f"RETURNING {_CATEGORY_COLS}"
        )
        params = (category_id, name, description)
        if conn is not None:
            row = _fetchone(conn, sql, params)
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    row = _fetchone(c, sql, params)
        assert row is not None
        return cast(ComponentCategoryRow, row)

    # -- spec registry -----------------------------------------------------

    def component_spec_get(
        self, spec_id: str, *, conn: Connection | None = None
    ) -> ComponentSpecRow | None:
        """Look up one spec row by ``spec_id``, or ``None``."""
        sql = f"SELECT {_SPEC_COLS} FROM component_specs WHERE spec_id = %s"
        if conn is not None:
            row = _fetchone(conn, sql, (spec_id,))
        else:
            with self.pool.connection() as c:
                row = _fetchone(c, sql, (spec_id,))
        return None if row is None else cast(ComponentSpecRow, row)

    def component_specs_list(
        self, *, category_id: str | None = None
    ) -> list[ComponentSpecRow]:
        """The spec registry, core first then proposed, then alphabetical.

        ``category_id=None`` (default) lists every spec. Pass a category to
        get only what's "applicable to category" — universal specs
        (``category_id IS NULL``) plus that category's own.
        """
        sql = f"SELECT {_SPEC_COLS} FROM component_specs "
        params: tuple[Any, ...] = ()
        if category_id is not None:
            sql += "WHERE category_id IS NULL OR category_id = %s "
            params = (category_id,)
        sql += "ORDER BY (status = 'core') DESC, spec_id"
        with self.pool.connection() as conn:
            rows = _fetchall(conn, sql, params)
        return [cast(ComponentSpecRow, r) for r in rows]

    def component_spec_mint(
        self,
        *,
        spec_id: str,
        name: str,
        canonical_unit: str | None,
        dimension: str | None,
        value_type: str,
        category_id: str | None,
        allowed_values: list[Any] | None = None,
        description: str | None = None,
        conn: Connection | None = None,
    ) -> ComponentSpecRow:
        """Insert a new ``proposed``-tier spec, scoped to ``category_id``
        (``None`` mints a universal spec — the handler only does this for a
        component-scoped mint in practice, per the proposal's runtime-mint
        resolution, but the store op itself stays general). Caller has
        already checked ``spec_id`` doesn't exist. Never mints ``core``."""
        sql = (
            "INSERT INTO component_specs "
            "(spec_id, name, canonical_unit, dimension, value_type, "
            " allowed_values, status, description, category_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'proposed', %s, %s) "
            f"RETURNING {_SPEC_COLS}"
        )
        params = (
            spec_id,
            name,
            canonical_unit,
            dimension,
            value_type,
            Jsonb(allowed_values) if allowed_values is not None else None,
            description,
            category_id,
        )
        if conn is not None:
            row = _fetchone(conn, sql, params)
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    row = _fetchone(c, sql, params)
        assert row is not None
        return cast(ComponentSpecRow, row)

    # -- values --------------------------------------------------------

    def component_value_insert(
        self,
        *,
        component_ref_id: int,
        spec_id: str,
        value_num: float | None = None,
        value_low: float | None = None,
        value_high: float | None = None,
        value_text: str | None = None,
        value_bool: bool | None = None,
        conditions: dict[str, Any] | None = None,
        maturity: str = "lab",
        method: str | None = None,
        source_ref_id: int | None = None,
        source_chunk: str | None = None,
        source_url: str | None = None,
        as_of: str | None = None,
        set_by: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Insert one sourced measurement row. Returns the new ``id``."""
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO component_spec_values "
                "(component_ref_id, spec_id, value_num, value_low, "
                " value_high, value_text, value_bool, conditions, maturity, "
                " method, source_ref_id, source_chunk, source_url, as_of, "
                " set_by, notes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id",
                (
                    component_ref_id,
                    spec_id,
                    value_num,
                    value_low,
                    value_high,
                    value_text,
                    value_bool,
                    Jsonb(conditions or {}),
                    maturity,
                    method,
                    source_ref_id,
                    source_chunk,
                    source_url,
                    as_of,
                    set_by,
                    notes,
                ),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def component_values_for_ref(
        self, component_ref_id: int
    ) -> list[ComponentValueRowWithSource]:
        """Every value row for one component, grouped by spec (ordered
        ``spec_id``, most-recent first within a spec) — the component-page
        read. Each row carries ``source_kind`` (the source ref's kind, e.g.
        ``paper``/``datasheet``; ``None`` when there is no ``source_ref_id``
        or the source_url/no-source shape is used) so the renderer can
        format a handle without a second query."""
        cols = ", ".join("cv." + c.strip() for c in _VALUE_COLS.split(", "))
        with self.pool.connection() as conn:
            rows = _fetchall(
                conn,
                f"SELECT {cols}, sr.kind AS source_kind "
                "FROM component_spec_values cv "
                "LEFT JOIN refs sr ON sr.ref_id = cv.source_ref_id "
                "WHERE cv.component_ref_id = %s "
                "ORDER BY cv.spec_id, cv.created_at DESC",
                (component_ref_id,),
            )
        return [cast(ComponentValueRowWithSource, r) for r in rows]

    # -- made-of link ------------------------------------------------------

    def component_link_made_of(
        self, *, component_ref_id: int, material_ref_id: int
    ) -> None:
        """Create the ``made-of`` edge (component -> material). Idempotent —
        ``add_link`` dedupes on the unique ``(src, dst, relation)`` tuple."""
        self.add_link(
            src_ref_id=component_ref_id,
            dst_ref_id=material_ref_id,
            relation="made-of",
        )

    # -- assembly tree (contains) -----------------------------------------

    def component_contains_edge_meta(
        self, parent_ref_id: int, child_ref_id: int
    ) -> dict[str, Any] | None:
        """The existing ``contains`` edge's ``meta`` (``{qty, ref}``) for
        ``(parent_ref_id, child_ref_id)``, or ``None`` if no such edge
        exists. The handler uses this to PRESERVE the current ``qty`` on a
        re-put that omits ``qty=`` (e.g. one only updating
        ``ref_designator=``)."""
        for link in self.links_for(parent_ref_id, direction="out", relation="contains"):
            if link.dst_ref_id == child_ref_id:
                return link.meta or {}
        return None

    def component_would_cycle(self, parent_ref_id: int, child_ref_id: int) -> bool:
        """True if ``parent_ref_id --contains--> child_ref_id`` would create
        a cycle: ``child_ref_id`` is ``parent_ref_id`` itself, or is already
        a transitive ANCESTOR of ``parent_ref_id`` (adding the edge would
        then close a loop). ``add_link`` only guards the direct self-loop;
        this walks the ancestor chain (BFS over incoming ``contains`` edges)
        to catch the deeper transitive case, keeping the tree a DAG."""
        if child_ref_id == parent_ref_id:
            return True
        seen = {parent_ref_id}
        frontier = [parent_ref_id]
        while frontier:
            next_frontier: list[int] = []
            for ref_id in frontier:
                for link in self.links_for(ref_id, direction="in", relation="contains"):
                    ancestor_id = link.src_ref_id
                    if ancestor_id == child_ref_id:
                        return True
                    if ancestor_id not in seen:
                        seen.add(ancestor_id)
                        next_frontier.append(ancestor_id)
            frontier = next_frontier
        return False

    def component_add_contains(
        self,
        parent_ref_id: int,
        child_ref_id: int,
        *,
        qty: int,
        ref_designator: str | None = None,
    ) -> None:
        """Create/update the ``contains`` edge (parent -> child), storing
        ``qty``/``ref_designator`` in the link ``meta``. ``merge_meta=True``
        so a re-put updates the existing edge's meta rather than duplicating
        it. Only sets ``meta['ref']`` when ``ref_designator`` is given, so a
        qty-only re-put doesn't clobber a previously-recorded designator."""
        meta: dict[str, Any] = {"qty": qty}
        if ref_designator is not None:
            meta["ref"] = ref_designator
        self.add_link(
            src_ref_id=parent_ref_id,
            dst_ref_id=child_ref_id,
            relation="contains",
            meta=meta,
            merge_meta=True,
        )

    def component_remove_contains(self, parent_ref_id: int, child_ref_id: int) -> bool:
        """Remove the ``contains`` edge (parent -> child). Returns whether
        an edge actually existed (so the handler can echo a real removal
        vs. a no-op typo'd detach)."""
        n = self.remove_link(
            src_ref_id=parent_ref_id, dst_ref_id=child_ref_id, relation="contains"
        )
        return n > 0

    def component_contains_children(self, ref_id: int) -> list[dict[str, Any]]:
        """The direct ``contains`` children of ``ref_id``, each
        ``{child_ref_id, qty, ref}`` from the link meta (``qty`` defaults to
        1, ``ref`` to ``None``, for a hand-edited/legacy row missing a key).
        Ordered by link creation — stable tree/BOM rendering. The tree-walk
        (``view='tree'``/``'bom'``) recurses on this."""
        out = []
        for link in self.links_for(ref_id, direction="out", relation="contains"):
            meta = link.meta or {}
            out.append(
                {
                    "child_ref_id": link.dst_ref_id,
                    "qty": meta.get("qty", 1),
                    "ref": meta.get("ref"),
                }
            )
        return out

    def component_current_spec_value(
        self, ref_id: int, spec_id: str
    ) -> ComponentValueRow | None:
        """THE single "current value" authority for one (component, spec):
        the most-recent row (``ORDER BY as_of DESC NULLS LAST, created_at
        DESC LIMIT 1``), or ``None`` if none is recorded. A
        ``component_spec_values`` row is append-only and a (component,
        spec) may legitimately hold many values (``unit_cost`` explicitly
        so — as_of + price-break conditions); both the BOM rollup and the
        consistency-query annotation resolve to exactly one value per leaf
        per spec through this one helper, so they never disagree."""
        cols = ", ".join(_VALUE_COLS.split(", "))
        with self.pool.connection() as conn:
            row = _fetchone(
                conn,
                f"SELECT {cols} FROM component_spec_values "
                "WHERE component_ref_id = %s AND spec_id = %s "
                "ORDER BY as_of DESC NULLS LAST, created_at DESC LIMIT 1",
                (ref_id, spec_id),
            )
        return None if row is None else cast(ComponentValueRow, row)

    # -- search ----------------------------------------------------------

    def component_search_entities(
        self, q: str, *, limit: int = 20
    ) -> list[tuple[int, str | None, str, dict[str, Any]]]:
        """Lexical match over the component entity's name / aliases / mpn /
        manufacturer / category.

        Returns ``(ref_id, slug, title, meta)`` tuples, most-recently-updated
        first. ``q`` is matched as a case-insensitive substring, same
        leniency as ``material_search_entities``.
        """
        pat = f"%{q}%"
        with self.pool.connection() as conn:
            rows = _fetchall(
                conn,
                "SELECT r.ref_id AS ref_id, "
                "  (SELECT id_value FROM ref_identifiers ri "
                "   WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key' "
                "   LIMIT 1) AS slug, "
                "  r.title AS title, r.meta AS meta "
                "FROM refs r "
                "WHERE r.kind = 'component' AND r.deleted_at IS NULL AND ("
                "  r.title ILIKE %(pat)s "
                "  OR COALESCE(r.meta->>'category', '') ILIKE %(pat)s "
                "  OR COALESCE(r.meta->>'mpn', '') ILIKE %(pat)s "
                "  OR COALESCE(r.meta->>'manufacturer', '') ILIKE %(pat)s "
                "  OR EXISTS ("
                "    SELECT 1 FROM jsonb_array_elements_text("
                "      COALESCE(r.meta->'aliases', '[]'::jsonb)) a(v) "
                "    WHERE a.v ILIKE %(pat)s"
                "  )"
                ") "
                "ORDER BY r.updated_at DESC LIMIT %(limit)s",
                {"pat": pat, "limit": limit},
            )
        return [(r["ref_id"], r["slug"], r["title"], r["meta"] or {}) for r in rows]

    def component_search_values(
        self,
        *,
        spec_id: str,
        min_val: float | None = None,
        max_val: float | None = None,
        maturity: str | None = None,
        category_id: str | None = None,
        limit: int = 20,
    ) -> list[ComponentValueSearchRow]:
        """Range filter over ``component_spec_values`` for one spec, joined
        back to the owning component's title/slug/category.

        Bounds are inclusive and in the spec's canonical unit (v1 does no
        conversion — the caller/handler is responsible for that contract).
        The filter is an **interval overlap**, not a point-in-range test:
        ``min_val``/``max_val`` are compared against
        ``value_high``/``value_low`` (falling back to ``value_num`` when a
        row has no band), so a banded value matches whenever its band
        overlaps the query range — a point value (band NULL) keeps
        matching exactly as before. ``category_id=`` optionally narrows to
        components in that category. Returns rows ordered by ``value_num``
        ascending, each carrying the owning component's ``ref_id``/``title``.
        """
        clauses = ["cv.spec_id = %s"]
        params: list[Any] = [spec_id]
        if min_val is not None:
            clauses.append("COALESCE(cv.value_high, cv.value_num) >= %s")
            params.append(min_val)
        if max_val is not None:
            clauses.append("COALESCE(cv.value_low, cv.value_num) <= %s")
            params.append(max_val)
        if maturity is not None:
            clauses.append("cv.maturity = %s")
            params.append(maturity)
        if category_id is not None:
            clauses.append("r.meta->>'category' = %s")
            params.append(category_id)
        params.append(limit)
        cols = ", ".join("cv." + c.strip() for c in _VALUE_COLS.split(", "))
        sql = (
            f"SELECT {cols}, sr.kind AS source_kind, "
            "r.title AS component_title "
            "FROM component_spec_values cv "
            "JOIN refs r ON r.ref_id = cv.component_ref_id AND r.deleted_at IS NULL "
            "LEFT JOIN refs sr ON sr.ref_id = cv.source_ref_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY cv.value_num ASC NULLS LAST "
            "LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = _fetchall(conn, sql, params)
        return [cast(ComponentValueSearchRow, r) for r in rows]


__all__ = [
    "ComponentCategoryRow",
    "ComponentMixin",
    "ComponentSpecRow",
    "ComponentValueRow",
    "ComponentValueRowWithSource",
    "ComponentValueSearchRow",
]
