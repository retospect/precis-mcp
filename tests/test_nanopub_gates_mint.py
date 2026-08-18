"""Layer-A mint gates + the approve→sign→view pipeline. DB-backed;
signing keys come from env (the secrets resolver is env-first), so no
vault rows and no network are involved."""

from __future__ import annotations

from typing import Any

import pytest

from precis.nanopub import evidence, gates, mint
from precis.nanopub.keys import generate_keypair
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
    doi: str = "10.1103/PhysRevLett.109.195502",
    sha: str | None = None,
    chunk_text: str = f"Tensorial analysis. {_QUOTE}, in stark contrast.",
    section: list[str] | None = None,
) -> tuple[int, int, str]:
    """A paper ref with meta.doi, one body chunk, and a pdf_sha256
    identifier row (unique per paper — the identifiers PK is
    ``(id_kind, id_value)``). Returns ``(ref_id, chunk_id, sha)``."""
    ref_id = seed_ref(store, title="Anisotropic Elastic Properties", kind="paper")
    if sha is None:
        sha = f"{ref_id:064x}"
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_build_object('doi', %s::text) "
            "WHERE ref_id = %s",
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


def _gate_slugs(store: Any, hub: int, payload: dict[str, Any]) -> set[str]:
    bundle = evidence.load_bundle(store, hub)
    return {v.gate for v in gates.run_mint_gates(store, bundle, payload)}


# ── gates ───────────────────────────────────────────────────────────────


def test_clean_payload_passes_all_gates(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "MOFs can be anisotropic up to 400:1.", paper, chunk)
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
    hub = _seed_hub(store, "MOFs can be anisotropic up to 400:1.", paper, chunk)
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
    hub = _seed_hub(store, "An ungrounded claim.", paper, chunk)
    assert "schema-lint" in _gate_slugs(store, hub, {"passages": []})
    # Explicitly hanging is mintable (publish preflight blocks it later).
    assert _gate_slugs(store, hub, {"passages": [], "hanging": True}) == set()


def test_hypothesis_with_quote_is_a_hard_error(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "A conjecture.", paper, chunk)
    payload = _payload(chunk, hypothesis=True)
    slugs = _gate_slugs(store, hub, payload)
    assert "schema-lint" in slugs  # quote on a hypothesis


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
    hub = _seed_hub(store, "MOFs can be anisotropic up to 400:1.", paper, chunk)

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


def test_sign_refuses_on_title_drift(store: Any, monkeypatch: Any) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)

    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "The approved sentence.", paper, chunk)
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
    """A review-time title override must sync refs.title (full length, not
    [:200]) so gate #14 (drift) fires only on post-approval edits."""
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)

    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "Short pre-review sentence.", paper, chunk)
    long_title = (
        "Spin-polarized DFT on graphene-fullerene nanobuds finds two junction "
        "bonding configurations whose next-nearest-atom bonds give strong "
        "ferromagnetic coupling, with net magnetic moments of 5.76 and 5.55 "
        "Bohr magnetons, while the other bonding configurations are "
        "non-magnetic."
    )
    assert len(long_title) > 200
    row = mint.approve(
        store, hub, payload=_payload(chunk, sha), title=long_title, interactive=True
    )
    assert row.approved_title == long_title
    ref = store.fetch_refs_by_ids([hub])[hub]
    assert ref.title == long_title
    signed = mint.sign(store, hub)
    assert signed.state == "signed"


def test_compound_requires_signed_atoms_then_chains_them(
    store: Any, monkeypatch: Any
) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)

    paper, chunk, sha = _seed_paper(store)
    atom = _seed_hub(store, "Atom A holds.", paper, chunk)
    compound = mint_hub(
        store, CanonicalClaim(sentence="Atom A holds and matters.", scope={})
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
