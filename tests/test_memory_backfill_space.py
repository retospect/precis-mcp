"""``precis memory backfill-space`` (gr548275): infer ``SPACE:`` on untagged memories.

Fixture memories carry each writer signature the classifier reads —
present tags, ``refs``-row meta / ``set_by``, ``ref_events`` sources,
link endpoints — and land in the expected bucket; the residue keeps no
SPACE tag but carries ``space:unknown``; a re-run is a no-op.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.cli import _build_parser
from precis.cli.memory import UNKNOWN_SPACE_TAG, backfill_space
from precis.cli.memory import run as memory_run
from precis.store import Store
from precis.store.types import ChunkInsert, Tag


def _memory(
    store: Store,
    *,
    title: str = "note",
    tags: tuple[str, ...] = (),
    meta: dict[str, Any] | None = None,
    set_by: str | None = None,
) -> int:
    """Insert a bare ``kind='memory'`` ref + body chunk; return its id."""
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory", slug=None, title=title, meta=meta or {}, conn=conn
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text="body", meta={"chunk_kind": "memory_body"})],
            conn=conn,
        )
        for t in tags:
            store.add_tag(
                ref.id,
                Tag.parse_strict(t, kind="memory"),
                set_by="system",
                conn=conn,
            )
        if set_by is not None:
            conn.execute(
                "UPDATE refs SET set_by = %s WHERE ref_id = %s", (set_by, ref.id)
            )
    return int(ref.id)


def _event(
    store: Store, ref_id: int, source: str, event: str = "body_replaced"
) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload) "
            "VALUES (%s, %s, %s, '{}'::jsonb)",
            (ref_id, source, event),
        )


def _link(store: Store, src: int, dst: int) -> None:
    store.add_link(src_ref_id=src, dst_ref_id=dst, relation="related-to")


def _decision(store: Store, ref_id: int) -> tuple[str, str]:
    rep = backfill_space(store, dry_run=True)
    for rid, space, rule in rep.decisions:
        if rid == ref_id:
            return space, rule
    raise AssertionError(f"{ref_id} not in plan: {rep.decisions}")


def _space_tag(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if t.namespace == "closed" and t.prefix == "SPACE":
            return t.value
    return None


@pytest.mark.parametrize(
    ("tags", "want"),
    [
        (("tier:synthetic-insight",), "research"),
        (("DREAM:speculative",), "research"),
        (("kind:lemma",), "research"),
        (("kind:inference",), "research"),
        (("dream-thread",), "research"),
        (("section:threads",), "repo-dev"),
        (("mirror:harness",), "repo-dev"),
        (("gotchas:none",), "repo-dev"),
    ],
)
def test_rule_a_tags(store: Store, tags: tuple[str, ...], want: str) -> None:
    rid = _memory(store, tags=tags)
    space, rule = _decision(store, rid)
    assert (space, rule) == (want, "a:tags")


@pytest.mark.parametrize(
    ("meta", "want", "rule"),
    [
        ({"file_mirror": {"namespace": "harness"}}, "repo-dev", "b:file-mirror"),
        ({"slug": "an-old-node"}, "repo-dev", "b:import-keys"),
        ({"section": "gotchas"}, "repo-dev", "b:import-keys"),
        ({"source": "asa"}, "research", "b:meta-source"),
        ({"source": "gripe-fix"}, "repo-dev", "b:meta-source"),
        ({"citer_watch_date": "2026-11-01"}, "research", "b:system-digest"),
        ({"deep_review_date": "2026-11-01"}, "research", "b:system-digest"),
        ({"dream_consolidated": [1, 2]}, "research", "b:system-digest"),
        ({"hook": "one-liner"}, "repo-dev", "b:hook"),
    ],
)
def test_rule_b_meta(store: Store, meta: dict[str, Any], want: str, rule: str) -> None:
    rid = _memory(store, meta=meta)
    assert _decision(store, rid) == (want, rule)


def test_rule_b_set_by(store: Store) -> None:
    assert _decision(store, _memory(store, set_by="dream")) == ("research", "b:set-by")
    assert _decision(store, _memory(store, set_by="system")) == ("research", "b:set-by")


def test_rule_b_events_fallback(store: Store) -> None:
    rid = _memory(store)
    _event(store, rid, "memory-mirror")
    assert _decision(store, rid) == ("repo-dev", "b:events")
    rid = _memory(store)
    _event(store, rid, "agent")  # unmapped source: no signal
    assert _decision(store, rid) == ("unknown", "d:none")
    rid = _memory(store)
    _event(store, rid, "asa")
    assert _decision(store, rid) == ("research", "b:events")


def test_rule_c_links(store: Store) -> None:
    paper = int(store.insert_ref(kind="paper", slug="backfill-p", title="paper").id)
    rid = _memory(store)
    _link(store, rid, paper)
    assert _decision(store, rid) == ("research", "c:links")

    with store.tx() as conn:
        gripe = int(store.insert_ref(kind="gripe", slug=None, title="g", conn=conn).id)
        # the gripe_status_check trigger demands exactly one STATUS tag
        store.add_tag(gripe, Tag.parse_strict("STATUS:open", kind="gripe"), conn=conn)
    rid = _memory(store)
    _link(store, gripe, rid)  # inbound counts too
    assert _decision(store, rid) == ("repo-dev", "c:links")

    rid_td = _memory(store)
    todo = int(store.insert_ref(kind="todo", slug=None, title="t").id)
    store.add_tag(todo, Tag.parse_strict("SPACE:repo-dev", kind="todo"))
    _link(store, rid_td, todo)
    assert _decision(store, rid_td) == ("repo-dev", "c:links")

    rid_mm = _memory(store)
    repo_dev_mem = _memory(store, tags=("SPACE:repo-dev",))
    _link(store, rid_mm, repo_dev_mem)
    assert _decision(store, rid_mm) == ("repo-dev", "c:links")

    # A plain todo/memory link carries no space evidence.
    rid_plain = _memory(store)
    todo2 = int(store.insert_ref(kind="todo", slug=None, title="t2").id)
    _link(store, rid_plain, todo2)
    assert _decision(store, rid_plain) == ("unknown", "d:none")


def test_rule_order_beats_later_evidence(store: Store) -> None:
    # Rule (a) fires before (b): a DREAM-tagged node with a hook is research.
    rid = _memory(store, tags=("DREAM:speculative",), meta={"hook": "h"})
    assert _decision(store, rid) == ("research", "a:tags")


def test_dry_run_writes_nothing_and_reports_buckets(store: Store) -> None:
    _memory(store, tags=("DREAM:speculative",))
    _memory(store, meta={"file_mirror": {"namespace": "harness"}})
    _memory(store)
    rep = backfill_space(store, dry_run=True)
    assert rep.counts == {"research": 1, "repo-dev": 1, "unknown": 1}
    text = rep.summary()
    assert "dry run" in text and "research: 1" in text
    for rid in [d[0] for d in rep.decisions]:
        assert _space_tag(store, rid) is None
        assert not store.has_tag(rid, "OPEN", UNKNOWN_SPACE_TAG)


def test_real_run_tags_and_stamps_residue(store: Store) -> None:
    research = _memory(store, tags=("DREAM:speculative",))
    repo_dev = _memory(store, meta={"slug": "x"})
    unknown = _memory(store)
    spaced = _memory(store, tags=("SPACE:personal",))

    rep = backfill_space(store)
    assert rep.tagged == {"research": 1, "repo-dev": 1}
    assert rep.unknown_stamped == 1
    assert _space_tag(store, research) == "research"
    assert _space_tag(store, repo_dev) == "repo-dev"
    assert _space_tag(store, unknown) is None
    assert store.has_tag(unknown, "OPEN", UNKNOWN_SPACE_TAG)
    assert _space_tag(store, spaced) == "personal"

    # Idempotent: nothing left to tag; the stamped residue is re-counted.
    rep2 = backfill_space(store)
    assert rep2.tagged == {}
    assert rep2.unknown_stamped == 0
    assert rep2.unknown_kept == 1
    assert rep2.counts == {"unknown": 1}


def test_stale_unknown_stamp_promoted(store: Store) -> None:
    rid = _memory(store)
    backfill_space(store)
    assert store.has_tag(rid, "OPEN", UNKNOWN_SPACE_TAG)
    # The node gains a writer signal later → real run promotes it.
    with store.tx() as conn:
        conn.execute("UPDATE refs SET set_by = 'dream' WHERE ref_id = %s", (rid,))
    rep = backfill_space(store)
    assert rep.tagged == {"research": 1}
    assert _space_tag(store, rid) == "research"
    assert not store.has_tag(rid, "OPEN", UNKNOWN_SPACE_TAG)


def test_limit(store: Store) -> None:
    for _ in range(3):
        _memory(store)
    rep = backfill_space(store, dry_run=True, limit=2)
    assert rep.scanned == 2
    assert rep.counts == {"unknown": 2}


def test_cli_wiring(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ns = _build_parser().parse_args(
        ["memory", "backfill-space", "--dry-run", "--limit", "5"]
    )
    assert ns.dry_run and ns.limit == 5
    _memory(store)
    monkeypatch.setattr("precis.cli.memory.resolve_dsn", lambda *a, **k: "unused")
    monkeypatch.setattr(Store, "connect", classmethod(lambda cls, dsn: store))
    monkeypatch.setattr(store, "close", lambda: None)
    memory_run(_build_parser().parse_args(["memory", "backfill-space", "--dry-run"]))
    out = capsys.readouterr().out
    assert "dry run" in out and "scanned 1" in out
