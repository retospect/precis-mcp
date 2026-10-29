"""gr524022: cite_key handling across ``dedup.merge_refs`` / ``merge_duplicate``.

The loser's live ``cite_key`` must keep resolving after the fold —
re-homed on the survivor as a ``cite_key_alias`` redirect — and a
junk-mint survivor key adopts the loser's real key outright, even when
the fold didn't inherit the byline.
"""

from __future__ import annotations

from precis.ingest.dedup import merge_refs
from precis.store import Store


def _seed_paper(store: Store, slug: str = "wang2020state") -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=f"Test paper {slug}",
        provider="manual",
        meta={},
    )
    return ref.id


class TestMergeCiteKey:
    def test_merge_rehomes_victim_cite_key_as_alias(self, store: Store) -> None:
        """gr524022: the victim's live cite_key survives on the tombstone
        as a ``cite_key_alias`` redirect to the survivor — old
        ``\\cite{<victim key>}`` references keep resolving."""
        survivor = _seed_paper(store, slug="realkey2033")
        victim = _seed_paper(store, slug="victimkey2033")
        merge_refs(store, victim, survivor)
        assert store.find_ref_by_identifier("cite_key", "victimkey2033") is None
        assert (
            store.find_ref_by_identifier("cite_key_alias", "victimkey2033") == survivor
        )
        got = store.get_ref(kind="paper", id="victimkey2033")
        assert got is not None and got.id == survivor

    def test_merge_junk_survivor_key_adopts_victims(self, store: Store) -> None:
        """gr524022: a survivor holding a junk-mint key takes the
        victim's non-junk key outright — even when the survivor already
        had a byline (the adoption used to fire only when authors were
        inherited in the same fold)."""
        survivor = _seed_paper(store, slug="anon88")
        victim = _seed_paper(store, slug="victimkey2034")
        store.set_paper_authors(
            survivor,
            [{"given": "Katta", "family": "Murty"}],
            source="human",
        )
        merge_refs(store, victim, survivor)
        assert store.find_ref_by_identifier("cite_key", "victimkey2034") == survivor
        assert store.find_ref_by_identifier("cite_key_alias", "anon88") == survivor
