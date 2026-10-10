"""Claim-type surfaces on the MCP side: the edit refusal, the nanopub
approve refusal and the consensus line on the evidence view
(docs/backlog/taproot-claim-model-v2.md, design point 2 and 3)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.nanopub import mint
from precis.taproot.canon import CanonicalClaim
from precis.taproot.claim_type import consensus_line
from precis.taproot.hub import mint_hub

_CLAIM = CanonicalClaim(
    sentence="Graphene composites are typically made by physical mixing.",
    scope={"material": "graphene"},
)


def _landscape_hub(store) -> int:
    hub = mint_hub(store, _CLAIM)
    store.update_ref(
        hub, meta_patch={"claim_type": "landscape", "claim_type_by": "llm"}
    )
    return hub


def test_edit_refuses_claim_type_naming_human_doors(store) -> None:
    hub = mint_hub(store, _CLAIM)
    h = FindingHandler(hub=Hub(store=store))
    with pytest.raises(BadInput, match="human door") as exc:
        h.edit(id=hub, meta={"claim_type": "landscape"})
    assert "classify" in str(exc.value.next)
    ref = store.get_ref(kind="finding", id=hub)
    assert "claim_type" not in (ref.meta or {})


def test_approve_refuses_landscape_before_publish_row(store) -> None:
    hub = _landscape_hub(store)
    with pytest.raises(BadInput, match="not publishable"):
        mint.approve(store, hub, payload={}, interactive=True)
    assert store.nanopub_publish_row(hub) is None


def test_evidence_view_shows_claim_type_header(store) -> None:
    hub = _landscape_hub(store)
    h = FindingHandler(hub=Hub(store=store))
    body = h.get(id=hub, view="evidence").body
    assert "claim type: landscape (llm)" in body


def test_consensus_line_pass_and_fail() -> None:
    assert "fail" in (consensus_line("landscape", 2) or "")
    assert "pass" in (consensus_line("landscape", 3) or "")
    assert consensus_line("measurement", 9) is None
