"""Real-PG regression tests for the smartdraft route's raw SQL.

``test_smartdraft_reader.py`` runs against the web ``FakeStore``, which
does *not* parse SQL, so a bug where a user-supplied ``q=`` is bound
unescaped into an ILIKE pattern is invisible there. This exercises the
``/smartdraft/{ident}/tag-suggest`` endpoint against the live ``store``
fixture. See CLAUDE.md "psycopg % LIKE / fake-store gap".
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from precis.store import ChunkInsert
from precis.store.types import Tag
from precis_web.app import create_app
from precis_web.config import WebConfig

from .conftest import FakeRuntime


def test_tag_suggest_treats_percent_literally(store) -> None:
    """A literal ``%`` in ``q=`` must not act as an ILIKE wildcard.

    ``pct%tag`` contains the literal substring ``t%t``; ``tXt-plain``
    contains ``t`` and ``t`` separated by a non-``%`` character. Querying
    for the literal ``t%t`` must match only the former — an unescaped
    ``%`` would turn it into a "t, anything, t" wildcard and wrongly pull
    in the latter too (and must not 500)."""
    ref = store.insert_ref(kind="draft", slug="sql-tagsuggest", title="t")
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="body")])
    store.add_tag(ref.id, Tag.open("pct%tag"), pos=0)
    store.add_tag(ref.id, Tag.open("tXt-plain"), pos=0)

    app = create_app(runtime=FakeRuntime(store), web_config=WebConfig(corpus_dir=None))
    client = TestClient(app)

    resp = client.get(f"/smartdraft/{ref.id}/tag-suggest", params={"q": "t%t"})
    assert resp.status_code == 200
    assert resp.json() == {"tags": ["pct%tag"]}
