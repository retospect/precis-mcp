"""Taproot Phase 1 — the ONE hub cite-key resolution policy.

Both ``precis resolve`` (:mod:`precis.cli.resolve`) and the draft export
(:mod:`precis.export.latex` / :mod:`precis.export.docx`) resolve a
``TAPROOT:claim`` hub's ``[<pub_id>]`` / ``[fi<id>]`` cite to the SAME
derived ``establishes`` originator(s) — falling back to corroborators,
then to in-flight when the hub has no supporting evidence at all. That
policy is locked here, once, and imported by both surfaces so they can
never quietly diverge (that divergence was the exact bug Phase 1 fixes).

Pins (Taproot slice A2, ``[<pub_id>>…]`` / ``[<pub_id>+…]``) are a
``precis resolve``-only overlay on top of this policy and stay in
:mod:`precis.cli.resolve` — out of scope here (Phase 2 wires them into
the draft ``mentions`` grammar).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from precis.taproot.seniority import (
    EvidenceEdge,
    HubEvidence,
    derive_evidence,
    is_claim_hub,
)


def _cite_keys_for_group(
    store: Any, edges: list[EvidenceEdge]
) -> tuple[list[str], list[int]]:
    """Resolve each edge's paper to its (oldest) ``cite_key`` alias.

    Returns ``(cite_keys, skipped_ref_ids)`` — a paper with no
    ``cite_key`` alias at all is dropped from the render rather than
    failing the whole hub, and its ``ref_id`` is reported back so the
    caller can warn about it.
    """
    cite_keys: list[str] = []
    skipped: list[int] = []
    for edge in edges:
        aliases = store.ref_cite_keys(edge.paper_ref_id)
        if aliases:
            cite_keys.append(aliases[0])
        else:
            skipped.append(edge.paper_ref_id)
    return cite_keys, skipped


def hub_cite_keys(
    store: Any, evidence: HubEvidence
) -> tuple[list[str], list[tuple[str, str]]]:
    """Locked resolution policy for a claim hub's living citation.

    1. Derived ``establishes`` originators, if any have a cite_key.
    2. Else ``corroborators``, if any have a cite_key (best-available
       fallback — the caller's warnings note these aren't derived
       originators yet).
    3. Else empty — the caller treats the hub as in-flight.

    Returns ``(cite_keys, notes)`` where ``notes`` are ``(status,
    detail)`` diagnostic pairs meant for a caller's warning/summary log
    (skipped no-cite_key papers, the corroborator-fallback flag).
    """
    notes: list[tuple[str, str]] = []
    originator_keys, skipped = _cite_keys_for_group(store, evidence.originators)
    for ref_id in skipped:
        notes.append(
            (
                "established",
                f"originator paper ref_id={ref_id} has no cite_key — skipped",
            )
        )
    if originator_keys:
        return originator_keys, notes

    corroborator_keys, skipped = _cite_keys_for_group(store, evidence.corroborators)
    for ref_id in skipped:
        notes.append(
            (
                "established",
                f"corroborator paper ref_id={ref_id} has no cite_key — skipped",
            )
        )
    if corroborator_keys:
        notes.append(
            (
                "established",
                "resolved via corroborator(s) — no derived originator yet",
            )
        )
        return corroborator_keys, notes

    return [], notes


@dataclass(frozen=True)
class FindingCite:
    """The resolved cite_key(s) for one finding handle (hub or plain)."""

    cite_keys: list[str]  # resolved bib keys; empty = in-flight / no source
    is_hub: bool
    inflight: bool  # a hub with no resolvable evidence
    notes: list[tuple[str, str]]  # (status, detail) diagnostics, for the caller


def finding_cite_keys(store: Any, ref_id: int) -> FindingCite:
    """Resolve a finding ref to its bibliographic cite_key(s) — the one
    entry point both ``precis resolve`` and the draft exporters call.

    A ``TAPROOT:claim`` hub resolves via :func:`hub_cite_keys` over its
    freshly :func:`~precis.taproot.seniority.derive_evidence` evidence — a
    living citation, recomputed on every call. A plain finding resolves
    off its own ``meta``: the ``primary_cite_key`` once the chase has
    established it, else the ``pub_id`` placeholder.
    """
    if is_claim_hub(store, ref_id):
        evidence = derive_evidence(store, ref_id)
        cite_keys, notes = hub_cite_keys(store, evidence)
        return FindingCite(
            cite_keys=cite_keys,
            is_hub=True,
            inflight=not cite_keys,
            notes=notes,
        )

    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None:
        return FindingCite(cite_keys=[], is_hub=False, inflight=True, notes=[])
    meta = ref.meta or {}
    key = meta.get("primary_cite_key") or meta.get("pub_id")
    cite_keys = [str(key)] if key else []
    return FindingCite(
        cite_keys=cite_keys,
        is_hub=False,
        inflight=not cite_keys,
        notes=[],
    )


__all__ = ["FindingCite", "finding_cite_keys", "hub_cite_keys"]
