"""OpenTimestamps anchoring (slice 3): batch, stamp, upgrade, audit.

What an anchor proves: existence **no later than** T — an upper bound
only; nothing about authorship (the signature's job) or truth. Primary
justification is key-rotation scoping, which is why the sweep is one
daily cadence, not per-item: granularity caps at 24h and that is enough.
Anchor early, publish late — an anchor over unpublished content is a
commitment, not a disclosure (32 content-blind bytes leave the box).

Batching: hash each signed artifact's **exact stored bytes**
(``byte_sha256``), Merkle-root whatever is waiting via the
opentimestamps library's ``make_merkle_tree``, stamp the root once.
Retained per batch (the proof-store completeness rule — the leaf table
and construction rule ARE the proof and exist nowhere else): each leaf's
serialized inclusion path, the construction rule string, the root, and
the ``.ots`` root proof with its pending/upgraded state — an upgrade
INSERTs a new proof row (append-only), the pending row stays as history.

Single calendar, risk named in the spec: a calendar cannot forge an
anchor (verification runs against Bitcoin block headers); the exposure
is the pending window, so the sweep alerts on a proof still pending past
:data:`STUCK_PENDING_DAYS` and a lost commitment is survivable by
re-stamping (the raw bytes are retained).

The calendar round-trip is injectable (``submit``/``fetch_upgrade``
callables) so everything below tests offline; the live default uses
``opentimestamps.calendar.RemoteCalendar``. Network runs only from the
sweep cadence, which ships dark (``PRECIS_OTS_ENABLED``)."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentimestamps.core.timestamp import Timestamp

    from precis.store import Store

log = logging.getLogger(__name__)

DEFAULT_CALENDAR = "https://alice.btc.calendar.opentimestamps.org"

#: Wall-clock cap on one calendar round-trip. The opentimestamps client
#: defaults to ``timeout=None`` (urllib blocks forever); an unreachable
#: calendar hung ``anchor --live`` for 12 h on 2026-08-23 (gr248596).
#: A stamp/upgrade is one small HTTP exchange — seconds when healthy.
CALENDAR_TIMEOUT_S = 30

#: A batch whose latest proof is still pending after this many days
#: trips the stuck-pending alert (calendar upgrades normally land in
#: hours to ~a day).
STUCK_PENDING_DAYS = 3


def construction_rule() -> str:
    """The recorded Merkle construction — a bare hash list cannot
    reproduce a root, so this string is part of the proof."""
    import importlib.metadata

    version = importlib.metadata.version("opentimestamps")
    return (
        f"python-opentimestamps {version} make_merkle_tree: leaf digest = "
        "sha256(exact stored artifact bytes); pairwise "
        "append/prepend+sha256 cat-then-hash tree, odd leaf promoted "
        "unhashed to the next level; leaf order = ascending artifact id; "
        "per-leaf proof = serialized OTS Timestamp from leaf digest "
        "(inclusion path merged with the batch's root proof); root proof "
        "= serialized OTS Timestamp over the root digest."
    )


def _serialize(ts: Timestamp) -> bytes:
    from opentimestamps.core.serialize import BytesSerializationContext

    ctx = BytesSerializationContext()
    ts.serialize(ctx)
    return ctx.getbytes()


def _deserialize(data: bytes, msg: bytes) -> Timestamp:
    from opentimestamps.core.serialize import BytesDeserializationContext
    from opentimestamps.core.timestamp import Timestamp

    return Timestamp.deserialize(BytesDeserializationContext(data), msg)


def _default_submit(calendar_url: str, digest: bytes) -> Timestamp:
    from opentimestamps.calendar import RemoteCalendar

    return RemoteCalendar(calendar_url).submit(digest, timeout=CALENDAR_TIMEOUT_S)


def _default_fetch_upgrade(calendar_url: str, commitment: bytes) -> Timestamp:
    from opentimestamps.calendar import RemoteCalendar

    return RemoteCalendar(calendar_url).get_timestamp(
        commitment, timeout=CALENDAR_TIMEOUT_S
    )


def stamp_batch(
    store: Store,
    *,
    calendar_url: str = DEFAULT_CALENDAR,
    submit: Callable[[str, bytes], Timestamp] | None = None,
) -> int | None:
    """Anchor everything waiting: every publish row in ``signed`` whose
    artifact exists becomes one leaf; one calendar request per batch.
    Returns the new batch id, or ``None`` when nothing waits.

    Anchor-when-text-stops-moving is the caller's concern (the daily
    cadence after audit settles); a row that re-opens later simply has a
    superseded anchor — a dated artifact, never cited."""
    from opentimestamps.core.timestamp import Timestamp, make_merkle_tree

    from precis.nanopub.gates import check_drift
    from precis.nanopub.mint import check_dependency_drift

    rows = store.nanopub_rows_in_state("signed", limit=10_000)
    refs_by_id = store.fetch_refs_by_ids([row.claim_ref_id for row in rows])
    leaves: list[tuple[int, int, Timestamp]] = []  # (row_id, artifact_id, ts)
    for row in rows:
        if row.artifact_id is None:
            continue
        # Anchor when text stops moving: a dependency-dirty row flips back
        # to reviewed for the topo re-mint; a title-drifted row is skipped
        # (needs re-review, and an anchor over soon-superseded bytes buys
        # only a dated artifact never cited).
        if check_dependency_drift(store, row):
            continue
        live_ref = refs_by_id.get(row.claim_ref_id)
        live_title = live_ref.title if live_ref is not None else None
        if live_title is not None and check_drift(live_title, row.claim_sha):
            log.warning(
                "nanopub ots: fi%s drifted from its approved string — "
                "not anchoring publish row %s (re-review it)",
                row.claim_ref_id,
                row.id,
            )
            continue
        artifact = store.nanopub_artifact(row.artifact_id)
        if artifact is None:
            continue
        digest = hashlib.sha256(artifact.trig_bytes).digest()
        # Belt-and-braces: the generated column must agree with a live
        # recompute before we anchor anything.
        if digest.hex() != artifact.byte_sha256:
            log.error(
                "nanopub ots: artifact %s byte_sha256 mismatch — skipping "
                "and leaving row %s unanchored",
                artifact.id,
                row.id,
            )
            continue
        leaves.append((row.id, artifact.id, Timestamp(digest)))

    if not leaves:
        return None
    leaves.sort(key=lambda item: item[1])  # ascending artifact id

    root_ts = make_merkle_tree([ts for _, _, ts in leaves])
    submitted = (submit or _default_submit)(calendar_url, root_ts.msg)
    root_ts.merge(submitted)
    if not any(root_ts.all_attestations()):
        raise RuntimeError("calendar submission left no attestation on root")

    batch_id = store.nanopub_create_batch(
        merkle_root=root_ts.msg.hex(),
        construction=construction_rule(),
        calendar_url=calendar_url,
        leaves=[
            (artifact_id, i, ts.msg.hex(), _serialize(ts))
            for i, (_, artifact_id, ts) in enumerate(leaves)
        ],
        pending_proof=_serialize(_root_only(root_ts)),
    )
    for row_id, _, _ in leaves:
        store.nanopub_set_batch(row_id, batch_id)
    log.info(
        "nanopub ots: stamped batch %s (%d leaves, root %s…)",
        batch_id,
        len(leaves),
        root_ts.msg.hex()[:16],
    )
    return batch_id


def _root_only(root_ts: Timestamp) -> Timestamp:
    """The root proof detached from the leaf tree: a fresh Timestamp over
    the root digest carrying only the calendar-side ops/attestations —
    what ``nanopub_ots_proofs`` stores (leaf paths live per-leaf)."""
    from opentimestamps.core.timestamp import Timestamp

    detached = Timestamp(root_ts.msg)
    detached.merge(root_ts)
    return detached


def upgrade_sweep(
    store: Store,
    *,
    fetch_upgrade: Callable[[str, bytes], Timestamp] | None = None,
) -> list[int]:
    """Poll the calendar for every batch whose latest proof is pending;
    a completed proof is INSERTed as a new ``upgraded`` row. Batches
    pending past :data:`STUCK_PENDING_DAYS` raise the stuck-pending
    alert (the survivable-loss remedy is a re-stamp at a later date).
    Returns the batch ids upgraded this sweep."""
    from datetime import datetime, timedelta

    from opentimestamps.core.notary import PendingAttestation

    fetch = fetch_upgrade or _default_fetch_upgrade
    upgraded: list[int] = []
    for batch in store.nanopub_pending_batches():
        latest = store.nanopub_latest_proof(batch.id)
        if latest is None:  # defect: batch without proof row
            _stuck_alert(store, batch.id, "batch has no proof row at all")
            continue
        _state, proof_bytes = latest
        root_msg = bytes.fromhex(batch.merkle_root)
        ts = _deserialize(proof_bytes, root_msg)

        progressed = False
        for msg, attestation in list(ts.all_attestations()):
            if not isinstance(attestation, PendingAttestation):
                continue
            try:
                fresh = fetch(batch.calendar_url, msg)
            except Exception:
                log.warning(
                    "nanopub ots: calendar upgrade fetch failed for batch %s",
                    batch.id,
                    exc_info=True,
                )
                continue
            for sub, node in _walk(ts):
                if sub == msg:
                    node.merge(fresh)
                    progressed = True

        if progressed and _is_complete(ts):
            store.nanopub_add_proof(
                batch.id, state="upgraded", ots_proof=_serialize(ts)
            )
            upgraded.append(batch.id)
            log.info("nanopub ots: batch %s upgraded", batch.id)
            continue

        age = datetime.now(UTC) - batch.created_at
        if age > timedelta(days=STUCK_PENDING_DAYS):
            _stuck_alert(
                store,
                batch.id,
                f"proof still pending after {age.days}d (threshold "
                f"{STUCK_PENDING_DAYS}d) — calendar lost the commitment? "
                f"Re-stamp: `precis nanopub re-stamp {batch.id}` (raw bytes "
                "are retained; the re-stamp carries a later date)",
            )
    return upgraded


def _walk(ts: Timestamp) -> list[tuple[bytes, Timestamp]]:
    """Every (msg, node) in the timestamp tree, root included."""
    out: list[tuple[bytes, Timestamp]] = [(ts.msg, ts)]
    for _op, sub in ts.ops.items():
        out.extend(_walk(sub))
    return out


def _is_complete(ts: Timestamp) -> bool:
    """Complete = at least one non-pending (Bitcoin) attestation."""
    from opentimestamps.core.notary import PendingAttestation

    return any(not isinstance(a, PendingAttestation) for _, a in ts.all_attestations())


def _stuck_alert(store: Store, batch_id: int, detail: str) -> None:
    from precis.alerts import raise_alert

    raise_alert(
        store,
        source="nanopub_ots",
        fingerprint=f"stuck-pending:{batch_id}",
        title=f"OTS batch {batch_id} stuck pending",
        detail=detail,
        severity="warn",
    )


# ── recompute audit ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """One integrity violation. ``kind`` is machine-routable; on any
    index-vs-bytes mismatch the bytes win — index columns are
    rebuildable, the bytes are not."""

    kind: str
    subject: str
    message: str


def audit(store: Store, *, limit: int = 5000) -> list[AuditFinding]:
    """Detection without trusting the store: recompute every derived
    value from the retained bytes.

    * per artifact: sha256 over ``trig_bytes`` vs ``byte_sha256``; the
      TriG re-parses; the trusty URI, AIDA URI and DOI extracts appear in
      the parsed quads (parse-derived extracts exceed what a SQL CHECK
      can pin — this is the covering audit the migration points at).
    * per batch: leaf hashes match their artifacts' bytes; each leaf's
      serialized inclusion path re-derives the recorded Merkle root; the
      latest proof deserializes against the root digest.
    """
    findings: list[AuditFinding] = []
    findings += _audit_artifacts(store, limit=limit)
    findings += _audit_batches(store)
    if findings:
        from precis.alerts import raise_alert

        raise_alert(
            store,
            source="nanopub_ots",
            fingerprint="audit-mismatch",
            title=f"nanopub proof-store audit: {len(findings)} finding(s)",
            detail="; ".join(
                f"[{f.kind}] {f.subject}: {f.message}" for f in findings[:10]
            ),
            severity="critical",
        )
    return findings


def _audit_artifacts(store: Store, *, limit: int) -> list[AuditFinding]:
    from rdflib import Dataset

    findings: list[AuditFinding] = []
    after = 0
    seen = 0
    while seen < limit:
        page = store.nanopub_artifacts_since(after_id=after, limit=500)
        if not page:
            break
        for art in page:
            after = art.id
            seen += 1
            subject = f"artifact {art.id}"
            recomputed = hashlib.sha256(art.trig_bytes).hexdigest()
            if recomputed != art.byte_sha256:
                findings.append(
                    AuditFinding(
                        "byte-sha",
                        subject,
                        f"stored byte_sha256 {art.byte_sha256[:12]}… != "
                        f"recomputed {recomputed[:12]}…",
                    )
                )
            try:
                ds = Dataset()
                ds.parse(data=art.trig_bytes.decode("utf-8"), format="trig")
            except Exception as exc:
                findings.append(
                    AuditFinding("parse", subject, f"TriG no longer parses: {exc}")
                )
                continue
            nquads = ds.serialize(format="nquads")
            for label, needle in (
                ("trusty-uri", art.trusty_uri),
                ("aida-uri", art.aida_uri),
            ):
                if needle and needle not in nquads:
                    findings.append(
                        AuditFinding(
                            label,
                            subject,
                            f"indexed {label} {needle!r} absent from the "
                            "parsed quads — index drifted from bytes "
                            "(bytes win; rebuild the extract)",
                        )
                    )
            for doi in art.dois:
                if f"https://doi.org/{doi}" not in nquads:
                    findings.append(
                        AuditFinding(
                            "doi",
                            subject,
                            f"indexed DOI {doi} absent from parsed quads",
                        )
                    )
    return findings


def _audit_batches(store: Store) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for batch in store.nanopub_batches():
        subject = f"batch {batch.id}"
        root_msg = bytes.fromhex(batch.merkle_root)
        leaves = store.nanopub_batch_leaves(batch.id)
        if len(leaves) != batch.leaf_count:
            findings.append(
                AuditFinding(
                    "leaf-count",
                    subject,
                    f"{len(leaves)} leaf rows vs recorded {batch.leaf_count}",
                )
            )
        for leaf in leaves:
            art = store.nanopub_artifact(leaf.artifact_id)
            if art is None:
                findings.append(
                    AuditFinding(
                        "leaf-artifact",
                        subject,
                        f"leaf {leaf.leaf_index} points at missing artifact "
                        f"{leaf.artifact_id}",
                    )
                )
                continue
            digest = hashlib.sha256(art.trig_bytes).digest()
            if digest.hex() != leaf.leaf_hash:
                findings.append(
                    AuditFinding(
                        "leaf-hash",
                        subject,
                        f"leaf {leaf.leaf_index} hash != sha256 of artifact "
                        f"{art.id} bytes",
                    )
                )
                continue
            try:
                leaf_ts = _deserialize(leaf.path_proof, digest)
            except Exception as exc:
                findings.append(
                    AuditFinding(
                        "leaf-path",
                        subject,
                        f"leaf {leaf.leaf_index} inclusion path no longer "
                        f"deserializes: {exc}",
                    )
                )
                continue
            if root_msg not in {msg for msg, _ in _walk(leaf_ts)}:
                findings.append(
                    AuditFinding(
                        "merkle-root",
                        subject,
                        f"leaf {leaf.leaf_index} path does not reach the "
                        "recorded root — the inclusion proof is broken",
                    )
                )
        latest = store.nanopub_latest_proof(batch.id)
        if latest is None:
            findings.append(AuditFinding("proof", subject, "no proof row at all"))
        else:
            try:
                _deserialize(latest[1], root_msg)
            except Exception as exc:
                findings.append(
                    AuditFinding(
                        "proof",
                        subject,
                        f"latest proof no longer deserializes: {exc}",
                    )
                )
    return findings
