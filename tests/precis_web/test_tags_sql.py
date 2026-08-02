"""Real-PG regression tests for the ``/tags`` route's raw SQL.

The ``test_routes.py`` suite runs against the web ``FakeStore``, which does
*not* parse SQL, so a bug where a user-supplied ``q=`` is bound unescaped
into an ILIKE pattern is invisible there. This exercises ``_list_tags``
against the live ``store`` fixture. See CLAUDE.md "psycopg % LIKE /
fake-store gap".
"""

from __future__ import annotations

from precis.store.types import Tag
from precis_web.routes.tags import _list_tags


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
