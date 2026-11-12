"""gr635829: the ``title=`` near-miss folds onto ``text``'s first line.

``title`` is a put/edit core param the slim schema advertises on every
kind; callers generalise it onto kinds whose title IS the first line of
``text=`` (todo, taxon, concept, …) — one prod session re-issued
``put(kind='taxon', title=…, text=…)`` five times, refused at the kwargs
gate every time (``does not accept ['title']``) despite the "the title
is the first line of text=" hint. On a kind whose handler declares
``text`` but not ``title`` the kwarg has exactly one reading, so
:func:`precis.runtime.dispatch._fold_title_kwarg` rewrites it before
either kwargs gate: a lone ``title`` becomes the whole ``text``, a
``text`` that already opens with the complete title (not a bare
prefix: ``'cat'`` inside ``'catalog'`` is a different title) drops the
redundant kwarg, and any
other ``text`` gets it prepended as the new first line. A handler that
declares ``title`` itself (memory, gripe, finding, …) keeps it
untouched.
"""

from __future__ import annotations

from precis.runtime import PrecisRuntime
from precis.store import Store
from tests.conftest import id_of


def test_put_title_only_becomes_the_text(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The gripe-adjacent minimum: ``title=`` alone on a text-kind is
    the whole first line — todo's title slot."""
    body = runtime_with_store.dispatch("put", {"kind": "todo", "title": "zzq titled"})
    assert "[error" not in body, body
    ref = store.get_ref(kind="todo", id=id_of(body))
    assert ref is not None and ref.title == "zzq titled"


def test_put_title_with_body_lands_title_and_details(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``title=`` + ``body=`` on todo reads exactly like the handler's
    own ``text=`` + ``body=``."""
    body = runtime_with_store.dispatch(
        "put", {"kind": "todo", "title": "zzq titled", "body": "the details"}
    )
    assert "[error" not in body, body
    card = runtime_with_store.dispatch(
        "get", {"kind": "todo", "id": id_of(body), "view": "full"}
    )
    assert "zzq titled" in card, card
    assert "the details" in card, card


def test_put_taxon_title_plus_text_keeps_the_text_verbatim(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The gripe's exact shape: ``text`` already opens with the title,
    so the kwarg is redundant — dropped, not duplicated into the
    definition."""
    body = runtime_with_store.dispatch(
        "put",
        {
            "kind": "taxon",
            "title": "zzq fold term",
            "text": "zzq fold term — the gloss a first line already carries",
        },
    )
    assert "[error" not in body, body
    ref = store.get_ref(kind="taxon", id=id_of(body))
    assert ref is not None
    assert ref.title == "zzq fold term"
    assert (ref.meta or {}).get("definition") == (
        "the gloss a first line already carries"
    )


def test_put_taxon_title_prefix_of_a_longer_word_is_not_redundant(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """td651769 review: ``title='cat'`` against ``text='catalog — …'``
    is a bare string prefix, not an opening title — the fold prepends
    so the requested title stands instead of silently creating
    'catalog'."""
    body = runtime_with_store.dispatch(
        "put",
        {
            "kind": "taxon",
            "title": "zzq cat",
            "text": "zzq catalog — the gloss under a longer first word",
        },
    )
    assert "[error" not in body, body
    ref = store.get_ref(kind="taxon", id=id_of(body))
    assert ref is not None
    assert ref.title == "zzq cat"
    assert (ref.meta or {}).get("definition") == (
        "zzq catalog — the gloss under a longer first word"
    )


def test_put_taxon_title_prepends_as_first_line(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``text`` that does not open with the title gets it prepended —
    both values land, nothing collapses silently."""
    body = runtime_with_store.dispatch(
        "put",
        {
            "kind": "taxon",
            "title": "zzq prepend term",
            "text": "a gloss that never named the node",
        },
    )
    assert "[error" not in body, body
    ref = store.get_ref(kind="taxon", id=id_of(body))
    assert ref is not None
    assert ref.title == "zzq prepend term"
    assert (ref.meta or {}).get("definition") == "a gloss that never named the node"


def test_put_title_inside_args_folds_the_same(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The ``args={'title': …}`` spelling folds identically — the same
    near-miss one layer down."""
    body = runtime_with_store.dispatch(
        "put", {"kind": "todo", "__extras__": {"title": "zzq args titled"}}
    )
    assert "[error" not in body, body
    ref = store.get_ref(kind="todo", id=id_of(body))
    assert ref is not None and ref.title == "zzq args titled"


def test_put_title_on_a_title_kind_is_untouched(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``kind='memory'`` declares ``title`` itself — the fold must leave
    it alone (no prepending into the body)."""
    body = runtime_with_store.dispatch(
        "put", {"kind": "memory", "title": "zzq mem title", "text": "the body"}
    )
    assert "[error" not in body, body
    ref = store.get_ref(kind="memory", id=id_of(body))
    assert ref is not None and ref.title == "zzq mem title"


def test_edit_title_folds_onto_the_text_slot(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """edit advertises the same core ``title=``; on todo it folds onto
    ``text=`` — the retitle slot."""
    rid = id_of(runtime_with_store.dispatch("put", {"kind": "todo", "text": "old"}))
    body = runtime_with_store.dispatch(
        "edit", {"kind": "todo", "id": rid, "title": "zzq retitled"}
    )
    assert "[error" not in body, body
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.title == "zzq retitled"
