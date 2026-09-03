"""``view='claims'`` — the Taproot claim hubs a paper grounds.

Distinct from the patent kind's ``view='claims'`` (a patent's own legal
claim set) — same name, disjoint per-kind meaning. DB-backed: real
``refs``/``links``/``ref_tags``/``ref_identifiers`` via ``seed_claim_hub``
and ``attach_evidence``, mirroring ``tests/test_taproot_cite_hint.py``.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.paper import PaperHandler
from precis.store import Store
from precis.taproot.authoring import seed_claim_hub
from precis.taproot.hub import attach_evidence
from precis.tools.command_parser import parse_command
from precis.utils import handle_registry
from tests.hintcheck import extract_hints
from tests.workers._helpers import seed_ref


def _handler(store: Store) -> PaperHandler:
    hub = Hub(store=store, embedder=MockEmbedder(dim=1024))
    return PaperHandler(hub=hub)


def test_claims_view_no_hubs_points_at_mint_affordance(store: Store) -> None:
    paper = seed_ref(store, title="Ungrounded Paper", kind="paper")
    pa = handle_registry.format_handle("paper", paper)

    out = _handler(store).get(id=pa, view="claims").body

    assert "no claim hubs" in out
    assert "supporters=" in out
    assert pa in out
    # The mint-affordance's
    # ``'source_handle': '<pc_id>'`` stays a template (this page has no
    # pc handle to substitute — an empty-state page), but it must still
    # PARSE as a template (angle-bracket placeholder, quoted — not a
    # bare, unparseable ``id=<N>`` bareword), and the description now
    # says where a real pc<id> comes from.
    hints = extract_hints(out)
    assert hints, "no hint found on the claims empty-state page"
    put_hint = next(h for h in hints if h.startswith("put("))
    verb, kwargs = parse_command(put_hint)  # must not raise
    assert verb == "put"
    assert kwargs["supporters"][0]["source_handle"] == "<pc_id>"
    assert "get a pc<id> chunk handle from" in out
    assert "view='toc'" in out  # pointer to where a pc<id> comes from


def test_claims_view_lists_grounded_hub_with_posture(store: Store) -> None:
    paper = seed_ref(store, title="Grounding Paper", kind="paper")
    pa = handle_registry.format_handle("paper", paper)

    out = seed_claim_hub(
        store,
        sentence="Top-gated 9-atom AGNR FETs reach Ion/Ioff ~1e5.",
        scope={"material": "AGNR"},
        supporters=[
            {
                "paper": paper,
                "support": "yes",
                "support_reason": "direct measurement",
                "verified_by": "test",
            }
        ],
    )
    hub_handle = handle_registry.format_handle("finding", out["hub_ref_id"])

    body = _handler(store).get(id=pa, view="claims").body

    assert f"[{hub_handle}]" in body
    assert "corroborates" in body
    assert "Top-gated 9-atom AGNR FETs" in body


def test_claims_view_contradicting_edge_renders_distinct_role(store: Store) -> None:
    supporting_paper = seed_ref(store, title="Supporting Paper", kind="paper")
    contradicting_paper = seed_ref(store, title="Contradicting Paper", kind="paper")
    pa_contradictor = handle_registry.format_handle("paper", contradicting_paper)

    out = seed_claim_hub(
        store,
        sentence="Disputed claim: Z accelerates W.",
        scope={"material": "Z"},
        supporters=[{"paper": supporting_paper}],
    )
    attach_evidence(
        store,
        hub_ref_id=out["hub_ref_id"],
        paper_ref_id=contradicting_paper,
        role="contradicts",
        meta={"support": "no"},
        set_by="system",
    )
    hub_handle = handle_registry.format_handle("finding", out["hub_ref_id"])

    body_supporter = (
        _handler(store)
        .get(id=handle_registry.format_handle("paper", supporting_paper), view="claims")
        .body
    )
    body_contradictor = _handler(store).get(id=pa_contradictor, view="claims").body

    assert "establishes" in body_supporter or "corroborates" in body_supporter
    assert "contradicts" in body_contradictor
    assert f"[{hub_handle}]" in body_contradictor
    # The disputed flag on the hub's own posture shows up in both readings
    # (posture is a property of the hub, not of which edge you're reading).
    assert "disputed" in body_supporter
    assert "disputed" in body_contradictor


def test_claims_view_ungrounded_pub_id_hub_marked_uncited(store: Store) -> None:
    """A hub this paper grounds but that hasn't been assigned a ``pub_id``
    yet is still shown here (unlike the cite-time nudge, which drops it) —
    marked so a reader doesn't try to cite it."""
    paper = seed_ref(store, title="Frontier Paper", kind="paper")
    pa = handle_registry.format_handle("paper", paper)

    seed_claim_hub(
        store,
        sentence="Frontier claim: A enables B.",
        scope={"material": "A"},
        supporters=[{"paper": paper}],
    )

    # seed_claim_hub always assigns a pub_id in the normal mint path, so
    # strip it here to exercise the require_pub_id=False branch directly.
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM ref_identifiers WHERE ref_id = ("
            "  SELECT dst_ref_id FROM links WHERE src_ref_id = %s LIMIT 1"
            ") AND id_kind = 'pub_id'",
            (paper,),
        )

    body = _handler(store).get(id=pa, view="claims").body

    assert "<uncited>" in body
