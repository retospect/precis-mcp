"""``gripe_parents`` — pipeline v2 slice 2 (``docs/backlog/
gripe-pipeline-v2.md`` §Intake): the 300 s Cadence that writes
``meta.dup_parents`` for every embedded gripe.

Covered: eligibility (embedded ``gripe_body`` + no ``dup_parents``
key + no live lease + not ``flood``), the non-terminal candidate
pre-filter, the cosine floor, one entry per candidate ref, the
``[]`` no-hits marker (key presence is the marker — a second fire
claims nothing), lease expiry re-claim, and the title-card
``potential duplicate parents:`` / ``duplicates: x N`` lines.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.store import ChunkInsert, Store
from precis.store.types import Tag
from precis.workers import gripe_parents as gp
from tests._refs import insert_gripe

pytestmark = pytest.mark.db

_E = MockEmbedder(dim=1024)

_DUP_TEXT = "the worker lease claim deadlocks under a racing claimant"
_FAR_TEXT = "anki sync credential rotation doubles the daily fire count"


def _embedded_gripe(
    store: Store,
    title: str,
    *,
    body: str,
    status: str = "open",
    meta: dict[str, Any] | None = None,
) -> int:
    """A gripe ref + STATUS tag + one embedded ``gripe_body`` chunk —
    what the pass's eligibility EXISTS clause needs."""
    ref = insert_gripe(store, title, status=status, meta=meta or {})
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text=body,
                meta={"chunk_kind": "gripe_body"},
                embedding=_E.embed_one(body),
            )
        ],
    )
    return int(ref.id)


def _parents(store: Store, ref_id: int) -> Any:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'dup_parents' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return row[0] if row else None


def _has_key(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta ? 'dup_parents' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return bool(row[0]) if row else False


def _claimed_at(store: Store, ref_id: int) -> Any:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'pipeline'->>'claimed_at' FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


class TestClaimAndWrite:
    def test_true_duplicate_pair(self, store: Store) -> None:
        """Identical body text → sim ~1.0, over the 0.80 floor: each
        gripe names the other as a potential parent with its status."""
        a = _embedded_gripe(store, "bug A", body=_DUP_TEXT)
        b = _embedded_gripe(store, "bug B", body=_DUP_TEXT)
        res = gp.run_gripe_parents_pass(store, 50)
        assert res.claimed == 2 and res.written == 2 and res.failed == 0
        pa, pb = _parents(store, a), _parents(store, b)
        assert [p["id"] for p in pa] == [b]
        assert [p["id"] for p in pb] == [a]
        assert pa[0]["sim"] >= 0.99 and pa[0]["status"] == "open"
        # The lease cleared on write-back.
        assert _claimed_at(store, a) is None

    def test_no_hits_writes_empty_marker(self, store: Store) -> None:
        """A lone gripe gets ``[]`` — key presence is the marker, so a
        second fire claims nothing."""
        gid = _embedded_gripe(store, "lone bug", body=_DUP_TEXT)
        res = gp.run_gripe_parents_pass(store, 50)
        assert res.claimed == 1 and res.no_hits == 1
        assert _parents(store, gid) == []
        res2 = gp.run_gripe_parents_pass(store, 50)
        assert res2.claimed == 0

    def test_floor_excludes_near_miss(self, store: Store) -> None:
        """Unrelated body text sits under the 0.80 similarity floor —
        written as ``[]``, not as a low-sim entry."""
        a = _embedded_gripe(store, "bug A", body=_DUP_TEXT)
        b = _embedded_gripe(store, "other", body=_FAR_TEXT)
        gp.run_gripe_parents_pass(store, 50)
        for gid, other in ((a, b), (b, a)):
            entries = _parents(store, gid)
            assert all(p["id"] != other for p in entries)

    def test_terminal_excluded_from_candidates_and_processed(
        self, store: Store
    ) -> None:
        """Closed gripes are never candidates — AND are never processed
        either (ruling: the pass is non-terminal only; no 5 h no-op
        backfill over ~2k terminal gripes)."""
        open_id = _embedded_gripe(store, "open", body=_DUP_TEXT)
        done_id = _embedded_gripe(store, "done", body=_DUP_TEXT, status="done")
        res = gp.run_gripe_parents_pass(store, 50)
        assert res.claimed == 1
        assert all(p["id"] != done_id for p in _parents(store, open_id))
        assert not _has_key(store, done_id)

    def test_verify_status_candidate_included(self, store: Store) -> None:
        """``verify`` is non-terminal (slice 1): a verify gripe IS a
        candidate parent."""
        open_id = _embedded_gripe(store, "open", body=_DUP_TEXT)
        v_id = _embedded_gripe(store, "verifying", body=_DUP_TEXT, status="verify")
        gp.run_gripe_parents_pass(store, 50)
        assert [p["id"] for p in _parents(store, open_id)] == [v_id]
        assert _parents(store, open_id)[0]["status"] == "verify"


class TestEligibility:
    def test_unembedded_gripe_not_claimed(self, store: Store) -> None:
        """No embedded ``gripe_body`` chunk → nothing to claim (the
        embed backlog hasn't reached it yet)."""
        ref = insert_gripe(store, "plain", status="open")
        res = gp.run_gripe_parents_pass(store, 50)
        assert res.claimed == 0
        assert not _has_key(store, int(ref.id))

    def test_keyed_gripe_not_reclaimed(self, store: Store) -> None:
        """``meta.dup_parents`` already set (even ``[]``) → the pass
        never re-scans it."""
        gid = _embedded_gripe(
            store, "done-pass", body=_DUP_TEXT, meta={"dup_parents": []}
        )
        res = gp.run_gripe_parents_pass(store, 50)
        assert res.claimed == 0

    def test_flood_tag_skipped(self, store: Store) -> None:
        """§Flood control: a ``flood``-tagged row skips the parents
        pass (the tick folds it into one keeper instead)."""
        gid = _embedded_gripe(store, "flood", body=_DUP_TEXT)
        store.add_tag(gid, Tag.flag("flood"), set_by="agent")
        res = gp.run_gripe_parents_pass(store, 50)
        assert res.claimed == 0

    def test_live_lease_blocks_reclaim(self, store: Store) -> None:
        gid = _embedded_gripe(
            store,
            "claimed",
            body=_DUP_TEXT,
            meta={"pipeline": {"claimed_at": "2999-01-01T00:00:00Z"}},
        )
        res = gp.run_gripe_parents_pass(store, 50)
        assert res.claimed == 0
        assert not _has_key(store, gid)

    def test_fresh_gripe_claim_writes_lease(self, store: Store) -> None:
        """The blocker fix: a gripe with NO ``meta.pipeline`` gets a
        real lease — ``jsonb_set`` doesn't create intermediate path
        objects, so the claim builds ``pipeline`` by hand. A second
        claim then loses (the lease blocks), which is what makes two
        racing claimants safe."""
        gid = _embedded_gripe(store, "fresh", body=_DUP_TEXT)
        assert gp._claim_batch(store, 50) == [gid]
        assert _claimed_at(store, gid) is not None
        # Second claimant loses while the lease is live.
        assert gp._claim_batch(store, 50) == []
        # …and the completed pass clears it (no ``"pipeline": {}`` left).
        # Drop the test-set lease so the pass can claim it again.
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta - 'pipeline' WHERE ref_id = %s",
                (gid,),
            )
        res = gp.run_gripe_parents_pass(store, 50)
        assert res.written == 1
        assert _claimed_at(store, gid) is None
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta ? 'pipeline' FROM refs WHERE ref_id = %s", (gid,)
            ).fetchone()
        assert row is not None and row[0] is False

    def test_gripe_comment_chunk_ignored(self, store: Store) -> None:
        """``gripe_body`` only: a comment chunk carrying the duplicate
        text never matches — and a gripe with ONLY an embedded comment
        is never claimed."""
        a = _embedded_gripe(store, "bug A", body=_DUP_TEXT)
        b = _embedded_gripe(store, "bug B", body=_FAR_TEXT)
        store.chunks.insert_chunks(
            b,
            [
                ChunkInsert(
                    ord=1,
                    text=_DUP_TEXT,
                    meta={"chunk_kind": "gripe_comment"},
                    embedding=_E.embed_one(_DUP_TEXT),
                )
            ],
        )
        res = gp.run_gripe_parents_pass(store, 50)
        assert res.claimed == 2  # a and b both have embedded bodies
        assert all(p["id"] != b for p in _parents(store, a))
        # Gripe with only a comment chunk embedded: not eligible.
        c_ref = insert_gripe(store, "comment-only", status="open")
        store.chunks.insert_chunks(
            c_ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text=_DUP_TEXT,
                    meta={"chunk_kind": "gripe_comment"},
                    embedding=_E.embed_one(_DUP_TEXT),
                )
            ],
        )
        assert gp._claim_batch(store, 50) == []

    def test_expired_lease_reclaimed(self, store: Store) -> None:
        """A crashed worker's ``claimed_at`` ages past the lease and
        the gripe is re-claimed."""
        gid = _embedded_gripe(
            store,
            "stale-claim",
            body=_DUP_TEXT,
            meta={"pipeline": {"claimed_at": "2020-01-01T00:00:00Z"}},
        )
        res = gp.run_gripe_parents_pass(store, 50)
        assert res.claimed == 1
        assert _has_key(store, gid)

    def test_supersedes_linked_excluded(self, store: Store) -> None:
        """A gripe already ``supersedes``-linked to the filing is not
        re-suggested as a parent."""
        a = _embedded_gripe(store, "keeper", body=_DUP_TEXT)
        b = _embedded_gripe(store, "dup", body=_DUP_TEXT)
        store.add_link(src_ref_id=b, dst_ref_id=a, relation="supersedes")
        gp.run_gripe_parents_pass(store, 50)
        # b is linked to a → a's candidates exclude b and vice versa.
        assert all(p["id"] != b for p in _parents(store, a))
        assert all(p["id"] != a for p in _parents(store, b))


class TestTitleCard:
    def _handler(self, store: Store) -> Any:
        from precis.dispatch import Hub
        from precis.handlers.gripe import GripeHandler

        return GripeHandler(hub=Hub(store=store))

    def test_card_shows_dup_parents_and_count(self, store: Store) -> None:
        a = _embedded_gripe(store, "keeper", body=_DUP_TEXT)
        b = _embedded_gripe(store, "dup", body=_DUP_TEXT)
        # Ruling: source supersedes target — the keeper's OUTBOUND links
        # count the parked duplicates.
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="supersedes")
        # Simulate a written parents list on the keeper.
        import json

        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (
                    json.dumps(
                        {
                            "dup_parents": [
                                {"id": b, "sim": 0.91, "status": "open"},
                                {"id": a, "sim": 0.84, "status": "triaged"},
                            ]
                        }
                    ),
                    a,
                ),
            )
        from precis.utils.handle_registry import format_handle

        card = self._handler(store).get(id=a).body
        assert "duplicates: x 1" in card
        assert (
            f"potential duplicate parents: {format_handle('gripe', b)}(0.91) "
            f"{format_handle('gripe', a)}(0.84)" in card
        )

    def test_card_skips_malformed_dup_entries(self, store: Store) -> None:
        """A hand-edited/garbage ``dup_parents`` entry never takes the
        card down — bad rows are skipped, good ones still render."""
        import json

        gid = _embedded_gripe(store, "keeper", body=_DUP_TEXT)
        other = _embedded_gripe(store, "dup", body=_DUP_TEXT)
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (
                    json.dumps(
                        {
                            "dup_parents": [
                                "garbage",
                                {"sim": "high"},
                                {"id": other, "sim": 0.9, "status": "open"},
                            ]
                        }
                    ),
                    gid,
                ),
            )
        from precis.utils.handle_registry import format_handle

        card = self._handler(store).get(id=gid).body
        assert (
            f"potential duplicate parents: {format_handle('gripe', other)}(0.90)"
            in card
        )

    def test_card_quiet_without_pipeline_data(self, store: Store) -> None:
        gid = _embedded_gripe(store, "plain", body=_DUP_TEXT)
        card = self._handler(store).get(id=gid).body
        assert "duplicate" not in card
