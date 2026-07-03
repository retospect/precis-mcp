"""Merged/superseded handle redirect (B) + bare-numeric ref_id fallback (A1).

Both features fix confusion mined from prod plan_tick transcripts:

* **B** — after a dedup merge the loser carries ``meta.superseded_by`` and is
  soft-deleted; a handle / link to it must *transparently* resolve to the live
  survivor (not hard-fail ``NotFound``) and nudge the agent to adopt the
  survivor handle.
* **A1** — a slug-addressed kind (paper/draft/…) passed a *bare number* (the
  ``pa`` prefix stripped off a handle) resolves it as the ref_id it almost
  certainly is, and admonishes so the habit — and bare numbers in cited text —
  doesn't take hold.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers._link_target import parse_link_target
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.store import Store


def _seed_paper(store: Store, slug: str) -> int:
    return store.insert_ref(
        kind="paper", slug=slug, title=f"Test paper {slug}", provider="manual", meta={}
    ).id


def _merge(store: Store, loser: int, survivor: int) -> None:
    """Mirror ``ingest/dedup.merge_duplicate``'s tombstone: pointer + delete."""
    store.stamp_ref_meta(loser, {"superseded_by": survivor, "dedup": "test"})
    store.soft_delete_ref(loser)


# ── B: follow_supersede + resolve_handle redirect ──────────────────────


def test_follow_supersede_returns_survivor(store: Store) -> None:
    survivor = _seed_paper(store, "survivor2020")
    loser = _seed_paper(store, "loser2020")
    _merge(store, loser, survivor)
    assert store.follow_supersede(loser) == survivor
    # A live ref is not a tombstone → None (nothing to follow).
    assert store.follow_supersede(survivor) is None


def test_resolve_handle_redirects_merged(store: Store) -> None:
    survivor = _seed_paper(store, "survivor2021")
    loser = _seed_paper(store, "loser2021")
    _merge(store, loser, survivor)
    resolved = store.resolve_handle(f"pa{loser}")
    assert resolved is not None
    assert resolved.ref_id == survivor
    assert resolved.redirected_from == f"pa{loser}"
    # The live survivor's own handle resolves with no redirect marker.
    live = store.resolve_handle(f"pa{survivor}")
    assert live is not None and live.redirected_from is None


def test_resolve_handle_chain(store: Store) -> None:
    """A→B→C supersede chain resolves to the final live survivor."""
    final = _seed_paper(store, "final2022")
    mid = _seed_paper(store, "mid2022")
    first = _seed_paper(store, "first2022")
    _merge(store, mid, final)
    _merge(store, first, mid)
    resolved = store.resolve_handle(f"pa{first}")
    assert resolved is not None and resolved.ref_id == final


def test_link_target_redirects_merged_prefixed(store: Store, hub: Hub) -> None:
    survivor = _seed_paper(store, "survivor2023")
    loser = _seed_paper(store, "loser2023")
    _merge(store, loser, survivor)
    with hub.request_scope():
        tgt = parse_link_target(f"paper:pa{loser}", store=store, hub=hub)
        assert tgt.ref_id == survivor
        assert tgt.redirected_from is not None
        hints = hub.hints.collect()
    assert any("merged into" in h.text for h in hints)


def test_link_target_redirects_bare_handle(store: Store) -> None:
    survivor = _seed_paper(store, "survivor2024")
    loser = _seed_paper(store, "loser2024")
    _merge(store, loser, survivor)
    tgt = parse_link_target(f"pa{loser}", store=store)
    assert tgt.ref_id == survivor


def test_link_target_still_raises_when_no_survivor(store: Store) -> None:
    """A soft-deleted ref with *no* ``superseded_by`` still hard-fails."""
    dead = _seed_paper(store, "dead2025")
    store.soft_delete_ref(dead)
    with pytest.raises(NotFound):
        parse_link_target(f"paper:pa{dead}", store=store)


# ── A1: bare-numeric ref_id fallback + admonish hint ───────────────────


def test_bare_numeric_resolves_paper(store: Store) -> None:
    ref_id = _seed_paper(store, "abazari2024design")
    # A bare number (prefix stripped) resolves to the ref_id it must be.
    ref = resolve_live_slug_ref(store, kind="paper", id=str(ref_id))
    assert ref.id == ref_id


def test_bare_numeric_emits_admonish_hint(store: Store, hub: Hub) -> None:
    ref_id = _seed_paper(store, "miller2023window")
    with hub.request_scope():
        resolve_live_slug_ref(store, kind="paper", id=str(ref_id), hub=hub)
        hints = hub.hints.collect()
    assert any(h.level == "warn" and "not a citation" in h.text for h in hints)


def test_bare_numeric_unknown_id_still_not_found(store: Store) -> None:
    with pytest.raises(NotFound):
        resolve_live_slug_ref(store, kind="paper", id="99999999")
