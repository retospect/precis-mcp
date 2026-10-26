"""``precis backfill-embedding-kinds`` — finish migration 0226 on a big table.

0226 added ``chunk_embeddings.ref_id/kind``, the trigger that fills them and
three NOT VALID constraints. On a large table (prod) it leaves legacy rows
NULL and builds no index. This command does the rest, in order:

0. **autovacuum off** — ``ALTER TABLE chunk_embeddings SET
   (autovacuum_enabled = false)`` for the whole apply run: every filled row
   is a non-HOT update and autovacuum would race the fill instead of running
   once. On exit (success or failure) the command runs a manual
   ``VACUUM ANALYZE`` and resets the reloption.
1. **btree** — ``CREATE INDEX CONCURRENTLY`` ``chunk_embeddings_ref_id_kind_idx``
   before any fill: the ``chunk_embeddings_ref_kind_fkey`` ON DELETE/UPDATE
   CASCADE trigger seq-scans the table on every refs delete or kind update
   until this index exists. Skipped with ``--no-btree`` (it must already
   exist, e.g. on a small DB where 0226 built it).
2. **fill** — non-paper rows first, then paper, in ``chunk_id`` windows, one
   short transaction per window (``statement_timeout`` per batch). A window
   cancelled by the timeout or rolled back by a deadlock is retried at half
   the span (the smaller span stays for the rest of the pass — a dense
   window means the next one is dense too). A window that fills nothing
   does not sleep. Each UPDATE only sets ``ref_id = NULL`` on unfilled
   rows: the trigger does the fill, so the copy keeps one definition.
   ``--until-pass`` bounds the run (e.g. ``non-paper`` to defer the 20–45 h
   paper pass). After each pass the run counts remaining unfilled rows per
   kind (chunks→refs join) and upserts a ``chunk_embeddings_kind_ready``
   marker per fill-complete ``_ANN_PARTIAL_KINDS`` kind (migration 0242) —
   the router's per-kind gate.
3. **validate** — VALIDATE the three constraints (SHARE UPDATE EXCLUSIVE;
   inserts keep flowing). Only runs when NO row is unfilled; the per-kind
   markers from step 2 do not wait for it.
4. **indexes** — ``CREATE INDEX CONCURRENTLY`` one partial HNSW per
   fill-complete kind in ``_ANN_PARTIAL_KINDS`` (the btree is re-ensured
   first, a no-op after step 1), each built once from filled data; an
   INVALID leftover is dropped CONCURRENTLY and rebuilt; every index is
   checked ``indisvalid`` after. The router uses a kind's index once it is
   valid+ready and the kind's fill is marked — or, for every kind at once,
   once the CHECK is validated (``store/_chunks_ops.py::_ann_route``).
5. ``--reindex-global`` — ``REINDEX INDEX CONCURRENTLY`` the global HNSW.
   Required once after the fill: every filled row was a non-HOT update and
   left a dead tuple in it. An INVALID ``*_ccnew``/``*_ccold`` leftover from
   a failed earlier reindex is dropped CONCURRENTLY first.

The index phases and ``--reindex-global`` run outside any transaction and
can take a long time, so the apply session connects with
``options=-c statement_timeout=0 -c lock_timeout=0`` (each fill window sets
its own ``statement_timeout`` locally) — which needs a direct Postgres DSN
(``--database-url``), not a pooler that rejects startup options. The session
also gets ``maintenance_work_mem`` and ``max_parallel_maintenance_workers``
(``--maintenance-work-mem``, ``--max-parallel-maintenance-workers``) before
any DDL. The 2 GB ``--maintenance-work-mem`` default is sized for the
32 GB prod host, which co-hosts the embedder: ~8 GB shared_buffers plus a
2 GB maintenance allocation leaves headroom the old 18 GB default did not
(jetsam risk, gr513663). A fill-only run (``--no-btree --no-indexes``, no
``--reindex-global``) still works through the pooler; ``run`` probes the
options connection first and refuses a pooler before touching anything.

Dry-run by default; ``--apply`` writes. Resume a fill with ``--resume-pass
<pass> --after <chunk_id>``: ``--after`` applies only to that pass, earlier
passes are skipped, later ones start from 0. ``--until-pass <pass>`` stops
the fill after that pass (default: the last pass), so
``--until-pass non-paper`` fills, marks and indexes the partial kinds
without starting the paper pass.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import Connection

from precis.cli._common import resolve_dsn
from precis.store._chunks_ops import _ANN_PARTIAL_KINDS, ann_partial_index_name

_CONSTRAINTS = (
    "chunk_embeddings_chunk_ref_fkey",
    "chunk_embeddings_ref_kind_fkey",
    "chunk_embeddings_ref_kind_filled",
)
_BTREE = "chunk_embeddings_ref_id_kind_idx"
_GLOBAL_HNSW = "chunk_embeddings_vec_hnsw_idx"
_GLOBAL_HNSW_DDL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_GLOBAL_HNSW} ON chunk_embeddings "
    "USING hnsw (vector vector_cosine_ops) WHERE status = 'ok' AND vector IS NOT NULL"
)

#: Fill passes, in order: the 534k non-paper rows switch their kinds on
#: first; paper (3.4M rows) only matters to the CHECK.
_PASSES = (("non-paper", "r.kind <> 'paper'"), ("paper", "r.kind = 'paper'"))

#: Errors one fill window retries at half span: the per-window
#: ``statement_timeout`` cancelling the UPDATE, or a deadlock victim.
_RETRYABLE = (psycopg.errors.QueryCanceled, psycopg.errors.DeadlockDetected)


@dataclass
class BackfillResult:
    """Outcome of one :func:`backfill_embedding_kinds` run."""

    unfilled_before: int
    updated: int
    last_chunk_id: int
    validated: bool
    last_pass: str = ""
    unfilled_after: int | None = None
    indexes: dict[str, bool] = field(default_factory=dict)
    unfilled_by_kind: dict[str, int] = field(default_factory=dict)
    marked: frozenset[str] = frozenset()


def _unfilled(conn: Connection, *, timeout_s: int = 1800) -> int:
    """One count of unfilled rows, under its own generous ``statement_timeout``."""
    with conn.transaction():
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)", (f"{int(timeout_s)}s",)
        )
        row = conn.execute(
            "SELECT count(*) FROM chunk_embeddings WHERE ref_id IS NULL OR kind IS NULL"
        ).fetchone()
    return int(row[0]) if row else 0


def _unfilled_rows(conn: Connection, *, limit: int = 25) -> list[tuple[Any, ...]]:
    """A sample of rows the fill could not fill, with the join diagnosis.

    A row stays unfilled when the trigger found nothing to copy: the chunk
    row is gone, or the chunk's ref is gone. Listed so a stalled fill's
    failure names the cause instead of a bare count.
    """
    return conn.execute(
        "SELECT ce.chunk_id, ce.status, ce.attempts, "
        "       (c.chunk_id IS NOT NULL) AS has_chunk, "
        "       r.ref_id AS ref_id, r.kind AS ref_kind "
        "FROM chunk_embeddings ce "
        "LEFT JOIN chunks c ON c.chunk_id = ce.chunk_id "
        "LEFT JOIN refs r ON r.ref_id = c.ref_id "
        "WHERE ce.ref_id IS NULL OR ce.kind IS NULL "
        "ORDER BY ce.chunk_id LIMIT %s",
        (limit,),
    ).fetchall()


def _index_valid(conn: Connection, name: str) -> bool | None:
    """``indisvalid`` of index ``name``; ``None`` when it does not exist."""
    row = conn.execute(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = %s",
        (name,),
    ).fetchone()
    return None if row is None else bool(row[0])


#: Marker table the router reads as the per-kind fill gate (migration 0242).
_KIND_READY_TABLE = "chunk_embeddings_kind_ready"


def _check_validated(conn: Connection) -> bool:
    """``True`` when the global 0226 CHECK is validated — every kind is
    fill-complete regardless of markers."""
    row = conn.execute(
        "SELECT convalidated FROM pg_constraint "
        "WHERE conrelid = 'chunk_embeddings'::regclass "
        "  AND conname = 'chunk_embeddings_ref_kind_filled'"
    ).fetchone()
    return bool(row and row[0])


def _mark_filled_kinds(
    conn: Connection, *, timeout_s: int = 1800
) -> tuple[dict[str, int], frozenset[str]]:
    """Attribute remaining unfilled rows to kinds; mark fill-complete ones.

    A ``chunk_embeddings`` row is "unfilled for kind K" when
    ``ref_id IS NULL OR kind IS NULL`` and its chunk's ref resolves to K —
    exactly the rows a global-route ``r.kind = K`` search would still find:
    a row whose chunk resolves to no ref matches no kind filter and is
    invisible to every route. Each ``_ANN_PARTIAL_KINDS`` kind with no
    unfilled row gets a ``_KIND_READY_TABLE`` row (upsert — markers are
    never deleted; a ref re-kinded while it still has unfilled rows can
    leave one stale — gr515083: the one re-kind site,
    ``scripts/_conventions_findings_to_memory.py``, fills its stragglers
    in the re-kind transaction, and a ratchet in
    ``tests/test_kind_scoped_ann.py`` bars a new ``UPDATE refs`` site
    from assigning ``kind``). Returns ``(unfilled_by_kind, marked)``.
    On a pre-0242 database (no marker table) both come back empty.
    """
    present = conn.execute(
        "SELECT to_regclass('chunk_embeddings_kind_ready')"
    ).fetchone()
    if present is None or present[0] is None:
        return {}, frozenset()
    with conn.transaction():
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)", (f"{int(timeout_s)}s",)
        )
        rows = conn.execute(
            "SELECT r.kind, count(*) FROM chunk_embeddings ce "
            "JOIN chunks c ON c.chunk_id = ce.chunk_id "
            "JOIN refs r ON r.ref_id = c.ref_id "
            "WHERE ce.ref_id IS NULL OR ce.kind IS NULL "
            "GROUP BY r.kind"
        ).fetchall()
    by_kind = {str(k): int(n) for k, n in rows}
    marked = frozenset(k for k in _ANN_PARTIAL_KINDS if k not in by_kind)
    if marked:
        conn.execute(
            f"INSERT INTO {_KIND_READY_TABLE} (kind) "
            "SELECT k FROM unnest(%s::text[]) u(k) "
            "ON CONFLICT (kind) DO UPDATE SET marked_at = now()",
            (sorted(marked),),
        )
    return by_kind, marked


def ensure_ref_kind_btree(conn: Connection) -> bool:
    """Ensure ``chunk_embeddings_ref_id_kind_idx``; return ``indisvalid``.

    ``conn`` must be autocommit (CONCURRENTLY refuses a transaction block).
    An INVALID leftover from a failed build is dropped CONCURRENTLY first —
    ``IF NOT EXISTS`` alone would keep it. This is step 1 of the rollout:
    the ``chunk_embeddings_ref_kind_fkey`` ON DELETE/UPDATE CASCADE trigger
    seq-scans the table on every refs delete or kind update without it.
    """
    if _index_valid(conn, _BTREE) is False:
        conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_BTREE}")
    conn.execute(
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_BTREE} "
        "ON chunk_embeddings (ref_id, kind)"
    )
    return bool(_index_valid(conn, _BTREE))


def _kind_index_ddl(
    kinds: tuple[str, ...] | list[str] | None = None,
) -> list[tuple[str, str]]:
    """``(name, CREATE INDEX CONCURRENTLY ...)`` for the per-kind partials.

    ``kinds`` restricts the set (default: all of ``_ANN_PARTIAL_KINDS``).
    """
    out: list[tuple[str, str]] = []
    for kind in _ANN_PARTIAL_KINDS if kinds is None else kinds:
        name = ann_partial_index_name(kind)
        lit = kind.replace("'", "''")
        out.append(
            (
                name,
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON chunk_embeddings "
                "USING hnsw (vector vector_cosine_ops) "
                f"WHERE kind = '{lit}' AND status = 'ok' AND vector IS NOT NULL",
            )
        )
    return out


def build_kind_indexes(
    conn: Connection,
    *,
    maintenance_work_mem: str | None = None,
    kinds: list[str] | tuple[str, ...] | None = None,
) -> dict[str, bool]:
    """Build the 0226 indexes CONCURRENTLY; return ``{name: indisvalid}``.

    ``conn`` must be autocommit (CONCURRENTLY refuses a transaction block).
    An index left INVALID by an earlier failed build is dropped CONCURRENTLY
    and rebuilt once; ``IF NOT EXISTS`` alone would skip it. ``kinds``
    restricts which partial indexes are built (default: all of
    ``_ANN_PARTIAL_KINDS``); the btree is always re-ensured.
    """
    if not conn.autocommit:
        raise ValueError("build_kind_indexes needs an autocommit connection")
    if maintenance_work_mem:
        # set_config binds the value; SET cannot take a parameter.
        conn.execute(
            "SELECT set_config('maintenance_work_mem', %s, false)",
            (maintenance_work_mem,),
        )
    result = {_BTREE: ensure_ref_kind_btree(conn)}
    for name, ddl in _kind_index_ddl(kinds):
        if _index_valid(conn, name) is False:
            conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        conn.execute(ddl)
        result[name] = bool(_index_valid(conn, name))
    return result


def _reindex_leftovers(conn: Connection, name: str) -> list[str]:
    """INVALID ``<name>_ccnew*``/``<name>_ccold*`` indexes a failed REINDEX
    CONCURRENTLY left behind."""
    rows = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
        "WHERE NOT i.indisvalid AND c.relkind = 'i' "
        "  AND (starts_with(c.relname, %s) OR starts_with(c.relname, %s))",
        (f"{name}_ccnew", f"{name}_ccold"),
    ).fetchall()
    return [str(r[0]) for r in rows]


def reindex_global(conn: Connection, *, name: str = _GLOBAL_HNSW) -> bool:
    """``REINDEX INDEX CONCURRENTLY`` ``name``; return ``indisvalid`` after.

    ``conn`` must be autocommit. Invalid ``*_ccnew``/``*_ccold`` leftovers of
    an earlier failed reindex are dropped CONCURRENTLY first — a retry would
    otherwise collide on the names. When the index itself is INVALID the
    reindex would fail, so it is dropped and created CONCURRENTLY from
    ``_GLOBAL_HNSW_DDL`` instead.
    """
    for stale in _reindex_leftovers(conn, name):
        conn.execute(
            psycopg.sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(
                psycopg.sql.Identifier(stale)
            )
        )
    if _index_valid(conn, name) is False:
        conn.execute(
            psycopg.sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(
                psycopg.sql.Identifier(name)
            )
        )
        conn.execute(_GLOBAL_HNSW_DDL)
    else:
        conn.execute(
            psycopg.sql.SQL("REINDEX INDEX CONCURRENTLY {}").format(
                psycopg.sql.Identifier(name)
            )
        )
    return bool(_index_valid(conn, name))


def _set_maintenance_params(
    conn: Connection, *, work_mem: str | None, parallel_workers: int | None
) -> None:
    """Session-level maintenance GUCs for the DDL session (``is_local=false``;
    a session SET survives autocommit's implicit commits)."""
    if work_mem:
        conn.execute(
            "SELECT set_config('maintenance_work_mem', %s, false)", (work_mem,)
        )
    if parallel_workers is not None:
        conn.execute(
            "SELECT set_config('max_parallel_maintenance_workers', %s, false)",
            (str(parallel_workers),),
        )


@contextmanager
def autovacuum_paused(conn: Connection) -> Iterator[None]:
    """Disable autovacuum on ``chunk_embeddings`` for the apply window.

    On exit — success or failure — run a manual ``VACUUM ANALYZE`` and reset
    the reloption. A cleanup failure is reported on stderr and raised only
    when the body did not itself fail, so it never masks the real error and
    never leaves autovacuum silently off.
    """
    conn.execute("ALTER TABLE chunk_embeddings SET (autovacuum_enabled = false)")
    try:
        yield
    finally:
        body_failed = sys.exc_info()[0] is not None
        errs: list[Exception] = []
        for stmt in (
            "VACUUM ANALYZE chunk_embeddings",
            "ALTER TABLE chunk_embeddings RESET (autovacuum_enabled)",
        ):
            try:
                conn.execute(stmt)
            except Exception as exc:
                errs.append(exc)
                print(
                    f"warning: {stmt!r} failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        if errs and not body_failed:
            raise errs[0]


def backfill_embedding_kinds(
    conn: Connection,
    *,
    apply: bool,
    window: int = 500,
    after: int = 0,
    sleep_s: float = 0.5,
    batch_timeout_s: int = 300,
    validate: bool = True,
    resume_pass: str = _PASSES[0][0],
    until_pass: str = _PASSES[-1][0],
) -> BackfillResult:
    """Fill unfilled rows in ``chunk_id`` windows (non-paper, then paper), then
    validate. Index building is :func:`build_kind_indexes`.

    Each window runs in its own transaction with ``SET LOCAL
    statement_timeout`` (on an autocommit ``conn`` that commits per window;
    otherwise the windows nest as savepoints in the caller's transaction,
    as the tests do). A window killed by that timeout or by a deadlock is
    retried at half the span — which then stays for the rest of the pass —
    and re-raised when it can no longer be halved. ``window`` is a
    ``chunk_id`` span, not a row count. ``after`` applies to the pass named
    ``resume_pass`` only; earlier passes are skipped (already done), later
    ones start at 0; ``until_pass`` bounds the run (default: the last pass),
    so ``until_pass='non-paper'`` defers the long paper pass while still
    marking the partial kinds. The ``sleep_s`` pause is skipped after a
    window that updated no rows. After each pass, :func:`_mark_filled_kinds`
    re-counts unfilled rows per kind and marks the fill-complete
    ``_ANN_PARTIAL_KINDS`` kinds in ``chunk_embeddings_kind_ready``.
    """
    names = [n for n, _ in _PASSES]
    if resume_pass not in names:
        raise ValueError(f"resume_pass must be one of {names}, got {resume_pass!r}")
    if until_pass not in names or names.index(until_pass) < names.index(resume_pass):
        raise ValueError(
            f"until_pass must be one of {names} at or after "
            f"{resume_pass!r}, got {until_pass!r}"
        )
    before = _unfilled(conn)
    if not apply:
        return BackfillResult(before, 0, after, False, resume_pass)
    row = conn.execute(
        "SELECT coalesce(max(chunk_id), 0) FROM chunk_embeddings"
    ).fetchone()
    max_id = int(row[0]) if row else 0
    updated = 0
    lo = after
    last_pass = resume_pass
    unfilled_by_kind: dict[str, int] = {}
    marked: frozenset[str] = frozenset()
    stop = names.index(until_pass) + 1
    for label, pred in _PASSES[names.index(resume_pass) : stop]:
        lo = after if label == resume_pass else 0
        last_pass = label
        span = window
        while lo <= max_id:
            try:
                with conn.transaction():
                    conn.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (f"{int(batch_timeout_s)}s",),
                    )
                    cur = conn.execute(
                        "UPDATE chunk_embeddings ce SET ref_id = NULL "
                        "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
                        "WHERE c.chunk_id = ce.chunk_id "
                        "  AND ce.chunk_id > %s AND ce.chunk_id <= %s "
                        f"  AND (ce.ref_id IS NULL OR ce.kind IS NULL) AND {pred}",
                        (lo, lo + span),
                    )
                    n = cur.rowcount or 0
            except _RETRYABLE:
                if span <= 1:
                    raise
                span = max(1, span // 2)
                continue
            updated += n
            lo += span
            if sleep_s and n:
                time.sleep(sleep_s)
        unfilled_by_kind, marked = _mark_filled_kinds(conn)
    validated = False
    left = _unfilled(conn)
    if validate and left == 0:
        for name in _CONSTRAINTS:
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout = 0")
                conn.execute(f"ALTER TABLE chunk_embeddings VALIDATE CONSTRAINT {name}")
        validated = True
    return BackfillResult(
        before,
        updated,
        lo,
        validated,
        last_pass,
        left,
        unfilled_by_kind=unfilled_by_kind,
        marked=marked,
    )


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``backfill-embedding-kinds`` subparser on ``sub``."""
    p = sub.add_parser(
        "backfill-embedding-kinds",
        help="Fill chunk_embeddings.ref_id/kind after migration 0226.",
        description=(
            "Build the FK-covering btree, fill unfilled chunk_embeddings rows "
            "(non-paper, then paper) so the 0226 trigger sets ref_id/kind, "
            "VALIDATE the 0226 constraints, build the per-kind indexes "
            "CONCURRENTLY, and optionally REINDEX the global HNSW. "
            "Dry-run by default."
        ),
    )
    p.add_argument("--apply", action="store_true", help="Write.")
    p.add_argument("--window", type=int, default=500, help="chunk_id span per UPDATE.")
    p.add_argument(
        "--after",
        type=int,
        default=0,
        help="Resume after this chunk_id, in the pass named by --resume-pass only "
        "(later passes start at 0).",
    )
    p.add_argument(
        "--resume-pass",
        choices=[n for n, _ in _PASSES],
        default=_PASSES[0][0],
        help="Pass --after applies to; earlier passes are skipped.",
    )
    p.add_argument(
        "--until-pass",
        choices=[n for n, _ in _PASSES],
        default=_PASSES[-1][0],
        help="Stop the fill after this pass (default: the last). "
        "--until-pass non-paper fills + marks the partial kinds without "
        "starting the 20-45 h paper pass.",
    )
    p.add_argument(
        "--maintenance-work-mem",
        default="2GB",
        help="maintenance_work_mem for the index/reindex session (Postgres "
        "size string). Default 2GB is sized for the 32 GB prod host, which "
        "co-hosts the embedder — ~8 GB shared_buffers plus concurrent "
        "builds need the headroom (the former 18GB default risked OOM/"
        "jetsam, gr513663). Raise only on a dedicated host.",
    )
    p.add_argument(
        "--max-parallel-maintenance-workers",
        type=int,
        default=4,
        help="max_parallel_maintenance_workers for the index/reindex session.",
    )
    p.add_argument("--sleep", type=float, default=0.5, help="Seconds between windows.")
    p.add_argument(
        "--batch-timeout",
        type=int,
        default=300,
        help="statement_timeout (s) per window; a timed-out window retries "
        "at half span.",
    )
    p.add_argument(
        "--no-btree",
        action="store_true",
        help=f"Skip the pre-fill {_BTREE} build (it must already exist).",
    )
    p.add_argument("--skip-fill", action="store_true", help="Skip fill + validate.")
    p.add_argument("--no-indexes", action="store_true", help="Skip the index phase.")
    p.add_argument(
        "--reindex-global",
        action="store_true",
        help=f"REINDEX INDEX CONCURRENTLY {_GLOBAL_HNSW} at the end.",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def require_direct_connection(dsn: str) -> None:
    """Exit before any work unless ``dsn`` accepts ``statement_timeout=0``.

    The index and reindex phases connect with ``options=-c statement_timeout=0``,
    which pgbouncer rejects at startup. Probing it here (one ``SELECT 1``)
    turns a failure after a multi-hour fill into an immediate refusal. The
    driver's error text is not echoed: it can carry connection details.
    """
    try:
        with psycopg.connect(
            dsn, autocommit=True, options="-c statement_timeout=0", connect_timeout=15
        ) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        print(
            "refusing: the btree/index/reindex phase needs a direct Postgres "
            "connection (not the pgbouncer pooler), which must accept "
            "'-c statement_timeout=0' at connect "
            f"({type(exc).__name__}). Either pass --database-url <direct DSN>, or "
            "pass --no-btree --no-indexes (and not --reindex-global) to run "
            "only the fill and validate through the pooler. Nothing was changed.",
            file=sys.stderr,
        )
        sys.exit(2)


def run(args: argparse.Namespace) -> None:
    """Execute ``precis backfill-embedding-kinds``."""
    dsn = resolve_dsn(args.database_url)
    mode = "APPLY" if args.apply else "DRY-RUN"
    needs_direct = args.apply and (
        not args.no_btree or not args.no_indexes or args.reindex_global
    )
    if needs_direct:
        require_direct_connection(dsn)
    connect_kw: dict[str, Any] = {"autocommit": True}
    if needs_direct:
        # lock_timeout=0 too: agent_rw's role default (0096) cancels a
        # CONCURRENTLY build while it waits out the embed worker's open
        # transactions, and these options replace PGOPTIONS. A CONCURRENTLY
        # build holds only SHARE UPDATE EXCLUSIVE, so waiting blocks no DML.
        connect_kw["options"] = "-c statement_timeout=0 -c lock_timeout=0"
    with psycopg.connect(dsn, **connect_kw) as conn:
        pause = autovacuum_paused(conn) if args.apply else nullcontext()
        with pause:
            if needs_direct:
                _set_maintenance_params(
                    conn,
                    work_mem=args.maintenance_work_mem,
                    parallel_workers=args.max_parallel_maintenance_workers,
                )
            if args.apply and not args.no_btree:
                ok = ensure_ref_kind_btree(conn)
                print(
                    f"  {_BTREE}: {'valid' if ok else 'INVALID'}",
                    file=sys.stderr,
                )
                if not ok:
                    sys.exit(1)
            if args.skip_fill:
                res = BackfillResult(
                    _unfilled(conn), 0, args.after, False, args.resume_pass
                )
                res.unfilled_after = res.unfilled_before
            else:
                res = backfill_embedding_kinds(
                    conn,
                    apply=args.apply,
                    window=args.window,
                    after=args.after,
                    sleep_s=args.sleep,
                    batch_timeout_s=args.batch_timeout,
                    resume_pass=args.resume_pass,
                    until_pass=args.until_pass,
                )
            print(
                f"backfill-embedding-kinds [{mode}]: unfilled before "
                f"{res.unfilled_before}, updated {res.updated}, "
                f"validated {res.validated}. Resume with --resume-pass "
                f"{res.last_pass} --after {res.last_chunk_id}.",
                file=sys.stderr,
            )
            if not args.apply:
                return
            if args.skip_fill:
                res.unfilled_by_kind, res.marked = _mark_filled_kinds(conn)
            left = (
                res.unfilled_after
                if res.unfilled_after is not None
                else _unfilled(conn)
            )
            # The index phase needs every indexed kind fill-complete — a
            # per-kind gate (0242 markers), not the table-wide CHECK, so a
            # straggler paper pass does not hold back 14 usable indexes.
            ready = (
                set(_ANN_PARTIAL_KINDS) if _check_validated(conn) else set(res.marked)
            )
            unready = [k for k in _ANN_PARTIAL_KINDS if k not in ready]
            if unready and not args.skip_fill:
                by_kind = res.unfilled_by_kind
                print(
                    f"{len(unready)} of {len(_ANN_PARTIAL_KINDS)} partial "
                    "kinds still have unfilled rows "
                    + ", ".join(
                        f"{k} ({by_kind[k]} unfilled)" if k in by_kind else k
                        for k in unready
                    )
                    + "; first 25 unfilled rows:",
                    file=sys.stderr,
                )
                for (
                    cid,
                    status,
                    attempts,
                    has_chunk,
                    ref_id,
                    ref_kind,
                ) in _unfilled_rows(conn):
                    print(
                        f"  chunk_id={cid} status={status} attempts={attempts} "
                        f"chunk={'yes' if has_chunk else 'MISSING'} "
                        f"ref_id={ref_id if ref_id is not None else 'MISSING'} "
                        f"ref_kind={ref_kind}",
                        file=sys.stderr,
                    )
                print("fill incomplete: index phase skipped.", file=sys.stderr)
                sys.exit(1)
            if unready:
                print(
                    "  not fill-complete, no index built: " + ", ".join(unready),
                    file=sys.stderr,
                )
            if left:
                print(
                    f"  {left} rows still unfilled "
                    f"({'--skip-fill' if args.skip_fill else 'not on any indexed kind'}); "
                    "the 0226 CHECK stays NOT VALID.",
                    file=sys.stderr,
                )
            if not args.no_indexes:
                built = build_kind_indexes(conn, kinds=sorted(ready))
                for name, ok in built.items():
                    print(f"  {name}: {'valid' if ok else 'INVALID'}", file=sys.stderr)
                if not all(built.values()):
                    sys.exit(1)
            if args.reindex_global:
                ok = reindex_global(conn)
                print(
                    f"  {_GLOBAL_HNSW}: reindexed, valid={ok}",
                    file=sys.stderr,
                )
                if not ok:
                    sys.exit(1)


__all__ = [
    "BackfillResult",
    "add_parser",
    "autovacuum_paused",
    "backfill_embedding_kinds",
    "build_kind_indexes",
    "ensure_ref_kind_btree",
    "reindex_global",
    "require_direct_connection",
    "run",
]
