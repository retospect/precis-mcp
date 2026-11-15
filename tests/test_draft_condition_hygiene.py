"""Warn-only conditions-completeness hygiene item
(docs/backlog/claim-conditions-completeness.md, step 5)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.draft import lint as _draft_lint
from precis.handlers.draft import DraftHandler

GAP = (
    "DFT calculations show introducing Pd on Cu turns the *NOH to *NHOH step "
    "from uphill 0.34 eV on Cu to downhill −0.17 eV on the Cu−Pd heterojunction."
)
CLEAN = (
    "DFT calculations at 0 V vs RHE in alkaline conditions find the *NOH to "
    "*NHOH step is downhill by 0.17 eV."
)


def _mk(hub: Hub, sentence: str) -> str:
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub
    from precis.utils import handle_registry

    hub_id = mint_hub(hub.live_store, CanonicalClaim(sentence=sentence, scope={}))
    return handle_registry.format_handle("finding", hub_id)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    d = DraftHandler(hub=hub)
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id
    d.put(id="nt", title="T", project=proj)
    return d


def _cite(draft: DraftHandler, fi: str) -> None:
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"as shown in [{fi}], the effect holds",
        at={"last": True},
    )


def _ref_id(hub: Hub) -> int:
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    return ref.id


def test_gap_hub_flagged_clean_hub_not(hub: Hub, draft: DraftHandler) -> None:
    gap_fi, clean_fi = _mk(hub, GAP), _mk(hub, CLEAN)
    _cite(draft, gap_fi)
    _cite(draft, clean_fi)
    cites = _draft_lint.find_condition_gap_cites(hub.live_store, _ref_id(hub))
    assert [f"fi{c.hub_ref_id}" for c in cites] == [gap_fi]
    out = draft.get(id="nt", view="hygiene").body
    lines = [ln for ln in out.splitlines() if "conditions:" in ln]
    print("\n".join(lines))
    assert any(
        gap_fi in ln and "(dft-step-thermo)" in ln and "no potential" in ln
        for ln in lines
    )
    assert not any(clean_fi in ln for ln in lines)
    assert any("1 bullet(s) cite 1 claim hub(s)" in ln for ln in lines)
    assert any("conditions-sweep --draft" in ln for ln in lines)


def test_elided_footer_is_capped_full_view_lists_all(
    hub: Hub, draft: DraftHandler
) -> None:
    n = 12
    for i in range(n):
        _cite(draft, _mk(hub, GAP.replace("0.34", f"0.{40 + i}")))
    outline = draft.get(id="nt").body
    per_bullet = [ln for ln in outline.splitlines() if "conditions: dc" in ln]
    assert len(per_bullet) == 8
    assert f"+{n - 8} more" in outline
    full = draft.get(id="nt", view="hygiene").body
    assert len([ln for ln in full.splitlines() if "conditions: dc" in ln]) == n


def test_no_gap_no_lines(hub: Hub, draft: DraftHandler) -> None:
    _cite(draft, _mk(hub, CLEAN))
    assert "conditions:" not in draft.get(id="nt", view="hygiene").body
