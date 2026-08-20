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
