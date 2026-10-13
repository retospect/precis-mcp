"""The web anchor for nanopub evidence (Reto 2026-10-03, review item
claims-and-evidence-3, option 1): a web page is cited by ``urn:sha256`` of
the raw bytes of its last fetch (``cache_state.content_sha256``, migration
0202) plus its URL as a second triple; the gate refuses a page with no sha
on record; approve freezes the sha from the record; a later differing sha
is a display-only drift signal. DB-backed; reuses the seed helpers of
``test_nanopub_gates_mint`` / ``test_nanopub_non_paper_sources``."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb
from rdflib import RDFS, URIRef

from precis.nanopub import assemble, evidence, gates, mint
from tests.test_nanopub_gates_mint import _gate_slugs, _seed_hub, _seed_paper
from tests.test_nanopub_non_paper_sources import (
    _SENTENCE,
    _anchorless_payload,
    _seed_non_paper_source,
)

_URL = "https://example.com/canatu/annual-report"
_SHA = "ab" * 32
_SHA_2 = "cd" * 32


def _give_cache_state(
    store: Any, ref_id: int, *, sha: str | None, url: str | None = _URL
) -> None:
    """The ``cache_state`` row a web fetch writes (provider ``web``)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO cache_state (ref_id, provider, request_hash, "
            "fetched_at, fresh_until, meta, content_sha256) "
            "VALUES (%s, 'web', %s, now(), now() + interval '7 days', %s, %s)",
            (ref_id, f"hash-{ref_id}", Jsonb({"url": url} if url else {}), sha),
        )


def _set_sha(store: Any, ref_id: int, sha: str | None) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE cache_state SET content_sha256 = %s WHERE ref_id = %s",
            (sha, ref_id),
        )


def _web_hub(
    store: Any, *, sha: str | None = _SHA, slug: str = "example-com-annual-report"
) -> tuple[int, int, int]:
    """A hub whose approved passage quotes a web page. ``web`` is not an
    attachable evidence kind, so the hub carries an ordinary paper edge and
    the payload's chunk names the page. Returns ``(hub, web_ref, web_chunk)``."""
    web_ref, web_chunk = _seed_non_paper_source(store, "web", slug=slug)
    _give_cache_state(store, web_ref, sha=sha)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    return hub, web_ref, web_chunk


def test_web_anchor_is_the_content_sha_with_the_url_as_a_second_identifier() -> None:
    anchor = evidence.source_anchor(
        "web", "example-com-a", None, content_sha256=_SHA, url=_URL
    )
    assert anchor.source_uri == f"urn:sha256:{_SHA}"
    assert anchor.source_url == _URL
    # No sha, no anchor — a page cannot be cited by its URL alone.
    assert evidence.source_anchor("web", "x", None, url=_URL).source_uri is None


def test_web_page_with_a_sha_passes_the_gates(store: Any) -> None:
    hub, _web, chunk = _web_hub(store)
    assert _gate_slugs(store, hub, _anchorless_payload(chunk)) == set()


def test_web_page_without_a_sha_is_refused_with_a_refetch_hint(store: Any) -> None:
    hub, _web, chunk = _web_hub(store, sha=None)
    bundle = evidence.load_bundle(store, hub)
    violations = gates.run_mint_gates(store, bundle, _anchorless_payload(chunk))
    (v,) = [x for x in violations if x.gate == "grounding"]
    assert "no content_sha256 on record" in v.message
    assert "re-fetch" in v.message
    # The page has no PDF copy: the pdf-sha gate stays out of it.
    assert not [x for x in violations if x.gate == "pdf-sha"]


def test_web_page_with_no_cache_row_is_refused(store: Any) -> None:
    web_ref, chunk = _seed_non_paper_source(store, "web", slug="no-cache-row")
    paper, pchunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, pchunk)
    assert web_ref
    assert "grounding" in _gate_slugs(store, hub, _anchorless_payload(chunk))


def test_approve_freezes_the_sha_from_the_record_not_the_payload(store: Any) -> None:
    hub, web_ref, chunk = _web_hub(store)
    payload = _anchorless_payload(chunk)
    p = payload["passages"][0]
    # Reviewer-editable fields that must never be the authority.
    p["content_sha256"] = "ee" * 32
    p["source_uri"] = "urn:sha256:" + "ee" * 32
    p["source_url"] = "https://attacker.example/page"
    p["source_ref_id"] = 999999
    p["pdf_sha256"] = "ff" * 32

    row = mint.approve(store, hub, payload=payload, interactive=True)
    (frozen,) = row.grounding["passages"]
    assert frozen["content_sha256"] == _SHA
    assert frozen["source_uri"] == f"urn:sha256:{_SHA}"
    assert frozen["source_url"] == _URL
    assert frozen["source_ref_id"] == web_ref
    assert "pdf_sha256" not in frozen

    inp, _deps = mint._mint_input(store, row, evidence.load_bundle(store, hub))
    _, prov, _ = assemble.build_graphs(inp, assemble.DRAFT_NS)
    node = URIRef(f"urn:sha256:{_SHA}")
    assert (node, RDFS.seeAlso, URIRef(_URL)) in prov
    assert "attacker.example" not in prov.serialize(format="nt")


def test_a_later_refresh_does_not_change_the_frozen_sha(store: Any) -> None:
    hub, web_ref, chunk = _web_hub(store)
    row = mint.approve(store, hub, payload=_anchorless_payload(chunk), interactive=True)
    _set_sha(store, web_ref, _SHA_2)
    reread = store.nanopub_publish_row(hub)
    assert reread.grounding["passages"][0]["content_sha256"] == _SHA
    assert row.state == reread.state


def test_drift_is_reported_when_the_live_sha_differs_and_not_otherwise(
    store: Any,
) -> None:
    hub, web_ref, chunk = _web_hub(store)
    row = mint.approve(store, hub, payload=_anchorless_payload(chunk), interactive=True)
    passages = row.grounding["passages"]

    assert evidence.web_source_drift(store, passages) == []

    _set_sha(store, web_ref, _SHA_2)
    (drift,) = evidence.web_source_drift(store, passages)
    assert drift["ref_id"] == web_ref
    assert drift["frozen_sha256"] == _SHA
    assert drift["live_sha256"] == _SHA_2
    assert drift["url"] == _URL

    # Back to the approved bytes: the notice goes away again.
    _set_sha(store, web_ref, _SHA)
    assert evidence.web_source_drift(store, passages) == []

    # A page with no live sha has nothing to compare: no drift claimed.
    _set_sha(store, web_ref, None)
    assert evidence.web_source_drift(store, passages) == []


def test_drift_ignores_passages_that_are_not_frozen_web_sources(store: Any) -> None:
    paper, chunk, sha = _seed_paper(store)
    assert (
        evidence.web_source_drift(store, [{"chunk_id": chunk, "pdf_sha256": sha}]) == []
    )
    assert evidence.web_source_drift(store, []) == []


def test_a_web_source_is_accepted_for_a_report_under_arm_kind_web(
    store: Any,
) -> None:
    _hub, web_ref, chunk = _web_hub(store)
    passages = [{"chunk_id": chunk}]
    (verdict,) = gates.report_source_verdicts(store, passages)
    assert verdict.ref_id == web_ref
    assert verdict.accepted
    assert verdict.arm == "kind:web"
    assert gates.check_report_sources(store, passages) == []


# ── unchanged refresh keeps its chunks; changed refresh replaces them ────


def _refresh(store: Any, ref_id: int, *, sha: str | None, text: str) -> None:
    """What a web refresh does to the store: ``update_cache_entry`` with the
    freshly fetched body and the new content sha."""
    from precis.store.types import ChunkInsert

    store.update_cache_entry(
        ref_id=ref_id,
        title="Annual report",
        body_blocks=[ChunkInsert(ord=0, text=text)],
        ttl_seconds=3600,
        cache_meta={"url": _URL},
        content_sha256=sha,
    )


def _chunk_ids(store: Any, ref_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY chunk_id",
            (ref_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _fetched_at(store: Any, ref_id: int) -> Any:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT fetched_at FROM cache_state WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def test_unchanged_refresh_keeps_the_chunk_ids(store: Any) -> None:
    web_ref, _chunk = _seed_non_paper_source(store, "web", slug="keep-chunks")
    _give_cache_state(store, web_ref, sha=_SHA)
    before = _chunk_ids(store, web_ref)
    stamp = _fetched_at(store, web_ref)

    _refresh(store, web_ref, sha=_SHA, text="identical page text")

    assert _chunk_ids(store, web_ref) == before
    # The fetch metadata still moved: only the chunks were spared.
    assert _fetched_at(store, web_ref) > stamp


def test_changed_or_unknown_sha_refresh_replaces_the_chunks(store: Any) -> None:
    changed_ref, _c = _seed_non_paper_source(store, "web", slug="changed")
    _give_cache_state(store, changed_ref, sha=_SHA)
    before = _chunk_ids(store, changed_ref)
    _refresh(store, changed_ref, sha=_SHA_2, text="revised page text")
    after = _chunk_ids(store, changed_ref)
    assert after and not set(after) & set(before)

    legacy_ref, _c2 = _seed_non_paper_source(store, "web", slug="legacy-null")
    _give_cache_state(store, legacy_ref, sha=None)
    before = _chunk_ids(store, legacy_ref)
    _refresh(store, legacy_ref, sha=_SHA, text="first sha ever")
    after = _chunk_ids(store, legacy_ref)
    assert after and not set(after) & set(before)
    assert evidence.web_content_sha(store, legacy_ref)[0] == _SHA


# ── post-approve: a drifted web source never voids the approval ──────────


def _changed_page(store: Any, web_ref: int) -> None:
    _refresh(store, web_ref, sha=_SHA_2, text="Entirely rewritten page.")


def test_sign_succeeds_after_the_page_changed_and_its_chunks_were_replaced(
    store: Any, monkeypatch: Any
) -> None:
    from precis.nanopub.keys import generate_keypair

    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    hub, web_ref, chunk = _web_hub(store)
    row = mint.approve(store, hub, payload=_anchorless_payload(chunk), interactive=True)
    _changed_page(store, web_ref)
    assert chunk not in _chunk_ids(store, web_ref)  # the approved chunk is gone

    signed = mint.sign(store, hub)
    assert signed.state == "signed" and signed.trusty_uri
    # The signed artifact still cites the page as approved, not as it is now.
    assert row.grounding["passages"][0]["source_uri"] == f"urn:sha256:{_SHA}"
    artifact = store.nanopub_artifact(signed.artifact_id)
    trig = artifact.trig_bytes.decode("utf-8")
    assert f"urn:sha256:{_SHA}" in trig
    assert f"urn:sha256:{_SHA_2}" not in trig
    # ... and the drift notice has its data.
    drift = evidence.web_source_drift(store, signed.grounding["passages"])
    assert [d["ref_id"] for d in drift] == [web_ref]


def test_frozen_gates_tolerate_a_vanished_web_chunk_but_not_a_paper_one(
    store: Any,
) -> None:
    hub, web_ref, chunk = _web_hub(store)
    row = mint.approve(store, hub, payload=_anchorless_payload(chunk), interactive=True)
    _changed_page(store, web_ref)
    bundle = evidence.load_bundle(store, hub)

    # Pre-approve semantics (the default) still refuse the missing chunk.
    assert gates.run_mint_gates(store, bundle, row.grounding)
    assert gates.run_mint_gates(store, bundle, row.grounding, frozen=True) == []

    # A tampered frozen sha/uri pair is not tolerated.
    bad = {
        **row.grounding,
        "passages": [
            {**row.grounding["passages"][0], "source_uri": "urn:sha256:" + "00" * 32}
        ],
    }
    assert gates.run_mint_gates(store, bundle, bad, frozen=True)


def test_a_vanished_paper_chunk_still_refuses_even_when_frozen(store: Any) -> None:
    from tests.test_nanopub_gates_mint import _payload

    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, _SENTENCE, paper, chunk)
    row = mint.approve(store, hub, payload=_payload(chunk, sha), interactive=True)
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk,))
    bundle = evidence.load_bundle(store, hub)
    violations = gates.run_mint_gates(store, bundle, row.grounding, frozen=True)
    assert any("not found" in v.message for v in violations)
