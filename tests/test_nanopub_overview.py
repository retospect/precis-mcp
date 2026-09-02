"""``precis.nanopub.overview.hub_rows`` — the claim-hub definition it must
agree with ``taproot.canon.block``/``hub.mint_hub`` on
(docs/backlog/claim-hub-definition-divergence.md). DB-backed via the
``store`` fixture; no LLM."""

from __future__ import annotations

from typing import Any

from precis.nanopub.overview import hub_rows
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from tests.workers._helpers import seed_ref


def test_hub_rows_excludes_taproot_claim_without_status_canonical(store: Any) -> None:
    """A finding carrying ``TAPROOT:claim`` but ``STATUS:established`` (a
    chase-tree finding mid-lifecycle, never minted through ``mint_hub``) is
    not a hub and must not appear in the queue table with a publish
    posture it can never have."""
    chase_ref = seed_ref(store, title="chase finding", kind="finding")
    store.add_tag(chase_ref, Tag.closed("TAPROOT", "claim"), set_by="system")
    store.add_tag(chase_ref, Tag.closed("STATUS", "established"), set_by="system")

    rows = hub_rows(store)

    assert chase_ref not in [r.ref_id for r in rows]


def test_hub_rows_includes_a_properly_minted_hub(store: Any) -> None:
    """A properly minted hub (``TAPROOT:claim`` + ``STATUS:canonical``) is
    still returned, unminted (no publish row)."""
    hub = mint_hub(store, CanonicalClaim(sentence="a minted hub claim", scope={}))

    rows = hub_rows(store)

    ids = [r.ref_id for r in rows]
    assert hub in ids
    row = next(r for r in rows if r.ref_id == hub)
    assert row.state is None


# ── ``tagline`` (precis.workers.hub_tagline) — presentation metadata on
# ``refs.meta``, threaded onto the overview row for the /nanopub forest. ──


def test_hub_rows_tagline_none_when_unset(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence="a taglineless hub claim", scope={}))

    rows = hub_rows(store)

    row = next(r for r in rows if r.ref_id == hub)
    assert row.tagline is None


def test_hub_rows_carries_tagline_from_meta(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence="a tagline-bearing claim", scope={}))
    store.update_ref(hub, meta_patch={"tagline": "Pd/C is Suzuki catalyst"})

    rows = hub_rows(store)

    row = next(r for r in rows if r.ref_id == hub)
    assert row.tagline == "Pd/C is Suzuki catalyst"


# ── ``open_disputes_count`` (D1, docs/backlog/
# disputes-edge-nonblocking-disagreement.md) — the non-blocking
# `disputes` complement to `disputed`/`disputed_since` above. ──────────


def test_hub_rows_open_disputes_count_counts_both_directions(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence="a disputed-open claim", scope={}))
    inbound = seed_ref(store, title="questions this claim", kind="finding")
    outbound = seed_ref(store, title="this claim questions that one", kind="finding")
    store.add_link(src_ref_id=inbound, dst_ref_id=hub, relation="disputes")
    store.add_link(src_ref_id=hub, dst_ref_id=outbound, relation="disputes")

    rows = hub_rows(store)

    row = next(r for r in rows if r.ref_id == hub)
    assert row.open_disputes_count == 2


def test_hub_rows_open_disputes_never_sets_disputed(store: Any) -> None:
    """`disputes` is the non-blocking open question — a hub touched only
    by a live `disputes` edge must NOT read `disputed`/`disputed_since`,
    which key on the adjudicated, blocking `contradicts` shape alone."""
    hub = mint_hub(
        store, CanonicalClaim(sentence="another open-question claim", scope={})
    )
    other = seed_ref(store, title="an open question", kind="finding")
    store.add_link(src_ref_id=other, dst_ref_id=hub, relation="disputes")

    rows = hub_rows(store)

    row = next(r for r in rows if r.ref_id == hub)
    assert row.open_disputes_count == 1
    assert row.disputed is False
    assert row.disputed_since is None
