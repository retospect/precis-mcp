"""``precis_web/claim_render.py::render_claim_evidence`` — the hub-derived
``status`` field (the trust-surfaces editor badges), populated
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
from precis_web.claim_render import (
    _render_quote,
    claim_cite_head_sets,
    hub_cite_heads,
    render_claim_evidence,
)

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def test_render_claim_evidence_status_unverified_when_print_set_empty(hub: Hub) -> None:
    """A hub with no print-visible supporter (inflight) — the empty print
    set the ``claim`` page already renders as "no print-visible supporter
    yet" — carries the SAME derived label in ``status``, not the old
    dormant ``None``."""
    store = hub.live_store
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
    store = hub.live_store
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


def test_render_claim_evidence_reflects_unacquirable_supporter(hub: Hub) -> None:
    """When the hub's sole grounding supporter declares itself unacquirable
    on its own Meta tab (a paper-level FACT, no mode), ``claim_trust``
    *hardens* clean → unverified (never straight to Ⓐ/✍ — that would
    fabricate a claim-backing assertion nobody made) and the render exposes
    the harden note via ``trust_note`` — AND the specific supporter row is
    itself marked (1-residual: name WHICH paper, not just the note)."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    supporter = store.insert_ref(
        kind="paper", slug="unacq-supporter", title="A paywalled paper"
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=supporter, role="corroborates"
    )
    store.update_ref(
        supporter,
        meta_patch={
            "unacquirable_override": {
                "by": "web:owner",
                "at": "2026-08-06T00:00:00+00:00",
                "note": "paywalled; abstract states the result",
            }
        },
    )
    head = handle_registry.format_handle("finding", claim_hub)

    data = render_claim_evidence(store, head)

    assert data is not None
    assert data["status"] == "unverified"
    assert data["trust_overridden"] is False
    assert data["trust_note"] == "grounded only on sources declared unacquirable"
    # 1-residual: the supporter row that IS the unacquirable source is marked.
    row = data["corroborators"][0]
    assert row["paper_ref_id"] == supporter
    assert row["unacquirable"] is True
    assert row["unacq_note"] == "paywalled; abstract states the result"


def test_render_claim_evidence_claim_level_override_softens_and_reflects(
    hub: Hub,
) -> None:
    """A claim-level declaration made ON THE HUB itself softens the (possibly
    hardened) label and IS reflected as ``trust_overridden``."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    supporter = store.insert_ref(
        kind="paper", slug="unacq-supporter2", title="A paywalled paper"
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=supporter, role="corroborates"
    )
    store.update_ref(
        supporter, meta_patch={"unacquirable_override": {"note": "paywalled"}}
    )
    store.update_ref(
        claim_hub,
        meta_patch={
            "unacquirable_override": {
                "mode": "abstract",
                "by": "web:owner",
                "at": "2026-08-06T00:00:00+00:00",
                "note": "abstract states the result",
            }
        },
    )
    head = handle_registry.format_handle("finding", claim_hub)

    data = render_claim_evidence(store, head)

    assert data is not None
    assert data["status"] == "abstract"
    assert data["trust_overridden"] is True
    assert data["trust_note"] == "abstract states the result"


def test_render_claim_evidence_acquirable_supporter_row_unmarked(hub: Hub) -> None:
    """A supporter with no unacquirable declaration renders an unmarked row —
    the mark is per-paper, not a blanket flag on every supporter."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    supporter = store.insert_ref(
        kind="paper", slug="acquirable-supporter", title="An open paper"
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=supporter, role="corroborates"
    )
    head = handle_registry.format_handle("finding", claim_hub)

    data = render_claim_evidence(store, head)

    assert data is not None
    row = data["corroborators"][0]
    assert row["unacquirable"] is False


# ---------------------------------------------------------------------------
# ``claim_cite_head_sets`` — the (hubs, pending) split feeding the smartdraft
# reader's hollow-◇/filled-◆ claim-cite rendering (linkify.py's
# ``pending_claims``/``claims`` side-channels).
# ---------------------------------------------------------------------------


def test_claim_cite_head_sets_splits_hub_pending_and_unresolved(hub: Hub) -> None:
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    hub_head = handle_registry.format_handle("finding", claim_hub)
    plain_finding = store.insert_ref(
        kind="finding", slug=None, title="Not yet a hub"
    ).id
    pending_head = handle_registry.format_handle("finding", plain_finding)
    unresolved_head = "zzzzzz"  # 6-char pub_id-shaped token, no matching ref

    texts = [f"see [{hub_head}] and [{pending_head}] and [{unresolved_head}]"]
    hubs, pending, refuted, hypothesis = claim_cite_head_sets(store, texts)

    assert hubs == frozenset({hub_head})
    assert pending == {pending_head: plain_finding}
    assert refuted == {}
    assert hypothesis == frozenset()
    assert unresolved_head not in hubs
    assert unresolved_head not in pending


def test_claim_cite_head_sets_empty_when_no_heads(hub: Hub) -> None:
    store = hub.live_store
    hubs, pending, refuted, hypothesis = claim_cite_head_sets(store, ["no cites here"])
    assert hubs == frozenset()
    assert pending == {}
    assert refuted == {}
    assert hypothesis == frozenset()


def test_claim_cite_head_sets_splits_out_refuted(hub: Hub) -> None:
    """A finding tagged ``STATUS:refuted`` lands in the ``refuted`` map, not
    ``pending`` — the do-not-repropose ledger's cite-site rendering
    (docs/backlog/quest-dossier-dialectic.md §"Refuted lifecycle")."""
    from precis.store.types import Tag

    store = hub.live_store
    refuted_finding = store.insert_ref(
        kind="finding", slug=None, title="A dead hypothesis"
    ).id
    store.add_tag(refuted_finding, Tag.closed("STATUS", "refuted"), replace_prefix=True)
    refuted_head = handle_registry.format_handle("finding", refuted_finding)
    texts = [f"see [{refuted_head}]"]

    hubs, pending, refuted, hypothesis = claim_cite_head_sets(store, texts)

    assert hubs == frozenset()
    assert pending == {}
    assert refuted == {refuted_head: refuted_finding}
    assert hypothesis == frozenset()


def test_claim_cite_head_sets_splits_out_hypothesis(hub: Hub) -> None:
    """A hub marked ``refs.meta.artifact_type == 'hypothesis'`` lands in the
    ``hypothesis`` set, carved OUT of ``hubs`` — precedence refuted →
    hypothesis → hubs → pending
    (docs/backlog/hypothesis-cites-render-not-stored.md)."""
    store = hub.live_store
    hyp_hub = mint_hub(store, _CLAIM, extra_meta={"artifact_type": "hypothesis"})
    hyp_head = handle_registry.format_handle("finding", hyp_hub)
    texts = [f"see [{hyp_head}]"]

    hubs, pending, refuted, hypothesis = claim_cite_head_sets(store, texts)

    assert hubs == frozenset()
    assert pending == {}
    assert refuted == {}
    assert hypothesis == frozenset({hyp_head})


def test_claim_cite_head_sets_refuted_wins_over_hypothesis(hub: Hub) -> None:
    """A refuted hypothesis lands in ``refuted``, not ``hypothesis`` — the
    do-not-repropose signal outranks the epistemic one."""
    from precis.store.types import Tag

    store = hub.live_store
    hyp_hub = mint_hub(store, _CLAIM, extra_meta={"artifact_type": "hypothesis"})
    store.add_tag(hyp_hub, Tag.closed("STATUS", "refuted"), replace_prefix=True)
    hyp_head = handle_registry.format_handle("finding", hyp_hub)
    texts = [f"see [{hyp_head}]"]

    hubs, pending, refuted, hypothesis = claim_cite_head_sets(store, texts)

    assert refuted == {hyp_head: hyp_hub}
    assert hypothesis == frozenset()
    assert hubs == frozenset()


def test_hub_cite_heads_is_the_hubs_half_of_claim_cite_head_sets(hub: Hub) -> None:
    """`hub_cite_heads` is a thin wrapper — its output must equal the
    ``hubs`` half of `claim_cite_head_sets` over the SAME text, hub and
    pending heads both present."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    hub_head = handle_registry.format_handle("finding", claim_hub)
    plain_finding = store.insert_ref(kind="finding", slug=None, title="Chase").id
    pending_head = handle_registry.format_handle("finding", plain_finding)
    texts = [f"[{hub_head}] [{pending_head}]"]

    hubs, _pending, _refuted, _hypothesis = claim_cite_head_sets(store, texts)
    assert hub_cite_heads(store, texts) == hubs == frozenset({hub_head})


# ---------------------------------------------------------------------------
# ``_render_quote`` — the whitespace-collapse fix (fi191167). Pure function,
# no DB — the shape checks belong here; the full render-through-Jinja check
# lives in ``test_claim_routes.py::test_claim_view_renders_table_math_and_
# all_three_passages``.
# ---------------------------------------------------------------------------


def test_render_quote_turns_markdown_table_into_real_table() -> None:
    text = "| Catalyst | Yield (%) |\n| --- | --- |\n| Pd/C | 92 |\n| Pd(OAc)2 | 78 |"

    html, truncated = _render_quote(text)

    assert truncated is False
    assert "<table" in html
    assert "<th" in html and "Catalyst" in html
    assert "<td" in html and "92" in html
    # No leftover raw pipe run — the old bug's symptom.
    assert "| Catalyst | Yield" not in html


def test_render_quote_keeps_tex_verbatim_for_client_katex() -> None:
    text = "The rate constant scales as $k = A e^{-E_a/RT}$ across the series."

    html, truncated = _render_quote(text)

    assert truncated is False
    assert "$k = A e^{-E_a/RT}$" in html


def test_render_quote_preserves_paragraph_line_breaks() -> None:
    text = "First line of the passage.\nSecond line, same paragraph."

    html, truncated = _render_quote(text)

    assert truncated is False
    assert "<br>" in html
    assert "First line of the passage." in html
    assert "Second line, same paragraph." in html


def test_render_quote_clamps_long_table_by_rows_not_chars() -> None:
    header = "| n |\n| --- |\n"
    rows = "".join(f"| {i} |\n" for i in range(50))
    text = header + rows

    html, truncated = _render_quote(text)

    assert truncated is True
    assert "<table" in html
    assert "truncated" in html
    # The table isn't cut mid-row — every emitted row is well-formed:
    # exactly the header row + the row-clamped body, never a partial row.
    assert html.count("<tr>") == 21  # 1 header + 20 clamped body rows


def test_render_quote_clamps_long_prose_by_chars() -> None:
    text = "word " * 400  # well over the prose char budget

    html, truncated = _render_quote(text)

    assert truncated is True
    assert "…" in html


def test_render_quote_empty_text_is_empty() -> None:
    html, truncated = _render_quote("")

    assert html == ""
    assert truncated is False


def test_render_claim_evidence_publish_state_none_without_row(hub: Hub) -> None:
    """A hub with no ``nanopub_publish`` row — nanopub work not started —
    renders ``publish_state``/``publish_at`` as ``None`` (the Claims-rail
    chip keeps its pre-ladder baseline colour)."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    head = handle_registry.format_handle("finding", claim_hub)

    data = render_claim_evidence(store, head)

    assert data is not None
    assert data["publish_state"] is None
    assert data["publish_at"] is None


def test_render_claim_evidence_publish_state_from_live_row(hub: Hub) -> None:
    """A live publish row surfaces its ladder state + updated_at — the
    Claims-rail chip colour and tooltip datestamp."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    row = store.nanopub_create_publish_row(claim_hub)
    head = handle_registry.format_handle("finding", claim_hub)

    data = render_claim_evidence(store, head)

    assert data is not None
    assert data["publish_state"] == "candidate"
    assert data["publish_at"] == row.updated_at
