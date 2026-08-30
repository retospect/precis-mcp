"""The publish state machine — legality layer.

``candidate`` → ``reviewed`` → ``signed`` → ``anchored`` → ``published``
→ ``superseded`` / ``retracted``; ``rejected`` branches off ``reviewed``.
Backward flips exist only pre-anchor: drift or an edit reopens
``reviewed``/``signed`` → ``candidate`` (recompute, not surgery — the
frozen fields are discarded, the append-only artifact row stays), and a
dependency's artifact code changing flips a downstream ``signed`` →
``reviewed`` for the topo re-mint cascade. From ``anchored`` on, nothing
moves backward: an anchor is irreversible (but discloses nothing), and
everything after publication is a new artifact (supersede/retract),
never an edit.

Pure policy; the CAS write is
``store.nanopub_transition(row_id, to_state=…, expect=…)`` — callers
check :func:`check_transition` first, and the CAS's ``expect`` list keeps
a racing writer honest.
"""

from __future__ import annotations

STATES = (
    "candidate",
    "reviewed",
    "signed",
    "anchored",
    "published",
    "superseded",
    "retracted",
    "rejected",
)

#: state → the states it may flip to.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "candidate": ("reviewed",),
    # rejected is terminal and branches off reviewed only;
    # reviewed → candidate is the edit/drift reopen.
    "reviewed": ("signed", "rejected", "candidate"),
    # signed → reviewed is the dependency-dirty flip (topo re-mint);
    # signed → candidate is the local edit reopen (discard artifact ref).
    "signed": ("anchored", "reviewed", "candidate"),
    "anchored": ("published",),
    "published": ("superseded", "retracted"),
    "superseded": (),
    "retracted": (),
    "rejected": (),
}

TERMINAL = ("superseded", "retracted", "rejected")


def check_transition(from_state: str, to_state: str) -> None:
    """Raise ``ValueError`` on an illegal flip; silent when legal."""
    allowed = TRANSITIONS.get(from_state)
    if allowed is None:
        raise ValueError(f"unknown publish state {from_state!r}")
    if to_state not in allowed:
        raise ValueError(
            f"illegal publish transition {from_state!r} → {to_state!r} "
            f"(allowed: {', '.join(allowed) or 'none — terminal state'})"
        )


#: Freeze rungs, weakest first — what a publish state has made immutable.
#: ``''`` nothing · ``'string'`` the claim sentence · ``'bytes'`` the signed
#: artifact · ``'published'`` public and forever.
FROZEN_NONE = ""
FROZEN_STRING = "string"
FROZEN_BYTES = "bytes"
FROZEN_PUBLISHED = "published"


def frozen_rung(state: str | None) -> str:
    """Which rung of the frozen-ness ladder ``state`` sits on.

    The ladder is documented in :mod:`precis.nanopub.overview`; this is
    the single pure reader of it, so the review surface's display and
    :mod:`precis.nanopub.demote`'s policy can never disagree about where
    the freeze line falls. ``None`` (unminted hub) and every terminal
    state read as unfrozen — a terminal row is not a live posture.
    """
    if state in ("signed", "anchored"):
        return FROZEN_BYTES
    if state == "reviewed":
        return FROZEN_STRING
    if state == "published":
        return FROZEN_PUBLISHED
    return FROZEN_NONE
