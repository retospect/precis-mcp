"""Linkable parts (design in the ``precis.handlers.part`` docstring).

A catalog part becomes a lazy ``part`` ref on its first add-mode link:
``Store.ensure_part_ref`` mints it, the two generic link doors call it,
``get(kind='part')`` shows the ref's ring, and a datasheet's
``part_lcsc`` dual-writes a ``datasheet-of`` edge.
"""

from __future__ import annotations

import threading

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.component import ComponentHandler
from precis.handlers.datasheet import DatasheetHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.part import PartHandler
from precis.pcb import catalog
from precis.utils import handle_registry


def _catalog_rows(*lcsc: int) -> list[dict]:
    rows = [
        catalog.normalize_jlcparts_row(
            {
                "lcsc": n,
                "manufacturer": "Samsung",
                "mfr_part": f"CL05B{n}",
                "description": "100nF 16V X7R 0402 capacitor",
                "basic": 1,
                "stock": 1000,
                "package": "0402",
            }
        )
        for n in lcsc
    ]
    return [r for r in rows if r]


@pytest.fixture
def seeded(store):
    store.parts_import(_catalog_rows(25804, 1525))
    return store


def _part_ref_count(store, lcsc: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs r JOIN ref_identifiers ri "
            "  ON ri.ref_id = r.ref_id AND ri.id_kind = 'lcsc' "
            "WHERE r.kind = 'part' AND ri.id_value = %s",
            (lcsc,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _memory(store) -> int:
    return store.insert_ref(kind="memory", slug=None, title="a thought").id


# ── ensure_part_ref ──────────────────────────────────────────────────


def test_ensure_part_ref_mints_once_and_reuses(seeded):
    first = seeded.ensure_part_ref("c25804")
    assert seeded.ensure_part_ref("C25804") == first
    ref = seeded.get_ref(kind="part", id="C25804")
    assert ref is not None and ref.id == first
    assert ref.title.startswith("CL05B25804 — 100nF")
    assert _part_ref_count(seeded, "C25804") == 1


def test_ensure_part_ref_refuses_a_c_number_not_in_the_catalog(seeded):
    with pytest.raises(NotFound):
        seeded.ensure_part_ref("C99999999")
    assert _part_ref_count(seeded, "C99999999") == 0


def test_concurrent_first_mints_converge_on_one_ref(seeded):
    ids: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def mint() -> None:
        try:
            barrier.wait()
            ids.append(seeded.ensure_part_ref("C25804"))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=mint) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(ids)) == 1
    assert _part_ref_count(seeded, "C25804") == 1


def test_a_retired_part_ref_is_replaced_by_a_fresh_mint(seeded):
    old = seeded.ensure_part_ref("C25804")
    seeded.retire_ref(old)
    new = seeded.ensure_part_ref("C25804")
    assert new != old
    assert seeded.part_ref_id("C25804") == new


# ── link doors ───────────────────────────────────────────────────────


def test_memory_link_to_a_part_mints_it_and_a_second_link_reuses_it(seeded):
    memory = MemoryHandler(hub=Hub(store=seeded))
    a, b = _memory(seeded), _memory(seeded)
    memory.link(id=a, target="part:C25804", rel="related-to")
    memory.link(id=b, target="part:C25804", rel="related-to")
    assert _part_ref_count(seeded, "C25804") == 1
    part_id = seeded.part_ref_id("C25804")
    assert {lk.src_ref_id for lk in seeded.links_for(part_id, direction="in")} == {
        a,
        b,
    }


def test_link_to_an_uncatalogued_part_mints_nothing(seeded):
    memory = MemoryHandler(hub=Hub(store=seeded))
    with pytest.raises(NotFound):
        memory.link(id=_memory(seeded), target="part:C99999999", rel="related-to")
    assert _part_ref_count(seeded, "C99999999") == 0


def test_unlink_and_a_bad_rel_never_mint(seeded):
    memory = MemoryHandler(hub=Hub(store=seeded))
    m = _memory(seeded)
    with pytest.raises(NotFound):
        memory.link(id=m, target="part:C25804", mode="remove")
    with pytest.raises(BadInput):
        memory.link(id=m, target="part:C25804", rel="no-such-relation")
    assert _part_ref_count(seeded, "C25804") == 0


def test_component_realized_by_part_shows_in_both_rings(seeded):
    seeded.insert_ref(kind="component", slug="decap-100n", title="decoupling cap")
    ComponentHandler(hub=Hub(store=seeded)).link(
        id="decap-100n", rel="realized-by", target="part:C25804"
    )
    part_body = PartHandler(hub=Hub(store=seeded)).get(id="C25804").body
    assert "decap-100n" in part_body or "decoupling cap" in part_body
    comp = seeded.get_ref(kind="component", id="decap-100n")
    out = seeded.links_for(comp.id, direction="out", relation="realized-by")
    assert [lk.dst_ref_id for lk in out] == [seeded.part_ref_id("C25804")]


def test_component_link_keeps_made_of_and_contains_on_put(seeded):
    seeded.insert_ref(kind="component", slug="bracket", title="bracket")
    with pytest.raises(BadInput, match="put"):
        ComponentHandler(hub=Hub(store=seeded)).link(
            id="bracket", rel="contains", target="component:bracket"
        )


# ── get + handle ─────────────────────────────────────────────────────


def test_get_part_shows_catalog_row_then_ring_once_linked(seeded):
    part = PartHandler(hub=Hub(store=seeded))
    before = part.get(id="C25804").body
    assert "CL05B25804" in before and "ref:" not in before
    MemoryHandler(hub=Hub(store=seeded)).link(id=_memory(seeded), target="part:C25804")
    after = part.get(id="C25804").body
    handle = handle_registry.format_handle("part", seeded.part_ref_id("C25804"))
    assert "CL05B25804" in after and f"ref: {handle}" in after
    assert "a thought" in after


def test_get_part_after_it_leaves_the_catalog_shows_the_ref(seeded):
    MemoryHandler(hub=Hub(store=seeded)).link(id=_memory(seeded), target="part:C25804")
    seeded.parts_bulk_replace(_catalog_rows(1525), force=True)  # swap drops C25804
    body = PartHandler(hub=Hub(store=seeded)).get(id="C25804").body
    assert "no longer in the catalog" in body
    assert "a thought" in body
    with pytest.raises(NotFound):
        PartHandler(hub=Hub(store=seeded)).get(id="C99999999")


def test_pn_handle_resolves_to_the_part_ref(seeded):
    ref_id = seeded.ensure_part_ref("C25804")
    handle = handle_registry.format_handle("part", ref_id)
    assert handle == f"pn{ref_id}"
    resolved = seeded.resolve_handle(handle)
    assert resolved is not None
    assert (resolved.kind, resolved.ref_id, resolved.public_id) == (
        "part",
        ref_id,
        "C25804",
    )


# ── write-time C-number mention autolink (gr520243) ──────────────────


def _ref_id(store, kind: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = %s", (kind,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _auto_mentions(store, src: int) -> set[int]:
    """dst ref_ids of the auto-mention ``related-to`` edges only —
    hand-added links are not the autolinker's business."""
    return {
        lk.dst_ref_id
        for lk in store.links_for(src, direction="out", relation="related-to")
        if (lk.meta or {}).get("auto") == "mention"
    }


def _jobs(store, job_type: str) -> list[str]:
    """lcsc param of each live ``job_type`` job ref, sorted."""
    with store.pool.connection() as conn:
        return sorted(
            str(r[0])
            for r in conn.execute(
                "SELECT meta->'params'->>'lcsc' FROM refs "
                "WHERE kind = 'job' AND meta->>'job_type' = %s "
                "ORDER BY ref_id",
                (job_type,),
            ).fetchall()
        )


def test_todo_text_naming_c_numbers_autolinks_their_part_refs(seeded):
    """Acceptance: a todo naming two C-numbers mints + links both, and
    each linked part queues its datasheet pull — bare prose C-numbers
    never queue the BIG-tier facts read (the cost policy, gr520243);
    it waits for an explicit door. A second edit naming the same parts
    queues nothing new."""
    from precis.handlers.todo import TodoHandler

    todo = TodoHandler(hub=Hub(store=seeded))
    resp = todo.put(text="swap R1 for C25804 and U1 for C1525")
    assert "created todo" in resp.body
    tid = _ref_id(seeded, "todo")
    part_a, part_b = seeded.part_ref_id("C25804"), seeded.part_ref_id("C1525")
    assert part_a is not None and part_b is not None
    assert _auto_mentions(seeded, tid) == {part_a, part_b}
    assert _jobs(seeded, "datasheet_pull") == ["C1525", "C25804"]
    assert _jobs(seeded, "datasheet_facts") == []
    # A re-write naming the same parts: idem dedupe reuses the jobs.
    todo.edit(id=tid, mode="replace", text="swap R1 for C25804, U1 for C1525")
    assert _auto_mentions(seeded, tid) == {part_a, part_b}
    assert _jobs(seeded, "datasheet_pull") == ["C1525", "C25804"]
    assert _jobs(seeded, "datasheet_facts") == []


def test_todo_edit_resyncs_c_number_mentions(seeded):
    """replace=True drops the removed mention's auto edge."""
    from precis.handlers.todo import TodoHandler

    todo = TodoHandler(hub=Hub(store=seeded))
    todo.put(text="buy C25804 for the BOM")
    tid = _ref_id(seeded, "todo")
    assert _auto_mentions(seeded, tid) == {seeded.part_ref_id("C25804")}
    todo.edit(id=tid, mode="replace", text="buy C1525 instead")
    assert _auto_mentions(seeded, tid) == {seeded.part_ref_id("C1525")}


def test_link_door_to_a_part_queues_its_pull(seeded):
    """The explicit ``link()`` door queues the pull+facts pair too."""
    memory = MemoryHandler(hub=Hub(store=seeded))
    memory.link(id=_memory(seeded), target="part:C25804", rel="related-to")
    assert _jobs(seeded, "datasheet_pull") == ["C25804"]
    assert _jobs(seeded, "datasheet_facts") == ["C25804"]
    # Second link to the same part: nothing new queued.
    memory.link(id=_memory(seeded), target="part:C25804", rel="related-to")
    assert _jobs(seeded, "datasheet_pull") == ["C25804"]


def test_an_uncatalogued_c_number_stays_unlinked(seeded):
    from precis.handlers.todo import TodoHandler

    todo = TodoHandler(hub=Hub(store=seeded))
    todo.put(text="maybe C99999999 works here")
    tid = _ref_id(seeded, "todo")
    assert _auto_mentions(seeded, tid) == set()
    assert _part_ref_count(seeded, "C99999999") == 0


def test_memory_mention_of_a_c_number_links_too(seeded):
    memory = MemoryHandler(hub=Hub(store=seeded))
    memory.put(text="note about C25804")
    assert _auto_mentions(seeded, _ref_id(seeded, "memory")) == {
        seeded.part_ref_id("C25804")
    }


def test_draft_chunk_naming_a_c_number_links_the_minted_part(seeded):
    from precis.handlers.draft import DraftHandler

    proj = seeded.insert_ref(kind="todo", slug=None, title="proj").id
    ref, _t = seeded.drafts.create_draft(name="bom", title="BOM", project_ref_id=proj)
    seeded.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="uses C25804",
        at={"last": True},
    )
    DraftHandler(hub=Hub(store=seeded)).sync_draft_links(ref.id)
    assert seeded.part_ref_id("C25804") in _auto_mentions(seeded, ref.id)


# ── the facts-queue cost policy (gr520243) ───────────────────────────


def test_bare_c_number_mentions_pull_but_never_queue_facts(seeded):
    """A bare C-number links + queues the pull only — a prose dump of
    200 of them mints 200 links and 200 pulls but ZERO facts jobs."""
    from precis.handlers.todo import TodoHandler

    nums = list(range(200000, 200200))
    seeded.parts_import(_catalog_rows(*nums))
    todo = TodoHandler(hub=Hub(store=seeded))
    resp = todo.put(text="BOM: " + " ".join(f"C{n}" for n in nums))
    assert "created todo" in resp.body
    tid = _ref_id(seeded, "todo")
    assert len(_auto_mentions(seeded, tid)) == 200
    assert _jobs(seeded, "datasheet_pull") == sorted(f"C{n}" for n in nums)
    assert _jobs(seeded, "datasheet_facts") == []


def test_part_mentions_queue_facts_only_up_to_the_write_cap(seeded):
    """8 explicit ``part:C…`` mentions in one write: at most 5 new
    facts jobs queue; the rest link + pull and the reply notes it."""
    from precis.handlers.todo import TodoHandler

    nums = list(range(210000, 210008))
    seeded.parts_import(_catalog_rows(*nums))
    todo = TodoHandler(hub=Hub(store=seeded))
    resp = todo.put(text="parts: " + " ".join(f"part:C{n}" for n in nums))
    assert len(_jobs(seeded, "datasheet_facts")) == 5
    assert _jobs(seeded, "datasheet_pull") == sorted(f"C{n}" for n in nums)
    assert "3 more parts linked; their briefs build when opened" in resp.body


def test_the_daily_facts_ceiling_skips_queueing(seeded):
    """Once 100 facts jobs were minted in the last 24 h (the spend cap)
    an explicit ``part:`` mention still links + pulls but queues no
    facts job."""
    from precis.handlers.todo import TodoHandler

    for i in range(100):
        seeded.insert_ref(
            kind="job",
            slug=None,
            title=f"facts {i}",
            meta={
                "job_type": "datasheet_facts",
                "params": {"lcsc": f"C900{i:04d}"},
            },
        )
    todo = TodoHandler(hub=Hub(store=seeded))
    resp = todo.put(text="use part:C25804 here")
    assert _jobs(seeded, "datasheet_pull") == ["C25804"]
    facts = _jobs(seeded, "datasheet_facts")
    assert "C25804" not in facts and len(facts) == 100
    assert "1 more parts linked; their briefs build when opened" in resp.body


def test_unscoped_draft_sync_links_without_queueing_jobs(seeded):
    """A sync with no touched-chunk set (refresh / export / taproot /
    chunk move+delete) re-materialises links only — it queues neither
    a pull nor a facts job."""
    from precis.handlers.draft import DraftHandler

    proj = seeded.insert_ref(kind="todo", slug=None, title="proj").id
    ref, _t = seeded.drafts.create_draft(name="bom2", title="BOM", project_ref_id=proj)
    seeded.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="uses C25804 and part:C1525",
        at={"last": True},
    )
    DraftHandler(hub=Hub(store=seeded)).sync_draft_links(ref.id)
    assert _auto_mentions(seeded, ref.id) == {
        seeded.part_ref_id("C25804"),
        seeded.part_ref_id("C1525"),
    }
    assert _jobs(seeded, "datasheet_pull") == []
    assert _jobs(seeded, "datasheet_facts") == []


def test_scoped_draft_sync_queues_pull_and_facts(seeded):
    """A scoped sync (a text write naming its chunks) does queue — pull
    for every part, facts for the explicit ``part:`` spelling only."""
    from precis.handlers.draft import DraftHandler

    proj = seeded.insert_ref(kind="todo", slug=None, title="proj").id
    ref, _t = seeded.drafts.create_draft(name="bom3", title="BOM", project_ref_id=proj)
    c = seeded.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="uses C25804 and part:C1525",
        at={"last": True},
    )[0]
    DraftHandler(hub=Hub(store=seeded)).sync_draft_links(
        ref.id, restamp_chunk_ids={c.chunk_id}
    )
    assert _jobs(seeded, "datasheet_pull") == ["C1525", "C25804"]
    assert _jobs(seeded, "datasheet_facts") == ["C1525"]


# ── datasheet dual-write ─────────────────────────────────────────────


def _datasheet_of(store, ds_id: int) -> list[int]:
    return [
        lk.dst_ref_id
        for lk in store.links_for(ds_id, direction="out", relation="datasheet-of")
    ]


def test_datasheet_part_lcsc_writes_meta_and_the_edge(seeded):
    ds = seeded.insert_ref(kind="datasheet", slug="cl05b-ds", title="CL05B datasheet")
    handler = DatasheetHandler(hub=Hub(store=seeded))
    handler.edit(id="cl05b-ds", part_lcsc="c25804")
    ref = seeded.get_ref(kind="datasheet", id=ds.id)
    assert ref.meta["part_lcsc"] == "C25804"
    assert _datasheet_of(seeded, ds.id) == [seeded.part_ref_id("C25804")]

    handler.edit(id="cl05b-ds", part_lcsc="C1525")
    assert _datasheet_of(seeded, ds.id) == [seeded.part_ref_id("C1525")]

    handler.edit(id="cl05b-ds", part_lcsc="")
    assert _datasheet_of(seeded, ds.id) == []
    assert seeded.get_ref(kind="datasheet", id=ds.id).meta["part_lcsc"] == ""


def test_datasheet_part_lcsc_outside_the_catalog_keeps_meta_only(seeded):
    ds = seeded.insert_ref(kind="datasheet", slug="odd-ds", title="odd datasheet")
    resp = DatasheetHandler(hub=Hub(store=seeded)).edit(
        id="odd-ds", part_lcsc="C99999999"
    )
    assert "not in the catalog" in resp.body
    assert seeded.get_ref(kind="datasheet", id=ds.id).meta["part_lcsc"] == "C99999999"
    assert _datasheet_of(seeded, ds.id) == []


# ── slice 2: board → part contains edges ─────────────────────────────

_NO_CHANGE = {"mint": 0, "add": 0, "update": 0, "remove": 0, "uncatalogued": 0}


def _board(store, slug: str, parts: dict[str, str | None]) -> int:
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=f"board {slug}",
        components=[
            {"refdes": refdes, "part_lcsc": lcsc, "pins": [{"name": "1"}]}
            for refdes, lcsc in parts.items()
        ],
        nets=[],
        connections=[],
    )
    return int(ref.id)


def _part_edges(store, board_id: int) -> dict[str, dict]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ri.id_value, l.meta FROM links l "
            "JOIN ref_identifiers ri "
            "  ON ri.ref_id = l.dst_ref_id AND ri.id_kind = 'lcsc' "
            "WHERE l.src_ref_id = %s AND l.relation = 'contains'",
            (board_id,),
        ).fetchall()
    return {str(r[0]): dict(r[1]) for r in rows}


def test_board_gets_one_contains_edge_per_part_with_refdes_and_qty(seeded):
    board = _board(seeded, "b1", {"R1": "C25804", "R2": "c25804", "U1": "C1525"})
    assert _part_edges(seeded, board) == {
        "C25804": {"refdes": ["R1", "R2"], "qty": 2},
        "C1525": {"refdes": ["U1"], "qty": 1},
    }
    with seeded.tx() as conn:
        assert seeded._pcb_reconcile_part_edges(conn, board, dry_run=True) == (
            _NO_CHANGE
        )


def test_an_uncatalogued_board_part_gets_no_edge_and_no_error(seeded):
    board = _board(seeded, "b2", {"R1": "C99999999", "R2": None})
    assert _part_edges(seeded, board) == {}
    assert _part_ref_count(seeded, "C99999999") == 0


def test_a_regenerated_expansion_moves_and_drops_part_edges(seeded, monkeypatch):
    from precis.pcb.generators import GeneratorExpansion

    def _expand(generator: str, name: str, params: dict) -> GeneratorExpansion:
        lcsc = {1: ["C25804", "C25804"], 2: ["C1525"]}[params["v"]]
        refdes = [name, f"{name}_B"][: len(lcsc)]
        return GeneratorExpansion(
            refdes=name,
            generator="fake_parts_gen",
            version=1,
            canonical_params=dict(params),
            components=[
                {"refdes": r, "part_lcsc": c, "pins": [{"name": "1"}]}
                for r, c in zip(refdes, lcsc, strict=True)
            ],
            nets=[],
            connections=[],
            footprints=[],
            features=[],
            ledger={},
        )

    monkeypatch.setattr("precis.pcb.generators.expand", _expand)

    def apply(v: int) -> int:
        ref, _created, _counts = seeded.pcb_apply(
            slug="b3",
            title="b3",
            components=[],
            nets=[],
            connections=[],
            generators=[
                {"name": "G", "generator": "fake_parts_gen", "params": {"v": v}}
            ],
        )
        return int(ref.id)

    board = apply(1)
    assert _part_edges(seeded, board) == {"C25804": {"refdes": ["G", "G_B"], "qty": 2}}
    apply(2)
    assert _part_edges(seeded, board) == {"C1525": {"refdes": ["G"], "qty": 1}}


def test_board_and_part_fisheyes_list_each_other(seeded):
    from precis.utils.eye_render import render_eye

    board = _board(seeded, "b4", {"R1": "C25804"})
    part_handle = handle_registry.format_handle("part", seeded.part_ref_id("C25804"))
    board_handle = handle_registry.format_handle("pcb", board)
    assert "board b4" in render_eye(seeded, part_handle, "fisheye+1hop")
    assert "CL05B25804" in render_eye(seeded, board_handle, "fisheye+1hop")


def test_link_parts_backfill_dry_run_then_idempotent(store):
    board = _board(store, "b5", {"R1": "C25804", "R2": "C25804"})
    assert _part_edges(store, board) == {}  # catalog empty at apply time
    store.parts_import(_catalog_rows(25804))
    dry = store.pcb_link_parts(dry_run=True)
    assert (dry["boards"], dry["mint"], dry["add"]) == (1, 1, 1)
    assert _part_edges(store, board) == {}
    real = store.pcb_link_parts(dry_run=False)
    assert (real["mint"], real["add"]) == (1, 1)
    assert _part_edges(store, board) == {"C25804": {"refdes": ["R1", "R2"], "qty": 2}}
    again = store.pcb_link_parts(dry_run=False)
    assert {k: again[k] for k in _NO_CHANGE} == _NO_CHANGE
