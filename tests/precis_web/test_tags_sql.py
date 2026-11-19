"""Real-PG regression tests for the ``/tags`` helpers' raw SQL.

The ``test_routes.py`` suite runs against the web ``FakeStore``, which does
*not* parse SQL, so a bug in the chip-resolution probe is invisible there.
This exercises ``resolve_tag_chip`` / ``count_tag_refs`` against the live
``store`` fixture. (The ``/tags`` leaderboard itself moved into the drive's
``k=tag`` view — gr636493; ``_list_tags`` went with it.)
"""

from __future__ import annotations

from precis.store.types import Tag
from precis_web.routes.tags import count_tag_refs, resolve_tag_chip


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
