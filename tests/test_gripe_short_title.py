"""Short gripe titles — gripe system batch F slice 6.

``put`` always files; a first line over 60 chars gets a derived
``Area: slogan`` title (the body keeps the full text) and the ack says so.
``edit(kind='gripe', title=)`` rewrites the title only. The one-time
gardening pass (``precis gripes --shorten-titles``) lists by default and
records the old title in a comment when it writes.
"""

from __future__ import annotations

import pytest

from precis.cli.gripe import shorten_titles
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _gripe_title
from precis.handlers._gripe_title import TITLE_MAX, short_title
from precis.handlers.gripe import GripeHandler
from tests._gripe import insert_gripe

LONG = (
    "pcb router: copper pours vanish after a reroute when the board has "
    "two ground nets, see the attached run"
)


@pytest.fixture
def gripe(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _latest(gripe: GripeHandler) -> int:
    return int(gripe.store.list_refs(kind="gripe", limit=1)[0].id)


def _title(gripe: GripeHandler, gid: int) -> str:
    ref = gripe.store.get_ref(kind="gripe", id=gid)
    assert ref is not None
    return str(ref.title)


def _comments(gripe: GripeHandler, gid: int) -> list[str]:
    return [
        c.text
        for c in gripe.store.chunks.list_chunks_for_ref(gid)
        if c.chunk_kind == "gripe_comment"
    ]


# ── the derivation ────────────────────────────────────────────────────────


def test_a_first_line_that_fits_is_kept() -> None:
    assert short_title("router: pours vanish\n\nlong detail " * 3) is None


def test_area_prefix_kept_and_cut_to_the_first_clause() -> None:
    assert (
        short_title(LONG) == "pcb router: copper pours vanish after a reroute when the"
    )
    title = short_title(
        "ship: the gate is red - every shard times out on the slow runner pool"
    )
    assert title == "ship: the gate is red"


def test_no_boundary_cuts_at_a_word_and_never_exceeds_the_cap() -> None:
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi"
    title = short_title(words)
    assert title is not None and len(title) <= TITLE_MAX
    assert words.startswith(title) and not title.endswith(" ")
    assert len(short_title("x" * 200) or "") == TITLE_MAX


# ── put / ack ─────────────────────────────────────────────────────────────


def test_long_put_stores_a_short_title_and_the_full_body(gripe: GripeHandler) -> None:
    body = gripe.put(text=LONG).body
    gid = _latest(gripe)
    want = short_title(LONG)
    assert _title(gripe, gid) == want
    assert f'title shortened to "{want}"' in body
    assert f"edit(kind='gripe', id={gid}, title='<new title>')" in body
    full = gripe.get(id=gid, view="comments").body
    assert LONG in full


def test_short_put_keeps_its_title_and_says_nothing(gripe: GripeHandler) -> None:
    body = gripe.put(text="router: pours vanish").body
    assert _title(gripe, _latest(gripe)) == "router: pours vanish"
    assert "title shortened" not in body


def test_a_refused_title_write_still_files_the_gripe(
    gripe: GripeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The title UPDATE runs in a savepoint: when it fails (an agent_ro
    connection cannot UPDATE refs; here a NUL byte psycopg refuses), the
    gripe files with its full-text title and the ack says nothing."""
    monkeypatch.setattr(_gripe_title, "short_title", lambda _t: "bad\x00title")
    body = gripe.put(text=LONG).body
    gid = _latest(gripe)
    assert f"created gripe id={gid}" in body
    assert "title shortened" not in body
    assert _title(gripe, gid) == LONG


# ── edit ──────────────────────────────────────────────────────────────────


def test_edit_rewrites_the_title_only(gripe: GripeHandler) -> None:
    gripe.put(text=LONG)
    gid = _latest(gripe)
    dry = gripe.edit(id=gid, title="pcb: pours vanish", dry_run=True).body
    assert "nothing written" in dry
    assert _title(gripe, gid) == short_title(LONG)
    gripe.edit(id=gid, title="  pcb:   pours vanish ")
    assert _title(gripe, gid) == "pcb: pours vanish"
    assert LONG in gripe.get(id=gid, view="comments").body


def test_edit_refuses_anything_but_a_title(gripe: GripeHandler) -> None:
    gripe.put(text="router: pours vanish")
    gid = _latest(gripe)
    with pytest.raises(BadInput, match="title only; text not accepted"):
        gripe.edit(id=gid, text="new body")
    with pytest.raises(BadInput, match="title= is empty"):
        gripe.edit(id=gid, title="   ")
    with pytest.raises(BadInput, match="title only"):
        gripe.edit(id=gid)


# ── the gardening pass ────────────────────────────────────────────────────


def test_gardening_pass_lists_by_default_and_records_the_old_title(
    gripe: GripeHandler,
) -> None:
    ref = insert_gripe(gripe.store, LONG)
    gid = int(ref.id)
    want = short_title(LONG)

    dry = shorten_titles(gripe.store, apply=False, ids=[gid])
    assert dry == [
        f'gr{gid}: "{LONG[:100]}" → "{want}"',
        "1 open gripe title(s) to shorten; dry run, nothing written",
    ]
    assert _title(gripe, gid) == LONG

    done = shorten_titles(gripe.store, apply=True, ids=[gid])
    assert done[-1] == "1 open gripe title(s) to shorten; shortened 1"
    assert _title(gripe, gid) == want
    assert _comments(gripe, gid) == [f"title shortened from: {LONG}"]

    again = shorten_titles(gripe.store, apply=True, ids=[gid])
    assert again == ["0 open gripe title(s) to shorten; shortened 0"]
