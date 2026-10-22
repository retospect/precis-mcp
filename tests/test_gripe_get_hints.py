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
    for view in (None, "body", "links", "log"):
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
    assert "second body line" not in body


def test_unknown_view_leads_with_the_nearest(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    rid = _gripe(handler)
    with pytest.raises(Unsupported) as near:
        handler.get(id=rid, view="bodys")
    assert "view='body'" in str(near.value.next)
    with pytest.raises(Unsupported) as exc:
        handler.get(id=rid, view="bogus")
    assert exc.value.options is not None and "full" in exc.value.options
