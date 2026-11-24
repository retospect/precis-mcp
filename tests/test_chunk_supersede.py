"""gr690836 — the chunk-level supersede map.

A merge's body move re-mints every ``chunk_id`` and deletes the donor
rows, so a ``pc<old>`` handle used to dangle. ``chunk_supersede`` now
records ``old → new`` at merge time — every copied chunk when the body
moves, and the same-``ord``/same-text pairs when the loser's body stays
behind under its tombstone. ``resolve_handle`` follows the map exactly
as it follows ``meta.superseded_by`` for record handles,
``evidence.fetch_chunks`` follows it so a pinned passage stays
checkable, the ``merge_into`` door names moving handles + pinned
dependants (and wants ``confirm_chunk_pins`` when any pin exists), and
draft ``view='hygiene'`` reports dead or moved pins.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.graph.garden.apply import apply_merge
from precis.handlers.draft import DraftHandler
from precis.handlers.paper import PaperHandler
from precis.ingest import body_ops, dedup
from precis.nanopub import evidence
from precis.store import ChunkInsert, Store

_TITLE = "Analyses of Reaction Schemes Using De Donder Relations"


def _seed_paper(store: Store, slug: str, **kw: Any) -> int:
    return store.insert_ref(
        kind="paper",
        slug=slug,
        title=kw.pop("title", _TITLE),
        provider="manual",
        meta={},
        **kw,
    ).id


def _body(store: Store, ref_id: int, texts: list[str]) -> list[int]:
    """Insert body chunks; returns their chunk_ids in ord order."""
    store.chunks.insert_chunks(
        ref_id, [ChunkInsert(ord=i, text=t) for i, t in enumerate(texts)]
    )
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _live_chunk_ids(store: Store, ref_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _map_rows(store: Store) -> list[tuple[int, int]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT old_chunk_id, new_chunk_id FROM chunk_supersede "
            "ORDER BY old_chunk_id"
        ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def _merge(store: Store, loser: int, winner: int) -> None:
    with store.tx() as conn:
        dedup.merge_duplicate(
            store,
            survivor_ref_id=winner,
            duplicate_ref_id=loser,
            source="test",
            reason="same paper",
            conn=conn,
        )


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


# ── the map + resolve_handle redirect ────────────────────────────────


def test_moved_chunk_handle_redirects(store: Store) -> None:
    loser = _seed_paper(store, "cspredloser2020", year=2020)
    winner = _seed_paper(store, "cspredwinner2020", year=2020)
    old_ids = _body(store, loser, ["first passage", "second passage"])
    _merge(store, loser, winner)
    new_ids = _live_chunk_ids(store, winner)
    assert len(new_ids) == 2 and set(new_ids).isdisjoint(old_ids)
    assert _map_rows(store) == sorted(zip(old_ids, new_ids))
    for old, new in zip(old_ids, new_ids):
        resolved = store.resolve_handle(f"pc{old}")
        assert resolved is not None
        assert resolved.ref_id == winner
        assert resolved.chunk_id == new
        assert resolved.redirected_from == f"pc{old}"
    # The live copy resolves with no redirect marker.
    live = store.resolve_handle(f"pc{new_ids[0]}")
    assert live is not None and live.redirected_from is None


def test_chunk_redirect_follows_chain(store: Store) -> None:
    """A→B→C: a merged-then-remmerged survivor's copies re-mint again;
    the old handle walks both map rows to the live chunk."""
    loser = _seed_paper(store, "chainloser2021", year=2021)
    mid = _seed_paper(store, "chainmid2021", year=2021)
    final = _seed_paper(store, "chainfinal2021", year=2021)
    (old,) = _body(store, loser, ["the passage"])
    _merge(store, loser, mid)
    (middle,) = _live_chunk_ids(store, mid)
    _merge(store, mid, final)
    (live,) = _live_chunk_ids(store, final)
    resolved = store.resolve_handle(f"pc{old}")
    assert resolved is not None
    assert resolved.ref_id == final
    assert resolved.chunk_id in _live_chunk_ids(store, final)

    from precis.nanopub import mint, term_coverage

    requested = [old, middle, live, old]
    found = evidence.fetch_chunks(store, requested)
    assert {c.resolved_from or c.chunk_id: c.chunk_id for c in found} == {
        old: live,
        middle: live,
        live: live,
    }
    assert len(found) == 3
    passages = [{"chunk_id": cid, "doi": "10.1/chunk-chain"} for cid in requested]
    assert term_coverage.passage_texts(store, {"passages": passages}) == [
        "the passage"
    ] * len(passages)
    assert all(
        row.get("contiguous_group") is True
        for row in mint._freeze_contiguity(store, passages)
    )


def test_unmoved_body_maps_same_text_only(store: Store) -> None:
    """Both sides hold a body: the loser's chunks stay under the
    tombstone; each pc<id> maps where the survivor holds the same
    passage (ord + text hash), and only there."""
    loser = _seed_paper(store, "keptloser2022", year=2022)
    winner = _seed_paper(store, "keptwinner2022", year=2022)
    same, diff = _body(store, loser, ["the shared passage", "the loser-only passage"])
    _body(store, winner, ["the shared passage", "a different passage"])
    _merge(store, loser, winner)
    rows = dict(_map_rows(store))
    # ord 0's identical text maps onto the winner's ord-0 chunk.
    assert same in rows
    resolved = store.resolve_handle(f"pc{same}")
    assert resolved is not None
    assert resolved.ref_id == winner
    assert resolved.chunk_id == _live_chunk_ids(store, winner)[0]
    assert resolved.redirected_from == f"pc{same}"
    # ord 1's different text has no honest target — it stays dead.
    assert diff not in rows
    assert store.resolve_handle(f"pc{diff}") is None


def test_fetch_chunks_follows_the_map(store: Store) -> None:
    """The pin-lint read path keeps reading a moved passage."""
    loser = _seed_paper(store, "fetchloser2023", year=2023)
    winner = _seed_paper(store, "fetchwinner2023", year=2023)
    (old,) = _body(store, loser, ["the pinned passage text"])
    assert evidence.fetch_chunks(store, [old]) != []
    _merge(store, loser, winner)
    found = evidence.fetch_chunks(store, [old])
    assert len(found) == 1
    assert found[0].ref_id == winner
    assert found[0].text == "the pinned passage text"
    assert found[0].chunk_id == _live_chunk_ids(store, winner)[0]
    # ``resolved_from`` carries the stale id the caller pinned — the key
    # request→result maps (withheld_edges, report_source_verdicts,
    # _freeze_source_anchor, passage_texts) must build on.
    assert found[0].resolved_from == old


def test_fetch_chunks_tombstone_redirect(store: Store) -> None:
    """A kept-under-tombstone chunk still has a row but its ref is dead;
    the map's redirect must win over that stale row."""
    loser = _seed_paper(store, "tombloser2023", year=2023)
    winner = _seed_paper(store, "tombwinner2023", year=2023)
    (old,) = _body(store, loser, ["the shared passage"])
    (live,) = _body(store, winner, ["the shared passage"])
    _merge(store, loser, winner)  # both hold a body — the loser keeps its rows
    found = evidence.fetch_chunks(store, [old])
    assert len(found) == 1
    assert found[0].chunk_id == live
    assert found[0].resolved_from == old


def test_fetch_chunks_tombstone_fallback_without_map(store: Store) -> None:
    """A tombstoned chunk with NO redirect keeps its legacy text answer
    (pre-map merges left rows like pa1181's behind a tombstone)."""
    loser = _seed_paper(store, "nostmaploser2023", year=2023)
    winner = _seed_paper(store, "nostmapwinner2023", year=2023)
    (old,) = _body(store, loser, ["a passage only the loser holds"])
    _body(store, winner, ["unrelated winner text"])
    _merge(store, loser, winner)
    found = evidence.fetch_chunks(store, [old])
    assert len(found) == 1
    assert found[0].chunk_id == old
    assert found[0].resolved_from is None


def test_passage_texts_follows_redirect(store: Store) -> None:
    """term_coverage.passage_texts keys results by the PINNED id."""
    from precis.nanopub import term_coverage

    loser = _seed_paper(store, "ptloser2023", year=2023)
    winner = _seed_paper(store, "ptwinner2023", year=2023)
    (old,) = _body(store, loser, ["the grounded passage"])
    _merge(store, loser, winner)
    grounding = {"passages": [{"chunk_id": old}]}
    assert term_coverage.passage_texts(store, grounding) == ["the grounded passage"]


def test_restored_remerge_repoints_redirect(store: Store) -> None:
    """A loser restored and merged into a DIFFERENT survivor re-points
    its old handles — the newest merge owns the redirect (DO UPDATE)."""
    loser = _seed_paper(store, "remargeloser2023", year=2023)
    first = _seed_paper(store, "remargefirst2023", year=2023)
    second = _seed_paper(store, "remargesecond2023", year=2023)
    (old,) = _body(store, loser, ["the shared passage"])
    _body(store, first, ["the shared passage"])
    _body(store, second, ["the shared passage"])
    _merge(store, loser, first)
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = NULL WHERE ref_id = %s", (loser,))
    _merge(store, loser, second)
    resolved = store.resolve_handle(f"pc{old}")
    assert resolved is not None
    assert resolved.ref_id == second
    assert resolved.chunk_id == _live_chunk_ids(store, second)[0]


def test_restored_remerge_removes_redirect_without_match(store: Store) -> None:
    loser = _seed_paper(store, "removenomatch2023", year=2023)
    first = _seed_paper(store, "removematch2023", year=2023)
    second = _seed_paper(store, "removeother2023", year=2023)
    (old,) = _body(store, loser, ["the shared passage"])
    _body(store, first, ["the shared passage"])
    _body(store, second, ["a different passage"])
    _merge(store, loser, first)
    assert store.resolve_handle(f"pc{old}") is not None
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = NULL WHERE ref_id = %s", (loser,))
    _merge(store, loser, second)
    assert old not in dict(_map_rows(store))
    assert store.resolve_handle(f"pc{old}") is None


def test_garden_merge_maps_kept_bodies(store: Store) -> None:
    loser = _seed_paper(store, "gardenchunkloser2023", year=2023)
    winner = _seed_paper(store, "gardenchunkwinner2023", year=2023)
    old, mismatch = _body(store, loser, ["the shared passage", "original text"])
    live, _ = _body(store, winner, ["the shared passage", "different text"])
    apply_merge(store, loser, winner, actor="test")
    assert dict(_map_rows(store)) == {old: live}
    assert store.resolve_handle(f"pc{mismatch}") is None


def test_standalone_body_move_does_not_redirect(store: Store) -> None:
    donor = _seed_paper(store, "standalonedonor2023", year=2023)
    target = _seed_paper(store, "standalonetarget2023", year=2023)
    (old,) = _body(store, donor, ["the transferred passage"])
    with store.tx() as conn:
        body_ops.move_body(src_ref_id=donor, dst_ref_id=target, conn=conn)
    assert len(_live_chunk_ids(store, target)) == 1
    assert store.resolve_handle(f"pc{old}") is None


# ── the merge door: counts + confirm flag ────────────────────────────


class TestMergeDoorChunkHandles:
    def test_dry_run_names_moving_handles(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "dryloser2024", year=2024)
        winner = _seed_paper(store, "drywinner2024", year=2024)
        _body(store, loser, ["one", "two"])
        resp = handler.edit(
            id=loser,
            args={"merge_into": winner, "reason": "dup"},
            dry_run=True,
        )
        assert "DRY RUN" in resp.body
        assert "2 chunk handle(s) re-mint" in resp.body
        assert _live_chunk_ids(store, loser)  # nothing moved

    def test_dry_run_names_pinned_dependants(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "pinloser2024", year=2024)
        winner = _seed_paper(store, "pinwinner2024", year=2024)
        pinner = _seed_paper(store, "pinner2024", year=2024)
        _body(store, loser, ["pinned passage"])
        store.add_link(src_ref_id=pinner, dst_ref_id=loser, dst_pos=0, relation="cites")
        resp = handler.edit(
            id=loser,
            args={"merge_into": winner, "reason": "dup"},
            dry_run=True,
        )
        assert "dependant(s) pin" in resp.body
        assert "confirm_chunk_pins" in resp.body

    def test_pinned_merge_needs_confirm(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "gateloser2025", year=2025)
        winner = _seed_paper(store, "gatewinner2025", year=2025)
        pinner = _seed_paper(store, "gatepinner2025", year=2025)
        _body(store, loser, ["pinned passage"])
        store.add_link(src_ref_id=pinner, dst_ref_id=loser, dst_pos=0, relation="cites")
        with pytest.raises(BadInput, match="confirm_chunk_pins"):
            handler.edit(
                id=loser,
                args={"merge_into": winner, "reason": "dup"},
            )
        resp = handler.edit(
            id=loser,
            args={
                "merge_into": winner,
                "reason": "dup",
                "confirm_chunk_pins": True,
            },
        )
        assert "merged" in resp.body
        assert "1 pc<old> handle(s) now redirect" in resp.body

    def test_text_pin_needs_confirm(self, store: Store, handler: PaperHandler) -> None:
        """A ``pc<id>`` token in another ref's chunk text counts as a
        pinned dependant — the tsvector probe finds it."""
        loser = _seed_paper(store, "txtloser2025", year=2025)
        winner = _seed_paper(store, "txtwinner2025", year=2025)
        citer = _seed_paper(store, "txtciter2025", year=2025)
        (old,) = _body(store, loser, ["the cited passage"])
        _body(store, citer, [f"as reported [pc{old}] in the literature"])
        with pytest.raises(BadInput, match="confirm_chunk_pins"):
            handler.edit(
                id=loser,
                args={"merge_into": winner, "reason": "dup"},
            )

    def test_grounding_pins_report_redirects_and_dangling_targets(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "groundedloser2025", year=2025)
        winner = _seed_paper(store, "groundedwinner2025", year=2025)
        grounded = _seed_paper(store, "groundedpinner2025", year=2025)
        shared, mismatch = _body(store, loser, ["shared text", "original text"])
        _body(store, winner, ["shared text", "different text"])
        store.record_grounding(
            "ref",
            grounded,
            grounding="sourced",
            pins=[f"pc{shared}", f"pc{mismatch}"],
            actor="test",
            actor_type="agent",
            reason="record evidence",
        )
        args = {"merge_into": winner, "reason": "dup"}
        preview = handler.edit(id=loser, args=args, dry_run=True)
        assert "2 grounding pin(s)" in preview.body
        counts = "1 pin(s) target redirecting handles, 1 pin(s) target dangling handles"
        assert counts in preview.body
        with pytest.raises(BadInput, match="confirm_chunk_pins"):
            handler.edit(id=loser, args=args)
        resp = handler.edit(id=loser, args={**args, "confirm_chunk_pins": True})
        assert counts in resp.body

    def test_unpinned_merge_needs_no_flag(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "freeloser2026", year=2026)
        winner = _seed_paper(store, "freewinner2026", year=2026)
        _body(store, loser, ["nobody cites this"])
        resp = handler.edit(id=loser, args={"merge_into": winner, "reason": "dup"})
        assert "1 pc<old> handle(s) now redirect" in resp.body

    def test_confirm_chunk_pins_is_a_declared_edit_key(self) -> None:
        """MCP dispatch rejects any args= key outside ARGS_KEYS[verb] —
        the flag must be declared or the refusal's ``next=`` paste fails."""
        assert "confirm_chunk_pins" in PaperHandler.ARGS_KEYS["edit"]

    def test_dry_run_names_kept_body_redirect_count(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """Both sides hold a body: the dry-run names how many pc<id>s
        actually redirect (the ord + text-hash matches), not just the
        loser's body size."""
        loser = _seed_paper(store, "keptdryloser2024", year=2024)
        winner = _seed_paper(store, "keptdrywinner2024", year=2024)
        _body(store, loser, ["the shared passage", "the loser-only passage"])
        _body(store, winner, ["the shared passage", "a different passage"])
        resp = handler.edit(
            id=loser,
            args={"merge_into": winner, "reason": "dup"},
            dry_run=True,
        )
        assert "DRY RUN" in resp.body
        assert "1 pc<id>(s) redirect" in resp.body
        resp = handler.edit(id=loser, args={"merge_into": winner, "reason": "dup"})
        assert "1 pc<old> handle(s) now redirect" in resp.body

    def test_ack_names_pinned_dependants(
        self, store: Store, handler: PaperHandler
    ) -> None:
        loser = _seed_paper(store, "ackloser2025", year=2025)
        winner = _seed_paper(store, "ackwinner2025", year=2025)
        pinner = _seed_paper(store, "ackpinner2025", year=2025)
        _body(store, loser, ["pinned passage"])
        store.add_link(src_ref_id=pinner, dst_ref_id=loser, dst_pos=0, relation="cites")
        resp = handler.edit(
            id=loser,
            args={
                "merge_into": winner,
                "reason": "dup",
                "confirm_chunk_pins": True,
            },
        )
        assert "1 pin(s) target redirecting handles" in resp.body

    def test_earlier_redirect_handle_counts_as_pin(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A pin written as pc<X> that redirects INTO the loser (X merged
        in earlier) is a dependant of THIS merge — it will chain onto the
        winner, and the probe must see it."""
        old_loser = _seed_paper(store, "ancestorloser2025", year=2025)
        loser = _seed_paper(store, "ancestorl2025", year=2025)
        winner = _seed_paper(store, "ancestorw2025", year=2025)
        citer = _seed_paper(store, "ancestorciter2025", year=2025)
        (ancestor,) = _body(store, old_loser, ["the ancestral passage"])
        _merge(store, old_loser, loser)  # pc<ancestor> now resolves into loser
        _body(store, citer, [f"as reported [pc{ancestor}] upstream"])
        # loser's own chunks carry no pins; only the ancestor handle does.
        with pytest.raises(BadInput, match="confirm_chunk_pins"):
            handler.edit(
                id=loser,
                args={"merge_into": winner, "reason": "dup"},
            )


# ── draft hygiene: dead / moved pins ─────────────────────────────────


def _draft(hub: Hub) -> DraftHandler:
    d = DraftHandler(hub=hub)
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id
    d.put(id="nt", title="T", project=proj)
    return d


def test_hygiene_lists_dead_pin(hub: Hub) -> None:
    d = _draft(hub)
    d.put(
        id="nt",
        chunk_kind="paragraph",
        text="the angle is 36° [fi1>pc999999999]",
        at={"last": True},
    )
    out = d.get(id="nt", view="hygiene").body
    assert "dead chunk pin" in out
    assert "pc999999999" in out


def test_hygiene_lists_moved_pin(hub: Hub) -> None:
    store = hub.live_store
    loser = _seed_paper(store, "hygloser2027", year=2027)
    winner = _seed_paper(store, "hygwinner2027", year=2027)
    (old,) = _body(store, loser, ["the kink angle passage"])
    d = _draft(hub)
    d.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"the angle is 36° [fi1>pc{old}]",
        at={"last": True},
    )
    _merge(store, loser, winner)
    out = d.get(id="nt", view="hygiene").body
    assert "merged chunk pin" in out
    assert f"pc{old}" in out
    assert "dead chunk pin" not in out
