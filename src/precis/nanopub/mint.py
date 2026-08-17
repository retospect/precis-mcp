"""Freeze-at-review + mint/sign pipeline (slice 2 — local, reversible).

Order inside one artifact (spec: Lifecycle): build with the library's
placeholder URI → sign the normalized quads → insert the signature →
hash all four graphs → rewrite self-references to the final trusty URI —
all inside :meth:`nanopub.Nanopub.sign`; trusty computation is never
hand-rolled here. The w3id base is inside the hashed content from the
first mint (base-URI-fixed-at-mint); the library's default base IS
``https://w3id.org/np/``.

Two objects, two identities: approval freezes the claim's identity
(``approved_title`` → ``claim_sha`` + AIDA URI) before any key is
touched; signing hashes the **artifact**. Rewording is a new claim
identity; re-signing alone is only a new artifact identity.

Everything here is reversible per the irreversibility map: delete the
publish row's pointer and re-mint at will — until an OTS anchor
(harmless, discloses nothing) and ultimately the registry POST (slice 5,
the one true point of no return, not in this module)."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.nanopub import assemble, evidence, gates
from precis.nanopub.aida import aida_uri, canonical_sentence
from precis.nanopub.keys import fingerprint, load_profile
from precis.store._nanopub_ops import PublishRow

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


class MintGateError(BadInput):
    """Mint refused: one or more Layer-A gates failed. ``violations``
    carries the full machine-routable list."""

    def __init__(self, violations: list[gates.GateViolation]) -> None:
        self.violations = violations
        lines = "; ".join(f"[{v.gate}] {v.message}" for v in violations)
        super().__init__(f"mint gates failed: {lines}")


def approve(
    store: Store,
    hub_ref_id: int,
    *,
    payload: dict[str, Any],
    title: str | None = None,
    interactive: bool = False,
) -> PublishRow:
    """Freeze-at-review: approve the exact claim string + payload and
    flip the publish row ``candidate`` → ``reviewed``.

    ``interactive=True`` is required — approval IS the human review act
    (the spec's "an attestation cannot be batched" / no-bulk-backfill
    invariant), so this door mirrors the attesting-key guard in
    :func:`precis.nanopub.keys.load_profile`: pass it only from a
    surface a person is driving right now; a worker, job, or scheduled
    pass calling it is a defect by definition.

    ``title`` defaults to the hub's live ``finding.title``; the frozen
    copy is the duplication the crypto requires (the signature covers
    those exact bytes, underivable from a hub that later drifts).
    ``payload`` is the grounding envelope (``passages`` / ``fields`` /
    ``motivation`` / ``testable_by`` / ``hanging`` / ``hypothesis`` /
    ``motivated_by_refs``) — every Layer-A gate runs NOW, so the review
    queue only ever holds strings that can actually mint."""
    if not interactive:
        raise PermissionError(
            "approve() is the human review act — invocable only from an "
            "interactive surface (pass interactive=True from code a person "
            "is driving); publication throughput equals human review "
            "throughput, by design"
        )
    bundle = evidence.load_bundle(store, hub_ref_id)
    hub_ref = store.fetch_refs_by_ids([hub_ref_id])[hub_ref_id]

    violations = gates.run_mint_gates(
        store, bundle, payload, hub_meta=hub_ref.meta or {}
    )
    if violations:
        raise MintGateError(violations)

    approved = (title if title is not None else bundle.sentence).strip()
    from precis.taproot.canon import claim_sha

    row = store.nanopub_publish_row(hub_ref_id)
    if row is None:
        artifact_type = (
            "hypothesis" if payload.get("hypothesis") else bundle.artifact_type
        )
        row = store.nanopub_create_publish_row(hub_ref_id, artifact_type=artifact_type)
    if row.state != "candidate":
        raise BadInput(
            f"publish row {row.id} is {row.state!r}, not candidate — "
            "reopen it first (an approved string is frozen; a change is a "
            "re-review, post-publication a supersede)"
        )
    # Sync the live hub title to the approved string BEFORE flipping the
    # row, so gate #14 (drift, computed off refs.title) fires only on
    # post-approval edits — not on the review-time rewording that just
    # happened. Sync-first makes the two independent commits safe: if the
    # approve below fails, a retitled still-candidate hub is benign (no
    # frozen sha exists yet), whereas approve-then-sync would leave a
    # reviewed row whose frozen sha spuriously drift-fails at sign.
    # Title-only on purpose: refine_claim_sentence also replaces
    # finding_body, which for chase-born findings is prose, not a
    # sentence copy.
    if approved != (hub_ref.title or "").strip():
        store.blocks.set_ref_title(hub_ref_id, approved, source="reviewer")
    ok = store.nanopub_approve(
        row.id,
        approved_title=approved,
        claim_sha=claim_sha(approved),
        aida_uri=aida_uri(approved),
        grounding=payload,
    )
    if not ok:
        raise BadInput(f"publish row {row.id} left candidate state mid-approve")
    refreshed = store.nanopub_publish_row_by_id(row.id)
    assert refreshed is not None
    return refreshed


def sign(
    store: Store,
    hub_ref_id: int,
    *,
    role: str = "bot",
    interactive: bool = False,
    llm_models: list[str] | None = None,
) -> PublishRow:
    """Mint + sign the hub's ``reviewed`` publish row into an immutable
    artifact: ``reviewed`` → ``signed``.

    Gates re-run (state may have moved since approval) plus the drift
    check; a compound resolves its atoms' trusty codes into
    ``dependency_codes`` (a later change of any code is the dirty signal
    that flips this row back to ``reviewed`` for the topo re-mint)."""
    row = store.nanopub_publish_row(hub_ref_id)
    if row is None or row.state != "reviewed":
        raise BadInput(
            f"hub fi{hub_ref_id} has no publish row in 'reviewed' "
            f"(found: {row.state if row else 'none'}) — approve first"
        )
    assert row.approved_title is not None and row.claim_sha is not None

    bundle = evidence.load_bundle(store, hub_ref_id)
    hub_ref = store.fetch_refs_by_ids([hub_ref_id])[hub_ref_id]
    violations = gates.run_mint_gates(
        store, bundle, row.grounding, hub_meta=hub_ref.meta or {}, at_sign=True
    )
    drift = gates.check_drift(bundle.sentence, row.claim_sha)
    if drift:
        violations.append(drift)
    if violations:
        raise MintGateError(violations)

    inp, dependency_codes = _mint_input(store, row, bundle)
    profile = load_profile(store, role, interactive=interactive)
    np = _build_and_sign(inp, profile, llm_models or [])

    trig_bytes = np.rdf.serialize(format="trig").encode("utf-8")
    trusty_uri = str(np.source_uri)
    dois = sorted({g.doi for g in inp.grounding if g.doi})
    artifact_id = store.nanopub_insert_artifact(
        publish_id=row.id,
        claim_ref_id=row.claim_ref_id,
        artifact_type=inp.artifact_type,
        trig_bytes=trig_bytes,
        trusty_uri=trusty_uri,
        aida_uri=inp.aida_uri,
        claim_sha=row.claim_sha,
        signer=profile.orcid_id,
        key_fingerprint=fingerprint(profile.public_key),
        dois=dois,
    )
    if not store.nanopub_record_signed(
        row.id,
        trusty_uri=trusty_uri,
        artifact_id=artifact_id,
        dependency_codes=dependency_codes,
    ):
        raise BadInput(
            f"publish row {row.id} left 'reviewed' mid-sign — artifact "
            f"{artifact_id} stays in the append-only store unreferenced"
        )
    log.info(
        "nanopub: signed fi%s as %s (artifact %s, %s)",
        hub_ref_id,
        trusty_uri,
        artifact_id,
        inp.artifact_type,
    )
    refreshed = store.nanopub_publish_row_by_id(row.id)
    assert refreshed is not None
    return refreshed


def check_dependency_drift(store: Store, row: PublishRow) -> bool:
    """Topo re-mint dirty signal: has any dependency's artifact code
    changed since this row signed? True = flipped back to ``reviewed``
    (the mint pass regenerates the closure atoms → compounds → citers)."""
    if not row.dependency_codes or row.state != "signed":
        return False
    for dep_ref_id, frozen_code in row.dependency_codes.items():
        dep = store.nanopub_publish_row(int(dep_ref_id))
        if dep is None or dep.trusty_uri != frozen_code:
            if store.nanopub_transition(
                row.id, to_state="reviewed", expect=("signed",)
            ):
                log.info(
                    "nanopub: fi%s dependency fi%s re-minted — flipping "
                    "publish row %s signed → reviewed for topo re-mint",
                    row.claim_ref_id,
                    dep_ref_id,
                    row.id,
                )
            return True
    return False


def _mint_input(
    store: Store, row: PublishRow, bundle: evidence.HubBundle
) -> tuple[assemble.MintInput, dict[str, str]]:
    payload = row.grounding
    assert row.approved_title is not None and row.aida_uri is not None

    grounding = [
        assemble.GroundingInput(
            doi=str(p.get("doi") or ""),
            pdf_sha256=str(p.get("pdf_sha256") or ""),
            quote=str(p.get("quote") or ""),
            snip=str(p.get("snip") or ""),
            role=str(p.get("role") or "corroborates"),
            source_title=p.get("source_title"),
        )
        for p in payload.get("passages") or []
    ]

    dependency_codes: dict[str, str] = {}
    conjuncts: list[assemble.ConjunctInput] = []
    for atom_id, _sentence in bundle.conjunct_atoms:
        atom_row = store.nanopub_publish_row(atom_id)
        # Gate #15 already guaranteed these exist and are signed.
        assert atom_row is not None and atom_row.trusty_uri is not None
        assert atom_row.aida_uri is not None
        conjuncts.append(
            assemble.ConjunctInput(
                aida_uri=atom_row.aida_uri, trusty_uri=atom_row.trusty_uri
            )
        )
        dependency_codes[str(atom_id)] = atom_row.trusty_uri

    motivated_by: list[str] = []
    for ref_id in payload.get("motivated_by_refs") or []:
        dep_row = store.nanopub_publish_row(int(ref_id))
        if dep_row is None or dep_row.trusty_uri is None:
            raise BadInput(
                f"motivating hub fi{ref_id} has no signed artifact — a "
                "hypothesis cites its motivating artifacts by trusty URI "
                "(mint them first)"
            )
        motivated_by.append(dep_row.trusty_uri)
        dependency_codes[str(ref_id)] = dep_row.trusty_uri

    inp = assemble.MintInput(
        artifact_type=row.artifact_type,
        sentence=canonical_sentence(row.approved_title),
        aida_uri=row.aida_uri,
        hub_ref_id=row.claim_ref_id,
        grounding=grounding,
        fields=dict(payload.get("fields") or {}),
        conjuncts=conjuncts,
        motivation=payload.get("motivation"),
        testable_by=payload.get("testable_by"),
        motivated_by=motivated_by,
        software=_software_provenance(),
    )
    return inp, dependency_codes


def _build_and_sign(
    inp: assemble.MintInput, profile: Any, llm_models: list[str]
) -> Any:
    from nanopub import Nanopub, NanopubConf
    from nanopub.definitions import DUMMY_NAMESPACE

    if llm_models:
        import dataclasses

        inp = dataclasses.replace(
            inp, software={**inp.software, "llm_models": llm_models}
        )

    assertion, provenance, pubinfo = assemble.build_graphs(inp, DUMMY_NAMESPACE)
    conf = NanopubConf(
        profile=profile,
        # dct:created is written by the mint step (gate #12): the
        # library stamps pubinfo at sign time — never hand-authored.
        add_prov_generated_time=False,
        add_pubinfo_generated_time=True,
        attribute_assertion_to_profile=False,
        attribute_publication_to_profile=True,
    )
    np = Nanopub(conf=conf, assertion=assertion, provenance=provenance, pubinfo=pubinfo)
    np.sign()
    if not (np.has_valid_trusty and np.has_valid_signature):
        raise RuntimeError("nanopub library produced an invalid artifact")
    return np


def _software_provenance() -> dict[str, Any]:
    """Structured software provenance, resolved live at mint (never
    hardcoded): package version + deployed sha. The sha resolution reuses
    the status surface's env-first chain (``PRECIS_GIT_SHA`` baked into
    images; live-checkout git state otherwise)."""
    from precis import __version__

    sha = os.environ.get("PRECIS_GIT_SHA", "").strip() or None
    if not sha or sha.lower() == "unknown":
        try:
            from precis.handlers.skill import _SOURCE_GIT_INFO

            sha = _SOURCE_GIT_INFO.get("git_sha")
        except Exception:  # pragma: no cover - status surface unavailable
            sha = None
    return {"name": "precis", "version": __version__, "sha": sha or "unknown"}
