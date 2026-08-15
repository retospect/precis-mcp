"""Publish preflight — the checking step that runs before any registry
POST, and stands alone for the review surface (spec: Publish-time gates).

Distinct from the mint gates (:mod:`precis.nanopub.gates`): those decide
whether a claim may become an artifact; this decides whether an existing
artifact may leave the building. Everything before the POST is
reversible, so the preflight's job is to be *loud*, not clever:

* **Withheld-edge enumeration** — every inbound evidence edge that is
  neither verified-by-refine (``links.meta['support']``, the chase
  verdict) nor literally signed off by a human
  (``links.meta['publish_signoff']``, written only through
  :func:`signoff_edge`'s interactive door) blocks publication. There is
  no mute button: unverified evidence can neither slip out nor be
  silently ignored.
* **Trust gate** — the artifact's (signer, key fingerprint) must have an
  open-window row in ``nanopub_trust_allowlist``, and publication
  additionally requires the entry be **attesting** — a bot signature
  alone publishes nothing. Validity-window-vs-signature-time and the
  allowlist-as-published-artifact are deferred (spec, Publish-time gates
  #3/#4).
* **State/identity checks** — state must be ``anchored``; live title
  must still hash to the frozen ``claim_sha``; a compound's dependency
  codes must be unchanged and every dependency already ``published``
  (publish order follows mint order: atoms → compounds → citers);
  a hanging claim (``grounding.hanging``) is mintable but never
  publishable; an unresolved ``contradicts`` edge blocks here exactly as
  it does at mint.

Not mechanized (recorded, not checked): canonicalizer-settledness —
"publish after the canonicalizer settles a hub, not during" is a
quiet-window operational rule, not a row predicate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from precis.nanopub import evidence, gates
from precis.store._nanopub_ops import PublishRow

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Inbound paper→hub evidence relations (mirrors ``taproot.hub.HUB_ROLES``).
EVIDENCE_RELATIONS = ("establishes", "corroborates", "contradicts")


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    """One reason (or note) against publishing. ``blocking=False`` rows
    are surfaced but do not gate the POST."""

    check: str
    message: str
    blocking: bool = True
    #: Machine hook for the review surface (link_id for a withheld edge,
    #: dependency ref id, …). ``None`` for hub-level issues.
    subject_id: int | None = None


@dataclass(frozen=True, slots=True)
class WithheldEdge:
    """One inbound evidence edge that would block publication."""

    link_id: int
    paper_ref_id: int
    paper_title: str
    relation: str


def withheld_edges(store: Store, hub_ref_id: int) -> list[WithheldEdge]:
    """Every inbound evidence edge on the hub that is neither
    verified-by-refine nor human-signed-off. ``contradicts`` edges are
    excluded — they block via the contradicts gate outright, and a
    sign-off must never make a live dispute publishable."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.link_id, l.src_ref_id, r.title, l.relation
              FROM links l
              JOIN refs r ON r.ref_id = l.src_ref_id AND r.deleted_at IS NULL
             WHERE l.dst_ref_id = %(hub)s
               AND l.relation IN ('establishes', 'corroborates')
               AND l.meta->>'support' IS NULL
               AND l.meta->'publish_signoff' IS NULL
             ORDER BY l.link_id
            """,
            {"hub": hub_ref_id},
        ).fetchall()
    return [
        WithheldEdge(
            link_id=int(r[0]),
            paper_ref_id=int(r[1]),
            paper_title=str(r[2] or ""),
            relation=str(r[3]),
        )
        for r in rows
    ]


def signoff_edge(
    store: Store,
    link_id: int,
    *,
    by: str,
    note: str,
    interactive: bool = False,
) -> bool:
    """Human sign-off of one unverified evidence edge — the literal
    attestation that makes it publishable (spec: "verify the edge, or
    sign it off literally"). Interactive door mirrors
    :func:`precis.nanopub.mint.approve`: a worker or scheduled pass
    calling this is a defect by definition. ``note`` is required — a
    silent sign-off defeats the audit purpose."""
    if not interactive:
        raise PermissionError(
            "signoff_edge() is a human attestation — invocable only from an "
            "interactive surface (pass interactive=True from code a person "
            "is driving)"
        )
    if not note.strip():
        from precis.errors import BadInput

        raise BadInput("a sign-off note is required — say why the edge is ok")
    signoff = {
        "by": by,
        "note": note.strip(),
        "at": datetime.now(UTC).isoformat(),
    }
    from psycopg.types.json import Jsonb

    with store.pool.connection() as conn:
        cur = conn.execute(
            "UPDATE links SET meta = COALESCE(meta, '{}'::jsonb) || "
            "jsonb_build_object('publish_signoff', %s::jsonb) "
            "WHERE link_id = %s AND relation IN ('establishes', 'corroborates')",
            (Jsonb(signoff), link_id),
        )
        return cur.rowcount == 1


def publish_preflight(
    store: Store, hub_ref_id: int, *, row: PublishRow | None = None
) -> list[PreflightIssue]:
    """Run every publish-time gate against one hub. Empty list = clear
    to POST. Pure read."""
    issues: list[PreflightIssue] = []
    row = row if row is not None else store.nanopub_publish_row(hub_ref_id)
    if row is None:
        return [
            PreflightIssue(
                check="state",
                message="no live publish row — the claim was never approved",
            )
        ]

    if row.state != "anchored":
        issues.append(
            PreflightIssue(
                check="state",
                message=(
                    f"state is {row.state!r}, not 'anchored' — publication "
                    "follows the state machine (approve → sign → anchor)"
                ),
            )
        )

    if (row.grounding or {}).get("hanging"):
        issues.append(
            PreflightIssue(
                check="hanging",
                message=(
                    "hanging claim — mintable for internal bookkeeping, "
                    "never publishable (no grounded passage)"
                ),
            )
        )

    bundle = evidence.load_bundle(store, hub_ref_id)
    issues.extend(
        PreflightIssue(check=v.gate, message=v.message)
        for v in gates.check_contradicts(store, bundle)
    )

    if row.claim_sha is not None:
        drift = gates.check_drift(bundle.sentence, row.claim_sha)
        if drift:
            issues.append(PreflightIssue(check=drift.gate, message=drift.message))

    for edge in withheld_edges(store, hub_ref_id):
        issues.append(
            PreflightIssue(
                check="withheld-edge",
                message=(
                    f"unverified evidence edge {edge.relation!r} from "
                    f"{edge.paper_title!r} (pc{edge.paper_ref_id}) — verify "
                    "it (refine) or sign it off literally (link "
                    f"{edge.link_id})"
                ),
                subject_id=edge.link_id,
            )
        )

    issues.extend(_dependency_issues(store, row))
    issues.extend(_trust_issues(store, row))
    issues.extend(_ots_notes(store, row))
    return issues


def _dependency_issues(store: Store, row: PublishRow) -> list[PreflightIssue]:
    """Publish order follows mint order: every dependency (conjunct atom,
    motivating artifact) must be unchanged since sign AND already
    published."""
    issues: list[PreflightIssue] = []
    for dep_ref_id, frozen_code in (row.dependency_codes or {}).items():
        dep = store.nanopub_publish_row(int(dep_ref_id))
        if dep is None or dep.trusty_uri != frozen_code:
            why = (
                "has no live publish row (retracted/rejected?)"
                if dep is None
                else "was re-minted since this artifact signed"
            )
            issues.append(
                PreflightIssue(
                    check="dependency-drift",
                    message=(
                        f"dependency fi{dep_ref_id} {why} — this artifact's "
                        "frozen code no longer holds; topo re-mint required"
                    ),
                    subject_id=int(dep_ref_id),
                )
            )
        elif dep.state != "published":
            issues.append(
                PreflightIssue(
                    check="dependency-unpublished",
                    message=(
                        f"dependency fi{dep_ref_id} is {dep.state!r} — atoms "
                        "publish before the compounds that cite them"
                    ),
                    subject_id=int(dep_ref_id),
                )
            )
    return issues


def _trust_issues(store: Store, row: PublishRow) -> list[PreflightIssue]:
    """The allowlist gate: pinned (identity, fingerprint), open window
    now, and attesting — only human-attested claims are publishable."""
    if row.artifact_id is None:
        return []  # unsigned — the state check already blocks
    artifact = store.nanopub_artifact(row.artifact_id)
    if artifact is None:
        return [
            PreflightIssue(
                check="trust",
                message=f"artifact {row.artifact_id} missing from the proof store",
            )
        ]
    now = datetime.now(UTC)
    entries = [
        e
        for e in store.nanopub_allowlist()
        if e.identity_uri == artifact.signer
        and e.key_fingerprint == artifact.key_fingerprint
        and e.valid_from <= now
        and (e.valid_until is None or e.valid_until > now)
    ]
    if not entries:
        return [
            PreflightIssue(
                check="trust",
                message=(
                    f"signer {artifact.signer!r} with key fingerprint "
                    f"{artifact.key_fingerprint[:16]}… has no open allowlist "
                    "entry — pin the (identity, fingerprint) pair by hand"
                ),
            )
        ]
    if not any(e.attesting for e in entries):
        return [
            PreflightIssue(
                check="trust",
                message=(
                    "signed with a non-attesting key — a bot signature alone "
                    "publishes nothing; re-sign with the attesting key "
                    "(`precis nanopub sign --attest`)"
                ),
            )
        ]
    return []


def _ots_notes(store: Store, row: PublishRow) -> list[PreflightIssue]:
    """Non-blocking: an anchor whose OTS proof is still calendar-pending
    has not reached a Bitcoin block yet — publishable (the pending proof
    upgrades in place), but worth seeing."""
    if row.batch_id is None:
        return []
    proof = store.nanopub_latest_proof(row.batch_id)
    if proof is not None and proof[0] == "pending":
        return [
            PreflightIssue(
                check="ots-pending",
                message=(
                    f"batch {row.batch_id} proof is still calendar-pending "
                    "(no Bitcoin attestation yet) — upgrade sweep will "
                    "finish it; not a publish blocker"
                ),
                blocking=False,
            )
        ]
    return []
