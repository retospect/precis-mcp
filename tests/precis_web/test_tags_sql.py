"""Real-PG regression tests for the ``/tags`` route's raw SQL.

The ``test_routes.py`` suite runs against the web ``FakeStore``, which does
*not* parse SQL, so a bug where a user-supplied ``q=`` is bound unescaped
into an ILIKE pattern is invisible there. This exercises ``_list_tags``
against the live ``store`` fixture. See CLAUDE.md "psycopg % LIKE /
fake-store gap".
"""

from __future__ import annotations

from precis.store.types import Tag
from precis_web.routes.tags import _list_tags, count_tag_refs, resolve_tag_chip


def test_list_tags_treats_percent_and_underscore_literally(store) -> None:
    """A literal ``%``/``_`` in ``q=`` must not act as an ILIKE wildcard —
    it should match only tags that actually contain that character, not
    every tag in the corpus."""
    ref = store.insert_ref(kind="memory", slug=None, title="t", meta={})
    store.add_tag(ref.id, Tag.open("pct%tag"))
    store.add_tag(ref.id, Tag.open("under_score"))
    store.add_tag(ref.id, Tag.open("plain"))

    percent_rows = _list_tags(store, "%", limit=50)
    assert {r["label"] for r in percent_rows} == {"OPEN:pct%tag"}

    underscore_rows = _list_tags(store, "_", limit=50)
    assert {r["label"] for r in underscore_rows} == {"OPEN:under_score"}


def test_list_tags_empty_query_lists_everything(store) -> None:
    """Sanity check the escaping didn't break the ``q=''`` "show all" path."""
    ref = store.insert_ref(kind="memory", slug=None, title="t", meta={})
    store.add_tag(ref.id, Tag.open("plain-one"))
    store.add_tag(ref.id, Tag.open("plain-two"))

    rows = _list_tags(store, "", limit=50)
    labels = {r["label"] for r in rows}
    assert {"OPEN:plain-one", "OPEN:plain-two"} <= labels


def test_resolve_tag_chip_mirrors_tag_parse(store) -> None:
    """gr575024: rendered chip text is ambiguous — ``section:index``
    lives on the OPEN pile (``Tag.parse`` sends a lowercase-prefixed
    spelling there), ``SPACE:repo-dev`` on the closed one. The stored
    row decides, OPEN probed first for a lowercase prefix."""
    ref = store.insert_ref(kind="memory", slug=None, title="t", meta={})
    store.add_tag(ref.id, Tag.open("section:index"))
    store.add_tag(ref.id, Tag.closed("SPACE", "repo-dev"))
    other = store.insert_ref(kind="todo", slug=None, title="u", meta={})
    store.add_tag(other.id, Tag.closed("SPACE", "repo-dev"))
    store.retire_ref(other.id)

    assert resolve_tag_chip(store, "section:index") == ("OPEN", "section:index")
    assert resolve_tag_chip(store, "SPACE:repo-dev") == ("SPACE", "repo-dev")
    assert resolve_tag_chip(store, "nope:not-a-tag") is None
    # the count is live, viewer-visible refs only — the retired one drops
    assert count_tag_refs(store, "OPEN", "section:index", None) == 1
    assert count_tag_refs(store, "SPACE", "repo-dev", None) == 1
