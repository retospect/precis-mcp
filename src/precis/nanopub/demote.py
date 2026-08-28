"""Demotion — what a claim's publish posture does when the evidence turns.

The ratchet (:mod:`precis.taproot`, "Two structural cautions"): every
stage of the claim lifecycle promotes and almost nothing demotes, so a
hub accumulates support and never re-opens when contradicting evidence
lands later. The backward edges have always been *legal*
(``state.TRANSITIONS`` allows ``reviewed``/``signed`` → ``candidate``,
and ``store.nanopub_reopen`` is the write) — they were only ever walked
by a drift/edit reopen. Nothing walked them because evidence arrived.
This module is the policy that does, keyed on **the freeze line**:

* **below it** (no publish row / ``candidate``) — nothing is frozen and
  the publish gates already block on a live ``contradicts`` edge, so
  there is nothing to undo: ``none``.
* **at it** (``reviewed``, ``signed``) — the claim string, or the
  artifact pointer, is frozen but nothing has left the building. Flip
  back to ``candidate``: the frozen fields are discarded, the
  append-only artifact row stays, and the hub must earn its approval
  again against the new evidence. ``reopen``.
* **above it** (``anchored``, ``published``) — an anchor is irreversible
  and a published claim is public forever; a third party holds the
  trusty URI and the AIDA sentence. Editing here is not demotion, it is
  rewriting history someone else may have cited. The only honest move is
  a *new artifact* — supersede or retract — and that is a human door
  (``precis nanopub publish``, ``retract``). So this raises an alert and
  changes no state: ``supersede-required``.

Terminal rows (``superseded``/``retracted``/``rejected``) are already
settled and read as ``none``.

**Additive, like every other writer on this path.** A demotion never
removes an edge, never edits the claim, and never touches the artifact
bytes — it moves one publish row down one rung and says so. The
scale caution applies here more than anywhere: a bad judge that
promotes wrongly is a nuisance, a bad judge wired to a demoter can
un-approve the corpus at machine speed. So the *only* caller is a
freshly written ``contradicts`` edge — a verdict a judge already
committed to the graph — never a bare suspicion, and the frozen rungs
are never demoted automatically at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from precis.nanopub.state import (
    FROZEN_BYTES,
    FROZEN_STRING,
    check_transition,
    frozen_rung,
)

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Nothing frozen (or already terminal) — no posture to walk back.
ACTION_NONE = "none"
#: Pre-anchor: flip the publish row back to ``candidate``.
ACTION_REOPEN = "reopen"
#: Anchored or published — a human must supersede/retract instead.
ACTION_SUPERSEDE_REQUIRED = "supersede-required"

ALERT_SOURCE = "nanopub-demote"


@dataclass(frozen=True, slots=True)
class DemotionRequest:
    """One hub whose evidence turned, queued for the post-commit drain.

    Collected inside the writer's open transaction and applied after it
    commits — the same deferral ``taproot.hub.attach_evidence``'s
    retraction check uses, and for the same reason: the demotion write
    opens its own pool connection.
    """

    hub_ref_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class Demotion:
    """What a demotion decided, and whether the write landed."""

    hub_ref_id: int
    action: str
    from_state: str | None
    reason: str
    #: True only when the state flip actually committed. ``False`` on a
    #: ``none``/``supersede-required`` decision, and on a lost CAS race.
    applied: bool = False


def plan_demotion(state: str | None) -> str:
    """The action the freeze line prescribes for a hub in ``state``.

    Pure: no store, no side effects, so the policy is testable on its
    own and readable in one place.
    """
    # Above the anchor first: ``anchored`` shares the ``bytes`` rung with
    # ``signed`` (both freeze the artifact) but not its reversibility —
    # signing is local, anchoring is a calendar attestation that cannot be
    # taken back. Rung alone would wrongly reopen it.
    if state in ("anchored", "published"):
        return ACTION_SUPERSEDE_REQUIRED
    if frozen_rung(state) in (FROZEN_STRING, FROZEN_BYTES):
        return ACTION_REOPEN
    # Everything left is unfrozen: no publish row, ``candidate``, or a
    # terminal row that is already settled.
    return ACTION_NONE


def demote_hub(store: Store, hub_ref_id: int, *, reason: str) -> Demotion:
    """Walk one hub down the ladder because its evidence turned.

    Reads the hub's live publish row, applies :func:`plan_demotion`, and
    for a ``reopen`` performs the flip through ``store.nanopub_reopen``
    (which is itself conditional on ``state IN ('reviewed','signed')``,
    so a racing signer simply wins and this returns ``applied=False``).
    An unminted hub is a no-op — there is no posture to lower.

    Never raises for an ordinary outcome: a lost race, a vanished row,
    and a frozen hub are all *results*, reported on the
    :class:`Demotion`, because the caller is a per-hub worker arm whose
    real work (the ``contradicts`` edge) has already committed.
    """
    row = store.nanopub_publish_row(hub_ref_id)
    state = row.state if row is not None else None
    action = plan_demotion(state)

    if action == ACTION_NONE:
        return Demotion(hub_ref_id, action, state, reason)

    if action == ACTION_SUPERSEDE_REQUIRED:
        # Nothing to write: the bytes are out. Say so loudly instead —
        # this is precisely the case a human has to adjudicate, and the
        # alert is the only thing standing between "published claim
        # gained a contradictor" and nobody ever noticing.
        from precis.alerts import raise_alert

        raise_alert(
            store,
            source=ALERT_SOURCE,
            fingerprint=f"contradicted-frozen:{hub_ref_id}",
            title=f"frozen claim #{hub_ref_id} gained contradicting evidence",
            detail=(
                f"state={state}; {reason}. The artifact is frozen — it cannot "
                f"be reopened. Adjudicate and, if the contradiction holds, "
                f"supersede or retract it (precis nanopub retract)."
            ),
            severity="warn",
            subject_ref_id=hub_ref_id,
        )
        log.warning(
            "nanopub_demote: hub #%d is %s -- supersede/retract required (%s)",
            hub_ref_id,
            state,
            reason,
        )
        return Demotion(hub_ref_id, action, state, reason)

    if row is None or state is None:  # pragma: no cover — reopen implies a row
        return Demotion(hub_ref_id, ACTION_NONE, state, reason)
    # Legality first, the CAS second — the module contract in state.py.
    check_transition(state, "candidate")
    applied = store.nanopub_reopen(row.id)
    if applied:
        log.info(
            "nanopub_demote: hub #%d reopened %s -> candidate (%s)",
            hub_ref_id,
            state,
            reason,
        )
    else:
        # The row moved under us between the read and the write; the
        # next contradicts edge (or a drift reopen) will catch it.
        log.info(
            "nanopub_demote: hub #%d reopen from %s lost the CAS -- skipped",
            hub_ref_id,
            state,
        )
    return Demotion(hub_ref_id, action, state, reason, applied=applied)


def run_demotions(
    store: Store, requests: list[DemotionRequest] | None
) -> list[Demotion]:
    """Drain a post-commit demotion queue, one hub at a time.

    Deduped on ``hub_ref_id`` (several contradicting edges in one pass
    still demote the hub once) and individually guarded: a failure to
    demote must never fail the pass that produced the evidence, which is
    the durable thing here.
    """
    if not requests:
        return []
    out: list[Demotion] = []
    seen: set[int] = set()
    for req in requests:
        if req.hub_ref_id in seen:
            continue
        seen.add(req.hub_ref_id)
        try:
            out.append(demote_hub(store, req.hub_ref_id, reason=req.reason))
        except Exception:
            log.warning(
                "nanopub_demote: demotion failed for hub #%d",
                req.hub_ref_id,
                exc_info=True,
            )
    return out


__all__ = [
    "ACTION_NONE",
    "ACTION_REOPEN",
    "ACTION_SUPERSEDE_REQUIRED",
    "ALERT_SOURCE",
    "Demotion",
    "DemotionRequest",
    "demote_hub",
    "plan_demotion",
    "run_demotions",
]
