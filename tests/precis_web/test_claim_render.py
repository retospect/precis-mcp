"""``precis_web/claim_render.py::render_claim_evidence`` — the hub-derived
``status`` field (``docs/proposals/finding-trust-surfaces.md`` §3), populated
from the shared ``precis.taproot.trust.claim_trust`` derivation. DB-backed
(the ``hub``/``store`` fixtures), mirroring ``tests/test_claim_routes.py``'s
setup style — the route-level ``/claim`` tests don't assert this field (the
template never rendered it before this proposal), so it's covered directly
here.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry
from precis_web.claim_render import render_claim_evidence

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def test_render_claim_evidence_status_unverified_when_print_set_empty(hub: Hub) -> None:
    """A hub with no print-visible supporter (inflight) — the empty print
    set the ``claim`` page already renders as "no print-visible supporter
    yet" — carries the SAME derived label in ``status``, not the old
    dormant ``None``."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    head = handle_registry.format_handle("finding", claim_hub)

    data = render_claim_evidence(store, head)

    assert data is not None
    assert data["inflight"] is True
    assert data["status"] == "unverified"


def test_render_claim_evidence_status_clean_with_print_visible_supporter(
    hub: Hub,
) -> None:
    """Any print-visible supporter (an originator, or a corroborator when no
    originator has been derived yet) flips the hub-derived label to
    "clean" — hub "unsupported" is deferred (a contradictor alongside
    support is normal science, already surfaced by the evidence lists
    themselves)."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    supporter = store.insert_ref(
        kind="paper", slug="claim-render-supporter", title="A supporting paper"
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=supporter, role="corroborates"
    )
    head = handle_registry.format_handle("finding", claim_hub)

    data = render_claim_evidence(store, head)

    assert data is not None
    assert data["inflight"] is False
    assert data["status"] == "clean"
