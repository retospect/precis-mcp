"""The one stable-identity hash for design-check findings.

A finding's id is 12 hex chars of a SHA-1 over ``rule|layer|participant+…``
— participants already canonicalised (sorted, never a coordinate, measured
value or list position) by the caller. ``pcb`` (:func:`precis.pcb.drc.
finding_key`) and ``se`` (:mod:`precis_se.finding_id`) each extract their own
participants and share only this hash step, so the two kinds' ids follow one
documented rule shape (skills ``precis-pcb-help`` / ``precis-se-help``)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable


def finding_hash(
    rule: str, participants: Iterable[str], layer: str | None = None
) -> str:
    """12 hex chars of SHA-1 over ``rule|layer|participant+participant``;
    ``participants`` must already be in canonical (sorted) order."""
    canon = "|".join((rule, layer or "", "+".join(participants)))
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]
