"""Kind-scoped semantic search (migration 0226, gr495190).

``chunk_embeddings`` carries a copy of its ref's ``kind`` that Postgres owns:
a trigger fills it, composite FKs check it and carry kind changes along. A big
kind gets its own partial HNSW index (built CONCURRENTLY by ``precis
backfill-embedding-kinds``, not by the migration); a small kind is searched
exactly over its own chunks. Both must return what brute force returns, and a
big kind whose index is missing or invalid stays on the global index.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import numpy as np
import psycopg
import pytest
from psycopg import Connection

from precis.cli.embedding_kind_backfill import (
    _GLOBAL_HNSW,
    _index_valid,
    _mark_filled_kinds,
    autovacuum_paused,
    backfill_embedding_kinds,
    build_kind_indexes,
    reindex_global,
)
from precis.store import Store, _chunks_ops
from precis.store._chunks_ops import (
    _ANN_PARTIAL_KINDS,
    _ann_kind_state,
    _ann_route,
    _reset_ann_state_cache,
    ann_partial_index_name,
    ann_route_plan,
)

_EMBEDDER = "bge-m3"
_DIM = 1024


def _unit(v: np.ndarray) -> list[float]:
    return (v / np.linalg.norm(v)).tolist()


def _chunk(store: Store, ref_id: int, text: str, ord_: int = 0) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, %s, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, ord_, text),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _embed(store: Store, chunk_id: int, vec: list[float]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, %s, %s, 'ok', 1)",
            (chunk_id, _EMBEDDER, vec),
        )


def _copy(store: Store, chunk_id: int) -> tuple[Any, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id, kind FROM chunk_embeddings WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    assert row is not None
    return row[0], row[1]


@pytest.fixture(autouse=True)
def _fresh_route_cache() -> Iterator[None]:
    _reset_ann_state_cache()
    yield
    _reset_ann_state_cache()


def _autocommit(store: Store) -> psycopg.Connection:
    return psycopg.connect(cast(str, store.pool.conninfo), autocommit=True)


@pytest.fixture
def kind_indexes(store: Store) -> Iterator[dict[str, bool]]:
    """The per-kind indexes, built the way prod builds them; dropped after."""
    with _autocommit(store) as conn:
        built = build_kind_indexes(conn)
    _reset_ann_state_cache()
    yield built
    with _autocommit(store) as conn:
        for kind in _ANN_PARTIAL_KINDS:
            conn.execute(f"DROP INDEX IF EXISTS {ann_partial_index_name(kind)}")
    _reset_ann_state_cache()


# --- the copy is the database's ------------------------------------------------


def test_plain_insert_gets_ref_id_and_kind_filled(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    cid = _chunk(store, ref.id, "memory body")
    _embed(store, cid, _unit(np.ones(_DIM)))
    assert _copy(store, cid) == (ref.id, "memory")


def test_mismatched_kind_fails_on_insert(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    cid = _chunk(store, ref.id, "memory body")
    with (
        store.pool.connection() as conn,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, kind) "
            "VALUES (%s, %s, %s, 'ok', 'paper')",
            (cid, _EMBEDDER, _unit(np.ones(_DIM))),
        )


def test_mismatched_ref_id_fails_on_insert(store: Store) -> None:
    a = store.insert_ref(kind="memory", slug=None, title="a", meta={})
    b = store.insert_ref(kind="memory", slug=None, title="b", meta={})
    cid = _chunk(store, a.id, "memory body")
    with (
        store.pool.connection() as conn,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, ref_id) "
            "VALUES (%s, %s, %s, 'ok', %s)",
            (cid, _EMBEDDER, _unit(np.ones(_DIM)), b.id),
        )


def test_ref_kind_update_cascades_to_the_copy(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    cid = _chunk(store, ref.id, "memory body")
    _embed(store, cid, _unit(np.ones(_DIM)))
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET kind = 'finding' WHERE ref_id = %s", (ref.id,))
    assert _copy(store, cid) == (ref.id, "finding")


def _legacy_nulls(conn: Connection, cids: list[int]) -> None:
    """Recreate prod's post-0226 shape on ``conn``: NULL legacy rows under a
    NOT VALID CHECK. The caller rolls back the whole transaction."""
    conn.execute(
        "ALTER TABLE chunk_embeddings DROP CONSTRAINT chunk_embeddings_ref_kind_filled"
    )
    conn.execute(
        "ALTER TABLE chunk_embeddings DISABLE TRIGGER chunk_embeddings_fill_ref_kind"
    )
    for cid in cids:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, %s, %s, 'ok')",
            (cid, _EMBEDDER, _unit(np.ones(_DIM))),
        )
    conn.execute(
        "ALTER TABLE chunk_embeddings ENABLE TRIGGER chunk_embeddings_fill_ref_kind"
    )
    conn.execute(
        "ALTER TABLE chunk_embeddings ADD CONSTRAINT chunk_embeddings_ref_kind_filled "
        "CHECK (ref_id IS NOT NULL AND kind IS NOT NULL) NOT VALID"
    )


def test_backfill_fills_legacy_rows_and_validates(store: Store) -> None:
    """Prod shape: NULL legacy rows under NOT VALID constraints. Rolled back."""
    ref = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    cids = [_chunk(store, ref.id, f"memory body {i}", i) for i in range(3)]
    with store.pool.connection() as conn:
        assert not conn.autocommit  # the DDL below must roll back
        _legacy_nulls(conn, cids)
        dry = backfill_embedding_kinds(conn, apply=False)
        assert dry.unfilled_before == 3 and dry.updated == 0
        res = backfill_embedding_kinds(conn, apply=True, window=1, sleep_s=0)
        assert res.updated == 3 and res.validated
        rows = conn.execute(
            "SELECT DISTINCT ref_id, kind FROM chunk_embeddings WHERE chunk_id = ANY(%s)",
            (cids,),
        ).fetchall()
        assert rows == [(ref.id, "memory")]
        valid = conn.execute(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = 'chunk_embeddings_ref_kind_filled'"
        ).fetchone()
        assert valid == (True,)
        conn.rollback()


def test_backfill_retries_a_cancelled_window_with_half_the_span(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A QueryCanceled/DeadlockDetected window retries at half span. Rolled back."""
    ref = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    cids = [_chunk(store, ref.id, f"memory body {i}", i) for i in range(3)]
    attempts: list[tuple[int, int]] = []
    real = Connection.execute
    failed = False

    def spy(self: Connection, query: Any, params: Any = None, **kw: Any) -> Any:
        nonlocal failed
        if isinstance(query, str) and "UPDATE chunk_embeddings ce" in query:
            attempts.append((int(params[0]), int(params[1])))
            if not failed:
                failed = True
                raise psycopg.errors.QueryCanceled(
                    "canceling statement due to statement timeout"
                )
        return real(self, query, params, **kw)

    monkeypatch.setattr(Connection, "execute", spy)
    with store.pool.connection() as conn:
        _legacy_nulls(conn, cids)
        res = backfill_embedding_kinds(conn, apply=True, window=6, sleep_s=0)
        conn.rollback()
    assert res.updated == 3 and res.validated
    # (0, 6] cancelled → span 3 → (0, 3], (3, 6] — the halved span stays for
    # the rest of the pass; the (empty) paper pass restarts at window 6.
    assert attempts == [(0, 6), (0, 3), (3, 6), (0, 6)]


def test_backfill_skips_the_sleep_after_an_empty_window(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 0.5 s pause is a write throttle — an empty window does not pause.
    Rolled back."""
    ref = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    cids = [_chunk(store, ref.id, f"memory body {i}", i) for i in range(3)]
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    with store.pool.connection() as conn:
        _legacy_nulls(conn, cids)
        # window=1 → one chunk_id per window: exactly the 3 windows holding a
        # legacy row update, every other window updates 0 and does not sleep.
        res = backfill_embedding_kinds(conn, apply=True, window=1, sleep_s=0.01)
        conn.rollback()
    assert res.updated == 3
    assert sleeps == [0.01] * 3


def test_backfill_until_non_paper_marks_kinds_and_leaves_paper(
    store: Store,
) -> None:
    """``until_pass='non-paper'`` fills the router's kinds and writes their
    ``chunk_embeddings_kind_ready`` markers; the deferred paper rows stay
    unfilled, so the CHECK is not validated. Rolled back."""
    mem = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    pap = store.insert_ref(kind="paper", slug="ks-p", title="p", meta={})
    mem_cids = [_chunk(store, mem.id, f"memory body {i}", i) for i in range(2)]
    pap_cids = [_chunk(store, pap.id, f"paper body {i}", i) for i in range(2)]
    with store.pool.connection() as conn:
        _legacy_nulls(conn, mem_cids + pap_cids)
        res = backfill_embedding_kinds(
            conn, apply=True, window=100, sleep_s=0, until_pass="non-paper"
        )
        assert res.updated == 2  # the memory rows; paper untouched
        assert res.unfilled_by_kind == {"paper": 2}
        assert res.marked == frozenset(_ANN_PARTIAL_KINDS)
        assert res.last_pass == "non-paper" and not res.validated
        marked = {
            str(r[0])
            for r in conn.execute(
                "SELECT kind FROM chunk_embeddings_kind_ready"
            ).fetchall()
        }
        assert set(_ANN_PARTIAL_KINDS) <= marked
        conn.rollback()


def test_backfill_leaves_a_straggler_kind_unmarked(store: Store) -> None:
    """A kind with rows the fill has not reached is NOT marked — here the
    memory rows are skipped entirely by a paper-only run. Rolled back."""
    mem = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    pap = store.insert_ref(kind="paper", slug="ks-p", title="p", meta={})
    mem_cids = [_chunk(store, mem.id, f"memory body {i}", i) for i in range(2)]
    pap_cids = [_chunk(store, pap.id, f"paper body {i}", i) for i in range(2)]
    with store.pool.connection() as conn:
        _legacy_nulls(conn, mem_cids + pap_cids)
        res = backfill_embedding_kinds(
            conn, apply=True, window=100, sleep_s=0, resume_pass="paper"
        )
        assert res.unfilled_by_kind == {"memory": 2}
        assert "memory" not in res.marked
        assert "job" in res.marked  # unfilled rows resolve to no other kind
        marked = {
            str(r[0])
            for r in conn.execute(
                "SELECT kind FROM chunk_embeddings_kind_ready"
            ).fetchall()
        }
        assert "memory" not in marked and "job" in marked
        conn.rollback()


def test_backfill_rejects_an_until_pass_before_the_resume_pass(
    store: Store,
) -> None:
    with store.pool.connection() as conn:
        with pytest.raises(ValueError, match="until_pass"):
            backfill_embedding_kinds(
                conn,
                apply=True,
                resume_pass="paper",
                until_pass="non-paper",
            )
        conn.rollback()


def test_mark_filled_kinds_on_an_empty_unfilled_set(store: Store) -> None:
    """Everything filled (small-DB shape): every partial kind marks."""
    with store.pool.connection() as conn:
        by_kind, marked = _mark_filled_kinds(conn)
        assert by_kind == {}
        assert marked == frozenset(_ANN_PARTIAL_KINDS)
        conn.rollback()


_ROOT = Path(__file__).resolve().parents[1]
#: The one audited re-kind site — fills the ref's unfilled
#: ``chunk_embeddings`` rows in the re-kind transaction (gr515083).
_KNOWN_REKIND = "scripts/_conventions_findings_to_memory.py"


_UPDATE_REFS = re.compile(r"UPDATE\s+(?:public\.)?\"?refs\"?\b", re.IGNORECASE)
_UPSERT_REFS = re.compile(
    r"INSERT\s+INTO\s+(?:public\.)?\"?refs\"?.{0,1200}?\bDO\s+UPDATE\s+SET\b",
    re.IGNORECASE | re.DOTALL,
)
#: ``kind =`` readings that are comparisons, not SET targets: a CASE
#: branch (``WHEN kind =``), a join predicate (``ON kind =`` /
#: ``ON x.kind =``), a filter in a subquery the clause scan can't see
#: around (``AND/OR/IN/IS/NOT/BETWEEN/LIKE kind``).
_CMP_WORDS = frozenset(
    {"WHEN", "ON", "AND", "OR", "THEN", "ELSE", "IN", "IS", "NOT", "BETWEEN", "LIKE"}
)


def _set_clause_assigns_kind(clause: str) -> bool:
    """A bare ``kind =`` column assignment in a SET clause — not
    ``x.kind =`` (``ON u.kind =``), not a call/docstring ``(kind=``,
    not a ``WHEN kind =`` comparison."""
    for m in re.finditer(r"(?<![\w.('\"`])kind\s*=", clause, re.IGNORECASE):
        before = re.search(r"(\w+)\s*$", clause[: m.start()])
        if before is None or before.group(1).upper() not in _CMP_WORDS:
            return True
    return False


def test_no_runtime_code_rekinds_refs_beyond_the_audited_site() -> None:
    """gr515083: an ``UPDATE refs`` (any schema/quote spelling) or an
    ``INSERT INTO refs … ON CONFLICT DO UPDATE`` assigning ``kind``
    re-homes the ref's unfilled ``chunk_embeddings`` rows under the NEW
    kind's ``chunk_embeddings_kind_ready`` marker — the (ref_id, kind)
    FK cascade cannot match a NULL key and the partial route filters on
    ``ce.kind``, so the marker goes stale silently. Any new re-kind site
    must fill the stragglers in the same transaction (the audited one
    does) or delete ``NEW.kind``'s marker — register it here after
    adding that handling."""
    offenders: set[str] = set()
    for root in (_ROOT / "src", _ROOT / "scripts"):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for pattern in (_UPDATE_REFS, _UPSERT_REFS):
                for m in pattern.finditer(text):
                    set_clause = re.split(
                        r"\bWHERE\b|\bRETURNING\b",
                        text[m.end() : m.end() + 800],
                        maxsplit=1,
                        flags=re.IGNORECASE,
                    )[0]
                    if _set_clause_assigns_kind(set_clause):
                        offenders.add(str(path.relative_to(_ROOT)))
    assert sorted(offenders) == [_KNOWN_REKIND]


def test_reindex_global_drops_an_invalid_ccnew_leftover(store: Store) -> None:
    """A failed REINDEX CONCURRENTLY leaves an INVALID *_ccnew index; the next
    run drops it before reindexing."""
    ccnew = f"{_GLOBAL_HNSW}_ccnew"
    with _autocommit(store) as conn:
        conn.execute(f"CREATE INDEX {ccnew} ON chunk_embeddings (status)")
        conn.execute(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = %s::regclass",
            (ccnew,),
        )
        assert _index_valid(conn, ccnew) is False
        assert reindex_global(conn) is True
        left = conn.execute(
            "SELECT count(*) FROM pg_class WHERE relname = %s", (ccnew,)
        ).fetchone()
        assert left == (0,)
        assert _index_valid(conn, _GLOBAL_HNSW) is True


def test_autovacuum_pause_vacuums_and_restores_reloptions_on_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The autovacuum window resets the reloption even when the body fails —
    and still runs the manual VACUUM ANALYZE first."""
    seen: list[str] = []
    real = Connection.execute

    def spy(self: Connection, query: Any, params: Any = None, **kw: Any) -> Any:
        if isinstance(query, str) and (
            "VACUUM" in query or "autovacuum_enabled" in query
        ):
            seen.append(query.split()[0])
        return real(self, query, params, **kw)

    monkeypatch.setattr(Connection, "execute", spy)
    reloptions = "SELECT reloptions FROM pg_class WHERE relname = 'chunk_embeddings'"
    with _autocommit(store) as conn:
        with pytest.raises(RuntimeError, match="window aborted"):
            with autovacuum_paused(conn):
                row = conn.execute(reloptions).fetchone()
                assert row is not None and "autovacuum_enabled=false" in (row[0] or [])
                raise RuntimeError("window aborted")
        row = conn.execute(reloptions).fetchone()
        # chunk_embeddings carries other reloptions; RESET removes only
        # autovacuum_enabled.
        assert row is not None and not any(
            "autovacuum_enabled" in o for o in (row[0] or [])
        )
        assert seen == ["ALTER", "VACUUM", "ALTER"]


# --- routing ---------------------------------------------------------------------


def test_build_kind_indexes_builds_every_kind_valid(
    kind_indexes: dict[str, bool],
) -> None:
    assert set(kind_indexes) == {"chunk_embeddings_ref_id_kind_idx"} | {
        ann_partial_index_name(k) for k in _ANN_PARTIAL_KINDS
    }
    assert all(kind_indexes.values())


def test_route_per_kind(store: Store, kind_indexes: dict[str, bool]) -> None:
    with store.pool.connection() as conn:
        state = _ann_kind_state(conn)
        assert state.filled
        assert set(_ANN_PARTIAL_KINDS) <= state.indexed
        assert "paper" not in state.indexed and "finding" not in state.indexed
        # The validated CHECK routes every indexed kind partial, markers or not.
        assert _ann_route(conn, "memory", None) == ("partial", "memory")
        assert _ann_route(conn, None, ["memory"]) == ("partial", "memory")
        assert _ann_route(conn, "finding", None) == ("exact", "finding")
        assert _ann_route(conn, "paper", None) == ("global", "paper")
        assert _ann_route(conn, None, ["memory", "finding"]) == ("global", None)
        assert _ann_route(conn, None, None) == ("global", None)


def test_route_refuses_a_missing_partial_index(store: Store) -> None:
    """Right after 0226 on prod: no per-kind index yet → global, not exact."""
    with store.pool.connection() as conn:
        assert _ann_route(conn, "memory", None) == ("global", "memory")
        assert _ann_route(conn, "finding", None) == ("exact", "finding")


def test_route_refuses_an_invalid_partial_index(
    store: Store, kind_indexes: dict[str, bool]
) -> None:
    """A failed CONCURRENTLY build leaves indisvalid = false. Rolled back."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = %s::regclass",
            (ann_partial_index_name("memory"),),
        )
        assert _ann_route(conn, "memory", None) == ("global", "memory")
        assert _ann_route(conn, "job", None) == ("partial", "job")
        assert "invalid or not ready" in ann_route_plan(conn, "memory", None)["reason"]
        conn.rollback()


def test_route_refuses_a_not_ready_partial_index(
    store: Store, kind_indexes: dict[str, bool]
) -> None:
    """A build still scanning is valid but not indisready → global.
    Rolled back."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pg_index SET indisready = false WHERE indexrelid = %s::regclass",
            (ann_partial_index_name("memory"),),
        )
        _reset_ann_state_cache()
        assert _ann_route(conn, "memory", None) == ("global", "memory")
        assert _ann_route(conn, "job", None) == ("partial", "job")
        conn.rollback()


def _unvalidate_check(conn: Connection) -> None:
    """Prod's current shape: the 0226 CHECK present but NOT VALID. The caller
    rolls back the transaction."""
    conn.execute(
        "ALTER TABLE chunk_embeddings DROP CONSTRAINT chunk_embeddings_ref_kind_filled"
    )
    conn.execute(
        "ALTER TABLE chunk_embeddings ADD CONSTRAINT chunk_embeddings_ref_kind_filled "
        "CHECK (ref_id IS NOT NULL AND kind IS NOT NULL) NOT VALID"
    )


def test_route_refuses_an_unvalidated_check(
    store: Store, kind_indexes: dict[str, bool]
) -> None:
    """Indexes valid but the kind's fill not marked and the CHECK NOT VALID
    → global. Rolled back."""
    with store.pool.connection() as conn:
        _unvalidate_check(conn)
        _reset_ann_state_cache()
        assert _ann_route(conn, "memory", None) == ("global", "memory")
        plan = ann_route_plan(conn, "memory", None)
        assert plan["route"] == "global"
        assert "backfill is incomplete" in plan["reason"]
        conn.rollback()


def test_route_uses_a_marked_kind_before_the_check_validates(
    store: Store, kind_indexes: dict[str, bool]
) -> None:
    """gr513663 option (a): a kind routes to its partial index once its own
    fill is marked in ``chunk_embeddings_kind_ready`` — the table-wide CHECK
    need not validate first. Rolled back."""
    with store.pool.connection() as conn:
        _unvalidate_check(conn)
        conn.execute("INSERT INTO chunk_embeddings_kind_ready (kind) VALUES ('memory')")
        _reset_ann_state_cache()
        assert _ann_route(conn, "memory", None) == ("partial", "memory")
        assert _ann_route(conn, "job", None) == ("global", "job")
        plan = ann_route_plan(conn, "memory", None)
        assert plan["route"] == "partial"
        assert plan["index"] == "chunk_embeddings_vec_hnsw_memory_idx"
        assert "kind fill-complete" in plan["reason"]
        conn.rollback()


def test_route_falls_back_when_the_search_role_cannot_read_markers(
    store: Store, kind_indexes: dict[str, bool]
) -> None:
    """The marker table's grants are provisioned out of tree: a search role
    without SELECT on it must keep the global route, not raise
    InsufficientPrivilege. Rolled back (the role is transactional DDL)."""
    with store.pool.connection() as conn:
        _unvalidate_check(conn)
        conn.execute("CREATE ROLE ann_no_marker_read")
        conn.execute("SET ROLE ann_no_marker_read")
        _reset_ann_state_cache()
        assert _ann_route(conn, "memory", None) == ("global", "memory")
        conn.execute("RESET ROLE")
        conn.rollback()


def test_route_missing_index_reason(store: Store) -> None:
    """No partial index yet (prod today): the plan names the gap, not just
    the fallback."""
    with store.pool.connection() as conn:
        plan = ann_route_plan(conn, "memory", None)
        assert plan["route"] == "global"
        assert plan["index"] == "chunk_embeddings_vec_hnsw_idx"
        assert "partial index is missing" in plan["reason"]


def test_plan_reason_all_kinds_filled(
    store: Store, kind_indexes: dict[str, bool]
) -> None:
    with store.pool.connection() as conn:
        plan = ann_route_plan(conn, "memory", None)
        assert plan["route"] == "partial"
        assert "all kinds filled" in plan["reason"]


def test_build_kind_indexes_rebuilds_an_invalid_index(
    store: Store, kind_indexes: dict[str, bool]
) -> None:
    name = ann_partial_index_name("memory")
    with _autocommit(store) as conn:
        conn.execute(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = %s::regclass",
            (name,),
        )
        assert build_kind_indexes(conn)[name] is True


def _seed(
    store: Store, kind: str, n_kind: int, n_paper: int
) -> tuple[list[float], dict[str, float]]:
    """Papers huddle around the query; ``kind`` rows sit further out."""
    rng = np.random.default_rng(11)
    base = rng.standard_normal(_DIM)
    base /= np.linalg.norm(base)

    def near(a: float) -> list[float]:
        noise = rng.standard_normal(_DIM)
        noise -= noise.dot(base) * base
        noise /= np.linalg.norm(noise)
        return _unit(base + a * noise)

    for i in range(n_paper):
        p = store.insert_ref(kind="paper", slug=f"ks-p{i}", title=f"Paper {i}", meta={})
        _embed(store, _chunk(store, p.id, f"paper body {i}"), near(0.05))
    truth: dict[str, float] = {}
    for i in range(n_kind):
        r = store.insert_ref(kind=kind, slug=None, title=f"{kind} {i}", meta={})
        vec = near(0.5 + 0.05 * i)
        _embed(store, _chunk(store, r.id, f"{kind} body {i}"), vec)
        truth[f"{kind} {i}"] = 1.0 - float(np.dot(np.array(vec), base))
    return base.tolist(), truth


@pytest.mark.parametrize("kind", ["memory", "finding"])
def test_kind_results_equal_brute_force(
    store: Store, kind_indexes: dict[str, bool], kind: str
) -> None:
    v, truth = _seed(store, kind, n_kind=12, n_paper=60)
    want = sorted(truth, key=truth.__getitem__)[:5]

    hits = store.chunks.search_chunks_semantic(query_vec=v, kind=kind, limit=5)
    assert [ref.title for _c, ref, _d in hits] == want
    for _c, ref, d in hits:
        assert d == pytest.approx(truth[ref.title], abs=1e-4)

    fused = store.chunks.search_chunks_fused(
        q="zzqx-no-lexical-hit", query_vec=v, kind=kind, limit=5
    )
    assert [ref.title for _c, ref, _s in fused] == want


@pytest.fixture
def record_ann_sql(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Capture every ANN statement the store executes."""
    seen: list[tuple[str, Any]] = []
    real = Connection.execute

    def spy(self: Connection, query: Any, params: Any = None, **kw: Any) -> Any:
        if isinstance(query, str) and "chunk_embeddings ce" in query and "<=>" in query:
            seen.append((query, params))
        return real(self, query, params, **kw)

    monkeypatch.setattr(Connection, "execute", spy)
    return seen


@contextmanager
def _no_seqscan(conn: Connection, *, no_sort: bool = False) -> Iterator[None]:
    conn.execute("SET LOCAL enable_seqscan = off")
    if no_sort:
        conn.execute("SET LOCAL enable_sort = off")
    yield


def _plan(store: Store, sql: str, params: Any, *, no_sort: bool = False) -> str:
    with store.pool.connection() as conn, _no_seqscan(conn, no_sort=no_sort):
        _chunks_ops._prepare_filtered_ann(conn, 20)
        return "\n".join(
            str(r[0]) for r in conn.execute("EXPLAIN " + sql, params).fetchall()
        )


def test_planner_uses_the_partial_index_for_a_big_kind(
    store: Store, kind_indexes: dict[str, bool], record_ann_sql: list[tuple[str, Any]]
) -> None:
    """The big-kind ANN statement CAN be served by its own partial index.

    This proves usability for the query shape: with seqscan and an explicit
    Sort forbidden, the ORDER BY distance has to come from
    ``chunk_embeddings_vec_hnsw_memory_idx`` itself, named below. It does not
    prove the planner *chooses* that index at prod scale; that needs real
    stats and belongs in a prod EXPLAIN smoke test.
    """
    v, _truth = _seed(store, "memory", n_kind=12, n_paper=60)
    store.chunks.search_chunks_semantic(query_vec=v, kind="memory", limit=5)
    sql, params = record_ann_sql[-1]
    # 72 rows: an exact scan plus an explicit Sort is genuinely cheaper than
    # any HNSW walk, and which side wins follows the table stats a neighbour
    # test left behind (gr496950), so the Sort is forbidden here.
    plan = _plan(store, sql, params, no_sort=True)
    assert "chunk_embeddings_vec_hnsw_memory_idx" in plan, re.sub(
        r"\[[^\]]*\]", "[..]", plan
    )


def test_small_kind_does_not_walk_any_hnsw_index(
    store: Store, record_ann_sql: list[tuple[str, Any]]
) -> None:
    v, _truth = _seed(store, "finding", n_kind=12, n_paper=60)
    store.chunks.search_chunks_semantic(query_vec=v, kind="finding", limit=5)
    sql, params = record_ann_sql[-1]
    plan = _plan(store, sql, params)
    assert "hnsw" not in plan, re.sub(r"\[[^\]]*\]", "[..]", plan)
