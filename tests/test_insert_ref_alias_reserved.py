"""insert_ref never hands out a key held as a cite_key_alias (gr646156)."""

from __future__ import annotations

import pytest

from precis.errors import Conflict
from precis.store.store import Store


def test_insert_ref_refuses_alias_reserved_slug(store: Store) -> None:
    old = store.insert_ref(kind="paper", slug="anonres24d", title="Old")
    store.set_ref_identifier(old.id, "cite_key_alias", "anonres24d", source="test")
    store.set_ref_identifier(old.id, "cite_key", "realres24", source="test")

    with pytest.raises(Conflict, match="cite_key_alias"):
        store.insert_ref(kind="paper", slug="anonres24d", title="New")

    assert store.find_ref_by_identifier("cite_key", "anonres24d") is None
    assert store.find_ref_by_identifier("cite_key_alias", "anonres24d") == old.id


def test_insert_ref_free_slug_unaffected(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="freeres24", title="Free")
    assert ref.slug == "freeres24"
