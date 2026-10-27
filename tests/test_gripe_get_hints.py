"""Get-surface hints for kind='gripe' (call-error cluster fixes).

gr503545: the timeline selectors ``args={'comment': K}`` /
``args={'comments': K}`` are legal inside ``view='full'`` too — the
skill introduced them next to "full is the complete read", so
``view='full', args={'comments': K}`` meant "body + the newest K" and
was refused anyway. Other views still refuse (a silent drop), with a
``next=`` showing both legal shapes.

gr503549: ``view='abstract'`` (a paper/patent spelling generalised onto
gripes) reads as ``view='body'`` and ``view='tags'`` as the title card,
each named by an ``interpreted:`` line; an unknown view's ``Unsupported``
leads its ``next=`` with the nearest view when there is one.
"""

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.gripe import GripeHandler


def _gripe(handler: GripeHandler, comments: int = 3) -> int:
    handler.put(text="hint fixture body\nsecond body line")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    for i in range(1, comments + 1):
        handler.put(id=rid, text=f"comment number {i}")
    return rid


# ── gr503545: selectors inside view='full' ────────────────────────────


def test_full_with_comments_window_is_body_plus_newest_k(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    rid = _gripe(handler)
    body = handler.get(id=rid, view="full", comments=2).body
    assert "hint fixture body" in body and "second body line" in body
    assert "comments: newest 2 of 3" in body
    assert "comment number 2" in body and "comment number 3" in body
    assert "comment number 1" not in body


def test_full_with_comment_selector_is_body_plus_one_uncapped(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    rid = _gripe(handler)
    body = handler.get(id=rid, view="full", comment=2).body
    assert "hint fixture body" in body
    assert "comment 2 of 3" in body and "comment number 2" in body
    assert "## comment 1" not in body and "## comment 3" not in body


def test_full_without_selector_stays_the_uncapped_read(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    rid = _gripe(handler)
    body = handler.get(id=rid, view="full").body
    for i in (1, 2, 3):
        assert f"comment number {i}" in body
    assert "comments: newest" not in body


def test_selectors_on_other_views_refuse_naming_full(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    rid = _gripe(handler)
    for view in ("body", "links", "log"):
        with pytest.raises(BadInput) as exc:
            handler.get(id=rid, view=view, comments=2)
        assert "view='full'" in str(exc.value.next)
        assert "view='comments'" in str(exc.value.next)


# ── gr503549: abstract/tags spellings read as gripe views ────────────


def test_view_abstract_reads_as_body(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    rid = _gripe(handler)
    body = handler.get(id=rid, view="abstract").body
    assert body.startswith("interpreted: view='abstract' read as view='body'")
    assert "hint fixture body" in body and "second body line" in body


def test_view_tags_reads_as_the_title_card(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    rid = _gripe(handler)
    body = handler.get(id=rid, view="tags").body
    assert "interpreted: view='tags' read as the default view" in body
    assert "title: hint fixture body" in body
    assert "comments: 3" in body
    assert "second body line" in body  # the default read carries the body


def test_unknown_view_leads_with_the_nearest(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    rid = _gripe(handler)
    with pytest.raises(Unsupported) as near:
        handler.get(id=rid, view="bodys")
    assert "view='body'" in str(near.value.next)
    with pytest.raises(Unsupported) as exc:
        handler.get(id=rid, view="bogus")
    assert exc.value.options is not None and "full" in exc.value.options


# ── gr514629: handles quoted in a gripe name their read path ─────────
#
# A gripe's body routinely quotes the rows it is about by handle
# (``pc<N>`` a chunk, ``jo<N>`` a job, ``pa<N>`` a paper). The title
# card lists them on a ``refs in text:`` line naming
# ``get(id='<handle>')`` as the read path; a handle pasted into the
# wrong kind's ``id=`` refuses with the same call as the ``next=``.


def test_card_names_the_universal_handles_the_body_quotes(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    handler.put(
        text="re-run lost rows from table pc5155615 (job jo512227, "
        "paper pa509706); b1 and zz99 are not handles"
    )
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    body = handler.get(id=rid).body
    assert "refs in text: pc5155615, jo512227, pa509706" in body
    assert "get(id='<handle>')" in body
    assert "zz99" not in body.split("refs in text:")[1].split("—")[0]


def test_card_matches_handles_case_insensitively(hub: Hub) -> None:
    """``PC5155615`` (a shouted handle) lists the same — the registry
    folds the prefix case at resolve time."""
    handler = GripeHandler(hub=hub)
    handler.put(text="the table PC5155615 lost five rows")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    body = handler.get(id=rid).body
    assert "refs in text: PC5155615" in body


def test_card_drops_chunk_handles_with_no_handle_read(hub: Hub) -> None:
    """``gc<N>``/``jc<N>`` (numeric-kind chunks) have no ``get(id=…)``
    path — naming them under "each reads via get" would lie."""
    handler = GripeHandler(hub=hub)
    handler.put(text="comment gc12 and event jc34 plus the table pc55")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    body = handler.get(id=rid).body
    assert "refs in text: pc55" in body
    refs_line = next(l for l in body.splitlines() if l.startswith("refs in text:"))
    assert "gc12" not in refs_line and "jc34" not in refs_line


def test_card_omits_refs_line_when_no_handle_parses(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    rid = _gripe(handler)
    body = handler.get(id=rid).body
    assert "refs in text:" not in body


def test_handle_in_wrong_kinds_id_names_the_direct_get(hub: Hub) -> None:
    """``get(kind='gripe', id='pc5155615')`` (a chunk handle under the
    wrong kind=) refuses naming ``get(id='pc5155615')`` — the call that
    reads the chunk's text — instead of only the search misdirection."""
    handler = GripeHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        handler.get(id="pc5155615")
    nxt = str(exc.value.next)
    assert "get(id='pc5155615')" in nxt
    assert "search(kind='gripe'" in nxt


def test_record_handle_in_wrong_kind_names_the_direct_get(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        handler.get(id="jo512227")
    nxt = str(exc.value.next)
    assert "get(id='jo512227')" in nxt
    assert "no kind=" in nxt


def test_same_kind_record_handle_names_the_int(hub: Hub) -> None:
    """``gr5`` on kind='gripe' itself (a direct handler call — dispatch
    rewrites it to 5 upstream) names the integer, not a redirect."""
    handler = GripeHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        handler.get(id="gr42")
    assert "the id is 42" in str(exc.value.next)


def test_same_kind_chunk_handle_names_the_owning_ref(hub: Hub) -> None:
    """A ``gc<N>`` (this kind's own chunk code) has no direct handle
    read — the hint names the owning ref's views, not ``get(id=…)``."""
    handler = GripeHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        handler.get(id="gc42")
    nxt = str(exc.value.next)
    assert "owning ref" in nxt
    assert "get(id='gc42')" not in nxt


def test_other_numeric_chunk_handle_also_names_the_owning_ref(hub: Hub) -> None:
    """A ``jc<N>`` (job chunk — another no-read kind) gets the same
    honest refusal, not the ``get(id=…)`` promise."""
    handler = GripeHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        handler.get(id="jc42")
    nxt = str(exc.value.next)
    assert "owning ref" in nxt
    assert "get(id='jc42')" not in nxt


def test_no_chunk_handle_read_kinds_is_the_numeric_chunk_kinds() -> None:
    """Pin the derived set: exactly the numeric-ref kinds that expose a
    chunk code. A numeric kind that later declares
    ``supports_chunk_selectors`` must leave this set (and earns its
    ``get(id=…)`` naming back)."""
    from precis.handlers._numeric_ref import _no_chunk_handle_read_kinds

    assert _no_chunk_handle_read_kinds() == {
        "gripe",
        "message",
        "finding",
        "job",
        "quest",
        "llm",
    }


def test_non_handle_still_names_the_search_path(hub: Hub) -> None:
    """A slug-ish non-handle keeps the pre-existing search hint."""
    handler = GripeHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        handler.get(id="backlog_foo")
    assert "search(kind='gripe'" in str(exc.value.next)
    assert "get(id=" not in str(exc.value.next)
