"""Salience primitives for dreaming target selection.

Deterministic, in-process core (docs/design/dreaming.md, §Target
selection): a chunk's score is ``last_seen - last_dreamt`` and the seed
is the argmax over ``paper`` + ``memory``. These tests pin the four
behaviours the design calls out:

- ``bump_salience`` advances ``last_seen`` + ``accesses`` for a page;
- dream-actor reads are excluded (feedback guard);
- ``touch_last_dreamt`` resets the rotation;
- ``select_dream_seed`` picks the most-due chunk and rotates.
"""

from __future__ import annotations

import pytest

from precis.store import Store, as_background_actor, as_dream_actor


def _mk_chunk(store: Store, ref_id: int, ord_: int, text: str) -> int:
    """Insert a body chunk and return its chunk_id."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, %s, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, ord_, text),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _salience(store: Store, chunk_id: int) -> tuple:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT last_seen, last_dreamt, accesses FROM chunks WHERE chunk_id = %s",
            (chunk_id,),
        ).fetchone()
    assert row is not None
    return row


def test_bump_advances_last_seen_and_accesses(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="p1", title="P1", meta={})
    cid = _mk_chunk(store, ref.id, 0, "body")
    seen0, _, acc0 = _salience(store, cid)
    n = store.bump_salience([cid])
    assert n == 1
    seen1, _, acc1 = _salience(store, cid)
    assert seen1 > seen0
    assert acc1 == acc0 + 1


def test_bump_empty_is_noop(store: Store) -> None:
    assert store.bump_salience([]) == 0


def test_dream_actor_reads_do_not_bump(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="p2", title="P2", meta={})
    cid = _mk_chunk(store, ref.id, 0, "body")
    _, _, acc0 = _salience(store, cid)
    with as_dream_actor():
        assert store.bump_salience([cid]) == 0
    _, _, acc1 = _salience(store, cid)
    assert acc1 == acc0  # suppressed: the dreamer must not heat itself


def test_touch_last_dreamt_resets_rotation(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    cid = store.upsert_card_combined(ref.id, "m")
    # bump so last_seen > last_dreamt (score > 0)
    store.bump_salience([cid])
    seen, dreamt, _ = _salience(store, cid)
    assert seen > dreamt
    store.touch_last_dreamt([cid])
    seen2, dreamt2, _ = _salience(store, cid)
    assert dreamt2 >= seen2  # rotated out: score back to <= 0


def test_select_dream_seed_picks_argmax_and_rotates(store: Store) -> None:
    pa = store.insert_ref(kind="paper", slug="pa", title="A", meta={})
    pb = store.insert_ref(kind="paper", slug="pb", title="B", meta={})
    ca = _mk_chunk(store, pa.id, 0, "a")
    cb = _mk_chunk(store, pb.id, 0, "b")
    # ca is accessed → highest last_seen - last_dreamt → the seed
    store.bump_salience([ca])
    assert store.select_dream_seed() == ca
    # surfacing ca stamps last_dreamt → cb (untouched, score 0) now tops
    store.touch_last_dreamt([ca])
    assert store.select_dream_seed() == cb


def test_select_dream_seed_excludes_non_target_kinds(store: Store) -> None:
    todo = store.insert_ref(kind="todo", slug=None, title="t", meta={})
    ct = _mk_chunk(store, todo.id, 0, "t")
    store.bump_salience([ct])  # hot, but a todo is never a dream target
    assert store.select_dream_seed() != ct


def test_select_dream_seed_empty_corpus(store: Store) -> None:
    assert store.select_dream_seed() is None


def test_select_dream_seed_skips_deleted_refs(store: Store) -> None:
    pa = store.insert_ref(kind="paper", slug="pd", title="D", meta={})
    ca = _mk_chunk(store, pa.id, 0, "a")
    store.bump_salience([ca])
    store.soft_delete_ref(pa.id)
    assert store.select_dream_seed() is None


@pytest.mark.parametrize("kinds", [("paper",), ("memory",)])
def test_select_dream_seed_respects_kind_filter(
    store: Store, kinds: tuple[str, ...]
) -> None:
    pa = store.insert_ref(kind="paper", slug="pf", title="F", meta={})
    cp = _mk_chunk(store, pa.id, 0, "p")
    mem = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    cm = store.upsert_card_combined(mem.id, "m")
    store.bump_salience([cp, cm])
    seed = store.select_dream_seed(kinds=kinds)
    assert seed == (cp if kinds == ("paper",) else cm)


# ── actor-parameterized salience (watching reuses the dream field) ──


def test_background_actor_suppresses_bump(store: Store) -> None:
    """Any background actor (not just dream) must suppress self-heat."""
    ref = store.insert_ref(kind="paper", slug="pw", title="W", meta={})
    cid = _mk_chunk(store, ref.id, 0, "body")
    _, _, acc0 = _salience(store, cid)
    with as_background_actor("watch"):
        assert store.bump_salience([cid]) == 0
    _, _, acc1 = _salience(store, cid)
    assert acc1 == acc0


def test_select_salient_watch_argmax_and_limit(store: Store) -> None:
    pa = store.insert_ref(kind="paper", slug="wa", title="A", meta={})
    pb = store.insert_ref(kind="paper", slug="wb", title="B", meta={})
    ca = _mk_chunk(store, pa.id, 0, "a")
    cb = _mk_chunk(store, pb.id, 0, "b")
    store.bump_salience([ca])  # ca hottest
    assert store.select_salient("watch", kinds=("paper",), limit=1) == [ca]
    # limit returns top-N most-due, ca first
    top2 = store.select_salient("watch", kinds=("paper",), limit=2)
    assert top2[0] == ca and set(top2) == {ca, cb}


def test_dream_and_watch_rotate_independently(store: Store) -> None:
    """The DRY+correctness property: the two actors share the heat field
    (last_seen) but rotate on independent stamps. Dreaming a paper must
    NOT cool it for the watcher, and vice versa. (Selection is argmax, so
    we prove it via which of two chunks tops, not via emptiness.)"""
    pa = store.insert_ref(kind="paper", slug="wi", title="I", meta={})
    pb = store.insert_ref(kind="paper", slug="wj", title="J", meta={})
    ca = _mk_chunk(store, pa.id, 0, "a")
    cb = _mk_chunk(store, pb.id, 0, "b")
    store.bump_salience([ca])  # ca hottest for BOTH actors
    assert store.select_salient("dream", kinds=("paper",))[0] == ca
    assert store.select_salient("watch", kinds=("paper",))[0] == ca
    # Dreamer rotates ca out → cb tops for dream...
    store.touch_attended("dream", [ca])
    assert store.select_salient("dream", kinds=("paper",))[0] == cb
    # ...but the watcher still sees ca as most-due (independent stamp).
    assert store.select_salient("watch", kinds=("paper",))[0] == ca
    # Watcher rotates ca out on its own clock → cb tops for watch too.
    store.touch_attended("watch", [ca])
    assert store.select_salient("watch", kinds=("paper",))[0] == cb


def test_touch_attended_unknown_actor_raises(store: Store) -> None:
    with pytest.raises(KeyError):
        store.touch_attended("bogus", [1])
    with pytest.raises(KeyError):
        store.select_salient("bogus", kinds=("paper",))


def test_touch_last_dreamt_still_works_via_wrapper(store: Store) -> None:
    """Back-compat: the dream wrapper rotates the dream column only."""
    pa = store.insert_ref(kind="paper", slug="wc", title="C", meta={})
    pb = store.insert_ref(kind="paper", slug="wd", title="D", meta={})
    ca = _mk_chunk(store, pa.id, 0, "a")
    cb = _mk_chunk(store, pb.id, 0, "b")
    store.bump_salience([ca])
    store.touch_last_dreamt([ca])
    assert store.select_dream_seed(kinds=("paper",)) == cb  # dream rotated to cb
    assert store.select_salient("watch", kinds=("paper",))[0] == ca  # watch untouched


# ── draft over-weighting in the dream seed ─────────────────────────


def _set_salience_secs(
    store: Store, chunk_id: int, *, dreamt_secs_ago: float
) -> None:
    """Pin last_seen=now and last_dreamt=now-Δ so the due-ness score
    (last_seen - last_dreamt) is exactly Δ seconds — deterministic."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET last_seen = now(), "
            "last_dreamt = now() - make_interval(secs => %s) WHERE chunk_id = %s",
            (float(dreamt_secs_ago), chunk_id),
        )


_DREAM_KINDS = ("paper", "memory", "draft")
_DAY = 86_400.0


def test_dream_seed_overweights_draft_within_boost(store: Store, monkeypatch) -> None:
    """A draft only mildly more recently dreamt than a due paper still wins,
    because the boost tips it over."""
    monkeypatch.setenv("PRECIS_DREAM_DRAFT_BOOST_DAYS", "2")
    pa = store.insert_ref(kind="paper", slug="ow-pa", title="A", meta={})
    da = store.insert_ref(kind="draft", slug="ow-da", title="D", meta={})
    cp = _mk_chunk(store, pa.id, 0, "paper body")
    cd = _mk_chunk(store, da.id, 0, "draft body")
    _set_salience_secs(store, cp, dreamt_secs_ago=1 * _DAY)  # score 1d
    _set_salience_secs(store, cd, dreamt_secs_ago=0)  # score 0 → +2d boost
    assert store.select_dream_seed(kinds=_DREAM_KINDS) == cd


def test_dream_seed_paper_wins_when_far_overdue(store: Store, monkeypatch) -> None:
    """The boost is 'kinda', not a takeover: a much-more-overdue paper out-
    scores a freshly-dreamt draft."""
    monkeypatch.setenv("PRECIS_DREAM_DRAFT_BOOST_DAYS", "2")
    pa = store.insert_ref(kind="paper", slug="ow-pb", title="A", meta={})
    da = store.insert_ref(kind="draft", slug="ow-db", title="D", meta={})
    cp = _mk_chunk(store, pa.id, 0, "paper body")
    cd = _mk_chunk(store, da.id, 0, "draft body")
    _set_salience_secs(store, cp, dreamt_secs_ago=5 * _DAY)  # score 5d
    _set_salience_secs(store, cd, dreamt_secs_ago=0)  # score 0 → +2d boost = 2d
    assert store.select_dream_seed(kinds=_DREAM_KINDS) == cp


def test_dream_seed_boost_disabled(store: Store, monkeypatch) -> None:
    """PRECIS_DREAM_DRAFT_BOOST_DAYS=0 → pure argmax, no draft tilt."""
    monkeypatch.setenv("PRECIS_DREAM_DRAFT_BOOST_DAYS", "0")
    pa = store.insert_ref(kind="paper", slug="ow-pc", title="A", meta={})
    da = store.insert_ref(kind="draft", slug="ow-dc", title="D", meta={})
    cp = _mk_chunk(store, pa.id, 0, "paper body")
    cd = _mk_chunk(store, da.id, 0, "draft body")
    _set_salience_secs(store, cp, dreamt_secs_ago=1 * _DAY)  # score 1d
    _set_salience_secs(store, cd, dreamt_secs_ago=0)  # score 0, no boost
    assert store.select_dream_seed(kinds=_DREAM_KINDS) == cp
