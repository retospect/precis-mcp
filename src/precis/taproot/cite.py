"""Taproot Phase 1 + 2 — the ONE hub cite-key resolution policy, plus the
shared authorial-pin overlay.

Both ``precis resolve`` (:mod:`precis.cli.resolve`) and the draft export
(:mod:`precis.export.latex` / :mod:`precis.export.docx`) resolve a
``TAPROOT:claim`` hub's ``[<pub_id>]`` / ``[fi<id>]`` cite to the SAME
derived ``establishes`` originator(s) — falling back to corroborators,
then to in-flight when the hub has no supporting evidence at all. That
policy is locked here, once, and imported by both surfaces so they can
never quietly diverge (that divergence was the exact bug Phase 1 fixes).

Pins (Taproot slice A2, ``[<pub_id>>…]`` / ``[<pub_id>+…]``) are the same
story one level up: :func:`apply_pin` / :func:`resolve_pin_handle` are the
ONE pin-application policy, shared by ``precis resolve`` (base32 ``[pub_id]``
token grammar, :mod:`precis.cli.resolve`) and the draft ``mentions`` bracket
grammar (Phase 2, :mod:`precis.utils.mentions`'s ``pin`` capture group,
consumed by the draft exporters) — so a pin behaves identically wherever an
author writes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from precis.taproot.seniority import (
    EvidenceEdge,
    HubEvidence,
    derive_evidence,
    is_claim_hub,
)

if TYPE_CHECKING:
    from precis.store.protocols import ClaimTrustStore, PinStore


def _cite_keys_for_group(
    store: ClaimTrustStore,
    edges: list[EvidenceEdge],
    *,
    cite_key_map: dict[int, list[str]] | None = None,
) -> tuple[list[str], list[int]]:
    """Resolve each edge's paper to its (oldest) ``cite_key`` alias.

    Returns ``(cite_keys, skipped_ref_ids)`` — a paper with no
    ``cite_key`` alias at all is dropped from the render rather than
    failing the whole hub, and its ``ref_id`` is reported back so the
    caller can warn about it.

    ``cite_key_map`` — a pre-fetched ``{paper_ref_id: aliases}`` map
    (:func:`~precis.store.Store.ref_cite_keys_bulk`) — skips the
    per-paper ``store.ref_cite_keys`` round trip entirely when given
    (the batch B fix: this was an N+1 per hub, and a *second* N+1 when
    ``claim_trust`` re-derived the same hub — see :mod:`precis.taproot.trust`).
    ``None`` (the default) preserves the old per-edge query behaviour.
    """
    cite_keys: list[str] = []
    skipped: list[int] = []
    for edge in edges:
        aliases = (
            cite_key_map.get(edge.paper_ref_id, [])
            if cite_key_map is not None
            else store.ref_cite_keys(edge.paper_ref_id)
        )
        if aliases:
            cite_keys.append(aliases[0])
        else:
            skipped.append(edge.paper_ref_id)
    return cite_keys, skipped


def hub_cite_keys(
    store: ClaimTrustStore,
    evidence: HubEvidence,
    *,
    cite_key_map: dict[int, list[str]] | None = None,
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

    ``cite_key_map`` threads through to :func:`_cite_keys_for_group` — a
    bulk caller resolving many hubs at once passes one pre-fetched map
    covering every supporter across every hub instead of paying a query
    per supporter here.
    """
    notes: list[tuple[str, str]] = []
    originator_keys, skipped = _cite_keys_for_group(
        store, evidence.originators, cite_key_map=cite_key_map
    )
    for ref_id in skipped:
        notes.append(
            (
                "established",
                f"originator paper ref_id={ref_id} has no cite_key — skipped",
            )
        )
    if originator_keys:
        return originator_keys, notes

    corroborator_keys, skipped = _cite_keys_for_group(
        store, evidence.corroborators, cite_key_map=cite_key_map
    )
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
    #: The hub's freshly-derived evidence — set only when ``is_hub`` (``None``
    #: for a plain finding), so a caller can :func:`apply_pin` without
    #: re-deriving it a second time.
    evidence: HubEvidence | None = None


def finding_cite_keys(
    store: ClaimTrustStore,
    ref_id: int,
    *,
    assume_hub: bool = False,
    cite_key_map: dict[int, list[str]] | None = None,
) -> FindingCite:
    """Resolve a finding ref to its bibliographic cite_key(s) — the one
    entry point both ``precis resolve`` and the draft exporters call.

    A ``TAPROOT:claim`` hub resolves via :func:`hub_cite_keys` over its
    freshly :func:`~precis.taproot.seniority.derive_evidence` evidence — a
    living citation, recomputed on every call. A plain finding resolves
    off its own ``meta``: the ``primary_cite_key`` once the chase has
    established it, else the ``pub_id`` placeholder.

    ``assume_hub``/``cite_key_map`` are the batch-B perf knobs — a caller
    that already confirmed ``ref_id`` is a hub (skips the redundant
    ``is_claim_hub`` re-check) and/or pre-fetched cite_key aliases in bulk
    can thread both through here. Both default to the old per-call
    behaviour.
    """
    if assume_hub or is_claim_hub(store, ref_id):
        evidence = derive_evidence(store, ref_id, assume_hub=assume_hub)
        cite_keys, notes = hub_cite_keys(store, evidence, cite_key_map=cite_key_map)
        return FindingCite(
            cite_keys=cite_keys,
            is_hub=True,
            inflight=not cite_keys,
            notes=notes,
            evidence=evidence,
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


# ── Authorial pins (Taproot slice A2) ──────────────────────────────────
#
# A hub's living-citation default (`hub_cite_keys`) can be overridden
# inline: `[<label>>pa5,pc293]` cites exactly those universal handles
# instead of the derived originators (**replace**); `[<label>+pa5]` cites
# the derived originators *plus* those (**supplement**, deduped). A
# `pc<id>` (paper-chunk/passage) handle resolves to its parent paper's
# cite_key. Purely syntactic — no storage, no draft-side edge. This is the
# ONE application policy: `precis resolve` (the base32 `[pub_id]` token
# grammar) and the draft ``mentions`` bracket grammar (Phase 2) both call
# `apply_pin` so a pin behaves identically wherever an author writes it.


def resolve_pin_handle(store: PinStore, handle: str) -> tuple[int, str] | None:
    """Resolve one authorial pin handle to ``(paper_ref_id, cite_key)``.

    A ``pc<id>`` (paper-chunk/passage) handle resolves to its **parent
    paper** — the ``.bib`` is paper-level, so pinning a passage means
    "grounded at this figure," not a separate citable unit.
    :func:`~precis.store.Store.resolve_handle` already does that
    parent-lookup for a chunk handle (``ResolvedHandle.ref_id`` is the
    owning ref), so this reuses it rather than hand-rolling chunk→paper
    resolution.

    ``None`` when the handle isn't well-formed, doesn't resolve to a
    live paper, or that paper has no ``cite_key`` alias — the caller
    warns and skips.
    """
    resolved = store.resolve_handle(handle)
    if resolved is None or resolved.kind != "paper":
        return None
    aliases = store.ref_cite_keys(resolved.ref_id)
    if not aliases:
        return None
    return resolved.ref_id, aliases[0]


@dataclass(frozen=True)
class PinResult:
    """The outcome of applying one authorial pin to a hub's derived
    cite_keys."""

    cite_keys: list[str]
    diverged: bool = False
    #: advisory text when a **replace** pin diverges from the derived
    #: ``establishes`` originators; ``None`` when not diverged (or op=='+').
    divergence: str | None = None
    #: ``(status, detail)`` diagnostics — unresolvable pinned handle, an
    #: empty-replace fallback, etc. Meant for the caller's warning log.
    warnings: list[tuple[str, str]] = field(default_factory=list)


def apply_pin(
    store: PinStore,
    *,
    label: str,
    op: str,
    handles: list[str],
    derived_cite_keys: list[str],
    evidence: HubEvidence,
) -> PinResult:
    """Apply an authorial pin to a hub's derived cite_keys — ``op`` is
    ``'>'`` (replace) or ``'+'`` (supplement).

    Resolves each pinned handle (:func:`resolve_pin_handle`, deduped by
    paper ref_id, first-seen order), warning + skipping an unresolvable
    one. Reports a divergence advisory when the pinned paper set differs
    from the hub's *actually derived* ``establishes`` originators (not the
    corroborator-fallback set — a pin only "diverges" from a real
    seniority split) — **replace (``>``) only**. A supplement (``+``) pin
    is purely additive ("derived plus these"), so its handle set
    legitimately differs from the full derived set on every normal use;
    it has no divergence concept and never fires the advisory.

    ``'>'`` (replace) with an empty resolved pin set falls back to
    ``derived_cite_keys`` unchanged, with a warning — a citation must
    never silently disappear because a pin went stale.

    ``label`` is the pinned finding's display label (a base32 pub_id for
    ``precis resolve``, a ``fi<id>`` handle for the draft grammar) —
    used only in warning/divergence message text.
    """
    from precis.utils import handle_registry

    warnings: list[tuple[str, str]] = []
    pinned: list[tuple[int, str]] = []
    seen_ref_ids: set[int] = set()
    for handle in handles:
        resolved = resolve_pin_handle(store, handle)
        if resolved is None:
            warnings.append(
                (
                    "pin",
                    f"pinned handle {handle} did not resolve to a cited "
                    "paper — skipped",
                )
            )
            continue
        ref_id, cite_key = resolved
        if ref_id in seen_ref_ids:
            continue
        seen_ref_ids.add(ref_id)
        pinned.append((ref_id, cite_key))

    pinned_ref_ids = {ref_id for ref_id, _ in pinned}
    pinned_keys = [cite_key for _, cite_key in pinned]

    if op == ">":
        # Divergence advisory — replace only (see docstring: a supplement
        # pin has no divergence concept).
        diverged = False
        divergence: str | None = None
        originator_ref_ids = {edge.paper_ref_id for edge in evidence.originators}
        if (
            pinned_ref_ids
            and originator_ref_ids
            and pinned_ref_ids != originator_ref_ids
        ):
            pinned_str = ", ".join(
                sorted(
                    handle_registry.format_handle("paper", r) for r in pinned_ref_ids
                )
            )
            derived_str = ", ".join(
                sorted(
                    handle_registry.format_handle("paper", r)
                    for r in originator_ref_ids
                )
            )
            divergence = (
                f"[{label}] pinned {{{pinned_str}}} but derived originator "
                f"is {{{derived_str}}} — reconsider"
            )
            diverged = True

        if pinned_keys:
            return PinResult(
                cite_keys=pinned_keys,
                diverged=diverged,
                divergence=divergence,
                warnings=warnings,
            )
        warnings.append(
            (
                "pin",
                "replace pin resolved to no usable cite_keys — falling "
                "back to derived hub resolution",
            )
        )
        return PinResult(
            cite_keys=derived_cite_keys,
            diverged=diverged,
            divergence=divergence,
            warnings=warnings,
        )
    # op == "+": supplement — derived originators first, pinned appended,
    # deduped by cite_key, deterministic (pin order after derived order).
    return PinResult(
        cite_keys=derived_cite_keys
        + [key for key in pinned_keys if key not in derived_cite_keys],
        warnings=warnings,
    )


__all__ = [
    "FindingCite",
    "PinResult",
    "apply_pin",
    "finding_cite_keys",
    "hub_cite_keys",
    "resolve_pin_handle",
]
