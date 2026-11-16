"""gr572710: gripe list rows and get footers spend their bytes on signal.

A 100-row ``search(kind='gripe', status='open')`` page spent most of its
~15 kB repeating the constant kind and up to four bracketed tags plus a
``+N more`` tail per row. Now the single-kind hits table drops the
``kind`` column and a gripe row's tag cell shows only its ``cluster:``
chip — the tag the listing groups by.

And the reads the listing hands off to: a short gripe's card footer
leads with ``view='full'`` — the one-call complete read a bare get →
view='body' → view='comments' chain was re-deriving in three calls; a
longer gripe's footer still names it, last. The batch-get footer names
``view='full'`` too — its summary blocks are titles only.
"""

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.runtime import PrecisRuntime
from precis.store import Tag
from tests._refs import insert_gripe
from tests.conftest import id_of


def test_hits_table_drops_the_kind_column(hub: Hub) -> None:
    """A single-kind listing no longer repeats the literal kind on every
    row — the kind-qualified handle already carries it."""
    handler = GripeHandler(hub=hub)
    gid = id_of(handler.put(text="kind column probe").body)
    body = handler.search(tags=["STATUS:open"]).body
    header = next(ln for ln in body.splitlines() if ln.startswith("{"))
    assert header.startswith("{id\t")
    assert "kind" not in header.split("\t")
    row = next(ln for ln in body.splitlines() if ln.startswith(f"gr{gid}\t"))
    assert not row.startswith("gripe\t")


def test_row_tag_cell_is_the_cluster_chip_only(hub: Hub) -> None:
    """The gripe row's tag cell caps to ``cluster:`` — every other chip
    and the ``+N more`` tail is dropped before the cap counts."""
    handler = GripeHandler(hub=hub)
    gid = int(insert_gripe(handler.store, "cluster probe\nbody").id)
    for t in ("cluster:byte-diet", "size:M", "theme:pipeline", "flood", "extra:e1"):
        handler.store.add_tag(gid, Tag.open(t), set_by="system")
    handler.store.add_tag(gid, Tag.closed("PRIO", "high"), set_by="system")
    body = handler.search(tags=["STATUS:open"]).body
    row = next(ln for ln in body.splitlines() if ln.startswith(f"gr{gid}\t"))
    assert "[cluster:byte-diet]" in row
    for gone in ("[size:M]", "[theme:", "[flood]", "[extra", "[PRIO", "more"):
        assert gone not in row


def test_ranked_hit_row_caps_chips_to_cluster(hub: Hub) -> None:
    """The same keep-list holds on a ``q=`` hit's ``##`` line."""
    handler = GripeHandler(hub=hub)
    gid = id_of(handler.put(text="ranked chips probe body").body)
    handler.store.add_tag(gid, Tag.open("cluster:ranked-probe"), set_by="system")
    handler.store.add_tag(gid, Tag.open("size:M"), set_by="system")
    body = handler.search(q="ranked chips probe").body
    line = next(ln for ln in body.splitlines() if ln.startswith("## "))
    assert f"gr{gid}" in line or f"gripe {gid}" in line
    assert "[cluster:ranked-probe]" in line
    assert "[size:M]" not in line


def test_short_gripe_card_leads_with_full(hub: Hub) -> None:
    """``view='full'`` is the complete read here — the footer leads with
    it instead of sending the agent to 'body' and 'comments' apart."""
    handler = GripeHandler(hub=hub)
    rid = id_of(handler.put(text="short probe body").body)
    handler.put(id=rid, text="one comment")
    body = handler.get(id=rid).body
    assert "comments: 1 · all: view='full' · body: view='body'" in body


def test_commentless_gripe_card_also_leads_with_full(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    rid = id_of(handler.put(text="no comments here").body)
    body = handler.get(id=rid).body
    assert "comments: 0 · all: view='full'" in body


def test_long_body_gripe_card_names_full_last(hub: Hub) -> None:
    """A body 'full' would clip is not the complete read — the footer
    names it last, after the focused reads."""
    handler = GripeHandler(hub=hub)
    rid = id_of(handler.put(text="pad\n" + "pad " * 900 + "tail").body)
    body = handler.get(id=rid).body
    assert "timeline: view='comments' · all: view='full'" in body
    assert "all: view='full' · body:" not in body


def test_comment_heavy_gripe_card_names_full_last(hub: Hub) -> None:
    """Comments past the 'full' page push 'full' to the tail the same
    way — the elided read is not the complete one."""
    handler = GripeHandler(hub=hub)
    rid = id_of(handler.put(text="small body").body)
    for i in range(60):
        handler.put(id=rid, text=f"comment {i} " + "x" * 60)
    body = handler.get(id=rid).body
    assert "timeline: view='comments' · all: view='full'" in body


def test_batch_get_footer_names_view_full(hub: Hub) -> None:
    """The batch read's summary blocks are titles only — the read-one
    hint names the complete read, not the card."""
    handler = GripeHandler(hub=hub)
    a = id_of(handler.put(text="batch probe a").body)
    b = id_of(handler.put(text="batch probe b").body)
    body = handler.get(id=[a, b]).body
    assert f"get(kind='gripe', id={a}, view='full')" in body


@pytest.mark.parametrize("view", [None, "list"])
@pytest.mark.parametrize("q", [None, "prefix override probe"])
def test_tag_prefixes_override(hub: Hub, view: str | None, q: str | None) -> None:
    handler = GripeHandler(hub=hub)
    gid = id_of(handler.put(text="prefix override probe").body)
    for tag in ("thread:x", "fix:code", "cluster:y"):
        handler.store.add_tag(gid, Tag.open(tag), set_by="system")
    default = handler.search(q=q, view=view).body
    assert "[cluster:y]" in default
    assert "[thread:x]" not in default
    body = handler.search(q=q, view=view, tag_prefixes=" thread:,fix ").body
    assert "[thread:x]" in body
    assert "[fix:code]" in body
    assert "[cluster:y]" not in body
    assert "[thread:x]" not in handler.search(q=q, view=view).body


@pytest.mark.parametrize("value", ["", " ", None, [], 12, "thread,,fix"])
def test_tag_prefixes_bad_input(hub: Hub, value: Any) -> None:
    handler = GripeHandler(hub=hub)
    with pytest.raises(BadInput, match="tag_prefixes"):
        handler.search(tag_prefixes=value)
    with pytest.raises(BadInput, match="tag_prefixes"):
        handler.get(id=[1, 2], tag_prefixes=value)


def test_tag_prefixes_through_dispatch(
    hub: Hub, runtime_with_store: PrecisRuntime
) -> None:
    handler = GripeHandler(hub=hub)
    gid = id_of(handler.put(text="dispatch prefix probe").body)
    handler.store.add_tag(gid, Tag.open("reto:ask"), set_by="system")
    for verb, params in (
        ("search", {"tags": ["STATUS:open"]}),
        ("get", {"id": [gid]}),
    ):
        body = runtime_with_store.dispatch(
            verb, {"kind": "gripe", **params, "args": {"tag_prefixes": "reto"}}
        )
        assert "[reto:ask]" in body
