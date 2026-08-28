"""``/claim/<head>`` full-page view + ``/preview/claim/<head>`` hover fragment —
both cite-head forms (``fi<id>`` and ``<pub_id>``), the inflight/print-set
rendering, and the non-hub-finding "missing" fallback."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def _hub_pub_id(store, hub_ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'pub_id'",
            (hub_ref_id,),
        ).fetchone()
    assert row is not None, f"no pub_id minted for hub ref_id={hub_ref_id}"
    return str(row[0])


@pytest.fixture
def claim_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed_hub(hub: Hub) -> tuple[int, str]:
    """Mint a claim hub with a derived (cited) originator. Returns
    ``(hub_ref_id, pub_id)``."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="claim-orig", title="The original report", year=2001
    ).id
    follower = store.insert_ref(
        kind="paper", slug="claim-follower", title="Follows the original", year=2005
    ).id
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=originator,
        role="corroborates",
        meta={"source_handle": "pc999"},
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    store.add_link(src_ref_id=follower, dst_ref_id=originator, relation="cites")
    return claim_hub, _hub_pub_id(store, claim_hub)


def test_claim_view_by_fi_handle(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert _CLAIM.sentence in r.text
    assert "★" in r.text
    # The store here is the real DB-backed Store (has the nanopub mixin) —
    # the review-and-sign section is merged onto the same page, one URL.
    assert 'id="review"' in r.text
    assert "Approve" in r.text  # unminted → approve is the offered action


class _NoNanopubMixinStore:
    """Wraps a real store but hides ``nanopub_publish_row`` — stands in for
    the ``FakeStore``s reader tests drive ``/claim`` with elsewhere, which
    predate the nanopub mixin. The merged page must degrade to the plain
    reader view, not 500."""

    def __init__(self, inner: object) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> object:
        if name == "nanopub_publish_row":
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_inner"), name)


def test_claim_page_context_degrades_without_nanopub_mixin(hub: Hub) -> None:
    from precis_web.routes.claim import claim_page_context

    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    ctx = claim_page_context(_NoNanopubMixinStore(hub.live_store), fi_handle)

    assert ctx["missing"] is False
    assert ctx["np"] is None  # the review section drops out, not the page
    assert _CLAIM.sentence in ctx["claim"]


def test_claim_view_reflects_unacquirable_supporter(
    claim_client: TestClient, hub: Hub
) -> None:
    """A hub grounded only on a supporter that declared itself unacquirable
    (a paper-level FACT, no mode) hardens to unverified — not Ⓐ/✍, since no
    author asserted this claim is backed. The supporter row and the harden
    note both surface; the claim-level softener control is offered."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    supporter = store.insert_ref(
        kind="paper", slug="unacq-route", title="A paywalled paper"
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
    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "⊘ unacquirable" in r.text
    assert "paywalled; abstract states the result" in r.text
    assert "grounded only on sources declared unacquirable" in r.text
    # The claim-level softener control is offered (status is unverified).
    assert f'action="/claim/{fi_handle}/unacquirable"' in r.text


def test_claim_view_claim_level_override_softens_and_shows_undo(
    claim_client: TestClient, hub: Hub
) -> None:
    """A claim-level declaration made ON THE HUB itself (never inherited
    from a paper) softens the label and renders the undo control."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    supporter = store.insert_ref(kind="paper", slug="unacq-route2", title="A paper").id
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
                "note": "I read the abstract, it backs this",
            }
        },
    )
    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "I read the abstract, it backs this" in r.text
    assert f'action="/claim/{fi_handle}/unacquirable"' in r.text
    assert 'value="clear"' in r.text


def test_claim_view_originator_handle_and_star(
    claim_client: TestClient, hub: Hub
) -> None:
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="claim-orig2", title="The original report v2", year=2002
    ).id
    follower = store.insert_ref(
        kind="paper", slug="claim-follower2", title="Follows v2", year=2006
    ).id
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=originator,
        role="corroborates",
        meta={"source_handle": "pc998"},
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    store.add_link(src_ref_id=follower, dst_ref_id=originator, relation="cites")

    fi_handle = handle_registry.format_handle("finding", claim_hub)
    originator_handle = handle_registry.format_handle("paper", originator)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert _CLAIM.sentence in r.text
    assert originator_handle in r.text
    assert "★" in r.text
    # The evidence-row paper link targets the shared 'precis-paper' window
    # (B) so clicking a source from the claim window reuses ONE paper window
    # instead of navigating the claim page away. It must carry NO rel="noopener"
    # (which the HTML spec makes the browser treat like _blank, spawning a fresh
    # tab per click instead of reusing the named window).
    assert f'<a href="/r/paper/{originator}" target="precis-paper" class=' in r.text


def test_render_quote_collapses_marker_page_anchor_without_shredding() -> None:
    """A grounding quote whose verbatim paper text carries a Marker page-anchor
    citation — with a blank line Marker's block-merge fused inside the bracket
    span — must render as a clean ``[11]`` in ONE paragraph, not leak the raw
    ``[11](#page-5-0)`` markdown nor shred into a stray ``<p>11</p>``."""
    from precis_web.claim_render import _render_quote

    html, _truncated = _render_quote(
        "our previous Letter [\n\n11\n\n](#page-5-0). For the creation of NanoBuds."
    )
    out = str(html)
    assert "<p>11</p>" not in out
    assert "(#page-5-0)" not in out
    assert "[11]" in out
    assert out.count("<p>") == 1  # one paragraph, not shredded into three


def test_claim_view_used_by_lists_citers(claim_client: TestClient, hub: Hub) -> None:
    """The "Used by" section lists inbound ``cites`` edges (who invokes this
    claim), distinct from the evidence roles. A chunk-pinned citer links to
    the citing passage via its ``pc<id>`` handle targeting the shared
    ``precis-paper`` window (so a click reuses the one paper tab — the
    "click and get to pc" ask); a chunk-less citer falls back to the source
    record handle plus its title."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)

    # A paper that cites the claim, pinned to a real chunk.
    citing_paper = store.insert_ref(
        kind="paper", slug="cites-with-chunk", title="Builds on the claim", year=2024
    ).id
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, 0, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (citing_paper, "We adopt Pd/C at RT, as established."),
        ).fetchone()
        assert row is not None
        citer_chunk_handle = handle_registry.format_handle(
            "paper", int(row[0]), chunk=True
        )
    store.add_link(
        src_ref_id=citing_paper, src_pos=0, dst_ref_id=claim_hub, relation="cites"
    )

    # A citer with no pinned chunk → falls back to the record handle.
    chunkless = store.insert_ref(
        kind="paper", slug="cites-no-chunk", title="Also cites, no anchor", year=2023
    ).id
    store.add_link(src_ref_id=chunkless, dst_ref_id=claim_hub, relation="cites")

    fi_handle = handle_registry.format_handle("finding", claim_hub)
    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "Used by" in r.text
    # Chunk-pinned citer: clickable pc handle in the shared paper window.
    assert f'href="/c/{citer_chunk_handle}" target="precis-paper"' in r.text
    assert "Builds on the claim" in r.text
    # Chunk-less citer: record handle + title still shown.
    assert handle_registry.format_handle("paper", chunkless) in r.text
    assert "Also cites, no anchor" in r.text


def test_claim_view_no_used_by_without_citers(
    claim_client: TestClient, hub: Hub
) -> None:
    """A hub with only evidence edges (no inbound ``cites``) shows no
    "Used by" section — the intra-supporter ``cites`` edge ``_seed_hub``
    writes points paper→paper, not at the hub, so it must not leak in."""
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "Used by" not in r.text


def test_refs_finding_redirects_hub_to_claim_page(
    claim_client: TestClient, hub: Hub
) -> None:
    """A finding that IS a claim hub has one canonical view: ``/refs/finding/<id>``
    redirects (307) to ``/claim/fi<id>`` so the links-table finding link and the
    smartdraft ◆ diamond land on the SAME page — no legacy duplicate view."""
    hub_ref_id, _pub_id = _seed_hub(hub)

    r = claim_client.get(f"/refs/finding/{hub_ref_id}", follow_redirects=False)

    assert r.status_code == 307
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)
    assert r.headers["location"] == f"/claim/{fi_handle}"


def test_refs_finding_non_hub_keeps_generic_detail(
    claim_client: TestClient, hub: Hub
) -> None:
    """An ordinary (non-hub) finding — the ~12% that are citation-pending
    markers / quality checks — has no claim page, so it must NOT redirect to
    ``/claim`` and keeps the generic finding detail."""
    store = hub.live_store
    plain = store.insert_ref(
        kind="finding", slug=None, title="[citation pending] check"
    ).id

    r = claim_client.get(f"/refs/finding/{plain}", follow_redirects=False)

    # Whatever the generic detail returns, it must not be the claim redirect.
    assert not (
        r.status_code == 307 and r.headers.get("location", "").startswith("/claim/")
    )


def test_claim_view_by_pub_id(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, pub_id = _seed_hub(hub)

    r = claim_client.get(f"/claim/{pub_id}")

    assert r.status_code == 200
    assert _CLAIM.sentence in r.text


def test_claim_preview_fragment(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/preview/claim/{fi_handle}")

    assert r.status_code == 200
    assert _CLAIM.sentence in r.text
    # No fake "click to open →" affordance — it was a plain <p> that did
    # nothing when clicked; the anchor under the popover is the click.
    assert "click to open" not in r.text
    # A review surface — no clamp on the claim sentence.
    assert "line-clamp-3" not in r.text


def test_claim_preview_threads_full_sentence(
    claim_client: TestClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The popover shows :func:`claim_full_sentence`'s result, not the
    (possibly shorter) title carried in the shared evidence shape — mirrors
    the /claim h1's own full-sentence override."""
    import precis_web.routes.claim as claim_route

    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)
    monkeypatch.setattr(
        claim_route,
        "claim_full_sentence",
        lambda store, rid: "FULL SENTENCE SENTINEL" if rid == hub_ref_id else None,
    )

    r = claim_client.get(f"/preview/claim/{fi_handle}")

    assert r.status_code == 200
    assert "FULL SENTENCE SENTINEL" in r.text


def test_claim_preview_falls_back_to_short_claim_without_body_chunk(
    claim_client: TestClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hub predating the finding_body write (``claim_full_sentence``
    returns ``None``) degrades to the shared evidence shape's title, not a
    blank popover."""
    import precis_web.routes.claim as claim_route

    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)
    monkeypatch.setattr(claim_route, "claim_full_sentence", lambda store, rid: None)

    r = claim_client.get(f"/preview/claim/{fi_handle}")

    assert r.status_code == 200
    assert _CLAIM.sentence in r.text


def _seed_hub_with_chunk(hub: Hub) -> tuple[int, str, str]:
    """Mint a claim hub whose corroborating edge grounds at a REAL paper
    chunk. Returns ``(hub_ref_id, chunk_handle, chunk_text)``."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    paper = store.insert_ref(
        kind="paper", slug="claim-grounded", title="The grounded report", year=2003
    ).id
    chunk_text = "Pd/C converts aryl halides at 25 °C when K2CO3 is present."
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, 0, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (paper, chunk_text),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
    chunk_handle = handle_registry.format_handle("paper", chunk_id, chunk=True)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": chunk_handle},
    )
    return claim_hub, chunk_handle, chunk_text


def test_claim_view_grounding_passage_linked_and_quoted(
    claim_client: TestClient, hub: Hub
) -> None:
    hub_ref_id, chunk_handle, chunk_text = _seed_hub_with_chunk(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    # The passage nests directly below its paper row (no separate
    # "Grounding passages" section repeating the pc handles).
    assert "Grounding passages" not in r.text
    assert chunk_text in r.text
    assert f"/c/{chunk_handle}" in r.text  # the chunk is clickable


def test_claim_view_dangling_source_handle_degrades(
    claim_client: TestClient, hub: Hub
) -> None:
    # _seed_hub grounds at pc999, which has no chunks row in the test DB.
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "/c/pc999" in r.text  # still clickable — hover degrades server-side
    assert "passage text not available" in r.text


# ── Quote-block abbreviation glossing (docs/backlog/claim-page-abbreviation-
# glossing.md) — reuses the draft reader's linkify._highlight_abbrevs, sourced
# from the QUOTED paper's own stored chunks, no persistent storage. ──


def test_claim_view_grounding_quote_glosses_paper_abbreviation(
    claim_client: TestClient, hub: Hub
) -> None:
    """A grounding quote that merely USES an abbreviation gets the hover
    gloss when that abbreviation is defined ELSEWHERE in the same source
    paper's own stored chunks (inline ``Long Form (ABBR)`` first-use,
    Schwartz-Hearst — same extractor the draft reader's recall highlight
    uses, run against the paper being quoted, not the claim hub)."""
    from precis_web import claim_render

    claim_render._PAPER_ABBREV_CACHE.clear()
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    paper = store.insert_ref(
        kind="paper", slug="claim-abbrev-src", title="Abbreviation source", year=2015
    ).id
    # ord=0: defines PEI inline, elsewhere in the SAME paper — never quoted.
    _insert_chunk(
        store,
        ref_id=paper,
        ord=0,
        text="We use polyethyleneimine (PEI) throughout the synthesis.",
    )
    # ord=1: the actual grounding quote — just uses the abbreviation.
    quote_text = "PEI coats the surface uniformly at low concentration."
    quote_handle = _insert_chunk(store, ref_id=paper, ord=1, text=quote_text)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": quote_handle},
    )
    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert quote_text in r.text
    assert 'class="pa"' in r.text
    assert "polyethyleneimine" in r.text  # the hover-gloss definition


def test_claim_view_grounding_quote_no_gloss_without_definitions(
    claim_client: TestClient, hub: Hub
) -> None:
    """A source paper with no abbreviations defined anywhere renders its
    quote exactly as before — the highlighter is a silent no-op, not an
    error, when the map is empty."""
    from precis_web import claim_render

    claim_render._PAPER_ABBREV_CACHE.clear()
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    paper = store.insert_ref(
        kind="paper", slug="claim-no-abbrev-src", title="No glossary here", year=2016
    ).id
    quote_text = "PEI is mentioned here but never defined anywhere in this paper."
    quote_handle = _insert_chunk(store, ref_id=paper, ord=0, text=quote_text)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": quote_handle},
    )
    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert quote_text in r.text
    assert 'class="pa"' not in r.text


def test_claim_view_grounding_quote_gloss_degrades_on_extraction_error(
    claim_client: TestClient, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A term-extraction error (``defined_terms`` raising) must never
    surface as a 500 — the quote page still renders, unglossed, exactly as
    it would with no map."""
    from precis.store._draft_ops import DraftStore
    from precis_web import claim_render

    claim_render._PAPER_ABBREV_CACHE.clear()

    def _boom(self: object, ref_id: int) -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(DraftStore, "defined_terms", _boom)

    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    paper = store.insert_ref(
        kind="paper", slug="claim-abbrev-err", title="Errors on extraction", year=2017
    ).id
    _insert_chunk(store, ref_id=paper, ord=0, text="We use polyethyleneimine (PEI).")
    quote_text = "PEI is applied at the final step."
    quote_handle = _insert_chunk(store, ref_id=paper, ord=1, text=quote_text)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": quote_handle},
    )
    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert quote_text in r.text
    assert 'class="pa"' not in r.text


def test_claim_preview_lists_cited_chunks(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, chunk_handle, chunk_text = _seed_hub_with_chunk(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/preview/claim/{fi_handle}")

    assert r.status_code == 200
    assert f"/c/{chunk_handle}" in r.text
    assert chunk_text[:80] in r.text


def _insert_chunk(store, *, ref_id: int, ord: int, text: str) -> str:
    """Insert a real paper chunk, return its universal chunk handle — the
    grounding-quote render fix (the claim-page rendering work)
    needs REAL chunk text, not the ``pc999``-dangling-handle shape
    `_seed_hub` uses."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, %s, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, ord, text),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
    return handle_registry.format_handle("paper", chunk_id, chunk=True)


def test_claim_view_renders_table_math_and_all_three_passages(
    claim_client: TestClient, hub: Hub
) -> None:
    """A hub grounded by three distinct passages — one plain, one a
    markdown pipe table, one carrying ``$…$`` TeX — across all three
    evidence roles (originator/corroborator/contradictor). The full
    ``/claim/<head>`` render must: turn the table into a real ``<table>``
    (not one flattened pipe run — fi191167), mark the quote container
    ``tex-scope`` so client KaTeX picks up the TeX span, list all three
    passages (not just the ★ print set), and mark the claim TITLE
    ``tex-scope`` too.

    Reversal (2026-08-28, Reto): the 2026-08-04 "Already decided"
    claim-titles-are-plain-text policy (commit 679393c9) kept the title
    unprocessed until a real hub (fi236297) rendered raw ``$math$`` in its
    big title — Reto asked for KaTeX over claim titles too, superseding
    that call. This test used to assert the opposite (``tex-scope not in
    title_line``); it now asserts the title carries ``tex-scope``."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)

    plain_paper = store.insert_ref(
        kind="paper", slug="claim-plain", title="Plain-passage paper", year=2010
    ).id
    plain_text = "Pd/C converts aryl halides at 25 °C when K2CO3 is present."
    plain_handle = _insert_chunk(store, ref_id=plain_paper, ord=0, text=plain_text)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=plain_paper,
        role="establishes",
        meta={"source_handle": plain_handle},
    )

    table_paper = store.insert_ref(
        kind="paper", slug="claim-table", title="Table-passage paper", year=2011
    ).id
    table_text = (
        "| Catalyst | Yield (%) |\n| --- | --- |\n| Pd/C | 92 |\n| Pd(OAc)2 | 78 |"
    )
    table_handle = _insert_chunk(store, ref_id=table_paper, ord=0, text=table_text)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=table_paper,
        role="corroborates",
        meta={"source_handle": table_handle},
    )
    # Seniority is DERIVED from the intra-supporter citation graph, not the
    # write-time role (`seniority.py`) — without a `cites` edge among the
    # supporters, both would fall back to "corroborator". table_paper
    # citing plain_paper makes plain_paper the derived originator.
    store.add_link(src_ref_id=table_paper, dst_ref_id=plain_paper, relation="cites")

    tex_paper = store.insert_ref(
        kind="paper", slug="claim-tex", title="TeX-passage paper", year=2012
    ).id
    tex_text = "The rate constant scales as $k = A e^{-E_a/RT}$ across the series."
    tex_handle = _insert_chunk(store, ref_id=tex_paper, ord=0, text=tex_text)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=tex_paper,
        role="contradicts",
        meta={"source_handle": tex_handle},
    )

    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    body = r.text
    assert "<table" in body
    assert 'class="tex-scope' in body
    # All three distinct grounding passages are listed, not just the ★
    # print set (originator only).
    assert f"/c/{plain_handle}" in body
    assert f"/c/{table_handle}" in body
    assert f"/c/{tex_handle}" in body
    assert plain_text in body
    assert "(originator)" in body
    assert "(corroborator)" in body
    assert "(contradictor)" in body
    # The claim title is math-processed too (2026-08-28 reversal, Reto):
    # the 2026-08-04 "Already decided" claim-titles-are-plain-text policy
    # (679393c9) held until a real hub (fi236297) rendered raw $math$ in
    # its title — Reto asked for KaTeX over claim titles, superseding it.
    title_line = next(line for line in body.splitlines() if "<h1" in line)
    assert "tex-scope" in title_line


def test_claim_view_non_hub_finding_shows_missing(
    claim_client: TestClient, hub: Hub
) -> None:
    store = hub.live_store
    finding = store.insert_ref(
        kind="finding", slug=None, title="An ordinary finding", meta={}
    ).id
    fi_handle = handle_registry.format_handle("finding", finding)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "No claim hub" in r.text


def test_claim_view_surfaces_both_src_chunk_grounding(
    claim_client: TestClient, hub: Hub
) -> None:
    """Two backfill-arm corroborates edges from ONE paper — grounding pinned
    via ``links.src_chunk_id`` (no ``meta.source_handle``) at two different
    chunks — must BOTH render as clickable grounding passages. The redirect
    that folded /refs/finding/<hub> into this page must not lose either
    (the "2 corroborates, we don't want to lose it" regression)."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    paper = store.insert_ref(
        kind="paper", slug="claim-backfill", title="Backfilled supporter", year=2004
    ).id
    text0 = "We describe monocrystalline graphitic films a few atoms thick."
    text1 = "Ballistic transport persists at room temperature in these films."
    h0 = _insert_chunk(store, ref_id=paper, ord=0, text=text0)
    h1 = _insert_chunk(store, ref_id=paper, ord=1, text=text1)
    # Backfill shape: grounding in src_chunk_id, meta.source_handle unset.
    store.add_link(
        src_ref_id=paper, dst_ref_id=claim_hub, relation="corroborates", src_pos=0
    )
    store.add_link(
        src_ref_id=paper, dst_ref_id=claim_hub, relation="corroborates", src_pos=1
    )
    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    # Both passages nest below the paper row (the standalone "Grounding
    # passages" section is gone — the pc handles listed once, not twice).
    assert text0 in r.text and text1 in r.text
    assert f"/c/{h0}" in r.text and f"/c/{h1}" in r.text  # both clickable


def test_claim_view_shows_full_untruncated_claim_sentence(
    claim_client: TestClient, hub: Hub
) -> None:
    """``refs.title`` is capped ``[:200]`` at mint; the whole sentence lives in
    the finding_body chunk. The claim page's h1 shows the FULL sentence, so a
    long claim isn't sheared mid-word ("concentrations up t" regression)."""
    store = hub.live_store
    long_sentence = (
        "Graphene supports ballistic electron transport at submicron distances "
    ) * 3 + "and this distinctive SENTINELTAIL closes the claim."
    assert len(long_sentence) > 200
    claim_hub = mint_hub(
        store, CanonicalClaim(sentence=long_sentence, scope={"material": "graphene"})
    )
    # The stored title alone is truncated and can't contain the tail.
    assert "SENTINELTAIL" not in long_sentence[:200]
    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "SENTINELTAIL" in r.text  # full sentence, not the [:200] title


def test_claim_view_has_ask_and_think_affordance(
    claim_client: TestClient, hub: Hub
) -> None:
    """The claim page carries the "Ask & think" follow-up form the generic
    finding detail had, posting to the same /refs/finding/<hub>/ask route —
    the finding→claim redirect must not drop it. (That route redirects to the
    spawned conv thread; the thread then lists here on the next page load.)"""
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "Discussion" in r.text
    assert "Ask &amp; think" in r.text
    assert f'action="/refs/finding/{hub_ref_id}/ask"' in r.text
    # Above the fold: Discussion renders BEFORE the evidence sections, and
    # the helper line names the model that answers (env-resolved).
    assert r.text.index("Discussion") < r.text.index("Prints on export")
    assert "runs an agentic" in r.text
    assert "sonnet" in r.text or "opus" in r.text


def test_claim_view_mixed_paper_labels_contradiction_not_support(
    claim_client: TestClient, hub: Hub
) -> None:
    """A paper that both corroborates and contradicts the same claim (at
    different chunks): the contradicting passage renders under the
    contradictor role, not relabeled as support (reviewer finding — grounding
    attribution keyed by relation, not paper)."""
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    paper = store.insert_ref(
        kind="paper", slug="mixed-ev", title="Mixed evidence", year=2004
    ).id
    sup_text = "This passage supports the claim strongly."
    con_text = "This other passage contradicts the claim."
    hs = _insert_chunk(store, ref_id=paper, ord=0, text=sup_text)
    hc = _insert_chunk(store, ref_id=paper, ord=1, text=con_text)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": hs},
    )
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=paper,
        role="contradicts",
        meta={"source_handle": hc},
    )
    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert sup_text in r.text and con_text in r.text
    assert "(contradictor)" in r.text  # B attributed to contradictor, not support


# ── Refuted lifecycle: the red banner (docs/backlog/quest-dossier-
# dialectic.md §"Refuted lifecycle") ──


def test_claim_view_refuted_shows_red_banner_with_ruling(
    claim_client: TestClient, hub: Hub
) -> None:
    """A hub tagged ``STATUS:refuted`` and linked ``superseded-by`` a ruling
    finding shows the red banner naming that ruling."""
    from precis.store.types import Tag

    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    ruling = store.insert_ref(
        kind="finding", slug=None, title="Pd/C does not catalyze this at RT"
    ).id
    store.add_link(src_ref_id=claim_hub, dst_ref_id=ruling, relation="superseded-by")
    store.add_tag(claim_hub, Tag.closed("STATUS", "refuted"), replace_prefix=True)
    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "Refuted" in r.text
    assert "the claim as stated is dead" in r.text
    assert "Pd/C does not catalyze this at RT" in r.text
    ruling_handle = handle_registry.format_handle("finding", ruling)
    assert f'href="/claim/{ruling_handle}"' in r.text


def test_claim_view_refuted_without_link_shows_ruling_unknown(
    claim_client: TestClient, hub: Hub
) -> None:
    """A hub tagged ``STATUS:refuted`` with no superseding-ruling link still
    shows the banner, but says the ruling is unknown rather than erroring."""
    from precis.store.types import Tag

    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    store.add_tag(claim_hub, Tag.closed("STATUS", "refuted"), replace_prefix=True)
    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "Refuted" in r.text
    assert "ruling unknown" in r.text


def test_claim_view_non_refuted_shows_no_banner(
    claim_client: TestClient, hub: Hub
) -> None:
    """A plain (non-refuted) claim page carries no red banner."""
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "the claim as stated is dead" not in r.text
    assert "ruling unknown" not in r.text


# ── Hypothesis falsification prose (docs/backlog/
# hypothesis-cites-render-not-stored.md) ──


def test_claim_view_hypothesis_shows_falsification_fields(
    claim_client: TestClient, hub: Hub
) -> None:
    """A hub minted a ``hypothesis`` shows real motivation/falsified-by
    fields, not just the raw JSON dump in the review textarea."""
    from precis.handlers._finding_hypothesis import (
        ARTIFACT_HYPOTHESIS,
        META_ARTIFACT_TYPE,
        META_PROPOSED_PAYLOAD,
    )

    store = hub.live_store
    hyp_hub = mint_hub(
        store,
        _CLAIM,
        extra_meta={
            META_ARTIFACT_TYPE: ARTIFACT_HYPOTHESIS,
            META_PROPOSED_PAYLOAD: {
                "motivation": "Both systems share a mechanism; untested transfer.",
                "testable_by": "an experiment discriminating the two mechanisms",
            },
        },
    )
    fi_handle = handle_registry.format_handle("finding", hyp_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "Hypothesis" in r.text
    assert "motivation, not evidence" in r.text
    assert "Both systems share a mechanism; untested transfer." in r.text
    assert "an experiment discriminating the two mechanisms" in r.text


def test_claim_view_non_hypothesis_shows_no_falsification_fields(
    claim_client: TestClient, hub: Hub
) -> None:
    """A plain (non-hypothesis) claim page carries no falsification box."""
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "motivation, not evidence" not in r.text


# ── POST /claim/<head>/unacquirable — the claim-level softener write door ──


def test_claim_unacquirable_set_abstract(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.post(
        f"/claim/{fi_handle}/unacquirable",
        data={"mode": "abstract", "note": "abstract backs this claim"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == f"/claim/{fi_handle}"
    ov = hub.live_store.fetch_refs_by_ids([hub_ref_id])[hub_ref_id].meta[
        "unacquirable_override"
    ]
    assert ov["mode"] == "abstract"
    assert ov["note"] == "abstract backs this claim"
    assert ov["by"] == "web:owner"
    assert ov["at"]


def test_claim_unacquirable_set_vouched(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.post(
        f"/claim/{fi_handle}/unacquirable",
        data={"mode": "vouched", "note": "I vouch for this"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    ov = hub.live_store.fetch_refs_by_ids([hub_ref_id])[hub_ref_id].meta[
        "unacquirable_override"
    ]
    assert ov["mode"] == "vouched"


def test_claim_unacquirable_requires_note(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.post(
        f"/claim/{fi_handle}/unacquirable",
        data={"mode": "abstract", "note": "   "},
        follow_redirects=False,
    )

    assert r.status_code == 400
    assert "unacquirable_override" not in (
        hub.live_store.fetch_refs_by_ids([hub_ref_id])[hub_ref_id].meta or {}
    )


def test_claim_unacquirable_unknown_mode_400s(
    claim_client: TestClient, hub: Hub
) -> None:
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.post(
        f"/claim/{fi_handle}/unacquirable",
        data={"mode": "bogus", "note": "x"},
        follow_redirects=False,
    )

    assert r.status_code == 400


def test_claim_unacquirable_clear_drops_override(
    claim_client: TestClient, hub: Hub
) -> None:
    hub_ref_id, _pub_id = _seed_hub(hub)
    hub.live_store.update_ref(
        hub_ref_id,
        meta_patch={"unacquirable_override": {"mode": "vouched", "note": "x"}},
    )
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.post(
        f"/claim/{fi_handle}/unacquirable",
        data={"mode": "clear"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    ref = hub.live_store.fetch_refs_by_ids([hub_ref_id])[hub_ref_id]
    assert (ref.meta or {}).get("unacquirable_override") is None


def test_claim_unacquirable_non_hub_head_errors(claim_client: TestClient) -> None:
    r = claim_client.post(
        "/claim/aaaaaa/unacquirable",
        data={"mode": "vouched", "note": "x"},
        follow_redirects=False,
    )

    assert r.status_code == 400
