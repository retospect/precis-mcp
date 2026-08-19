"""Registry mirror (migration 0130): parse-leniently/validate-strictly
indexing, upsert immutability, PK-diff sync with an injected fetch, the
authoritative-retraction flag rule, and the concurrence alert. No
network anywhere — the "registry" is a dict."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from precis.nanopub import mirror
from tests.test_nanopub_preflight import _signed_hub

FIXTURES = Path(__file__).parent.parent / "docs" / "reference" / "nanopub-example"

#: A well-formed (shape-wise) but fake artifact code for hand-made rows.
FAKE = "RA" + "x" * 43


def _code_of(row: Any) -> str:
    return str(row.trusty_uri).rsplit("/", 1)[-1]


def _plain_row(
    store: Any,
    code: str,
    *,
    signer: str | None = None,
    verified: bool = True,
    aida_uri: str | None = None,
) -> None:
    store.mirror_upsert(
        code,
        trig_bytes=f"# stub {code}".encode(),
        source_url=f"test://np/{code}",
        verified=verified,
        signer=signer,
        aida_uri=aida_uri,
    )


# ── indexing ────────────────────────────────────────────────────────────


def test_our_own_signed_artifact_verifies_as_external(
    store: Any, monkeypatch: Any
) -> None:
    """The strongest fixture is a real signed nanopub: our own artifact,
    fed to the mirror indexer as if fetched, must verify and index."""
    _hub, row = _signed_hub(
        store, monkeypatch, "DFT shows a mirror-roundtrip claim holds."
    )
    artifact = store.nanopub_artifact(row.artifact_id)
    code = _code_of(artifact)

    idx = mirror.ingest_one(
        store, code, artifact.trig_bytes, source_url=f"test://np/{code}"
    )
    assert idx.verified
    assert idx.signer == artifact.signer
    assert idx.key_fingerprint == artifact.key_fingerprint
    assert idx.assertion_predicates  # parsed assertion graph

    stored = store.mirror_row(code)
    assert stored is not None and stored.verified
    assert stored.trig_bytes == artifact.trig_bytes
    assert stored.byte_sha256 == artifact.byte_sha256


def test_wrong_code_fails_verification(store: Any, monkeypatch: Any) -> None:
    """Anti-substitution: valid bytes served under a DIFFERENT code must
    not verify (a hostile mirror can't swap artifacts)."""
    _hub, row = _signed_hub(store, monkeypatch, "DFT shows a substituted claim holds.")
    artifact = store.nanopub_artifact(row.artifact_id)
    idx = mirror.index_bytes(FAKE, artifact.trig_bytes)
    assert not idx.verified


def test_placeholder_fixture_parses_but_does_not_verify() -> None:
    """The docs fixtures carry RAPLACEHOLDER codes — well-formed
    structure, invalid trusty. Lenient parse, strict validation."""
    trig = (FIXTURES / "qi-atom-a-mechanical-tuning.trig").read_bytes()
    idx = mirror.index_bytes("RAPLACEHOLDER_ATOM_A", trig)
    assert not idx.verified
    assert idx.assertion_predicates  # it still parsed and indexed


def test_garbage_bytes_are_kept_but_unverified(store: Any) -> None:
    idx = mirror.ingest_one(
        store, FAKE, b"\x00 not trig at all", source_url="test://np/x"
    )
    assert not idx.verified and not idx.assertion_predicates
    stored = store.mirror_row(FAKE)
    assert stored is not None and not stored.verified


def test_extraction_crash_degrades_to_unverified(store: Any, monkeypatch: Any) -> None:
    """A parseable artifact whose post-parse extraction blows up must
    still be cached (verified=False) — otherwise it becomes a poison
    code re-fetched on every future pass."""
    trig = (FIXTURES / "qi-atom-a-mechanical-tuning.trig").read_bytes()

    def boom(ds: Any, meta: Any, code: str) -> Any:
        raise RuntimeError("rdflib had a bad day")

    monkeypatch.setattr(mirror, "_extract", boom)
    idx = mirror.ingest_one(
        store, "RAPLACEHOLDER_ATOM_A", trig, source_url="test://np/x"
    )
    assert not idx.verified
    stored = store.mirror_row("RAPLACEHOLDER_ATOM_A")
    assert stored is not None and not stored.verified


# ── upsert immutability ─────────────────────────────────────────────────


def test_verified_row_is_never_overwritten(store: Any) -> None:
    _plain_row(store, FAKE, verified=True)
    original = store.mirror_row(FAKE)
    assert original is not None
    assert not store.mirror_upsert(
        FAKE, trig_bytes=b"different", source_url="test://2", verified=False
    )
    after = store.mirror_row(FAKE)
    assert after is not None and after.trig_bytes == original.trig_bytes


def test_unverified_row_may_be_refetched(store: Any) -> None:
    _plain_row(store, FAKE, verified=False)
    assert store.mirror_upsert(
        FAKE, trig_bytes=b"better bytes", source_url="test://2", verified=True
    )
    after = store.mirror_row(FAKE)
    assert after is not None and after.verified
    assert after.trig_bytes == b"better bytes"


# ── sync (injected fetch) ───────────────────────────────────────────────


def test_sync_fetches_only_the_pk_diff(store: Any) -> None:
    import json

    have = "RA" + "h" * 43
    miss1 = "RA" + "m" * 43
    miss2 = "RA" + "n" * 43
    _plain_row(store, have)

    fetched_urls: list[str] = []

    def fake_fetch(url: str) -> bytes:
        fetched_urls.append(url)
        if url.endswith("/nanopubs.json"):
            return json.dumps([have, miss1, miss2]).encode()
        return b"# stub artifact"

    result = mirror.sync(store, limit=1, fetch=fake_fetch, delay_s=0)
    assert result.listed == 3 and result.already == 1
    assert result.fetched == 1 and result.remaining == 1
    assert any(url.endswith(f"/np/{miss1}.trig") for url in fetched_urls)
    assert not any(have in url for url in fetched_urls if "/np/" in url)

    result = mirror.sync(store, limit=10, fetch=fake_fetch, delay_s=0)
    assert result.fetched == 1 and result.remaining == 0
    assert store.mirror_row(miss2) is not None


def test_sync_skips_malformed_registry_codes(store: Any) -> None:
    """The code list is untrusted registry output — entries that don't
    match the trusty shape never reach a fetch URL or a PK."""
    import json

    good = "RA" + "k" * 43
    evil = ["../../../admin", "RAshort", "http://169.254.169.254/", ""]
    fetched_urls: list[str] = []

    def fake_fetch(url: str) -> bytes:
        fetched_urls.append(url)
        if url.endswith("/nanopubs.json"):
            return json.dumps([*evil, good]).encode()
        return b"# stub artifact"

    result = mirror.sync(store, limit=10, fetch=fake_fetch, delay_s=0)
    assert result.listed == 1 and result.fetched == 1
    assert all("/np/" not in u or good in u for u in fetched_urls)
    assert store.mirror_codes() == {good}


def test_sync_survives_one_bad_artifact(store: Any) -> None:
    import json

    good = "RA" + "g" * 43
    bad = "RA" + "b" * 43

    def fake_fetch(url: str) -> bytes:
        if url.endswith("/nanopubs.json"):
            return json.dumps([bad, good]).encode()
        if bad in url:
            raise RuntimeError("registry hiccup")
        return b"# stub artifact"

    result = mirror.sync(store, limit=10, fetch=fake_fetch, delay_s=0)
    assert result.fetched == 1 and result.failed == 1
    assert store.mirror_row(good) is not None


def test_sync_html_response_fails_without_storing(store: Any) -> None:
    """A registry answering HTML instead of TriG (content-negotiation
    slip) must count as a failed fetch and store NOTHING — a stored junk
    row would be skipped by the PK diff on every later pass."""
    import json

    code = "RA" + "w" * 43

    def fake_fetch(url: str) -> bytes:
        if url.endswith("/nanopubs.json"):
            return json.dumps([code]).encode()
        return b"<!DOCTYPE HTML>\n<html><head><title>Nanopub</title>"

    result = mirror.sync(store, limit=10, fetch=fake_fetch, delay_s=0)
    assert result.fetched == 0 and result.failed == 1
    assert store.mirror_row(code) is None
    assert store.mirror_codes() == set()


# ── flags: the authoritative-retraction rule ────────────────────────────


def test_only_same_signer_retraction_flags(store: Any) -> None:
    author = "https://orcid.org/0000-0001-0000-0001"
    stranger = "https://orcid.org/0000-0002-0000-0002"
    target = "RA" + "t" * 43
    own_retraction = "RA" + "r" * 43
    drive_by = "RA" + "d" * 43

    _plain_row(store, target, signer=author)
    _plain_row(store, own_retraction, signer=author)
    _plain_row(store, drive_by, signer=stranger)
    store.mirror_replace_edges(own_retraction, [(target, "retracts")])
    store.mirror_replace_edges(drive_by, [(target, "retracts")])

    assert store.mirror_apply_flags() == 1
    row = store.mirror_row(target)
    assert row is not None and row.retracted_by == own_retraction
    # Idempotent: nothing new the second time.
    assert store.mirror_apply_flags() == 0
    # Both claimants stay visible in the edge table.
    assert len(store.mirror_edges_to(target)) == 2


def test_unverified_retractor_does_not_flag(store: Any) -> None:
    author = "https://orcid.org/0000-0003-0000-0003"
    target = "RA" + "u" * 43
    retractor = "RA" + "v" * 43
    _plain_row(store, target, signer=author)
    _plain_row(store, retractor, signer=author, verified=False)
    store.mirror_replace_edges(retractor, [(target, "retracts")])
    assert store.mirror_apply_flags() == 0
    row = store.mirror_row(target)
    assert row is not None and row.retracted_by is None


def test_unverified_target_is_not_flagged(store: Any) -> None:
    """An unverified target's extracted signer came from untrusted
    bytes — a signer coincidence must not put a derived flag on it."""
    author = "https://orcid.org/0000-0004-0000-0004"
    target = "RA" + "w" * 43
    retractor = "RA" + "y" * 43
    _plain_row(store, target, signer=author, verified=False)
    _plain_row(store, retractor, signer=author, verified=True)
    store.mirror_replace_edges(retractor, [(target, "retracts")])
    assert store.mirror_apply_flags() == 0
    row = store.mirror_row(target)
    assert row is not None and row.retracted_by is None


# ── concurrence ─────────────────────────────────────────────────────────


def test_concurrence_alert_across_encodings(store: Any, monkeypatch: Any) -> None:
    """An external nanopub asserting our AIDA sentence — with '+' where
    we use '%20' — raises one deduped alert."""
    hub, row = _signed_hub(store, monkeypatch, "DFT shows a concurrence claim holds.")
    plus_variant = str(row.aida_uri).replace("%20", "+")
    _plain_row(store, "RA" + "c" * 43, aida_uri=plus_variant)

    assert mirror.concurrence_scan(store) == 1
    # Fingerprint-deduped: a re-scan is quiet.
    assert mirror.concurrence_scan(store) == 0
