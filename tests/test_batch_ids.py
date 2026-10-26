"""``get``/``tag``/``link``/``edit`` with ``id=[...]`` on gripe, alert,
todo, memory, finding (get/tag only there — link stays the single-id
Taproot door, and alert/job have no ``edit``).

Batch ``get`` is summary-shaped by default (one block per id, order
kept, missing ids are a one-line block); a ``view=`` maps the kind's own
single-id view over the batch. Batch ``tag`` is one transaction (one bad
id rolls every id back); batch ``link`` fans one target/rel across every
source id, also in one transaction; batch ``edit`` applies the same edit
to every id, again all-or-nothing (gr514482). All share the
``_coerce_ids`` normaliser and its cap; every other verb and every other
kind still refuses a list with a ``BadInput`` naming the batch form,
never an ``Internal``.

``edit(id=[...])`` is uniform across every kind (gr514482): the
handler's ``edit_id_list`` picks ``'native'`` (its own list-taking
batch), ``'repeat'`` (the dispatcher replays the scalar edit per id
inside ``store.atomic()`` — store-backed kinds whose per-id edit fits
one transaction) or ``'refuse'`` — refusal with a ``BadInput`` naming
the per-id call (file-backed kinds that can't honour the rollback
claim; structure, whose per-design in-process relax is too long for
one transaction). ``None`` means undeclared and fails the parity test
below, so a new kind that adds ``edit`` must pick a policy.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from precis.alerts import STATE_OPEN, STATE_RESOLVED, raise_alert
from precis.config import PrecisConfig
from precis.dispatch import Hub, boot
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._numeric_ref import BATCH_ID_CAP
from precis.handlers.alert import AlertHandler
from precis.handlers.anki import AnkiHandler
from precis.handlers.gripe import GripeHandler
from precis.handlers.job import JobHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.quest import QuestHandler
from precis.handlers.todo import TodoHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from tests.conftest import id_of


def _gripes(hub: Hub, n: int) -> tuple[GripeHandler, list[int]]:
    handler = GripeHandler(hub=hub)
    return handler, [
        id_of(handler.put(text=f"batch gripe {i}\nmore body").body) for i in range(n)
    ]


def _batch(*ids: int | str | None) -> list[str | int]:
    """The ``id=[...]`` argument, typed as the verbs declare it."""
    return [i for i in ids if i is not None]


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


# ── get ────────────────────────────────────────────────────────────


def test_get_batch_gripe_is_summary_shaped_in_order(hub: Hub) -> None:
    handler, ids = _gripes(hub, 3)
    handler.tag(id=ids[1], add=["STATUS:triaged", "PRIO:high"])
    handler.link(id=ids[0], target=f"gripe:{ids[2]}", rel="related-to")
    missing = max(ids) + 100_000
    body = handler.get(id=_batch(ids[2], missing, ids[0], ids[1])).body
    assert body.startswith("# 4 gripe (batch)")
    blocks = body.split("\n\n")[1:5]
    assert blocks[0].startswith(f"gr{ids[2]}  ")
    assert blocks[1] == f"gr{missing}  not found"
    assert blocks[2].startswith(f"gr{ids[0]}  ")
    assert "links=1" in blocks[2]
    assert "STATUS:triaged" in blocks[3] and "prio=" in blocks[3]
    # first line only — never the full render
    assert "batch gripe 1" in blocks[3] and "more body" not in body
    assert "## comment" not in body


def test_get_batch_marks_deleted_and_accepts_handles(hub: Hub) -> None:
    handler, ids = _gripes(hub, 2)
    handler.delete(id=ids[1])
    body = handler.get(id=_batch(f"gr{ids[0]}", f"gripe:{ids[1]}", str(ids[0]))).body
    assert "# 2 gripe (batch)" in body  # duplicate collapsed
    assert f"gr{ids[1]}  deleted" in body


def test_get_batch_alert_shows_state(store: Store, hub: Hub) -> None:
    a, _ = raise_alert(store, source="s", fingerprint="fp-a", title="alert A\ndetail")
    b, _ = raise_alert(store, source="s", fingerprint="fp-b", title="alert B")
    body = AlertHandler(hub=hub).get(id=_batch(a, b)).body
    assert f"al{a}  {STATE_OPEN}" in body
    assert "alert B" in body and "detail" not in body


def test_get_batch_todo_skips_ancestry(hub: Hub) -> None:
    handler = TodoHandler(hub=hub)
    root = id_of(handler.put(text="root todo").body)
    leaf = handler.put(text="leaf todo", parent_id=root).ref_id
    body = handler.get(id=_batch(leaf, root)).body
    assert body.index(f"td{leaf}") < body.index(f"td{root}")
    assert "STATUS:open" in body
    assert "ancestry" not in body.lower()


def _findings(store: Store, hub: Hub, n: int) -> tuple[Any, list[int]]:
    """Mint ``n`` chase findings off one seeded paper chunk."""
    from precis.handlers.finding import FindingHandler
    from precis.store.types import ChunkInsert

    handler = FindingHandler(hub=hub)
    paper = store.insert_ref(
        kind="paper", slug="batchsrc23a", title="batch source paper", meta={}
    )
    store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text="source body.", meta={})]
    )
    ids = [
        id_of(
            handler.put(
                title=f"batch finding {i}",
                body=f"claim body {i}",
                cited_in="batchsrc23a~0",
            ).body
        )
        for i in range(n)
    ]
    return handler, ids


def test_get_batch_job_summary_shaped(store: Store, hub: Hub) -> None:
    # gr516881: job refused id=[...] ("one call per ref") while the other
    # numeric kinds batch — now the same summary shape.
    handler = JobHandler(hub=hub)
    a = store.insert_ref(kind="job", slug=None, title="job one")
    b = store.insert_ref(kind="job", slug=None, title="job two")
    missing = max(a.id, b.id) + 100_000
    body = handler.get(id=_batch(b.id, missing, a.id)).body
    assert body.startswith("# 3 job (batch)")
    blocks = body.split("\n\n")[1:4]
    assert blocks[0].startswith(f"jo{b.id}  ")
    assert blocks[1] == f"jo{missing}  not found"
    assert blocks[2].startswith(f"jo{a.id}  ")
    assert "job two" in blocks[0] and "job one" in blocks[2]


def test_get_batch_finding_summary_in_order(store: Store, hub: Hub) -> None:
    # gr506770: reading N findings cost N get calls; now one batch.
    handler, ids = _findings(store, hub, 3)
    missing = max(ids) + 100_000
    body = handler.get(id=_batch(ids[2], missing, ids[0])).body
    assert body.startswith("# 3 finding (batch)")
    assert body.index(f"fi{ids[2]}") < body.index(f"fi{missing}  not found")
    assert body.index(f"fi{missing}  not found") < body.index(f"fi{ids[0]}")
    assert "batch finding 0" in body


def test_link_batch_finding_is_refused_with_the_single_door(
    store: Store, hub: Hub
) -> None:
    # gr506770: batch get/tag landed but link stays single-id — a fan-in
    # would bypass the per-source Taproot door.
    handler, ids = _findings(store, hub, 2)
    with pytest.raises(BadInput, match="one source id per call"):
        handler.link(id=_batch(*ids), target="me1", rel="related-to")


def test_tag_batch_finding_applies_to_all(store: Store, hub: Hub) -> None:
    handler, ids = _findings(store, hub, 2)
    body = handler.tag(id=_batch(*ids), add=["topic:batch-probe"]).body
    assert "tagged 2 finding" in body
    assert all("topic:batch-probe" in _tags(handler.store, int(rid)) for rid in ids)


def test_search_filter_accepts_an_in_use_unregistered_axis(
    store: Store, hub: Hub
) -> None:
    # gr504808: machine-stamped axes (MATERIAL:, METHOD:, …) live in the
    # tags table without a _CLOSED_VOCAB entry — a read filter naming one
    # must pass, while a truly-unknown axis still refuses (writes stay
    # strict via Tag.parse_strict). MATERIAL itself is registered since
    # ontology-v2 slice 0 (data/axes yaml) and kind-gated off gripe, so the
    # probe uses an axis no registry knows.
    handler, ids = _gripes(hub, 1)
    store.add_tag(ids[0], Tag.closed("PROBEAXIS", "mof"), set_by="agent")
    body = handler.search(q="batch gripe", tags=["PROBEAXIS:mof"]).body
    assert f"gripe {ids[0]}" in body
    with pytest.raises(BadInput, match="unknown closed-prefix axis"):
        handler.search(q="batch gripe", tags=["STATSU:open"])


def test_get_batch_cap_empty_view_and_foreign_handle(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    with pytest.raises(BadInput, match=f"at most {BATCH_ID_CAP}"):
        handler.get(id=_batch(*range(1, BATCH_ID_CAP + 2)))
    with pytest.raises(BadInput, match="empty"):
        handler.get(id=[])
    with pytest.raises(BadInput, match="todo handle, not gripe"):
        handler.get(id=_batch("td5"))
    mixed: list[Any] = [1, 2.5]
    with pytest.raises(BadInput, match=r"id\[1\] must be an integer"):
        handler.get(id=mixed)


def test_list_id_refused_cleanly_elsewhere(hub: Hub) -> None:
    with pytest.raises(BadInput, match="does not take id=") as refused:
        AnkiHandler(hub=hub).get(id=_batch(1, 2))
    assert "gripe" in str(refused.value.next) and "memory" in str(refused.value.next)
    assert "get(kind='anki', id=1)" in str(refused.value.next)
    assert "search(kind='anki'" in str(refused.value.next)
    assert "take id=[...]: " in str(refused.value.next)
    assert "anki" not in str(refused.value.next).split("take id=[...]: ")[1]
    # A non-batch kind's link still refuses a list.
    with pytest.raises(BadInput, match="does not take id="):
        AnkiHandler(hub=hub).link(id=_batch(1, 2), target="anki:3")


def test_get_batch_memory_renders_full_bodies_in_order(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    first = id_of(handler.put(text="first memory\nlong tail line one").body)
    second = id_of(handler.put(text="second memory\nlong tail line two").body)
    missing = max(first, second) + 100_000
    body = handler.get(id=_batch(second, missing, first)).body
    assert body.startswith("# 3 memory (batch)")
    assert body.index("long tail line two") < body.index(f"me{missing}  not found")
    assert body.index(f"me{missing}  not found") < body.index("long tail line one")
    assert "\n---\n" in body
    assert body.count("Next:") == 1
    assert f"get(kind='memory', id={second}, view='fisheye+1hop')" in body


def test_get_batch_memory_dedupes_title_echo(hub: Hub) -> None:
    """gr504805: bodies whose first line repeats the title (digests,
    one-liners) must not print that text twice per batch block."""
    handler = MemoryHandler(hub=hub)
    first = id_of(handler.put(text="# Digest heading\n\nDigest body text").body)
    second = id_of(handler.put(text="one liner body").body)
    body = handler.get(id=_batch(first, second)).body
    assert body.count("Digest heading") == 1
    assert "Digest body text" in body
    assert body.count("one liner body") == 1


def test_tag_batch_memory_is_one_transaction(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    ids: list[str | int] = [
        id_of(handler.put(text=f"tag batch {i}").body) for i in range(2)
    ]
    body = handler.tag(id=ids, add=["batch-probe"]).body
    assert "tagged 2 memory" in body
    assert all("batch-probe" in _tags(handler.store, int(rid)) for rid in ids)


def test_get_batch_through_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    _handler, ids = _gripes(hub, 2)
    body = runtime_with_store.dispatch("get", {"kind": "gripe", "id": ids})
    assert "# 2 gripe (batch)" in body
    assert "[error" not in body


# ── tag ────────────────────────────────────────────────────────────


def test_tag_batch_gripe_applies_to_all_with_per_id_lines(hub: Hub) -> None:
    handler, ids = _gripes(hub, 3)
    body = handler.tag(id=_batch(*ids), add=["STATUS:triaged", "PRIO:high"]).body
    assert body.startswith("tagged 3 gripe in one transaction")
    lines = body.splitlines()[1:]
    assert [ln.split(":")[0] for ln in lines] == [f"gr{i}" for i in ids]
    for rid in ids:
        assert "STATUS:triaged" in _tags(handler.store, rid)
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None and ref.prio is not None


def test_tag_batch_one_bad_id_rolls_back_all(hub: Hub) -> None:
    handler, ids = _gripes(hub, 3)
    missing = max(ids) + 100_000
    with pytest.raises(NotFound, match=f"gr{missing}: .*rolled back, nothing applied"):
        handler.tag(id=_batch(*ids, missing), add=["STATUS:triaged", "PRIO:high"])
    for rid in ids:
        tags = _tags(handler.store, rid)
        assert "STATUS:open" in tags and "STATUS:triaged" not in tags
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert (
            ref is not None and ref.prio is None
        )  # set_prio ran outside tx(), still undone


def test_tag_batch_todo_prio_and_open_tag(hub: Hub) -> None:
    handler = TodoHandler(hub=hub)
    ids = [rid for i in range(2) if (rid := handler.put(text=f"batch todo {i}").ref_id)]
    body = handler.tag(id=_batch(*ids), add=["context:batch"], prio=2).body
    assert "tagged 2 todo" in body
    for rid in ids:
        assert "context:batch" in _tags(handler.store, rid)
        ref = handler.store.get_ref(kind="todo", id=rid)
        assert ref is not None and ref.prio == 2


def test_tag_batch_alert_resolve_syncs_column(store: Store, hub: Hub) -> None:
    a, _ = raise_alert(store, source="s", fingerprint="fp-1", title="one")
    b, _ = raise_alert(store, source="s", fingerprint="fp-2", title="two")
    AlertHandler(hub=hub).tag(
        id=_batch(a, b), add=[STATE_RESOLVED], remove=[STATE_OPEN]
    )
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT resolved_at FROM refs WHERE ref_id = ANY(%s)", ([a, b],)
        ).fetchall()
    assert len(rows) == 2 and all(r[0] is not None for r in rows)
    assert STATE_RESOLVED in _tags(store, a) and STATE_OPEN not in _tags(store, b)


def test_tag_batch_rejects_bad_tag_before_any_write(hub: Hub) -> None:
    handler, ids = _gripes(hub, 2)
    with pytest.raises(BadInput):
        handler.tag(id=_batch(*ids), add=["STATUS:bogus"])
    for rid in ids:
        assert "STATUS:open" in _tags(handler.store, rid)


def test_tag_batch_through_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    handler, ids = _gripes(hub, 2)
    body = runtime_with_store.dispatch(
        "tag", {"kind": "gripe", "id": ids, "add": ["STATUS:triaged"]}
    )
    assert "tagged 2 gripe" in body and "[error" not in body
    assert all("STATUS:triaged" in _tags(handler.store, rid) for rid in ids)


# ── get with view= mapped over the batch ───────────────────────────


def test_get_batch_view_alert_detail(store: Store, hub: Hub) -> None:
    a, _ = raise_alert(store, source="s", fingerprint="fp-da", title="alert A")
    b, _ = raise_alert(store, source="s", fingerprint="fp-db", title="alert B")
    body = AlertHandler(hub=hub).get(id=_batch(a, b), view="detail").body
    assert body.startswith("# 2 alert (batch, view='detail')")
    assert f"# alert {a} [open]" in body and f"# alert {b} [open]" in body
    assert "fingerprint: fp-da" in body and "severity:" in body


def test_get_batch_view_gripe_comments_and_missing(hub: Hub) -> None:
    handler, ids = _gripes(hub, 2)
    handler.put(id=ids[0], text="note on first")
    missing = max(ids) + 100_000
    body = handler.get(id=_batch(ids[0], missing, ids[1]), view="comments").body
    assert body.startswith("# 3 gripe (batch, view='comments')")
    assert "note on first" in body
    assert f"gr{missing}  not found" in body


def test_get_batch_view_pages_whole_blocks(hub: Hub) -> None:
    """gr515524: a batch view join that would overflow the fold emits the
    whole per-id blocks that fit and names the unread ids as the next
    call — never a mid-text cut behind a more() cursor."""
    handler = GripeHandler(hub=hub)
    ids = [
        id_of(handler.put(text=f"paged batch {i}\n" + "x" * 1200).body)
        for i in range(4)
    ]
    resp = handler.get(id=_batch(*ids), view="full")
    body = resp.body
    assert body.startswith("# 4 gripe (batch, view='full')")
    assert f"# gripe {ids[0]}" in body and f"# gripe {ids[1]}" in body
    assert f"# gripe {ids[2]}" not in body and f"# gripe {ids[3]}" not in body
    assert (
        f"2 more — next: get(kind='gripe', id=[{ids[2]}, {ids[3]}], "
        "view='full')" in body
    )
    assert resp.result_count == 2
    # The named continuation is the remaining ids verbatim — re-issuing
    # it returns them whole, no cursor needed.
    rest = handler.get(id=_batch(ids[2], ids[3]), view="full").body
    assert rest.startswith("# 2 gripe (batch, view='full')")
    assert f"# gripe {ids[2]}" in rest and f"# gripe {ids[3]}" in rest
    assert "more — next:" not in rest


def test_get_batch_view_unknown_view_raises(hub: Hub) -> None:
    handler, ids = _gripes(hub, 1)
    with pytest.raises(Unsupported, match="unknown view"):
        handler.get(id=_batch(*ids), view="bogus")


def test_get_batch_view_raw_base_view(hub: Hub) -> None:
    handler, ids = _gripes(hub, 1)
    body = handler.get(id=_batch(*ids), view="raw").body
    assert "batch, view='raw'" in body


def test_get_batch_view_through_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    _handler, ids = _gripes(hub, 2)
    body = runtime_with_store.dispatch(
        "get", {"kind": "gripe", "id": ids, "view": "raw"}
    )
    assert "batch, view='raw'" in body and "[error" not in body


def test_get_batch_comments_through_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    # gr492451: the full MCP path, not just the handler, for the
    # gripe-only ``comments`` view over a batch.
    handler, ids = _gripes(hub, 2)
    handler.put(id=ids[1], text="note on second")
    body = runtime_with_store.dispatch(
        "get", {"kind": "gripe", "id": ids, "view": "comments"}
    )
    assert "batch, view='comments'" in body and "[error" not in body
    assert "note on second" in body


# ── link fan-in (``id=[...]``) ──────────────────────────────────────


def _memories(hub: Hub, n: int) -> tuple[MemoryHandler, list[int]]:
    handler = MemoryHandler(hub=hub)
    return handler, [id_of(handler.put(text=f"link batch {i}").body) for i in range(n)]


def _out_links(store: Store, ref_id: int) -> list[tuple[int, str]]:
    return [
        (lk.dst_ref_id, lk.relation) for lk in store.links_for(ref_id, direction="out")
    ]


def test_link_batch_fans_in_one_target(hub: Hub) -> None:
    handler, ids = _memories(hub, 3)
    target = id_of(handler.put(text="the shared target").body)
    body = handler.link(
        id=_batch(*ids), target=f"memory:{target}", rel="qualifies"
    ).body
    assert body == (
        f"linked 3 memory -> memory:{target} [qualifies] in one transaction"
    )
    for rid in ids:
        assert _out_links(handler.store, rid) == [(target, "qualifies")]


def test_link_batch_one_bad_id_rolls_back_all(hub: Hub) -> None:
    handler, ids = _memories(hub, 2)
    target = id_of(handler.put(text="target").body)
    missing = max(*ids, target) + 100_000
    with pytest.raises(NotFound, match="rolled back, nothing applied"):
        handler.link(id=_batch(*ids, missing), target=f"memory:{target}")
    for rid in ids:
        assert not _out_links(handler.store, rid)


def test_link_batch_remove_and_bad_mode(hub: Hub) -> None:
    handler, ids = _memories(hub, 2)
    target = id_of(handler.put(text="target").body)
    handler.link(id=_batch(*ids), target=f"memory:{target}")
    body = handler.link(id=_batch(*ids), target=f"memory:{target}", mode="remove").body
    assert body.startswith("unlinked 2 memory")
    assert "[" not in body  # no rel= → no relation claimed in the ack
    for rid in ids:
        assert not _out_links(handler.store, rid)
    with pytest.raises(BadInput, match="'add' or 'remove'"):
        handler.link(id=_batch(*ids), target=f"memory:{target}", mode="bogus")


def test_link_batch_requires_target_and_caps(hub: Hub) -> None:
    handler, ids = _memories(hub, 1)
    with pytest.raises(BadInput, match="requires target="):
        handler.link(id=_batch(*ids))
    with pytest.raises(BadInput, match=f"at most {BATCH_ID_CAP}"):
        handler.link(id=_batch(*range(BATCH_ID_CAP + 1)), target="memory:1")


def test_link_batch_through_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    handler, ids = _memories(hub, 2)
    target = id_of(handler.put(text="shared target").body)
    body = runtime_with_store.dispatch(
        "link",
        {
            "kind": "memory",
            "id": ids,
            "target": f"memory:{target}",
            "rel": "related-to",
        },
    )
    assert "linked 2 memory" in body and "[error" not in body
    for rid in ids:
        assert _out_links(handler.store, rid) == [(target, "related-to")]


# ── edit ───────────────────────────────────────────────────────────


def test_edit_batch_gripe_retitles_all(hub: Hub) -> None:
    # gr514482: edit took id=[...] as a refusal while get/tag/link
    # already batched — now the same one-transaction shape.
    handler, ids = _gripes(hub, 3)
    body = handler.edit(id=_batch(*ids), title="Area: one retitle").body
    assert body.startswith("edited 3 gripe in one transaction")
    lines = body.splitlines()[1:]
    assert [ln.split(":")[0] for ln in lines] == [f"gr{i}" for i in ids]
    for rid in ids:
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None and ref.title == "Area: one retitle"


def test_edit_batch_one_bad_id_rolls_back_all(hub: Hub) -> None:
    handler, ids = _gripes(hub, 2)
    missing = max(ids) + 100_000
    with pytest.raises(NotFound, match="rolled back, nothing applied"):
        handler.edit(id=_batch(*ids, missing), title="Area: retitle")
    for rid in ids:
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None and "batch gripe" in (ref.title or "")


def test_edit_batch_memory_replaces_bodies(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    ids = [id_of(handler.put(text=f"old body {i}").body) for i in range(2)]
    body = handler.edit(id=_batch(*ids), mode="replace", text="the new body").body
    assert body.startswith("edited 2 memory in one transaction")
    for rid in ids:
        assert "the new body" in handler.get(id=rid).body


def test_edit_batch_todo_retitles_all(hub: Hub) -> None:
    handler = TodoHandler(hub=hub)
    ids = [rid for i in range(2) if (rid := handler.put(text=f"batch todo {i}").ref_id)]
    body = handler.edit(id=_batch(*ids), mode="replace", text="one new title").body
    assert body.startswith("edited 2 todo in one transaction")
    for rid in ids:
        ref = handler.store.get_ref(kind="todo", id=rid)
        assert ref is not None and ref.title == "one new title"


def test_edit_batch_finding_rewrites_bodies(store: Store, hub: Hub) -> None:
    handler, ids = _findings(store, hub, 2)
    body = handler.edit(
        id=_batch(*ids), mode="replace", text="the corrected claim"
    ).body
    assert body.startswith("edited 2 finding in one transaction")
    for rid in ids:
        assert "the corrected claim" in handler.get(id=rid).body


def test_edit_batch_refusals(hub: Hub) -> None:
    handler, _ids = _gripes(hub, 1)
    with pytest.raises(BadInput, match="empty"):
        handler.edit(id=[], title="x")
    with pytest.raises(BadInput, match=f"at most {BATCH_ID_CAP}"):
        handler.edit(id=_batch(*range(1, BATCH_ID_CAP + 2)), title="x")
    # batch_ids doesn't grant the verb — alert has no edit at all.
    with pytest.raises(Unsupported, match="does not support edit"):
        AlertHandler(hub=hub).edit(id=_batch(1, 2))
    # A non-batch numeric kind with edit refuses the list cleanly.
    list_id: Any = _batch(
        1, 2
    )  # quest's edit signature has no list arm — that's the point
    with pytest.raises(BadInput, match="got list"):
        QuestHandler(hub=hub).edit(id=list_id, text="x")


def test_edit_batch_through_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    _handler, ids = _gripes(hub, 2)
    body = runtime_with_store.dispatch(
        "edit", {"kind": "gripe", "id": ids, "title": "Area: via dispatch"}
    )
    assert "edited 2 gripe" in body and "[error" not in body


# ── store.atomic ───────────────────────────────────────────────────


def test_store_atomic_joins_self_connected_ops(store: Store) -> None:
    """Ops that open their own pooled connection run inside the scope's
    transaction: an exception after them undoes their writes."""
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory", slug=None, title="atomic probe", conn=conn
        )
    with pytest.raises(RuntimeError, match="boom"), store.atomic():
        store.set_prio(ref.id, 3)
        store.add_tag(ref.id, Tag.open("atomic-probe"), set_by="agent")
        raise RuntimeError("boom")
    fetched = store.get_ref(kind="memory", id=ref.id)
    assert fetched is not None and fetched.prio is None
    assert "atomic-probe" not in _tags(store, ref.id)
    with store.atomic():
        store.set_prio(ref.id, 4)
    fetched = store.get_ref(kind="memory", id=ref.id)
    assert fetched is not None and fetched.prio == 4


# ── edit id=[...] uniform across kinds (gr514482) ──────────────────


def test_edit_id_list_every_handler_declares_a_mode(store: Store) -> None:
    """Parity guard over the real registry: every kind exposing ``edit``
    declares ``edit_id_list`` explicitly — ``'native'`` / ``'repeat'`` /
    ``'refuse'``. ``None`` means undeclared and fails here, so a new
    kind that adds ``edit`` must pick a policy. ``'native'`` means its
    ``edit`` really declares a list-taking ``id``; ``'repeat'`` and
    ``'refuse'`` mean it doesn't (the dispatcher owns the list path)."""
    full = boot(store=store)
    problems: list[str] = []
    for kind in sorted(full.handlers):
        handler = full.handlers[kind]
        mode = handler.edit_id_list
        if mode not in (None, "native", "repeat", "refuse"):
            problems.append(f"{kind}: edit_id_list={mode!r}")
            continue
        if not handler.spec.supports("edit"):
            continue
        if mode is None:
            problems.append(f"{kind}: exposes edit but edit_id_list is unset")
            continue
        param = inspect.signature(handler.edit).parameters.get("id")
        ann = str(param.annotation) if param is not None else "<no id param>"
        if mode == "native" and "list" not in ann:
            problems.append(f"{kind}: 'native' but edit(id: {ann})")
        if mode in ("repeat", "refuse") and "list" in ann:
            problems.append(f"{kind}: {mode!r} but edit(id: {ann})")
    assert not problems, problems


def test_edit_id_list_repeat_folder_through_dispatch(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A scalar-id kind whose edit is store-backed gets the dispatcher's
    per-id repeat — one transaction, per-id outcome lines."""
    a = store.insert_ref(kind="folder", slug=None, title="fold a", meta={})
    b = store.insert_ref(kind="folder", slug=None, title="fold b", meta={})
    body = runtime_with_store.dispatch(
        "edit", {"kind": "folder", "id": [a.id, b.id], "text": "renamed"}
    )
    assert "[error" not in body
    assert body.startswith("edited 2 folder in one transaction")
    for rid in (a.id, b.id):
        ref = store.get_ref(kind="folder", id=rid)
        assert ref is not None and ref.title == "renamed"


def test_edit_id_list_repeat_rolls_back_all(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """One bad id aborts the whole repeat — the surviving id keeps its
    old title (the per-id writes joined ``store.atomic()``)."""
    a = store.insert_ref(kind="folder", slug=None, title="keep a", meta={})
    missing = a.id + 100_000
    body = runtime_with_store.dispatch(
        "edit", {"kind": "folder", "id": [a.id, missing], "text": "renamed"}
    )
    assert "[error" in body
    assert "rolled back, nothing applied" in body
    ref = store.get_ref(kind="folder", id=a.id)
    assert ref is not None and ref.title == "keep a"


def test_edit_id_list_repeat_keeps_full_per_id_body(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A draft ``dry_run`` list edit shows each chunk's proposed diff —
    the diff lives below the reply's first line, which is all the old
    one-line-per-id shape kept (gripe:285774)."""
    proj = store.insert_ref(kind="todo", slug=None, title="proj", meta={}).id
    ref, _title = store.drafts.create_draft(name="dryb", title="T", project_ref_id=proj)
    a, b = (
        store.drafts.add_chunks(
            ref_id=ref.id, chunk_kind="paragraph", text=t, at={"last": True}
        )[0].dc
        for t in ("alpha body", "beta body")
    )
    body = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "draft",
            "id": [a, b],
            "find": "body",
            "text": "corpse",
            "dry_run": True,
        },
    )
    assert "[error" not in body
    assert body.startswith("edited 2 draft in one transaction")
    assert f"{a}:" in body and f"{b}:" in body
    assert "-alpha body" in body and "+alpha corpse" in body
    assert "-beta body" in body and "+beta corpse" in body


def test_edit_id_list_repeat_dedupes_and_caps(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    a = store.insert_ref(kind="folder", slug=None, title="fold d", meta={})
    body = runtime_with_store.dispatch(
        "edit", {"kind": "folder", "id": [a.id, a.id], "text": "once"}
    )
    assert "[error" not in body
    assert body.startswith("edited 1 folder in one transaction")
    body = runtime_with_store.dispatch(
        "edit",
        {"kind": "folder", "id": list(range(BATCH_ID_CAP + 1)), "text": "x"},
    )
    assert "[error" in body
    assert f"at most {BATCH_ID_CAP}" in body


def test_edit_id_list_repeat_dedupes_spelling_variants(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``fo5`` == ``FO5`` == ``5`` == ``'5'`` == ``'folder:5'`` — every
    spelling of one ref collapses to a single edit; a second find-
    replace pass would fail and roll the whole batch back."""
    a = store.insert_ref(kind="folder", slug=None, title="fold v", meta={})
    ids: list[Any] = [
        a.id,
        str(a.id),
        f"fo{a.id}",
        f"FO{a.id}",
        f"folder:{a.id}",
    ]
    body = runtime_with_store.dispatch(
        "edit", {"kind": "folder", "id": ids, "text": "once"}
    )
    assert "[error" not in body
    assert body.startswith("edited 1 folder in one transaction")


def test_edit_id_list_refused_on_structure(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr514482 ruling: structure's per-design in-process relax is too
    long for one transaction, so a list refuses naming the per-id call —
    before any handler work runs."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "edit", {"kind": "structure", "id": ["d1", "d2"]}
    )
    assert is_error
    assert "one id= at a time" in body
    assert "call edit once per id" in body


def test_edit_id_list_refused_on_file_kind(store: Store, tmp_path: Path) -> None:
    """File-backed kinds can't honour the rollback claim, so their
    ``edit_id_list='refuse'`` turns a list into a BadInput naming the
    per-id call — never a silent first-ref edit."""
    runtime = PrecisRuntime(
        config=PrecisConfig(), hub=boot(store=store, precis_root=str(tmp_path))
    )
    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n", encoding="utf-8")
    body, is_error = runtime.dispatch_with_status(
        "edit",
        {
            "kind": "markdown",
            "id": ["a", "b"],
            "mode": "find-replace",
            "find": "A",
            "text": "X",
        },
    )
    assert is_error
    assert "one id= at a time" in body
    assert "call edit once per id" in body
    assert "edit(kind='markdown', id='a'" in body
    assert "# A" in (tmp_path / "a.md").read_text(encoding="utf-8")
