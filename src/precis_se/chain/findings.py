"""The handler-side ``chain_*`` findings — the ones that need the store,
and the first pass in ``se`` that **replaces** a pure finding instead of
adding to it.

:func:`precis_se.chain.drc.findings` is pure over the tree by contract
(:func:`precis_se.drc.drc`'s own docstring), so ``chain_floppy``'s pure
rows measure every single-stranded span against ssDNA's *coded*
persistence length. When the design carries a ``material``
``persistence_length`` row for a helix
(:data:`precis_se.compose.LP_KEY`, resolved through
:func:`precis_se.chain.relax.material_lp_m` — the same reader
``relax_chain``'s hinge springs use), that row is strictly better
information about the same span, and a design would otherwise carry **two**
findings about one span: one against the coded default and one against the
row. So this pass **supersedes** the rule rather than appending to it.

That makes it the odd one out: :func:`precis_se.precedent.findings` and
:func:`precis_se.kinematics_drc.findings` only ever append. The
supersession is therefore deliberately *visible* at the call site —
:func:`findings` returns :class:`HandlerFindings`, naming the rules it
replaces, and :func:`precis_se.handler._render_drc` is what drops them.
Nothing here reaches into the pure pass's list.

**Rows are rebuilt for every helix, not only the ones with a row.** The
re-emission runs :func:`precis_se.chain.drc.floppy_findings` over the whole
design with a per-helix Lp reader, so a design where one helix has a
material row and three do not still ends up with exactly one row per span —
the three keep the coded default, and say so.

**The fold rows are append-only** (:func:`precis_se.chain.fold.findings` —
``chain_fold_unavailable``, ``chain_fold_skipped``, ``chain_fold_disagree``,
``chain_offtarget``). They supersede nothing: no pure rule measures a fold,
and a design with no sequenced strand gets none of them. They live
handler-side because ViennaRNA is an optional dependency and the pure DRC
pass may not depend on one — a ``view='drc'`` render must not change shape
with the venv it runs in, beyond the one info row that says it did.

**The transition rows are append-only too** (``se-walker-light-protocol``):
``chain_transition_guard`` — a transition whose ``params.guard`` the
from-state's occupancy violates — and the spectral budget pair
(:mod:`precis_se.chain.spectral`). They live here because transitions
are not on the tree: ``design_states.transitions_for`` is a store read,
and the band widths come from ``material`` rows. A design with no
state-carrying block costs one query and gets no row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.design import states as design_states
from precis.utils.units import format_quantity
from precis_se.chain import drc as chain_drc
from precis_se.chain import fold as chain_fold
from precis_se.chain import nucleic
from precis_se.chain import spectral as chain_spectral
from precis_se.chain.occupancy import guard_violations
from precis_se.chain.pairing import derive_pairing
from precis_se.chain.relax import material_lp_m
from precis_se.validate import ValidationIssue


@dataclass(frozen=True)
class HandlerFindings:
    """What a store-reading chain pass contributes to ``view='drc'``.

    ``supersedes`` names the pure rules whose rows this pass's ``rows``
    **replace** — empty when it only adds (or contributes nothing), which
    is the common case. The caller drops the named rules from the pure
    list before extending it with ``rows``; doing it there rather than in
    here keeps the one destructive step in the one place that owns the
    findings list.
    """

    supersedes: frozenset[str] = frozenset()
    rows: list[ValidationIssue] = field(default_factory=list)


def findings(store: Any, tree: Any, ref_id: int) -> HandlerFindings:
    """The store-reading ``chain_*`` findings for ``tree``.

    Two passes, one superseding and one appending: ``chain_floppy``
    re-emitted against a ``material`` persistence-length row (this module's
    docstring), and the fold checks (:func:`precis_se.chain.fold.findings`).
    With no ``material`` row anywhere the pure ``chain_floppy`` rows stand
    unchanged, and with no sequenced strand the fold pass returns nothing —
    together the fast path for the overwhelming majority of designs (one
    store read per helix, and none for a design with no helices).

    ``ref_id`` keys the transition pass (module docstring); the material
    row resolution goes through the design's own slug
    (:func:`precis_se.chain.relax.material_lp_m`) and the fold checks read
    only the tree.
    """
    extra_rows = [
        *chain_fold.findings(tree),
        *_transition_findings(store, tree, ref_id),
    ]
    geoms, loops = chain_drc.geometry_and_loops(tree)
    if not geoms:
        return HandlerFindings(rows=extra_rows)
    sourced = material_lp_m(store, tree, sorted(geoms))
    if not sourced:
        return HandlerFindings(rows=extra_rows)
    coded = format_quantity(nucleic.LP_SSDNA_M, "length")

    def lp_of(helix: str) -> tuple[float, str]:
        hit = sourced.get(helix)
        if hit is None:
            return (
                nucleic.LP_SSDNA_M,
                f"{chain_drc.CODED_LP_NOTE}; this design has one for other "
                "helices but not this one",
            )
        value_m, source = hit
        return value_m, f"{source}, not the coded {coded}"

    return HandlerFindings(
        supersedes=frozenset({"chain_floppy"}),
        rows=[
            *chain_drc.floppy_findings(derive_pairing(tree), loops, lp_of=lp_of),
            *extra_rows,
        ],
    )


def _transition_findings(
    store: Any, tree: Any, ref_id: int | None
) -> list[ValidationIssue]:
    """``chain_transition_guard`` over every state-carrying block's
    transitions, then the spectral budget over all of them together
    (channels are a design-wide resource). Nothing for an unsaved design."""
    if store is None or ref_id is None:
        return []
    uids = design_states.state_carrying_uids(store, ref_id)
    if not uids:
        return []
    by_uid = {
        node.uid: name
        for name, node in tree.blocks.items()
        if getattr(node, "uid", None) in uids
    }
    states = design_states.design_states(store, ref_id)
    domains = list(getattr(tree, "domains", []) or [])
    rows: list[ValidationIssue] = []
    per_block: dict[str, list[design_states.Transition]] = {}
    for uid, name in sorted(by_uid.items(), key=lambda kv: kv[1]):
        edges = design_states.transitions_for(store, ref_id, uid)
        if not edges:
            continue
        per_block[name] = edges
        occupancy_of = {s.name: s.occupancy for s in states.get(uid, [])}
        for t in edges:
            guard = (t.params or {}).get("guard")
            if not guard:
                continue
            subject = f"{name} {t.from_state}→{t.to_state}"
            if not isinstance(guard, dict):
                rows.append(
                    ValidationIssue(
                        rule="chain_transition_guard",
                        subject=subject,
                        severity="error",
                        detail=(
                            f"params.guard is {guard!r}, not a "
                            "{'<strand>.<ord>': 'bound'|'free'|'<helix>@<offset>'} "
                            "map — re-declare the transition"
                        ),
                    )
                )
                continue
            failed = guard_violations(guard, occupancy_of.get(t.from_state), domains)
            if failed:
                rows.append(
                    ValidationIssue(
                        rule="chain_transition_guard",
                        subject=subject,
                        severity="error",
                        detail=(
                            "the from-state violates the ratchet guard: "
                            + "; ".join(failed)
                            + " — the transition cannot fire from there; fix "
                            "the guard or the state's occupancy"
                        ),
                    )
                )
    if not per_block:
        return rows
    optics = getattr(tree, "optics", None)
    available = optics.get("channels_available") if isinstance(optics, dict) else None
    rows.extend(
        chain_spectral.findings(
            chain_spectral.channels(per_block),
            channels_available=available,
            band_of=chain_spectral.band_resolver(store, tree),
        )
    )
    return rows
