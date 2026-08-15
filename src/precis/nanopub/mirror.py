"""Registry mirror — pull-all + delta sync of the public nanopub corpus
(docs/backlog/nanopub-registry-mirror.md). Read-only sidecar: external
nanopubs never enter taproot as evidence; this buys concurrence
detection, a coverage check, and a real-world fixture corpus.

**Parse leniently, validate strictly.** The wild corpus contains typo'd
predicates and malformed keys; an unparseable fetch is stored with
``verified=False`` and no extracts — kept as fixture material, never
indexed as valid. Verification is the trusty recompute over the fetched
bytes (the artifact code IS the content hash — frozen by construction),
via the ``nanopub`` lib's :func:`~nanopub.sign_utils.verify_trusty`,
never hand-rolled, PLUS the requested-code check (a hostile mirror
serving a *different* valid nanopub still fails).

**Network protocol** (probed live 2026-08-15, registry 1.11.4):
``GET /nanopubs.json`` returns ALL artifact codes in one flat JSON array
(87,256 at probe time, ~4 MB — no paging; the earlier ~2k observation
was a fetch-tool truncation). ``GET /np/<code>`` returns the TriG.
Failed fetches retry against the next mirror host. All outbound HTTP
goes through ``safe_fetch`` per convention.

**Sync = PK diff.** codes(registry) − codes(mirror) is the work list
and the resume cursor in one; each pass is bounded (``limit``) so the
initial ~87k pull spreads across passes or one manual
``precis nanopub mirror sync --live`` loop.

DARK unless ``PRECIS_MIRROR_ENABLED`` (same posture as OTS); the manual
CLI door requires ``--live`` regardless.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Same protocol on every host; a failed fetch retries down the list.
MIRROR_HOSTS = (
    "https://registry.petapico.org",
    "https://registry.knowledgepixels.com",
    "https://registry.np.trustyuri.net",
)

#: Trusty artifact code (base64url sha256; the lib uses the same shape).
TRUSTY_RE = re.compile(r"RA[A-Za-z0-9_\-]{40,}")

AIDA_PREFIX = "http://purl.org/aida/"
NPX = "http://purl.org/nanopub/x/"

#: Politeness delay between artifact fetches (public infrastructure).
FETCH_DELAY_S = 0.05

#: A fetch function: url -> body bytes. Injectable for tests; the
#: default routes through safe_fetch.
Fetch = Callable[[str], bytes]


def mirror_enabled() -> bool:
    return os.environ.get("PRECIS_MIRROR_ENABLED", "").strip() in ("1", "true", "yes")


@dataclass(frozen=True, slots=True)
class MirrorIndex:
    """Extracts from one fetched artifact — everything rebuildable."""

    verified: bool
    aida_uri: str | None = None
    signer: str | None = None
    key_fingerprint: str | None = None
    dois: list[str] = field(default_factory=list)
    assertion_predicates: list[str] = field(default_factory=list)
    #: Outbound np→np references: (to_code, relation).
    edges: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SyncResult:
    listed: int
    already: int
    fetched: int
    verified: int
    failed: int
    remaining: int


def _default_fetch(url: str) -> bytes:
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_get

    with http_client(timeout=30.0) as client:
        resp = safe_get(client, url)
    resp.raise_for_status()
    return resp.content


def list_registry_codes(*, fetch: Fetch | None = None) -> tuple[list[str], str]:
    """The registry's full artifact-code list (one flat array — no
    paging). Returns ``(codes, host_used)``; tries each mirror in turn."""
    last_error: Exception | None = None
    for host in MIRROR_HOSTS:
        try:
            body = (fetch or _default_fetch)(f"{host}/nanopubs.json")
            codes = json.loads(body)
            if not isinstance(codes, list):
                raise ValueError(f"{host}/nanopubs.json is not a JSON array")
            # Shape-gate before the codes reach fetch URLs or PKs —
            # the list is untrusted registry output.
            valid = [str(c) for c in codes if TRUSTY_RE.fullmatch(str(c))]
            if len(valid) != len(codes):
                log.warning(
                    "mirror: %s listed %d malformed codes (skipped)",
                    host,
                    len(codes) - len(valid),
                )
            return valid, host
        except Exception as exc:
            last_error = exc
            log.warning("mirror: code list from %s failed: %s", host, exc)
    raise RuntimeError(f"every registry mirror failed: {last_error}")


def fetch_code(code: str, *, host: str, fetch: Fetch | None = None) -> bytes:
    return (fetch or _default_fetch)(f"{host}/np/{code}")


def index_bytes(code: str, trig_bytes: bytes) -> MirrorIndex:
    """Parse leniently, validate strictly. Any parse/structure failure
    → ``verified=False`` with no extracts (the row is still stored as
    fixture material)."""
    from rdflib import Dataset

    try:
        ds = Dataset()
        ds.parse(data=trig_bytes.decode("utf-8", errors="replace"), format="trig")
    except Exception:
        log.debug("mirror: %s does not parse as TriG", code)
        return MirrorIndex(verified=False)

    try:
        from nanopub.utils import extract_np_metadata

        meta = extract_np_metadata(ds)
    except Exception:
        log.debug("mirror: %s has no well-formed nanopub structure", code)
        return MirrorIndex(verified=False)

    # Extraction failures past this point must ALSO degrade to an
    # unverified row — an artifact that parses but trips rdflib
    # downstream would otherwise never be cached and be re-fetched on
    # every future pass (poison-code loop).
    try:
        return _extract(ds, meta, code)
    except Exception:
        log.debug("mirror: %s extraction failed past parse", code)
        return MirrorIndex(verified=False)


def _extract(ds: Any, meta: Any, code: str) -> MirrorIndex:
    from rdflib import URIRef

    # Strict validation: recomputed trusty must match AND the nanopub's
    # own URI must carry the code we asked for (anti-substitution).
    verified = False
    if meta.trusty == code:
        try:
            from nanopub.sign_utils import verify_trusty

            verified = bool(verify_trusty(ds, str(meta.np_uri), meta.namespace))
        except Exception:
            verified = False

    signer: str | None = None
    for _s, _p, o, _g in ds.quads((None, URIRef(NPX + "signedBy"), None, None)):
        signer = str(o)
        break

    key_fingerprint: str | None = None
    if meta.public_key:
        try:
            from precis.nanopub.keys import fingerprint

            key_fingerprint = fingerprint(str(meta.public_key))
        except Exception:
            key_fingerprint = None

    aida_uri: str | None = None
    dois: set[str] = set()
    edge_codes: dict[str, str] = {}
    assertion_graph = ds.get_context(meta.assertion)
    predicates = sorted({str(p) for _s, p, _o in assertion_graph})

    for s, p, o, _g in ds.quads((None, None, None, None)):
        for term in (s, o):
            if not isinstance(term, URIRef):
                continue
            uri = str(term)
            if aida_uri is None and uri.startswith(AIDA_PREFIX):
                aida_uri = uri
            if "doi.org/" in uri:
                dois.add(unquote(uri.split("doi.org/", 1)[1]))
        if isinstance(o, URIRef):
            m = TRUSTY_RE.search(str(o))
            if m and m.group(0) != code:
                to_code = m.group(0)
                p_str = str(p)
                if p_str == NPX + "retracts":
                    edge_codes[to_code] = "retracts"
                elif p_str == NPX + "supersedes":
                    edge_codes[to_code] = "supersedes"
                else:
                    edge_codes.setdefault(to_code, "refers-to")

    return MirrorIndex(
        verified=verified,
        aida_uri=aida_uri,
        signer=signer,
        key_fingerprint=key_fingerprint,
        dois=sorted(dois),
        assertion_predicates=predicates,
        edges=sorted(edge_codes.items()),
    )


def ingest_one(
    store: Store, code: str, trig_bytes: bytes, *, source_url: str
) -> MirrorIndex:
    """Index + upsert one fetched artifact (edges included)."""
    idx = index_bytes(code, trig_bytes)
    written = store.mirror_upsert(
        code,
        trig_bytes=trig_bytes,
        source_url=source_url,
        verified=idx.verified,
        aida_uri=idx.aida_uri,
        signer=idx.signer,
        key_fingerprint=idx.key_fingerprint,
        dois=idx.dois,
        assertion_predicates=idx.assertion_predicates,
    )
    if written:
        store.mirror_replace_edges(code, idx.edges)
    return idx


def sync(
    store: Store,
    *,
    limit: int = 1000,
    fetch: Fetch | None = None,
    delay_s: float = FETCH_DELAY_S,
) -> SyncResult:
    """One bounded sync pass: list → PK diff → fetch/verify/index the
    first ``limit`` missing codes. One bad artifact never kills the pass."""
    codes, host = list_registry_codes(fetch=fetch)
    have = store.mirror_codes()
    missing = [c for c in codes if c not in have]
    fetched = verified = failed = 0
    for code in missing[:limit]:
        try:
            body = fetch_code(code, host=host, fetch=fetch)
            idx = ingest_one(store, code, body, source_url=f"{host}/np/{code}")
            fetched += 1
            if idx.verified:
                verified += 1
        except Exception:
            log.exception("mirror: fetch/index of %s failed", code)
            failed += 1
        if delay_s:
            time.sleep(delay_s)
    return SyncResult(
        listed=len(codes),
        already=len(have),
        fetched=fetched,
        verified=verified,
        failed=failed,
        remaining=max(0, len(missing) - limit),
    )


def _aida_variants(aida_uri: str) -> list[str]:
    """The encodings a wild AIDA URI shows up in — both ``%20`` and
    ``+`` live out there; compare on all of them."""
    decoded = unquote(aida_uri).replace("+", " ")
    tail = decoded.removeprefix(AIDA_PREFIX)
    return sorted(
        {
            aida_uri,
            AIDA_PREFIX + tail.replace(" ", "%20"),
            AIDA_PREFIX + tail.replace(" ", "+"),
        }
    )


def concurrence_scan(store: Store) -> int:
    """Alert on external nanopubs asserting the same AIDA sentence as one
    of our live publish rows (spec priority 4: inbound concurrence
    without polling). Alert dedup is fingerprint-based, so re-scans are
    quiet. Returns new alerts raised."""
    from precis.alerts import raise_alert

    new = 0
    with store.pool.connection() as conn:
        ours = conn.execute(
            "SELECT claim_ref_id, aida_uri FROM nanopub_publish "
            "WHERE aida_uri IS NOT NULL AND state NOT IN "
            "('superseded', 'retracted', 'rejected')"
        ).fetchall()
    for claim_ref_id, aida in ours:
        for row in store.mirror_aida_matches(_aida_variants(str(aida))):
            _alert_id, is_new = raise_alert(
                store,
                source="nanopub_mirror",
                fingerprint=f"concurrence:{row.artifact_code}:fi{claim_ref_id}",
                title=(
                    f"external nanopub concurs with fi{claim_ref_id}: "
                    f"{row.artifact_code}"
                ),
                detail=(
                    f"mirrored artifact {row.artifact_code} (signer "
                    f"{row.signer or 'unknown'}) asserts the same AIDA "
                    f"sentence as our claim fi{claim_ref_id} — consider "
                    "converging on the existing URI instead of a near-dup"
                ),
                severity="info",
                subject_ref_id=int(claim_ref_id),
            )
            if is_new:
                new += 1
    return new
