"""Draft-write-path Taproot claim-hub cite nudge (`_draft_lint.
pc_cite_claim_hub_hint`) + the draft-level scoreboard line
(`_hygiene_lines`). DB-backed (real `refs`/`chunks`/`links`/
`ref_identifiers` via the `store`/`hub` fixtures); `seed_claim_hub` seeds
the hub + evidence, mirroring `tests/test_taproot_authoring.py`.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.store.store import Store
from precis.taproot.authoring import seed_claim_hub
from precis.utils import handle_registry
from tests.workers._helpers import seed_chunk, seed_ref


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _order(hub: Hub, slug: str) -> list:
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return hub.live_store.drafts.reading_order(ref.id)


def _seed_pc(store: Store, *, paper_ref_id: int, text: str = "supporting text") -> int:
    """Seed one real paper chunk; return its ``chunk_id`` (the ``pc<id>``
    handle body)."""
    return seed_chunk(store, ref_id=paper_ref_id, text=text)


# ── Deliverable 2: write-path nudge ──────────────────────────────────────


def test_pc_cite_hints_grounded_hub(draft: DraftHandler, hub: Hub) -> None:
    paper = seed_ref(hub.live_store, title="Top-gated AGNR FETs", kind="paper")
    chunk_id = _seed_pc(hub.live_store, paper_ref_id=paper)

    out = seed_claim_hub(
        hub.live_store,
        sentence="Top-gated 9-atom AGNR FETs reach Ion/Ioff ~1e5.",
        scope={"material": "AGNR"},
        supporters=[{"paper": paper}],
    )

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle

    r = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"On-currents are strong [pc{chunk_id}].",
        at={"after": "¶" + th},
    )

    hub_handle = handle_registry.format_handle("finding", out["hub_ref_id"])
    assert "taproot" in r.body
    assert f"pc{chunk_id}" in r.body
    assert f"[{hub_handle}]" in r.body
    assert f"[{hub_handle}>pc{chunk_id}]" in r.body
    assert "grounds claim hub" in r.body


def test_pc_cite_hint_via_edit(draft: DraftHandler, hub: Hub) -> None:
    """The nudge fires on ``edit`` (both whole-rewrite and find-replace),
    not just ``put``."""
    paper = seed_ref(hub.live_store, title="Top-gated AGNR FETs", kind="paper")
    chunk_id = _seed_pc(hub.live_store, paper_ref_id=paper)
    out = seed_claim_hub(
        hub.live_store,
        sentence="Top-gated 9-atom AGNR FETs reach Ion/Ioff ~1e5.",
        scope={},
        supporters=[{"paper": paper}],
    )

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="A placeholder sentence.",
        at={"after": "¶" + th},
    )
    dc = "dc" + str(_order(hub, "nt")[-1].chunk_id)

    hub_handle = handle_registry.format_handle("finding", out["hub_ref_id"])
    r = draft.edit(id=dc, text=f"On-currents are strong [pc{chunk_id}].")
    assert f"[{hub_handle}]" in r.body
    assert "grounds claim hub" in r.body

    # find-replace edit path too.
    r2 = draft.edit(
        id=dc,
        find="strong",
        text=f"very strong [pc{chunk_id}]",
    )
    assert f"[{hub_handle}]" in r2.body


def test_pc_cite_many_to_many_lists_both_hubs(draft: DraftHandler, hub: Hub) -> None:
    """One paper grounding TWO hubs -> the hint lists BOTH pub_ids."""
    paper = seed_ref(hub.live_store, title="Shared Corroborator", kind="paper")
    chunk_id = _seed_pc(hub.live_store, paper_ref_id=paper)

    out_a = seed_claim_hub(
        hub.live_store,
        sentence="Pd/C catalyzes Suzuki coupling at room temperature.",
        scope={"material": "Pd/C"},
        supporters=[{"paper": paper}],
    )
    out_b = seed_claim_hub(
        hub.live_store,
        sentence="Nickel foam electrodes reduce overpotential in alkaline OER.",
        scope={"material": "Ni foam"},
        supporters=[{"paper": paper}],
    )

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle

    r = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"Both effects are reported [pc{chunk_id}].",
        at={"after": "¶" + th},
    )

    hub_a_handle = handle_registry.format_handle("finding", out_a["hub_ref_id"])
    hub_b_handle = handle_registry.format_handle("finding", out_b["hub_ref_id"])
    assert f"[{hub_a_handle}]" in r.body
    assert f"[{hub_b_handle}]" in r.body


def test_pc_cite_no_hub_emits_no_hint(draft: DraftHandler, hub: Hub) -> None:
    paper = seed_ref(hub.live_store, title="Ungrounded Paper", kind="paper")
    chunk_id = _seed_pc(hub.live_store, paper_ref_id=paper)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle

    r = draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"A claim with no hub yet [pc{chunk_id}].",
        at={"after": "¶" + th},
    )

    assert "taproot" not in r.body
    assert "grounds claim hub" not in r.body


# ── Deliverable 3: draft-level scoreboard ────────────────────────────────


def test_outline_scoreboard_present_when_hub_grounded(
    draft: DraftHandler, hub: Hub
) -> None:
    paper = seed_ref(hub.live_store, title="Top-gated AGNR FETs", kind="paper")
    chunk_id = _seed_pc(hub.live_store, paper_ref_id=paper)
    seed_claim_hub(
        hub.live_store,
        sentence="Top-gated 9-atom AGNR FETs reach Ion/Ioff ~1e5.",
        scope={},
        supporters=[{"paper": paper}],
    )

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"On-currents are strong [pc{chunk_id}].",
        at={"after": "¶" + th},
    )

    out = draft.get(id="nt").body
    assert "taproot: 1 of 1 cited passages have a claim hub available" in out


def test_outline_scoreboard_absent_when_no_hub(draft: DraftHandler, hub: Hub) -> None:
    paper = seed_ref(hub.live_store, title="Ungrounded Paper", kind="paper")
    chunk_id = _seed_pc(hub.live_store, paper_ref_id=paper)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"A claim with no hub yet [pc{chunk_id}].",
        at={"after": "¶" + th},
    )

    out = draft.get(id="nt").body
    assert "taproot:" not in out


# ── gr263023: cite-time signal for a badly-postured cited hub ────────────


def test_hygiene_flags_refuted_disputed_but_not_clean_cited_hub(
    draft: DraftHandler, hub: Hub
) -> None:
    """A draft citing three hubs — one whose only evidence is a
    ``support: "no"`` verdict (refuted), one carrying a live
    ``contradicts`` edge (disputed), and one cleanly supported — surfaces
    exactly the first two by handle + condition, and says nothing about
    the clean one. Default finding search stays unfiltered by design
    (gr263023); this is the cite-time backstop that reaches the writer
    without a second lookup."""
    from precis.taproot.hub import attach_evidence

    store = hub.live_store
    paper = seed_ref(store, title="Evidence Paper", kind="paper")

    refuted_out = seed_claim_hub(
        store,
        sentence="Refuted claim: X does not affect Y.",
        scope={"material": "X"},
        supporters=[
            {
                "paper": paper,
                "support": "no",
                "support_reason": "the passage does not carry the claim",
                "verified_by": "test",
            }
        ],
    )
    refuted_id = refuted_out["hub_ref_id"]

    disputed_out = seed_claim_hub(
        store,
        sentence="Disputed claim: Z accelerates W.",
        scope={"material": "Z"},
        supporters=[],
    )
    disputed_id = disputed_out["hub_ref_id"]
    attach_evidence(
        store,
        hub_ref_id=disputed_id,
        paper_ref_id=paper,
        role="contradicts",
        meta={"support": "no"},
        set_by="system",
    )

    clean_out = seed_claim_hub(
        store,
        sentence="Clean claim: A enhances B.",
        scope={"material": "A"},
        supporters=[
            {
                "paper": paper,
                "support": "yes",
                "support_reason": "direct measurement",
                "verified_by": "test",
            }
        ],
    )
    clean_id = clean_out["hub_ref_id"]

    refuted_h = handle_registry.format_handle("finding", refuted_id)
    disputed_h = handle_registry.format_handle("finding", disputed_id)
    clean_h = handle_registry.format_handle("finding", clean_id)

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=(
            f"See [{refuted_h}], [{disputed_h}], and [{clean_h}] for "
            "the state of the art."
        ),
        at={"after": "¶" + th},
    )

    out = draft.get(id="nt").body
    assert "bad posture" in out
    assert f"{refuted_h} (refuted)" in out
    assert f"{disputed_h} (disputed)" in out
    assert clean_h not in out.split("## Hygiene")[-1]

    full = draft.get(id="nt", view="hygiene").body
    assert f"{refuted_h} (refuted)" in full
    assert f"{disputed_h} (disputed)" in full


def test_hygiene_silent_when_every_cited_hub_is_clean(
    draft: DraftHandler, hub: Hub
) -> None:
    store = hub.live_store
    paper = seed_ref(store, title="Evidence Paper", kind="paper")
    clean_out = seed_claim_hub(
        store,
        sentence="Clean claim: C stabilizes D.",
        scope={"material": "C"},
        supporters=[
            {
                "paper": paper,
                "support": "yes",
                "support_reason": "direct measurement",
                "verified_by": "test",
            }
        ],
    )
    clean_h = handle_registry.format_handle("finding", clean_out["hub_ref_id"])

    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    th = _order(hub, "nt")[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text=f"See [{clean_h}] for background.",
        at={"after": "¶" + th},
    )

    out = draft.get(id="nt").body
    assert "bad posture" not in out
