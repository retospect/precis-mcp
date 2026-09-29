"""``catalogue`` — the DB-backed :class:`~hexfold.catalogue.CatalogueStore`
(``docs/backlog/hexfold-integration.md`` step 6 slice 2; table
``se_hexfold_catalogue``, migration ``precis_se/0016``).

:mod:`hexfold.catalogue`'s own store is :class:`~hexfold.catalogue.
MemoryStore`, process-local by design — its module docstring names *this*
module as the persistent twin keyed the same way. The key is
:class:`~hexfold.catalogue.EnvKey` and the table key is
:meth:`~hexfold.catalogue.EnvKey.hash`, computed in Python by the same
method both stores call, so the two cannot drift apart on key
construction. Row bodies are the dataclasses' own ``to_dict`` /
``from_dict`` verbatim; this module adds persistence and nothing about
the physics.

**Rows are not scoped to a ref, and that is the whole point.** A rim
type's decay length is a fact about sp2 carbon at an edge, not about the
design that happened to ask first, so one row serves every design in the
database. The flip side is that a wrong row is wrong *everywhere*, which
is why this store, unlike :class:`~hexfold.catalogue.MemoryStore`, does
not hand every row it holds to :func:`~hexfold.catalogue.resolve_edge`:

**The measured-row gate (the step 6 slice 1 ruling).**
:func:`~hexfold.catalogue.resolve_edge` prefers an exact or nearest
measured ``N`` over the pinned wildcard — correct once measurements are
trustworthy, and today they are not.
:func:`~hexfold.catalogue.measure_environment`'s ``seam_radius`` tracks
the *length of the measurement tube*, which :class:`~hexfold.catalogue.
EnvKey` does not record, rather than the environment it claims to
describe (gripe 456641; the evidence is in
``docs/backlog/hexfold-integration.md`` §"Step 6 slice 1"). Two rows
measured at different lengths therefore collide on one key, and in a
*shared* table whichever tube warmed the cache first would set
``compose``'s guard band for every later join.

So :meth:`DbCatalogueStore.rows` — the only method ``resolve_edge``
calls — omits ``source="measured"`` rows unless the store was built with
``trust_measured=True``. Measured rows are still stored, still returned
by :meth:`~DbCatalogueStore.get`, and still listed by
:meth:`~DbCatalogueStore.measured`, so a warm-up's output can be read,
compared and reported; it just cannot silently outrank a pinned constant.
Flipping the default is gated on gripe 456641's three fixes, not on
taste: assert the relaxer converged, reject a non-monotone ``max_disp``
profile, and put the measurement extent into ``EnvKey``.

``compose`` never writes here (it is read-only on whatever store it is
given); rows arrive from :meth:`~DbCatalogueStore.seed` (the pinned
wildcards, restating :mod:`hexfold.join`'s constants) or from an explicit
:func:`warm_edge` call.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from hexfold import catalogue as hx_catalogue
from hexfold.catalogue import (
    BulkCell,
    EdgeMotif,
    EnvKey,
    Row,
    SeamMotif,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection

#: ``se_hexfold_catalogue.zone`` doubles as the row-type discriminator:
#: the mapping is total and one-to-one, so the table carries one column
#: rather than two that could disagree (migration 0016's own comment).
_LOADERS: dict[str, Any] = {
    "bulk": BulkCell.from_dict,
    "edge": EdgeMotif.from_dict,
    "seam": SeamMotif.from_dict,
}

#: The one ``source`` value :meth:`DbCatalogueStore.rows` withholds from
#: ``resolve_edge`` by default. ``"forced"`` is a deliberate operator
#: load and ``"pinned-<date>"`` restates a module constant; only this one
#: comes from :func:`~hexfold.catalogue.measure_environment`, whose
#: radius is not yet a function of its key (gripe 456641).
UNTRUSTED_SOURCE = "measured"


class DbCatalogueStore:
    """:class:`~hexfold.catalogue.CatalogueStore` over
    ``se_hexfold_catalogue``.

    ``store`` is the precis :class:`~precis.store.Store` (untyped here for
    the same circular-import reason the rest of :mod:`precis_se` keeps it
    ``Any``). ``conn`` joins an outer transaction when the caller owns
    one — the handler idiom — and is left ``None`` for a standalone
    lookup, which takes a pooled connection per call.

    ``trust_measured=True`` lets measured rows reach
    :func:`~hexfold.catalogue.resolve_edge`. Read the module docstring
    before setting it: it is not a performance knob, it decides whether a
    measurement nobody has validated can widen or narrow the seam guard
    band for every design in the database.
    """

    def __init__(
        self,
        store: Any,
        *,
        conn: Connection | None = None,
        trust_measured: bool = False,
    ) -> None:
        self._store = store
        self._conn = conn
        self.trust_measured = trust_measured

    # ---------- plumbing ----------

    def _read(self) -> Any:
        if self._conn is not None:
            return nullcontext(self._conn)
        return self._store.pool.connection()

    def _write(self) -> Any:
        if self._conn is not None:
            return nullcontext(self._conn)
        return self._store.tx()

    @staticmethod
    def _hydrate(zone: str, row_json: dict[str, Any]) -> Row:
        loader = _LOADERS.get(zone)
        if loader is None:  # pragma: no cover - guarded by the zone CHECK
            raise hx_catalogue.CatalogueError(
                f"se_hexfold_catalogue holds an unknown zone {zone!r}"
            )
        hydrated: Row = loader(row_json)
        return hydrated

    # ---------- CatalogueStore ----------

    def get(self, key: EnvKey) -> Row | None:
        """The row for exactly this key, measured or not — the gate in
        :meth:`rows` is about what ``resolve_edge`` may *prefer*, not
        about hiding rows from a caller that asked for one by name."""
        with self._read() as conn:
            got = conn.execute(
                "SELECT zone, row_json FROM se_hexfold_catalogue WHERE key_hash = %s",
                (key.hash(),),
            ).fetchone()
        if got is None:
            return None
        return self._hydrate(got[0], got[1])

    def put(self, row: Row, *, force: bool = False) -> None:
        """First-wins, like :meth:`~hexfold.catalogue.MemoryStore.put`:
        an existing row for the same key stays unless ``force``. A forced
        write is recorded as such by the row's own ``source`` — this
        method does not rewrite it, because a caller re-loading a pinned
        dump under ``force`` is not thereby producing a ``"forced"``
        measurement."""
        payload = row.to_dict()
        params = (
            row.key.hash(),
            row.key.zone,
            Jsonb(payload["key"]),
            Jsonb(payload),
            row.source,
            getattr(row, "hexfold_version", None),
        )
        conflict = (
            "DO UPDATE SET zone = EXCLUDED.zone, key_json = EXCLUDED.key_json, "
            "row_json = EXCLUDED.row_json, source = EXCLUDED.source, "
            "hexfold_version = EXCLUDED.hexfold_version, updated_at = now()"
            if force
            else "DO NOTHING"
        )
        with self._write() as conn:
            conn.execute(
                "INSERT INTO se_hexfold_catalogue "
                "(key_hash, zone, key_json, row_json, source, hexfold_version) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                f"ON CONFLICT (key_hash) {conflict}",
                params,
            )

    def rows(self, zone: str | None = None) -> list[Row]:
        """The rows :func:`~hexfold.catalogue.resolve_edge` is allowed to
        choose from — everything except ``source="measured"``, unless
        this store was built ``trust_measured=True``. See the module
        docstring for why the default is the restrictive one."""
        sql = "SELECT zone, row_json FROM se_hexfold_catalogue"
        params: list[Any] = []
        where: list[str] = []
        if zone is not None:
            where.append("zone = %s")
            params.append(zone)
        if not self.trust_measured:
            where.append("source <> %s")
            params.append(UNTRUSTED_SOURCE)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY key_hash"
        with self._read() as conn:
            got = conn.execute(sql, tuple(params)).fetchall()
        return [self._hydrate(z, rj) for z, rj in got]

    # ---------- beyond the protocol ----------

    def measured(self, zone: str | None = None) -> list[Row]:
        """The rows :meth:`rows` withholds. Reporting and comparison
        only: a caller that wants to show what a warm-up measured next to
        the pinned constant it did not replace reads them here."""
        sql = "SELECT zone, row_json FROM se_hexfold_catalogue WHERE source = %s"
        params: list[Any] = [UNTRUSTED_SOURCE]
        if zone is not None:
            sql += " AND zone = %s"
            params.append(zone)
        sql += " ORDER BY key_hash"
        with self._read() as conn:
            got = conn.execute(sql, tuple(params)).fetchall()
        return [self._hydrate(z, rj) for z, rj in got]

    def seed(self) -> int:
        """Load the pinned wildcard rows
        (:func:`~hexfold.catalogue.seed_rows`, which restates
        :mod:`hexfold.join`'s constants) if they are not already here.
        Idempotent and first-wins, so calling it on every join is cheap
        and never overwrites a forced load. Returns the number of rows
        offered, not the number inserted — the insert is
        ``ON CONFLICT DO NOTHING`` and does not report."""
        rows = hx_catalogue.seed_rows()
        with self._write() as conn:
            inner = DbCatalogueStore(self._store, conn=conn)
            for row in rows:
                inner.put(row)
        return len(rows)


def for_store(
    store: Any,
    *,
    conn: Connection | None = None,
    trust_measured: bool = False,
) -> DbCatalogueStore:
    """A seeded :class:`DbCatalogueStore` — the ordinary entry point for
    a caller (the se join op) that wants a catalogue to hand
    :func:`hexfold.join.compose`. Seeding is idempotent, so this is safe
    to call per join; it is the DB twin of
    :meth:`~hexfold.catalogue.MemoryStore.seeded`."""
    cat = DbCatalogueStore(store, conn=conn, trust_measured=trust_measured)
    cat.seed()
    return cat


def warm_edge(
    store: Any,
    nm: tuple[int, int],
    *,
    rung: str = "stick",
    conn: Connection | None = None,
    **kwargs: Any,
) -> tuple[EdgeMotif, BulkCell] | None:
    """Measure one edge environment and store both rows it yields.

    **Explicit and opt-in, never automatic.** The plan for this slice had
    the se join op warm a row on first use; that is held, because under
    the step 6 slice 1 ruling a measured row cannot influence a join's
    outcome, so an automatic warm-up would spend a full build-and-relax
    per new environment to produce a row nothing reads. Wiring it to a
    flag rather than to every join keeps the measurement available —
    for the convergence work gripe 456641 asks for — without charging
    every join for it.

    Returns the ``(edge, bulk)`` pair
    :func:`~hexfold.catalogue.measure_environment` produces — both are
    stored — or ``None`` when it declines the environment (a mixed rim,
    or a net over :data:`~hexfold.catalogue.MEASURE_ATOM_CAP`). Declining
    is an ordinary outcome for a caller sweeping tubes, not an error, so
    it comes back as ``None`` rather than as
    :class:`~hexfold.catalogue.CatalogueError`."""
    try:
        edge, bulk = hx_catalogue.measure_environment(nm, rung=rung, **kwargs)
    except hx_catalogue.CatalogueError:
        return None
    with nullcontext(conn) if conn is not None else store.tx() as c:
        inner = DbCatalogueStore(store, conn=c)
        inner.put(edge)
        inner.put(bulk)
    return edge, bulk


__all__ = [
    "UNTRUSTED_SOURCE",
    "BulkCell",
    "DbCatalogueStore",
    "EdgeMotif",
    "EnvKey",
    "SeamMotif",
    "for_store",
    "warm_edge",
]
