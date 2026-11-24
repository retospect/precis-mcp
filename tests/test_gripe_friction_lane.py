"""gr764123 (0368): the friction-gripe holding lane.

A friction filing — the ``PRECIS_FRICTION_REFLECT`` footer's channel:
a marked ro-stamped ``file_gripe_readonly`` call, and a write-capable
``put(tags=['friction'])`` — lands ``STATUS:held``, off every open
queue (``/open``, the ``status='open'`` live cohort) but searchable
(``status='held'``, ``get(id='/held')``). The ``gripe_triage`` tick's
``_friction_sweep`` groups held filings that name the same tool or
skill and say much the same thing — never on a shared theme tag alone
(gr735596) — releases a group to ``open`` once reports from 3 separate
runs land inside 30 days, and ages the rest out to ``wontfix``.
``friction_stats`` is the lane's one-query measure, surfaced as the
last line of ``get(id='/stats')``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.utils.friction_reflect import RO_FRICTION_PREFIX
from precis.workers import gripe_triage
from tests._refs import insert_gripe

pytestmark = pytest.mark.db


def _file_ro(store: Store, text: str, source: str) -> int:
    """The ro channel itself: ``file_gripe_readonly`` under a source."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT public.file_gripe_readonly(%s, %s)", (text, source)
        ).fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


def _seed_held(
    store: Store,
    text: str,
    *,
    run: str | None = None,
    age_days: float = 0.0,
) -> int:
    """Mint the held+filing shape the ro definer writes (STATUS:held +
    friction, optional ``agent:<run>``), with a gripe_body chunk."""
    ref = insert_gripe(store, text, status="held", meta={"footer_friction": True})
    store.add_tag(ref.id, Tag.open("friction"), set_by="agent")
    if run is not None:
        store.add_tag(ref.id, Tag.open(f"agent:{run}"), set_by="agent")
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=text, meta={"chunk_kind": "gripe_body"})],
    )
    if age_days:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = now() - make_interval(days => %s) "
                "WHERE ref_id = %s",
                (age_days, ref.id),
            )
            conn.commit()
    return int(ref.id)


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


def _status(store: Store, ref_id: int) -> str:
    return next(
        str(t).split(":", 1)[1] for t in _tags(store, ref_id) if "STATUS:" in str(t)
    )


def _sweep(store: Store) -> dict[str, int]:
    return gripe_triage._friction_sweep(store, now=datetime.now(UTC))


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = next(r for r in store.list_refs(kind="gripe", limit=200) if r.id == ref_id)
    return dict(ref.meta or {})


# ── filing ──────────────────────────────────────────────────────────


def test_ro_filing_lands_held_off_the_open_queue(hub: Hub, store: Store) -> None:
    """An ro friction filing is stamped STATUS:held by the definer —
    off ``/open`` and the ``status='open'`` cohort, reachable on the
    ``/held`` shelf and by ``status='held'``."""
    from precis.handlers.gripe import GripeHandler

    held = _file_ro(
        store, f"{RO_FRICTION_PREFIX}mcp__precis__search( dropped my query", "run-1"
    )
    h = GripeHandler(hub=hub)
    assert "STATUS:held" in _tags(store, held)

    assert f"gr{held}" not in h.get(id="/open").body
    assert f"gr{held}" in h.get(id="/held").body
    assert f"gr{held}" not in h.search(status="open").body
    assert f"gr{held}" in h.search(status="held").body
    assert f"gr{held}" in h.search(status="*").body


def test_ordinary_ro_filing_is_open_with_footer_off(
    hub: Hub, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.handlers.gripe import GripeHandler

    monkeypatch.setenv("PRECIS_FRICTION_REFLECT", "off")
    monkeypatch.setattr(GripeHandler, "_is_ro", lambda self: True)
    h = GripeHandler(hub=hub)
    resp = h.put(text="the search page 500s on a percent sign")
    m = re.search(r"id=(\d+)", resp.body)
    assert m is not None
    assert _status(store, int(m.group(1))) == "open"
    assert "STATUS:held" not in resp.body
    assert gripe_triage.friction_stats(store)["filed"] == 0


def test_write_friction_filing_lands_held(hub: Hub, store: Store) -> None:
    """A write-capable ``friction``-tagged filing is held too — and the
    hold is written last, so a caller-passed STATUS: cannot undo it."""
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    resp = h.put(
        text="friction: mcp__precis__get( returns stale chunks",
        tags=["friction", "STATUS:triaged"],
        new=True,
    )
    m = re.search(r"id=(\d+)", resp.body)
    assert m is not None
    rid = int(m.group(1))
    assert _status(store, rid) == "held"
    assert "STATUS:held" in resp.body


def test_ordinary_filing_still_lands_open(hub: Hub, store: Store) -> None:
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    resp = h.put(text="the search page 500s on a percent sign", new=True)
    m = re.search(r"id=(\d+)", resp.body)
    assert m is not None
    assert _status(store, int(m.group(1))) == "open"


# ── grouping ────────────────────────────────────────────────────────


def test_sweep_seeds_a_group(store: Store) -> None:
    rid = _seed_held(store, "mcp__precis__search( lost my filter", run="run-1")
    out = _sweep(store)
    assert out["friction_seeded"] == 1
    assert "friction-group" in _tags(store, rid)
    fr = _meta(store, rid)["friction"]
    assert fr["count"] == 1 and fr["runs"] == ["agent:run-1"]
    assert "verb:search" in fr["tools"]
    assert _status(store, rid) == "held"


def test_matching_filings_fold_into_one_group(store: Store) -> None:
    seed = _seed_held(
        store,
        "mcp__precis__search( dropped my filter silently on the last query",
        run="run-1",
    )
    member = _seed_held(
        store,
        "mcp__precis__search( dropped my filter silently again today",
        run="run-2",
    )
    out = _sweep(store)
    assert out["friction_seeded"] == 1 and out["friction_folded"] == 1
    assert f"cluster:friction-gr{seed}" in _tags(store, member)
    fr = _meta(store, seed)["friction"]
    assert fr["count"] == 2
    assert sorted(fr["runs"]) == ["agent:run-1", "agent:run-2"]


def test_shared_theme_alone_never_merges(store: Store) -> None:
    """gr735596: two reports sharing vocabulary but naming no common
    tool/skill seed two groups — the dedup is tool-anchored."""
    a = _seed_held(
        store,
        "the gripe workflow felt slow and the queue kept hiding filings",
        run="run-1",
    )
    b = _seed_held(
        store,
        "the gripe workflow felt slow again, queue kept hiding filings",
        run="run-2",
    )
    out = _sweep(store)
    assert out["friction_seeded"] == 2 and out["friction_folded"] == 0
    assert "friction-group" in _tags(store, a)
    assert "friction-group" in _tags(store, b)


def test_different_tool_never_merges(store: Store) -> None:
    a = _seed_held(store, "mcp__precis__get( returns stale chunks", run="run-1")
    b = _seed_held(store, "mcp__precis__put( refused my tags outright", run="run-2")
    out = _sweep(store)
    assert out["friction_seeded"] == 2 and out["friction_folded"] == 0
    assert f"cluster:friction-gr{a}" not in _tags(store, b)


# ── release ─────────────────────────────────────────────────────────


def test_third_separate_run_releases_the_group(store: Store) -> None:
    seed = _seed_held(store, "mcp__precis__edit( lost my meta patch", run="run-1")
    _seed_held(store, "mcp__precis__edit( lost my meta patch again", run="run-2")
    third = _seed_held(
        store, "mcp__precis__edit( lost my meta patch a third time", run="run-3"
    )
    out = _sweep(store)
    assert out["friction_released"] == 1
    assert _status(store, seed) == "open"
    fr = _meta(store, seed)["friction"]
    assert fr["count"] == 3 and len(fr["runs"]) == 3
    assert fr["outcome"] == "released"
    # Members stay held — the group's searchable evidence trail.
    assert _status(store, third) == "held"
    assert f"cluster:friction-gr{seed}" in _tags(store, third)


def test_released_keeper_absorbs_later_matches(store: Store) -> None:
    body = "mcp__precis__edit( lost my meta patch"
    seed = _seed_held(store, body, run="run-1")
    for run in ("run-2", "run-3", "run-4"):
        _seed_held(store, body, run=run)
    out = _sweep(store)
    assert out["friction_released"] == 1
    assert out["friction_folded"] == 3
    released_at = _meta(store, seed)["friction"]["outcome_at"]

    store.add_tag(
        seed, Tag.closed("STATUS", "triaged"), set_by="system", replace_prefix=True
    )
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '31 days' WHERE ref_id = %s",
            (seed,),
        )
    report = _seed_held(store, body, run="run-5")
    out = _sweep(store)
    assert out == {
        "friction_folded": 1,
        "friction_seeded": 0,
        "friction_released": 0,
        "friction_aged_out": 0,
    }
    assert f"cluster:friction-gr{seed}" in _tags(store, report)
    assert _status(store, seed) == "triaged"
    fr = _meta(store, seed)["friction"]
    assert fr["count"] == 5
    assert fr["outcome"] == "released" and fr["outcome_at"] == released_at
    assert gripe_triage.friction_stats(store) == {
        "filed": 5,
        "released": 1,
        "aged_out": 0,
    }

    store.add_tag(
        seed, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    report = _seed_held(store, body, run="run-6")
    assert _sweep(store)["friction_seeded"] == 1
    assert "friction-group" in _tags(store, report)


def test_same_run_twice_counts_as_one_run(store: Store) -> None:
    """The threshold counts separate runs — one run reporting twice is
    still one run's noise."""
    seed = _seed_held(store, "mcp__precis__tag( dropped an open tag", run="run-1")
    _seed_held(store, "mcp__precis__tag( dropped an open tag twice", run="run-1")
    _sweep(store)
    fr = _meta(store, seed)["friction"]
    assert fr["count"] == 2 and fr["runs"] == ["agent:run-1"]
    assert _status(store, seed) == "held"


def test_released_group_is_claimable_by_ordinary_triage(store: Store) -> None:
    """A released keeper sits at STATUS:open — the ordinary claim picks
    it up (``dup_parents`` marks it seen by the parents pass)."""
    seed = _seed_held(
        store,
        "mcp__precis__link( refused my relation outright",
        run="run-1",
    )
    _seed_held(store, "mcp__precis__link( refused my relation flat", run="run-2")
    _seed_held(store, "mcp__precis__link( refused my relation cold", run="run-3")
    _sweep(store)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = coalesce(meta, '{}'::jsonb) "
            "|| '{\"dup_parents\": []}'::jsonb WHERE ref_id = %s",
            (seed,),
        )
        conn.commit()
    claimed = [i for i, _ in gripe_triage._claim_batch(store, 40)]
    assert seed in claimed


# ── age-out ─────────────────────────────────────────────────────────


def test_group_ages_out_to_wontfix(store: Store) -> None:
    """A group that never draws 3 runs inside 30 days closes quietly —
    keeper and folded members alike."""
    seed = _seed_held(
        store, "mcp__precis__delete( hid my confirmation", run="run-1", age_days=31
    )
    member = _seed_held(
        store,
        "mcp__precis__delete( hid my confirmation again",
        run="run-2",
        age_days=10,
    )
    # Fold first while the seed still qualifies? No — age-out precedes
    # folding: the stale group closes and the member seeds fresh.
    out = _sweep(store)
    assert out["friction_aged_out"] == 1
    assert _status(store, seed) == "wontfix"
    fr = _meta(store, seed)["friction"]
    assert fr["outcome"] == "aged_out"
    # The young member seeds its own fresh group rather than inheriting
    # the dead one.
    assert _status(store, member) == "held"
    assert "friction-group" in _tags(store, member)


def test_members_age_out_with_their_keeper(store: Store) -> None:
    seed = _seed_held(store, "mcp__precis__search( timed out hard", run="run-1")
    member = _seed_held(store, "mcp__precis__search( timed out hard again", run="run-2")
    _sweep(store)
    assert f"cluster:friction-gr{seed}" in _tags(store, member)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '31 days' WHERE ref_id = %s",
            (seed,),
        )
        conn.commit()
    out = _sweep(store)
    assert out["friction_aged_out"] == 1
    assert _status(store, seed) == "wontfix"
    assert _status(store, member) == "wontfix"


# ── measure ─────────────────────────────────────────────────────────


def test_friction_stats_counts_filed_released_aged_out(store: Store) -> None:
    """The lane's one-query measure (gr764123 §Measure): reports filed,
    groups released, groups aged out — junk rate = aged out ÷ filed."""
    rel_seed = _seed_held(store, "mcp__precis__get( lost the body", run="run-1")
    _seed_held(store, "mcp__precis__get( lost the body again", run="run-2")
    _seed_held(store, "mcp__precis__get( lost the body once more", run="run-3")
    _seed_held(store, "mcp__precis__put( ate my argument", run="run-4", age_days=45)
    stats = gripe_triage.friction_stats(store)
    assert stats == {"filed": 4, "released": 0, "aged_out": 0}
    _sweep(store)
    stats = gripe_triage.friction_stats(store)
    assert stats == {"filed": 4, "released": 1, "aged_out": 1}
    assert _status(store, rel_seed) == "open"


def test_stats_view_surfaces_the_measure(hub: Hub, store: Store) -> None:
    """The spec's "one query" is ``get(id='/stats')``: its last line
    reads the lane's filed / released / aged out plus the junk rate."""
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    # Nothing filed yet reads as n/a, not a misleading 0%.
    assert (
        "0 filed · 0 released · 0 aged out · junk rate n/a" in h.get(id="/stats").body
    )
    _seed_held(store, "mcp__precis__get( lost the body", run="run-1")
    _seed_held(store, "mcp__precis__get( lost the body again", run="run-2")
    _seed_held(store, "mcp__precis__get( lost the body once more", run="run-3")
    _seed_held(store, "mcp__precis__put( ate my argument", run="run-4", age_days=45)
    _sweep(store)
    body = h.get(id="/stats").body
    assert "4 filed · 1 released · 1 aged out · junk rate 25%" in body


def test_sweep_is_idempotent(store: Store) -> None:
    """A second sweep over the same state is a no-op — the group and
    member markers make every filing's disposition durable."""
    _seed_held(store, "mcp__precis__search( ate my under arg", run="run-1")
    _seed_held(store, "mcp__precis__search( ate my under arg again", run="run-2")
    _sweep(store)
    out = _sweep(store)
    assert out == {
        "friction_folded": 0,
        "friction_seeded": 0,
        "friction_released": 0,
        "friction_aged_out": 0,
    }
