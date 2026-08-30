"""Layer-A mint gates + the approve→sign→view pipeline. DB-backed;
signing keys come from env (the secrets resolver is env-first), so no
vault rows and no network are involved."""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.errors import BadInput
from precis.nanopub import evidence, gates, mint
from precis.nanopub.keys import generate_keypair
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, link_claims, mint_hub
from tests.workers._helpers import seed_ref

_QUOTE = (
    "This anisotropy can reach a 400:1 ratio between the most rigid and "
    "weakest directions"
)
_SNIP = "anisotropy can reach a 400 1 ratio"


def _seed_paper(
    store: Any,
    *,
    doi: str | None = None,
    sha: str | None = None,
    title: str = "Anisotropic Elastic Properties",
    chunk_text: str = f"Tensorial analysis. {_QUOTE}, in stark contrast.",
    section: list[str] | None = None,
) -> tuple[int, int, str]:
    """A paper ref with a ``ref_identifiers`` DOI row (prod's canonical
    DOI location — ``refs.meta['doi']`` is a legacy spot ~3 rows carry),
    one body chunk, and a pdf_sha256 identifier row. Defaults are unique
    per paper — the identifiers PK is ``(id_kind, id_value)``. Returns
    ``(ref_id, chunk_id, sha)``."""
    ref_id = seed_ref(store, title=title, kind="paper")
    if sha is None:
        sha = f"{ref_id:064x}"
    if doi is None:
        doi = f"10.1103/PhysRevLett.109.{ref_id}"
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('doi', %s, %s, 'test')",
            (doi, ref_id),
        )
        row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            "section_path) VALUES (%s, 'system', 0, 'paragraph', %s, %s) "
            "RETURNING chunk_id",
            (ref_id, chunk_text, section or ["Results"]),
        ).fetchone()
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('pdf_sha256', %s, %s, 'test')",
            (sha, ref_id),
        )
    return ref_id, int(row[0]), sha


def _seed_hub(store: Any, sentence: str, paper_ref: int, chunk_id: int) -> int:
    hub = mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper_ref,
        role="corroborates",
        meta={"source_handle": f"pc{chunk_id}"},
        check_retraction=False,
    )
    return hub


def _payload(chunk_id: int, sha: str = "", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "passages": [
            {
                "doi": "10.1103/PhysRevLett.109.195502",
                "pdf_sha256": sha,
                "quote": _QUOTE,
                "snip": _SNIP,
                "chunk_id": chunk_id,
                "role": "corroborates",
            }
        ],
        "fields": {"quantity": "400:1", "quantity_bound": "upper"},
    }
    base.update(over)
    return base


def _finding_body(store: Any, hub: int) -> str:
    """The hub's ``finding_body`` chunk text (ord=0) — what
    ``canon.block()`` ANN-retrieves over."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = 0 "
            "AND chunk_kind = 'finding_body'",
            (hub,),
        ).fetchone()
    return str(row[0]) if row else ""


def _pub_ids(store: Any, hub: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'pub_id'",
            (hub,),
        ).fetchall()
    return {str(r[0]) for r in rows}


def _gate_slugs(store: Any, hub: int, payload: dict[str, Any]) -> set[str]:
    bundle = evidence.load_bundle(store, hub)
    return {v.gate for v in gates.run_mint_gates(store, bundle, payload)}


# ── gates ───────────────────────────────────────────────────────────────


def test_clean_payload_passes_all_gates(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(
        store, "DFT shows MOFs can be anisotropic up to 400:1.", paper, chunk
    )
    assert _gate_slugs(store, hub, _payload(chunk)) == set()


def test_pdf_sha_alias_row_does_not_block_mint(store: Any) -> None:
    # The metadata write-back (_maybe_patch_pdf) leaves TWO identifier
    # rows per patched PDF — post-patch canonical + as-downloaded alias.
    # refs.pdf_sha256 pins the held copy; alias rows only index dedup.
    paper, chunk, sha = _seed_paper(store)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 1, %s) "
            "ON CONFLICT DO NOTHING",
            (sha, sha, f"/tmp/{sha}.pdf"),
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, paper))
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('pdf_sha256', %s, %s, 'test')",
            (f"a{paper:063x}", paper),
        )
    assert evidence.pdf_sha_rows(store, paper) == [sha]
    hub = _seed_hub(
        store, "DFT shows MOFs can be anisotropic up to 400:1.", paper, chunk
    )
    assert _gate_slugs(store, hub, _payload(chunk)) == set()


def test_hearsay_section_grounding_is_rejected(store: Any) -> None:
    # The fi34867 class: quote checks out but lives in a references list.
    paper, chunk, _sha = _seed_paper(store, section=["References"])
    hub = _seed_hub(store, "Han et al. demonstrated X.", paper, chunk)
    assert "primary-source" in _gate_slugs(store, hub, _payload(chunk))
    for section in (["Related Work"], ["2. Background"], ["Prior Art Survey"]):
        paper2, chunk2, _sha2 = _seed_paper(store, section=section)
        hub2 = _seed_hub(store, f"Claim about {section[0]}.", paper2, chunk2)
        assert "primary-source" in _gate_slugs(store, hub2, _payload(chunk2))


def test_quote_citation_marker_is_rejected(store: Any) -> None:
    # The fi19981/fi19987 class: an intro chunk citing the primary work
    # slips past the section-path gate ("intro" isn't hearsay-listed),
    # but the quote's own citation marker gives the attribution away.
    for quoted in (
        "Moore observed that transistor counts double every year [12]",
        "graphene shows very high carrier mobility (Novoselov et al. 2004)",
        # Marker-extracted markdown-link residue escapes the brackets —
        # the fi19981 sim25 class: many predicted the demise.[\[1,2\]](#p)
        "many have predicted the demise of the law.[\\[1,2\\]](#page-17-0)",
        # pc550457's shape (pa4365, 2026-08-17): marker-ingest renders
        # superscript citation numerals as literal <sup>N</sup> HTML.
        "which is similar to the previous report.<sup>8</sup>",
    ):
        paper, chunk, _sha = _seed_paper(
            store,
            chunk_text=f"Intro prose. {quoted}, as is well known.",
            section=["I. Introduction"],
        )
        hub = _seed_hub(store, "A cited-marker claim.", paper, chunk)
        payload = _payload(chunk, fields={})
        payload["passages"][0]["quote"] = quoted
        payload["passages"][0]["snip"] = "as is well known"
        slugs = _gate_slugs(store, hub, payload)
        assert "primary-source" in slugs
        assert "quote-verbatim" not in slugs  # only the marker is at fault


def test_miller_index_bracket_is_not_a_citation(store: Any) -> None:
    quoted = "growth proceeds along the [100] direction with 3:1 anisotropy"
    paper, chunk, _sha = _seed_paper(
        store,
        chunk_text=f"Our results. {quoted}, we find.",
        section=["III. Results"],
    )
    hub = _seed_hub(store, "A crystallographic claim.", paper, chunk)
    payload = _payload(chunk, fields={})
    payload["passages"][0]["quote"] = quoted
    payload["passages"][0]["snip"] = "we find"
    assert "primary-source" not in _gate_slugs(store, hub, payload)


def test_genuine_superscript_notation_is_not_a_citation(store: Any) -> None:
    # <sup>-1</sup> exponents and <sup>3+</sup> ionic charges are real
    # chemistry/math notation, not citation-marker residue — only a bare
    # integer list (the <sup>8</sup> pc550457 shape) should trip the gate.
    for quoted in (
        "the peak sits at 1580 cm<sup>-1</sup> in the Raman spectrum",
        "the resulting Fe<sup>3+</sup> centers dominate the signal",
        "the C<sub>60</sub> cage retains its icosahedral symmetry",
        "the conductance increases by a factor of 10<sup>3</sup> here",
        "the <sup>13</sup>C chemical shifts localize to the neck",
        "a specific surface area of 1000 m<sup>2</sup>/g is measured",
        "a pore volume of 1.2 cm<sup>3</sup>/g in the composite",
    ):
        assert evidence.citation_markers(quoted) == [], quoted
        paper, chunk, _sha = _seed_paper(
            store,
            chunk_text=f"Our results. {quoted}, we find.",
            section=["III. Results"],
        )
        hub = _seed_hub(store, "A spectroscopy claim.", paper, chunk)
        payload = _payload(chunk, fields={})
        payload["passages"][0]["quote"] = quoted
        payload["passages"][0]["snip"] = "we find"
        assert "primary-source" not in _gate_slugs(store, hub, payload)


def test_superscript_citation_markers_detected(store: Any) -> None:
    # Multi-cite and dangling-fragment shapes of the <sup>N</sup> residue.
    assert evidence.citation_markers("prior work.<sup>8</sup>") == ["<sup>8</sup>"]
    assert evidence.citation_markers("prior work.<sup>3,4</sup>") == ["<sup>3,4</sup>"]
    assert evidence.citation_markers("prior work.<sup>3–5</sup>") == ["<sup>3–5</sup>"]
    # A quote trimmed right before the closing tag still dangles the cite.
    assert evidence.citation_markers("prior work.<sup>8") == ["<sup>8"]
    # Letter-preceded cites still trip when the number can't be a unit
    # exponent (multi-cite, or outside the 2–4 exponent range).
    assert evidence.citation_markers("as shown for nanobuds<sup>12</sup>") == [
        "<sup>12</sup>"
    ]
    assert evidence.citation_markers("as reported earlier<sup>3,4</sup>") == [
        "<sup>3,4</sup>"
    ]


def test_acquisition_marked_hub_rejects_grounded_mint(store: Any) -> None:
    # Harvester wrote "Paper not in corpus — needs acquisition." into the
    # finding body: grounding it in a *citing* paper is hearsay whatever
    # section the chunk sits in; explicitly hanging stays allowed.
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A secondhand claim.", paper, chunk)
    with store.pool.connection() as conn:
        n = conn.execute(
            "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'finding_body'",
            (hub,),
        ).rowcount
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
            "VALUES (%s, 'system', 0, 'finding_body', "
            "'A secondhand claim. Paper not in corpus — needs acquisition.')",
            (hub,),
        )
    assert n == 1  # the marker replaced the real body chunk, not thin air
    assert "primary-source" in _gate_slugs(store, hub, _payload(chunk))
    hanging = {"passages": [], "hanging": True}
    assert "primary-source" not in _gate_slugs(store, hub, hanging)


def test_unheld_evidence_source_rejects_grounded_mint(store: Any) -> None:
    """The derived arm: an evidence paper with no live body chunks is one
    we hold the metadata of but not the text — the claim's primary source
    is not in the corpus, whatever the hub's prose says. A quote from the
    *citing* paper is secondhand by construction."""
    citing, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the secondhand claim holds.", citing, chunk)
    # Baseline: one held source, no marker prose — mints clean.
    assert _gate_slugs(store, hub, _payload(chunk, sha)) == set()

    stub = seed_ref(store, title="The primary we do not hold", kind="paper")
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=stub,
        role="corroborates",
        check_retraction=False,
    )
    bundle = evidence.load_bundle(store, hub)
    assert [s.ref_id for s in bundle.unheld_sources] == [stub]
    assert "primary-source" in _gate_slugs(store, hub, _payload(chunk, sha))
    # Hanging mints stay allowed — that IS the path while the hunt runs.
    assert "primary-source" not in _gate_slugs(
        store, hub, {"passages": [], "hanging": True}
    )


def test_review_titled_source_rejects_grounded_mint(store: Any) -> None:
    """The fifth arm (2026-08-27 audit): the source IS held — body chunks
    and all — but its title marks it a review, so a quote from it is
    secondhand by genre. Distinct gate slug ``review-source`` so the review
    surface can route it to re-grounding advice."""
    review, chunk, sha = _seed_paper(
        store, title="Recent advances in anisotropic elasticity: A review"
    )
    hub = _seed_hub(store, "DFT shows the surveyed claim holds.", review, chunk)
    assert "review-source" in _gate_slugs(store, hub, _payload(chunk, sha))
    # Hanging stays the designed escape — same as every acquisition arm.
    assert "review-source" not in _gate_slugs(
        store, hub, {"passages": [], "hanging": True}
    )


def test_review_source_quiet_for_synthesis_claim(store: Any) -> None:
    """The escape: a claim whose sentence declares a synthesis mode makes
    the review the primary source, not hearsay."""
    review, chunk, sha = _seed_paper(
        store, title="Metal-organic frameworks for catalysis: A review"
    )
    hub = _seed_hub(
        store,
        "Review synthesis identifies metal-linker hydrolysis as the main "
        "degradation pathway for MOF electrocatalysts.",
        review,
        chunk,
    )
    assert "review-source" not in _gate_slugs(store, hub, _payload(chunk, sha))


def test_held_primary_research_title_is_not_a_review(store: Any) -> None:
    """Control: an ordinary research-paper title never trips the arm."""
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the ordinary claim holds.", paper, chunk)
    assert "review-source" not in _gate_slugs(store, hub, _payload(chunk, sha))


def _mute_prose_arm(monkeypatch: Any) -> None:
    """Disable :data:`evidence.ACQUISITION_MARKER` for one test.

    Every structural-arm test runs with the prose arm muted, so a refusal
    proves the structure did it — the prose is a fallback we intend to
    delete (see ``check_primary_source``'s retirement note), and a test
    that passes only because of it would silently become a regression the
    day it goes."""
    monkeypatch.setattr(evidence, "ACQUISITION_MARKER", re.compile(r"(?!x)x"))


def _await_stub(store: Any, hub: int, title: str) -> int:
    """A ``DREAM:acquire``-shaped paper stub bound to ``hub`` the way
    ``put(kind='finding', wants=...)`` binds one: an outbound
    ``awaits-evidence`` link, no evidence edge (the stub supports nothing
    yet). Returns the stub's ref_id."""
    stub = seed_ref(store, title=title, kind="paper")
    store.add_link(
        src_ref_id=hub,
        dst_ref_id=stub,
        relation="awaits-evidence",
        set_by="agent",
    )
    return stub


def test_awaits_evidence_stub_rejects_grounded_mint(
    store: Any, monkeypatch: Any
) -> None:
    """Case (a) — the primary is known only as a ``wants=`` descriptor, so
    it has a ``DREAM:acquire`` stub and no evidence edge. The derived arm
    is blind to it (``awaits-evidence`` is not an evidence relation, so the
    stub never enters ``bundle.sources``); the awaiting arm reads the edge
    the acquisition mint already wrote."""
    _mute_prose_arm(monkeypatch)
    citing, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the awaited claim holds.", citing, chunk)
    assert _gate_slugs(store, hub, _payload(chunk, sha)) == set()

    stub = _await_stub(store, hub, "The primary we are still fetching")
    bundle = evidence.load_bundle(store, hub)
    assert [s.ref_id for s in bundle.awaiting_sources] == [stub]
    assert bundle.unheld_sources == []  # the derived arm cannot see it
    assert "primary-source" in _gate_slugs(store, hub, _payload(chunk, sha))
    # Hanging mints stay allowed — that IS the path while the fetch runs.
    assert "primary-source" not in _gate_slugs(
        store, hub, {"passages": [], "hanging": True}
    )


def test_declared_primary_unheld_rejects_grounded_mint(
    store: Any, monkeypatch: Any
) -> None:
    """Case (b) — the marker's canonical shape: the claim was read out of a
    *citing* paper we hold, and the primary has no ``refs`` row at all, so
    no edge can express its absence. ``refs.meta`` carries the state
    instead, where a reword cannot reach it."""
    _mute_prose_arm(monkeypatch)
    citing, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the declared claim holds.", citing, chunk)
    assert _gate_slugs(store, hub, _payload(chunk, sha)) == set()

    store.update_ref(hub, meta_patch={evidence.PRIMARY_UNHELD_META_KEY: True})
    bundle = evidence.load_bundle(store, hub)
    assert bundle.primary_source_unheld
    assert bundle.unheld_sources == [] and bundle.awaiting_sources == []
    assert "primary-source" in _gate_slugs(store, hub, _payload(chunk, sha))
    assert "primary-source" not in _gate_slugs(
        store, hub, {"passages": [], "hanging": True}
    )


def test_acquiring_hub_without_live_stub_rejects_grounded_mint(
    store: Any, monkeypatch: Any
) -> None:
    """Case (c) — zero evidence edges, and the awaited stub since
    soft-deleted, so both source-shaped arms resolve to nothing. The
    lifecycle tag is the surviving trace: ``chase`` flips
    ``STATUS:acquiring`` the moment a claim grounds, so a hub still
    carrying it was never grounded."""
    _mute_prose_arm(monkeypatch)
    _citing, chunk, _sha = _seed_paper(store)
    hub = mint_hub(
        store, CanonicalClaim(sentence="DFT shows the orphaned claim holds.", scope={})
    )
    stub = _await_stub(store, hub, "The primary whose stub was withdrawn")
    store.retire_ref(stub)
    store.add_tag(
        hub,
        Tag.closed("STATUS", "acquiring"),
        set_by="agent",
        replace_prefix=True,
    )

    bundle = evidence.load_bundle(store, hub)
    assert bundle.sources == [] and bundle.awaiting_sources == []
    assert bundle.acquiring
    assert "primary-source" in _gate_slugs(store, hub, _payload(chunk))
    assert "primary-source" not in _gate_slugs(
        store, hub, {"passages": [], "hanging": True}
    )


def test_held_primary_with_no_acquisition_state_mints_clean(
    store: Any, monkeypatch: Any
) -> None:
    """Control — the primary IS in the corpus and nothing records
    otherwise: zero violations. The awaited stub landed its text, which is
    how ``awaiting_sources`` self-clears (no cleanup pass deletes the
    edge)."""
    _mute_prose_arm(monkeypatch)
    primary, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the held claim holds.", primary, chunk)
    stub = _await_stub(store, hub, "The primary that landed")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
            "VALUES (%s, 'system', 0, 'paragraph', 'The acquired text.')",
            (stub,),
        )

    bundle = evidence.load_bundle(store, hub)
    assert bundle.awaiting_sources == []
    assert not bundle.acquiring and not bundle.primary_source_unheld
    assert gates.run_mint_gates(store, bundle, _payload(chunk, sha)) == []


def test_reword_cannot_launder_the_structural_arms(store: Any) -> None:
    """The defect that motivated all this: ``refine_claim_sentence``
    replaces ``finding_body``, so a reword erases the prose marker. It
    cannot touch an ``awaits-evidence`` edge."""
    citing, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the laundered claim holds.", citing, chunk)
    stub = _await_stub(store, hub, "The primary a reword cannot hide")
    store.chunks.replace_body_chunk(
        hub,
        "DFT shows the laundered claim holds. Paper not in corpus — needs acquisition.",
        chunk_kind="finding_body",
        source="agent",
    )
    assert evidence.ACQUISITION_MARKER.search(evidence.hub_body(store, hub))

    # The reword the retitle door performs: body replaced, marker gone.
    store.chunks.replace_body_chunk(
        hub,
        "DFT shows the reworded claim holds.",
        chunk_kind="finding_body",
        source="reviewer",
    )
    assert not evidence.ACQUISITION_MARKER.search(evidence.hub_body(store, hub))
    bundle = evidence.load_bundle(store, hub)
    assert [s.ref_id for s in bundle.awaiting_sources] == [stub]
    assert "primary-source" in _gate_slugs(store, hub, _payload(chunk, sha))


def test_backfill_moves_the_prose_marker_onto_the_declared_flag(
    store: Any, monkeypatch: Any
) -> None:
    """``precis nanopub backfill-unheld``: the one-off that lets the prose
    arm retire. Picks up exactly the marked hubs, is a no-op on re-run, and
    leaves the body text alone (``chunks`` is append-only)."""
    citing, chunk, sha = _seed_paper(store)
    marked = _seed_hub(store, "DFT shows the legacy claim holds.", citing, chunk)
    clean = _seed_hub(store, "DFT shows the tidy claim holds.", citing, chunk)
    body = "DFT shows the legacy claim holds. Paper not in corpus — needs acquisition."
    store.chunks.replace_body_chunk(
        marked, body, chunk_kind="finding_body", source="agent"
    )

    found = evidence.prose_marked_hubs(store)
    assert [h[0] for h in found] == [marked]
    assert evidence.ACQUISITION_MARKER.search(found[0][2])

    assert evidence.declare_primary_source_unheld(store, [h[0] for h in found]) == 1
    # Idempotent: a stamped hub drops out of the query, so the dry run
    # doubles as "is the prose arm retirable yet?".
    assert evidence.prose_marked_hubs(store) == []
    assert evidence.hub_body(store, marked) == body  # prose left in place

    # The structural arm now carries what the prose used to, with the
    # prose muted — which is the whole point of the move.
    _mute_prose_arm(monkeypatch)
    assert evidence.load_bundle(store, marked).primary_source_unheld
    assert "primary-source" in _gate_slugs(store, marked, _payload(chunk, sha))
    assert _gate_slugs(store, clean, _payload(chunk, sha)) == set()


def test_backfill_sees_marked_findings_that_are_not_canonical_hubs(
    store: Any,
) -> None:
    """The retirement test must span the *gate's* reach, not the corpus's
    definition of a hub. ``mint``/``approve`` apply no claim-hub predicate,
    so a ``TAPROOT:claim`` finding that lost ``STATUS:canonical`` to
    ``chase.py::_set_status`` still reaches ``check_primary_source`` — and
    all six of prod's prose-marked rows are exactly that shape. Scoping this
    query to canonical hubs (as it did until 2026-08-21) reported "no prose
    left, retirable" while their acquisition state lived only in prose."""
    citing, chunk, _sha = _seed_paper(store)
    chased = _seed_hub(store, "DFT shows the chased claim holds.", citing, chunk)
    store.chunks.replace_body_chunk(
        chased,
        "DFT shows the chased claim holds. Paper not in corpus — needs acquisition.",
        chunk_kind="finding_body",
        source="agent",
    )
    # Demote out of the claim-hub predicate the way chase.py does: the
    # STATUS tag is replaced wholesale, TAPROOT:claim is left alone.
    with store.pool.connection() as conn:
        conn.execute(
            """
            DELETE FROM ref_tags
             USING tags
             WHERE ref_tags.tag_id = tags.tag_id
               AND ref_tags.ref_id = %s
               AND tags.namespace = 'STATUS'
            """,
            (chased,),
        )

    assert [h[0] for h in evidence.prose_marked_hubs(store)] == [chased]


def test_card_variant_chunk_does_not_make_a_stub_held(store: Any) -> None:
    # ord < 0 is a synthesized card, not the paper's text.
    citing, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the card-variant claim holds.", citing, chunk)
    stub = seed_ref(store, title="Stub with a summary card", kind="paper")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
            "VALUES (%s, 'system', -1, 'card_glossary', 'A synthesized card.')",
            (stub,),
        )
    assert evidence.refs_without_body_chunks(store, [stub]) == {stub}
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=stub,
        role="corroborates",
        check_retraction=False,
    )
    assert "primary-source" in _gate_slugs(store, hub, _payload(chunk, sha))


def test_paraphrase_quote_fails_verbatim_gate(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "MOFs are anisotropic.", paper, chunk)
    payload = _payload(chunk)
    payload["passages"][0]["quote"] = "anisotropy reaches roughly 400:1"
    assert "quote-verbatim" in _gate_slugs(store, hub, payload)


def test_ambiguous_snip_fails_uniqueness(store: Any) -> None:
    text = f"First: {_QUOTE}. Later restated: {_QUOTE}."
    paper, chunk, _sha = _seed_paper(store, chunk_text=text)
    hub = _seed_hub(store, "MOFs are anisotropic (dup snip).", paper, chunk)
    assert "snip" in _gate_slugs(store, hub, _payload(chunk))


def test_quantity_without_bound_fails(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "MOFs reach 400:1 (no bound).", paper, chunk)
    payload = _payload(chunk)
    payload["fields"] = {"quantity": "400:1"}
    assert "quantity-bound" in _gate_slugs(store, hub, payload)


def test_field_not_contained_in_quote_fails(store: Any) -> None:
    # The fi176435 "2–30 GPa" class: a structured value the quotes never state.
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "MOFs span 2-30 GPa (overclaim).", paper, chunk)
    payload = _payload(chunk)
    payload["fields"] = {"quantity": "2–30 GPa", "quantity_bound": "approx-range"}
    assert "field-containment" in _gate_slugs(store, hub, payload)


def test_claim_without_quote_needs_explicit_hanging(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the ungrounded claim persists.", paper, chunk)
    assert "schema-lint" in _gate_slugs(store, hub, {"passages": []})
    # Explicitly hanging is mintable (publish preflight blocks it later).
    assert _gate_slugs(store, hub, {"passages": [], "hanging": True}) == set()


def test_hypothesis_with_quote_is_a_hard_error(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "A conjecture.", paper, chunk)
    payload = _payload(chunk, hypothesis=True)
    slugs = _gate_slugs(store, hub, payload)
    assert "schema-lint" in slugs  # quote on a hypothesis


def test_agent_parked_payload_without_llm_models_is_refused(store: Any) -> None:
    """fi211520 post-mortem: an agent-prepared payload must name its
    authoring model(s). The parked-proposal marker on the hub is the
    "an agent prepared this" signal; a human-typed payload stays exempt."""
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(
        store, "DFT shows MOFs can be anisotropic up to 400:1.", paper, chunk
    )
    bundle = evidence.load_bundle(store, hub)
    parked: dict[str, Any] = {gates.META_PROPOSED_PAYLOAD: {"passages": []}}

    refused = {
        v.gate
        for v in gates.run_mint_gates(store, bundle, _payload(chunk), hub_meta=parked)
    }
    assert "llm-attribution" in refused

    attributed = _payload(chunk, llm_models=["claude-fable-5"])
    assert {
        v.gate for v in gates.run_mint_gates(store, bundle, attributed, hub_meta=parked)
    } == set()

    # No parked marker = human-typed = exempt.
    assert "llm-attribution" not in _gate_slugs(store, hub, _payload(chunk))


def test_malformed_llm_models_is_refused_even_unparked(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows a malformed envelope key.", paper, chunk)
    for bad in ("claude-fable-5", [], [""], [42]):
        assert "llm-attribution" in _gate_slugs(
            store, hub, _payload(chunk, llm_models=bad)
        )


def test_parked_marker_constant_mirrors_the_handler() -> None:
    # gates.py cannot import the handler (module cycle), so the key is
    # mirrored as a literal — this is the pin that keeps the mirror honest.
    from precis.handlers._finding_hypothesis import META_PROPOSED_PAYLOAD

    assert gates.META_PROPOSED_PAYLOAD == META_PROPOSED_PAYLOAD


def test_fold_llm_models_is_additive_and_deduped() -> None:
    # The frozen envelope's attribution can never be dropped by sign; the
    # CLI --llm-model flag only adds.
    fold = mint._fold_llm_models
    assert fold({"llm_models": ["a", "b"]}, ["b", "c", "  "]) == ["a", "b", "c"]
    assert fold({}, None) == []
    assert fold({"llm_models": []}, ["x"]) == ["x"]


def test_dup_pdf_sha_rows_block_mint(store: Any) -> None:
    # The ref-5937 class: two sha rows from dup ingest.
    paper, chunk, sha = _seed_paper(store)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('pdf_sha256', %s, %s, 'test')",
            ("dead" * 16, paper),
        )
    hub = _seed_hub(store, "Ambiguous copy.", paper, chunk)
    assert "pdf-sha" in _gate_slugs(store, hub, _payload(chunk))


def test_contradicts_edge_blocks_mint_first(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "Contact angle is 85 degrees.", paper, chunk)
    disputing, _c, _s = _seed_paper(store, doi="10.1038/41284")
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=disputing,
        role="contradicts",
        check_retraction=False,
    )
    assert "contradicts" in _gate_slugs(store, hub, _payload(chunk))


# ── bimodal evidence read ───────────────────────────────────────────────


def test_bundle_reads_outbound_derived_from_shape(store: Any) -> None:
    # The dry-run-49 lesson: 37/49 hubs carry only hub→paper derived-from.
    paper, _chunk, _sha = _seed_paper(store)
    hub = mint_hub(store, CanonicalClaim(sentence="Lineage-only hub.", scope={}))
    store.add_link(
        src_ref_id=hub,
        dst_ref_id=paper,
        relation="derived-from",
        set_by="agent",
    )
    bundle = evidence.load_bundle(store, hub)
    assert [s.role for s in bundle.sources] == ["derived-from"]
    assert bundle.sources[0].via == "outbound"


# ── approve → sign → view ───────────────────────────────────────────────


def test_full_mint_pipeline(store: Any, monkeypatch: Any) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)

    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(
        store, "DFT shows MOFs can be anisotropic up to 400:1.", paper, chunk
    )

    row = mint.approve(store, hub, payload=_payload(chunk, sha), interactive=True)
    assert row.state == "reviewed"
    assert row.aida_uri is not None and "%20" in row.aida_uri

    signed = mint.sign(store, hub)
    assert signed.state == "signed"
    assert signed.trusty_uri is not None
    assert signed.trusty_uri.startswith("https://w3id.org/np/RA")

    artifact = store.nanopub_artifact(signed.artifact_id)
    assert artifact is not None
    assert artifact.dois == ["10.1103/PhysRevLett.109.195502"]
    trig = artifact.trig_bytes.decode("utf-8")
    assert _QUOTE in trig
    assert sha in trig  # sourcePdfSha256 pins the exact quoted copy

    # The view now serves the exact frozen bytes.
    from precis.handlers._finding_nanopub import render_nanopub_view

    ref = store.fetch_refs_by_ids([hub])[hub]
    body = render_nanopub_view(store, ref).body
    assert signed.trusty_uri in body
    assert trig in body


def test_frozen_llm_models_land_in_the_signed_artifact(
    store: Any, monkeypatch: Any
) -> None:
    """The auto-flow: llm_models in the approve payload → frozen into
    `grounding` → folded into pubinfo at sign with NO --llm-model flag."""
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)

    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(
        store, "DFT shows MOFs can be anisotropic up to 400:1.", paper, chunk
    )
    payload = _payload(chunk, sha, llm_models=["claude-fable-5"])
    row = mint.approve(store, hub, payload=payload, interactive=True)
    assert row.grounding["llm_models"] == ["claude-fable-5"]

    signed = mint.sign(store, hub)
    artifact = store.nanopub_artifact(signed.artifact_id)
    assert artifact is not None
    trig = artifact.trig_bytes.decode("utf-8")
    assert "llmModel" in trig
    assert "claude-fable-5" in trig


def test_sign_refuses_on_title_drift(store: Any, monkeypatch: Any) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)

    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the approved sentence holds.", paper, chunk)
    mint.approve(store, hub, payload=_payload(chunk), interactive=True)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET title = 'A silently edited sentence.' WHERE ref_id = %s",
            (hub,),
        )
    with pytest.raises(mint.MintGateError) as exc:
        mint.sign(store, hub)
    assert any(v.gate == "drift" for v in exc.value.violations)


def test_approve_title_override_syncs_hub_and_signs(
    store: Any, monkeypatch: Any
) -> None:
    """A review-time title override runs through the retitle door: it must
    sync refs.title (full length, not [:200]) AND the finding_body chunk the
    dedup index retrieves over, so gate #14 (drift) fires only on
    post-approval edits and no hub is left half-reworded."""
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)

    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(
        store, "DFT shows the short pre-review sentence holds.", paper, chunk
    )
    long_title = (
        "Spin-polarized DFT calculations on graphene-fullerene nanobuds find "
        "two distinct junction bonding configurations with net magnetic "
        "moments of 5.76 and 5.55 Bohr magnetons, while other configurations "
        "remain non-magnetic overall."
    )
    assert len(long_title) > 200
    row = mint.approve(
        store, hub, payload=_payload(chunk, sha), title=long_title, interactive=True
    )
    assert row.approved_title == long_title
    ref = store.fetch_refs_by_ids([hub])[hub]
    assert ref.title == long_title
    # canon.block() ANN-retrieves over finding_body — a title-only sync
    # leaves the dedup index searching the pre-review wording.
    assert _finding_body(store, hub) == long_title
    signed = mint.sign(store, hub)
    assert signed.state == "signed"


def test_approve_gates_the_reviewers_override_not_the_old_sentence(
    store: Any,
) -> None:
    """The Layer-A gates must validate exactly the string that gets frozen.
    A hub whose live sentence lints clean cannot smuggle an inadmissible
    override past approve just because the OLD text passed."""
    paper, chunk, sha = _seed_paper(store)
    clean = "DFT shows MOFs can be anisotropic up to 400:1."
    hub = _seed_hub(store, clean, paper, chunk)
    assert _gate_slugs(store, hub, _payload(chunk, sha)) == set()

    with pytest.raises(mint.MintGateError) as exc:
        mint.approve(
            store,
            hub,
            payload=_payload(chunk, sha),
            # No controlled evidence verb (predicts/finds/shows/…) — the
            # blocking half of lint_claim_sentence.
            title="DFT suggests MOFs can be anisotropic up to 400:1.",
            interactive=True,
        )
    assert any(v.gate == "claim-sentence" for v in exc.value.violations)
    assert any("no-evidence-verb" in v.message for v in exc.value.violations)
    # Nothing froze: the row never left candidate (it was never created).
    assert store.nanopub_publish_row(hub) is None


def test_approve_reword_cannot_launder_the_acquisition_marker(store: Any) -> None:
    """A reword must not buy its way past the acquisition-marker gate.

    refine_claim_sentence replaces finding_body with the new sentence, so
    a title override that drops the harvester's "not in corpus" note would
    erase the marker on the very call that approves the claim — publishing
    a hub whose primary source we do not hold, grounded only in a citing
    paper. approve() runs the acquisition gate against the PRE-reword body
    (and refuses before the rewrite) for exactly this reason."""
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the pre-review sentence holds.", paper, chunk)
    # Chase-born shape: the body is harvester prose, not a sentence copy.
    store.chunks.replace_body_chunk(
        hub,
        "DFT shows the pre-review sentence holds. Paper not in corpus — "
        "needs acquisition.",
        chunk_kind="finding_body",
        source="test",
    )
    assert "primary-source" in _gate_slugs(store, hub, _payload(chunk, sha))

    with pytest.raises(mint.MintGateError) as exc:
        mint.approve(
            store,
            hub,
            payload=_payload(chunk, sha),
            title="DFT shows MOFs can be anisotropic up to 400:1.",
            interactive=True,
        )
    assert any(v.gate == "primary-source" for v in exc.value.violations)
    assert store.nanopub_publish_row(hub) is None


def test_approve_retry_after_a_marker_refusal_still_refuses(store: Any) -> None:
    """The retry hole: refusing with a pre-reword SNAPSHOT is not enough.

    The reword lands anyway (a gate refusal leaves the hub reworded), so
    the reviewer's second, identical approve finds requested == title —
    no reword, nothing to snapshot — and re-reads a body the first call
    already laundered. approve() therefore refuses the acquisition gate
    BEFORE the rewrite, so the marker is still there on every retry."""
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT shows the pre-review sentence holds.", paper, chunk)
    marked = (
        "DFT shows the pre-review sentence holds. Paper not in corpus — "
        "needs acquisition."
    )
    store.chunks.replace_body_chunk(
        hub, marked, chunk_kind="finding_body", source="test"
    )
    reworded = "DFT shows MOFs can be anisotropic up to 400:1."

    for attempt in (1, 2):
        with pytest.raises(mint.MintGateError) as exc:
            mint.approve(
                store,
                hub,
                payload=_payload(chunk, sha),
                title=reworded,
                interactive=True,
            )
        assert any(v.gate == "primary-source" for v in exc.value.violations), attempt
        assert store.nanopub_publish_row(hub) is None, attempt
    # Refusing before the rewrite also means the marker survives intact —
    # nothing to carry forward, nothing for the next writer to forget.
    assert _finding_body(store, hub) == marked


def test_approve_reword_recomputes_pub_id_and_keeps_the_old_as_alias(
    store: Any,
) -> None:
    """Rewording IS a new claim identity (module docstring) — approve routes
    the override through refine_claim_sentence, so the content-derived
    pub_id is recomputed and the old handle stays live as an alias."""
    from precis.identity import make_pub_id, make_taproot_hub_paper_id

    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(
        store, "DFT shows the short pre-review sentence holds.", paper, chunk
    )
    old_pub_ids = _pub_ids(store, hub)
    assert len(old_pub_ids) == 1

    reworded = "DFT shows the reviewer-sharpened sentence holds up to 400:1."
    mint.approve(
        store, hub, payload=_payload(chunk, sha), title=reworded, interactive=True
    )

    expected = make_pub_id(make_taproot_hub_paper_id(reworded, {}))
    assert expected not in old_pub_ids
    # New identity minted, old handle kept — prose citing [<old>] resolves.
    assert _pub_ids(store, hub) == old_pub_ids | {expected}


def test_approve_reword_survives_a_failed_flip_as_a_candidate(
    store: Any, monkeypatch: Any
) -> None:
    """Ordering contract: the hub syncs BEFORE the publish row flips, so a
    failure at the flip leaves a benign retitled STILL-candidate hub — never
    a reviewed row whose frozen sha spuriously drift-fails at sign."""
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(
        store, "DFT shows the short pre-review sentence holds.", paper, chunk
    )
    reworded = "DFT shows the reviewer-sharpened sentence holds up to 400:1."
    monkeypatch.setattr(store, "nanopub_approve", lambda *a, **k: False)

    with pytest.raises(BadInput, match="left candidate state mid-approve"):
        mint.approve(
            store, hub, payload=_payload(chunk, sha), title=reworded, interactive=True
        )

    ref = store.fetch_refs_by_ids([hub])[hub]
    assert ref.title == reworded
    assert _finding_body(store, hub) == reworded
    row = store.nanopub_publish_row(hub)
    assert row is not None and row.state == "candidate"
    assert row.claim_sha is None and row.approved_title is None


def test_compound_requires_signed_atoms_then_chains_them(
    store: Any, monkeypatch: Any
) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)

    paper, chunk, sha = _seed_paper(store)
    atom = _seed_hub(store, "DFT shows atom A holds.", paper, chunk)
    compound = mint_hub(
        store, CanonicalClaim(sentence="DFT shows atom A holds and matters.", scope={})
    )
    link_claims(
        store,
        from_hub_ref_id=atom,
        to_hub_ref_id=compound,
        relation="conjunct-of",
    )

    # Approval order is free: the compound's text freezes even though its
    # atom is unsigned — only SIGNING is topo-constrained.
    row = mint.approve(store, compound, payload={"passages": []}, interactive=True)
    with pytest.raises(mint.MintGateError) as exc:
        mint.sign(store, compound)
    assert any(v.gate == "mint-order" for v in exc.value.violations)

    mint.approve(store, atom, payload=_payload(chunk), interactive=True)
    atom_row = mint.sign(store, atom)

    signed = mint.sign(store, compound)
    assert signed.dependency_codes == {str(atom): atom_row.trusty_uri}
    trig = store.nanopub_artifact(signed.artifact_id).trig_bytes.decode()
    assert atom_row.trusty_uri in trig  # provenance hash-chain
    assert atom_row.aida_uri in trig  # assertion names the atom semantically
    assert row.artifact_type == "compound"

    # Re-mint cascade: atom re-signs → compound flips signed → reviewed.
    assert store.nanopub_reopen(atom_row.id)
    mint.approve(store, atom, payload=_payload(chunk), interactive=True)
    new_atom_row = mint.sign(store, atom)
    assert new_atom_row.trusty_uri != atom_row.trusty_uri
    assert mint.check_dependency_drift(store, store.nanopub_publish_row(compound))
    assert store.nanopub_publish_row(compound).state == "reviewed"


def test_attesting_key_needs_the_interactive_door(store: Any, monkeypatch: Any) -> None:
    from precis.nanopub.keys import load_profile

    with pytest.raises(PermissionError):
        load_profile(store, "attesting", interactive=False)


def test_approve_needs_the_interactive_door(store: Any) -> None:
    # Approval IS the human review act — a worker/job calling it without
    # the interactive door is a defect (no bulk backfill, by design).
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "Batch-approved claim.", paper, chunk)
    with pytest.raises(PermissionError):
        mint.approve(store, hub, payload=_payload(chunk))


def test_advisory_lint_is_exactly_the_nonblocking_half() -> None:
    """``advisory_lint`` returns the lint warnings the mint gate does NOT
    enforce, and only those: a sentence carrying both a blocking code
    (e-notation) and an advisory one (tilde-approximation) splits cleanly
    — the advisory list carries the tilde note, the blocking code stays
    with ``check_claim_sentence``, neither leaks into the other, and an
    empty sentence yields no advisories."""
    from precis.nanopub.gates import advisory_lint, check_claim_sentence

    s = "DFT shows the barrier is ~0.5 eV with a rate of 1e-6 per second."
    advisories = advisory_lint(s)
    codes = [w.split(":", 1)[0].strip() for w in advisories]
    assert "tilde-approximation" in codes
    assert "e-notation" not in codes  # blocking — never advisory
    assert len(codes) == len(set(codes))  # de-duplicated
    blocking_msgs = " ".join(v.message for v in check_claim_sentence(s))
    assert "e-notation" in blocking_msgs
    assert advisory_lint("") == []
