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
from precis_web.claim_render import _render_quote, render_claim_evidence

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


def test_render_claim_evidence_reflects_unacquirable_supporter(hub: Hub) -> None:
    """When the hub's sole grounding supporter is declared unacquirable on its
    own Meta tab, ``claim_trust`` softens clean → Ⓐ/✍ and the render exposes
    ``trust_overridden``/``trust_note`` so the claim page can explain the calm
    badge (gap-1 per-claim reflection) — AND the specific supporter row is
    itself marked (1-residual: name WHICH paper, not just the mode+note)."""
    store = hub.store
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
                "mode": "abstract",
                "by": "web:owner",
                "at": "2026-08-06T00:00:00+00:00",
                "note": "paywalled; abstract states the result",
            }
        },
    )
    head = handle_registry.format_handle("finding", claim_hub)

    data = render_claim_evidence(store, head)

    assert data is not None
    assert data["status"] == "abstract"
    assert data["trust_overridden"] is True
    assert data["trust_note"] == "paywalled; abstract states the result"
    # 1-residual: the supporter row that IS the unacquirable source is marked.
    row = data["corroborators"][0]
    assert row["paper_ref_id"] == supporter
    assert row["unacquirable"] is True
    assert row["unacq_mode"] == "abstract"
    assert row["unacq_note"] == "paywalled; abstract states the result"


def test_render_claim_evidence_acquirable_supporter_row_unmarked(hub: Hub) -> None:
    """A supporter with no unacquirable declaration renders an unmarked row —
    the mark is per-paper, not a blanket flag on every supporter."""
    store = hub.store
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
    assert row["unacq_mode"] is None


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
