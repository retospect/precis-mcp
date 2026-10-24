"""Tests for the dream-consolidation sweep and the dream-cleanup script."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib import util as _ilu
from pathlib import Path

from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers.dream_consolidate import (
    SERVICE_NAME,
    plan_dream_consolidate,
    run_dream_consolidate_pass,
)


def _light(store: Store, prio: int = 5) -> None:
    """Write the ``service_config`` row that gates the sweep (the
    ``gripe_triage`` dark-gate shape)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('*', %s, %s) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = EXCLUDED.prio",
            (SERVICE_NAME, prio),
        )
        conn.commit()


def _dream(store: Store, title: str, *, thread: bool = False, days: int = 90) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title, meta={})
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text=f"{title} body " + "x " * 40,
                meta={"chunk_kind": "memory_body"},
            )
        ],
    )
    store.add_tag(
        ref.id, Tag.parse_strict("DREAM:speculative", kind="memory"), set_by="agent"
    )
    if thread:
        store.add_tag(ref.id, Tag.open("dream-thread"), set_by="agent")
    # Backdate all signal columns past the horizon.
    old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at=%s, updated_at=%s, "
            "last_viewed_at=NULL, last_recalled_at=NULL WHERE ref_id=%s",
            (old, old, ref.id),
        )
    return int(ref.id)


def test_plan_retires_stale_dreams_not_fresh_or_recalled(store: Store) -> None:
    stale = _dream(store, "old hunch", days=80)
    fresh = _dream(store, "new hunch", days=30)
    recalled = _dream(store, "read hunch", days=80)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET last_recalled_at=now() WHERE ref_id=%s", (recalled,)
        )
    clusters = plan_dream_consolidate(store)
    ids = {m.ref_id for c in clusters for m in c.members}
    assert stale in ids
    assert fresh not in ids
    assert recalled not in ids


def test_thread_dreams_get_the_90_day_horizon(store: Store) -> None:
    """A dream-thread at 70d survives; an ordinary dream at 70d retires."""
    ordinary = _dream(store, "plain dream", days=70)
    thread = _dream(store, "long thread", thread=True, days=70)
    # Legacy bare ``thread:`` (pre-gr504795) counts as a thread too.
    legacy = _dream(store, "legacy thread", days=70)
    store.add_tag(legacy, Tag.open("thread:"), set_by="agent")
    ids = {m.ref_id for c in plan_dream_consolidate(store) for m in c.members}
    assert ordinary in ids
    assert thread not in ids
    assert legacy not in ids


def test_pass_dark_without_the_flag(store: Store) -> None:
    """No service_config row → the sweep does nothing (off by default)."""
    stale = _dream(store, "old hunch", days=80)
    result = run_dream_consolidate_pass(store)
    assert (result.claimed, result.ok, result.failed) == (0, 0, 0)
    assert store.get_ref(kind="memory", id=stale) is not None
    with store.pool.connection() as conn:
        summaries = conn.execute(
            "SELECT ref_id FROM refs WHERE meta ? 'dream_consolidated'"
        ).fetchall()
    assert summaries == []


def test_lit_but_dry_run_reports_without_writing(store: Store) -> None:
    """Lit + dry_run still on (the default): plans, reports, writes nothing."""
    _light(store)
    stale = _dream(store, "old hunch", days=80)
    result = run_dream_consolidate_pass(store)
    assert result.claimed >= 1
    assert result.ok == 0 and result.failed == 0
    assert store.get_ref(kind="memory", id=stale) is not None


def test_pass_writes_one_summary_per_cluster_and_retires(store: Store) -> None:
    _light(store)
    a = _dream(store, "alpha dream", days=80)
    b = _dream(store, "beta dream", days=80)
    result = run_dream_consolidate_pass(store, dry_run=False)
    assert result.failed == 0
    assert result.ok == 2
    with store.pool.connection() as conn:
        retired = conn.execute(
            "SELECT ref_id, meta->>'superseded_by' FROM refs WHERE ref_id = ANY(%s)",
            ([a, b],),
        ).fetchall()
        summaries = conn.execute(
            "SELECT r.ref_id FROM refs r WHERE r.kind='memory' "
            "AND r.retired_at IS NULL AND r.meta ? 'dream_consolidated'"
        ).fetchall()
    assert all(int(r[1]) == summaries[0][0] for r in retired)
    assert len(summaries) == 1
    sid = int(summaries[0][0])
    assert any(str(t) == "DREAM:consolidated" for t in store.tags_for(sid))
    links = store.links_for(sid, direction="out", relation="supersedes")
    assert {getattr(l, "dst_ref_id", None) for l in links} == {a, b}


# ── cleanup script (selection + apply on the dev/test DB) ─────────


def _load_cleanup():
    spec = _ilu.spec_from_file_location(
        "_dream_cleanup", Path(__file__).parent.parent / "scripts" / "_dream_cleanup.py"
    )
    assert spec is not None and spec.loader is not None
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _digest(store: Store, body: str, *, tags: list[str] | None = None) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title="digest", meta={})
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})],
    )
    for t in ["digest:structural", *(tags or [])]:
        store.add_tag(ref.id, Tag.open(t), set_by="system")
    return int(ref.id)


def test_cleanup_dry_run_reports_then_apply_writes(
    store: Store, tmp_path: Path
) -> None:
    mod = _load_cleanup()
    marker = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    store.add_tag(marker.id, Tag.open("review-fail:structural"), set_by="system")
    digest = _digest(
        store, "Digest\n\n" + "ok " * 120, tags=["internal-thought", "user:asa"]
    )
    tailed = _digest(
        store, "Digest\n\n" + "ok " * 120 + "\nLet me check the rest now.\n"
    )

    dry = mod.run(store, apply=False)
    assert any("retire-fail-markers: 1" in line for line in dry)
    assert any("untag-digests: 1" in line for line in dry)
    assert any("trim-transcript-tails: 1" in line for line in dry)
    # Nothing written yet, and no manifest without --manifest on a dry run.
    assert store.get_ref(kind="memory", id=int(marker.id)) is not None
    assert any(str(t) == "internal-thought" for t in store.tags_for(digest))
    assert not (tmp_path / "m.json").exists()

    manifest = tmp_path / "m.json"
    out = mod.run(store, apply=True, manifest_path=manifest)
    assert any(str(manifest) in line for line in out)
    sel = json.loads(manifest.read_text(encoding="utf-8"))["actions"]
    assert sel["retire-fail-markers"]["ref_ids"] == [int(marker.id)]
    assert sel["untag-digests"]["ref_ids"] == [digest]
    assert [t["ref_id"] for t in sel["trim-transcript-tails"]] == [tailed]
    assert store.get_ref(kind="memory", id=int(marker.id)) is None
    tags = {str(t) for t in store.tags_for(digest)}
    assert "internal-thought" not in tags and "user:asa" not in tags
    assert "digest:structural" in tags
    body = [
        c.text
        for c in store.chunks.list_chunks_for_ref(tailed)
        if c.chunk_kind == "memory_body"
    ][0]
    assert "Let me check" not in body


def test_cleanup_correction_edges(store: Store, tmp_path: Path) -> None:
    mod = _load_cleanup()
    wrong = store.insert_ref(kind="memory", slug=None, title="wrong hunch", meta={})
    corr_body = (
        "Follow-up: me" + str(wrong.id) + " did not check out — the cited "
        "paper reports the opposite sign. " + "x " * 40
    )
    corr = store.insert_ref(kind="memory", slug=None, title="correction", meta={})
    store.chunks.insert_chunks(
        corr.id,
        [ChunkInsert(ord=0, text=corr_body, meta={"chunk_kind": "memory_body"})],
    )
    store.add_tag(
        corr.id, Tag.parse_strict("DREAM:speculative", kind="memory"), set_by="agent"
    )

    mod.run(store, apply=True, manifest_path=tmp_path / "m.json")
    links = store.links_for(int(corr.id), direction="out", relation="contradicts")
    assert any(getattr(l, "dst_ref_id", None) == int(wrong.id) for l in links)


def test_correction_edges_skip_non_adjacent_handles(
    store: Store, tmp_path: Path
) -> None:
    """Only the me<id> adjacent to the correction phrase is linked —
    other handles in the body (the right-answer cite) stay unlinked."""
    mod = _load_cleanup()
    wrong = store.insert_ref(kind="memory", slug=None, title="wrong hunch", meta={})
    right = store.insert_ref(kind="memory", slug=None, title="right hunch", meta={})
    corr_body = (
        f"Checked me{int(wrong.id)} again: it did not check out — the cited "
        "paper reports the opposite sign.\n\n"
        f"The mechanism that survives is me{int(right.id)}'s; see also "
        "the notes from last week."
    )
    corr = store.insert_ref(kind="memory", slug=None, title="correction", meta={})
    store.chunks.insert_chunks(
        corr.id,
        [ChunkInsert(ord=0, text=corr_body, meta={"chunk_kind": "memory_body"})],
    )
    store.add_tag(
        corr.id, Tag.parse_strict("DREAM:speculative", kind="memory"), set_by="agent"
    )

    mod.run(store, apply=True, manifest_path=tmp_path / "m.json")
    links = store.links_for(int(corr.id), direction="out", relation="contradicts")
    dsts = {getattr(l, "dst_ref_id", None) for l in links}
    assert dsts == {int(wrong.id)}


def test_trim_tail_bounds() -> None:
    mod = _load_cleanup()
    assert mod._trim_tail("short\nLet me go") is not None
    assert mod._trim_tail("Digest\n\n" + "ok " * 80) is None
