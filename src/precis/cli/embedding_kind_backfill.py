"""``precis backfill-embedding-kinds`` — finish migration 0226 on a big table.

0226 added ``chunk_embeddings.ref_id/kind``, the trigger that fills them and
three NOT VALID constraints. On a large table (prod) it leaves legacy rows
NULL and builds no index. This command does the rest, in order:

1. **fill** — non-paper rows first, then paper, in ``chunk_id`` windows, one
   short transaction per window (``statement_timeout`` per batch, a pause
   between batches). Each UPDATE only sets ``ref_id = NULL`` on unfilled
   rows: the trigger does the fill, so the copy keeps one definition.
2. **validate** — VALIDATE the three constraints (SHARE UPDATE EXCLUSIVE;
   inserts keep flowing).
3. **indexes** — ``CREATE INDEX CONCURRENTLY`` the FK-covering btree and one
   partial HNSW per kind in ``_ANN_PARTIAL_KINDS``, each built once from
   filled data; an INVALID leftover is dropped CONCURRENTLY and rebuilt;
   every index is checked ``indisvalid`` after. The router uses a kind's
   index only when it is valid and the CHECK is validated
   (``store/_chunks_ops.py::_ann_route``).
4. ``--reindex-global`` — ``REINDEX INDEX CONCURRENTLY`` the global HNSW.
   Required once after the fill: every filled row was a non-HOT update and
   left a dead tuple in it.

Phase 3 and 4 run outside any transaction and can take a long time, so they
need a connection whose ``statement_timeout`` allows it: the command passes
``options=-c statement_timeout=0`` at connect, which needs a direct Postgres
DSN (``--database-url``), not a pooler that rejects startup options.

Dry-run by default; ``--apply`` writes. Resume a fill with ``--resume-pass
<pass> --after <chunk_id>``: ``--after`` applies only to that pass, earlier
passes are skipped, later ones start from 0.

The index phase and ``--reindex-global`` need a direct DSN; ``run`` checks
that with a throwaway connection before any fill and refuses a pooler.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

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

#: Fill passes, in order: the 534k non-paper rows switch their kinds on
#: first; paper (3.4M rows) only matters to the CHECK.
_PASSES = (("non-paper", "r.kind <> 'paper'"), ("paper", "r.kind = 'paper'"))


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


def _index_valid(conn: Connection, name: str) -> bool | None:
    """``indisvalid`` of index ``name``; ``None`` when it does not exist."""
    row = conn.execute(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = %s",
        (name,),
    ).fetchone()
    return None if row is None else bool(row[0])


def _index_ddl() -> list[tuple[str, str]]:
    """``(name, CREATE INDEX CONCURRENTLY ...)`` for every 0226 index."""
    out = [
        (
            _BTREE,
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_BTREE} "
            "ON chunk_embeddings (ref_id, kind)",
        )
    ]
    for kind in _ANN_PARTIAL_KINDS:
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
    conn: Connection, *, maintenance_work_mem: str | None = None
) -> dict[str, bool]:
    """Build every 0226 index CONCURRENTLY; return ``{name: indisvalid}``.

    ``conn`` must be autocommit (CONCURRENTLY refuses a transaction block).
    An index left INVALID by an earlier failed build is dropped CONCURRENTLY
    and rebuilt once; ``IF NOT EXISTS`` alone would skip it.
    """
    if not conn.autocommit:
        raise ValueError("build_kind_indexes needs an autocommit connection")
    if maintenance_work_mem:
        # set_config binds the value; SET cannot take a parameter.
        conn.execute(
            "SELECT set_config('maintenance_work_mem', %s, false)",
            (maintenance_work_mem,),
        )
    result: dict[str, bool] = {}
    for name, ddl in _index_ddl():
        if _index_valid(conn, name) is False:
            conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        conn.execute(ddl)
        result[name] = bool(_index_valid(conn, name))
    return result


def backfill_embedding_kinds(
    conn: Connection,
    *,
    apply: bool,
    window: int = 500,
    after: int = 0,
    sleep_s: float = 0.5,
    batch_timeout_s: int = 60,
    validate: bool = True,
    resume_pass: str = _PASSES[0][0],
) -> BackfillResult:
    """Fill unfilled rows in ``chunk_id`` windows (non-paper, then paper), then
    validate. Index building is :func:`build_kind_indexes`.

    Each window runs in its own transaction with ``SET LOCAL
    statement_timeout`` (on an autocommit ``conn`` that commits per window;
    otherwise the windows nest as savepoints in the caller's transaction,
    as the tests do). ``window`` is a ``chunk_id`` span, not a row count.
    ``after`` applies to the pass named ``resume_pass`` only; earlier passes
    are skipped (already done), later ones start at 0.
    """
    names = [n for n, _ in _PASSES]
    if resume_pass not in names:
        raise ValueError(f"resume_pass must be one of {names}, got {resume_pass!r}")
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
    for label, pred in _PASSES[names.index(resume_pass) :]:
        lo = after if label == resume_pass else 0
        last_pass = label
        while lo <= max_id:
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
                    (lo, lo + window),
                )
                updated += cur.rowcount or 0
            lo += window
            if sleep_s:
                time.sleep(sleep_s)
    validated = False
    left = _unfilled(conn)
    if validate and left == 0:
        for name in _CONSTRAINTS:
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout = 0")
                conn.execute(f"ALTER TABLE chunk_embeddings VALIDATE CONSTRAINT {name}")
        validated = True
    return BackfillResult(before, updated, lo, validated, last_pass, left)


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``backfill-embedding-kinds`` subparser on ``sub``."""
    p = sub.add_parser(
        "backfill-embedding-kinds",
        help="Fill chunk_embeddings.ref_id/kind after migration 0226.",
        description=(
            "Fill unfilled chunk_embeddings rows (non-paper, then paper) so the "
            "0226 trigger sets ref_id/kind, VALIDATE the 0226 constraints, then "
            "build the per-kind indexes CONCURRENTLY. Dry-run by default."
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
        "--maintenance-work-mem",
        default="2GB",
        help="maintenance_work_mem for the index builds (Postgres size string).",
    )
    p.add_argument("--sleep", type=float, default=0.5, help="Seconds between windows.")
    p.add_argument(
        "--batch-timeout",
        type=int,
        default=60,
        help="statement_timeout (s) per window.",
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
    import psycopg

    try:
        with psycopg.connect(
            dsn, autocommit=True, options="-c statement_timeout=0", connect_timeout=15
        ) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        print(
            "refusing: the index/reindex phase needs a direct Postgres connection "
            "(not the pgbouncer pooler), which must accept "
            "'-c statement_timeout=0' at connect "
            f"({type(exc).__name__}). Either pass --database-url <direct DSN>, or "
            "pass --no-indexes (and not --reindex-global) to run only the fill "
            "and validate through the pooler. Nothing was changed.",
            file=sys.stderr,
        )
        sys.exit(2)


def run(args: argparse.Namespace) -> None:
    """Execute ``precis backfill-embedding-kinds``."""
    import psycopg

    dsn = resolve_dsn(args.database_url)
    mode = "APPLY" if args.apply else "DRY-RUN"
    needs_direct = (not args.no_indexes) or args.reindex_global
    if args.apply and needs_direct:
        require_direct_connection(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
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
            )
        print(
            f"backfill-embedding-kinds [{mode}]: unfilled before {res.unfilled_before}, "
            f"updated {res.updated}, validated {res.validated}. "
            f"Resume with --resume-pass {res.last_pass} --after {res.last_chunk_id}.",
            file=sys.stderr,
        )
        if not args.apply:
            return
        left = res.unfilled_after if res.unfilled_after is not None else _unfilled(conn)
        if left:
            print("rows still unfilled: index phase skipped.", file=sys.stderr)
            sys.exit(1)
    if not needs_direct:
        return
    # Long, transaction-less DDL: no statement_timeout for this session.
    with psycopg.connect(
        dsn, autocommit=True, options="-c statement_timeout=0"
    ) as conn:
        if not args.no_indexes:
            built = build_kind_indexes(
                conn, maintenance_work_mem=args.maintenance_work_mem
            )
            for name, ok in built.items():
                print(f"  {name}: {'valid' if ok else 'INVALID'}", file=sys.stderr)
            if not all(built.values()):
                sys.exit(1)
        if args.reindex_global:
            conn.execute(f"REINDEX INDEX CONCURRENTLY {_GLOBAL_HNSW}")
            print(
                f"  {_GLOBAL_HNSW}: reindexed, valid={_index_valid(conn, _GLOBAL_HNSW)}",
                file=sys.stderr,
            )


__all__ = [
    "BackfillResult",
    "add_parser",
    "backfill_embedding_kinds",
    "build_kind_indexes",
    "require_direct_connection",
    "run",
]
