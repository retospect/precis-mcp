"""The elite archive (Decision 9): a small, deliberately DIVERSE set of
good poses the placer's restarts round-robin from, so the search keeps
several basins open instead of collapsing onto one optimum.

Diversity rule (spec §1a): a candidate better than the worst elite is
admitted only if, for EVERY elite, its mean part displacement is ≥
``d_min`` (default 2 mm) or it differs in ≥ ``rot_frac`` (10 %) of
rotations; otherwise it replaces the nearest elite only when better
(crowding — a close rival may improve a niche, never clone it).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class Elite:
    pose: np.ndarray  # (N, 4)
    cost: float


def mean_displacement(a: np.ndarray, b: np.ndarray) -> float:
    """Mean per-part |Δxy| between two poses — the diversity metric's
    spatial leg."""
    return float(np.hypot(a[:, 0] - b[:, 0], a[:, 1] - b[:, 1]).mean())


def rotations_differ(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of parts whose orientation (rot_quarter + side) differs
    — the diversity metric's orientation leg."""
    return float(np.mean((a[:, 2] != b[:, 2]) | (a[:, 3] != b[:, 3])))


class EliteArchive:
    def __init__(
        self, size: int = 8, *, d_min: float = 2.0, rot_frac: float = 0.1
    ) -> None:
        self.size = size
        self.d_min = d_min
        self.rot_frac = rot_frac
        self._elites: list[Elite] = []

    @property
    def elites(self) -> tuple[Elite, ...]:
        return tuple(sorted(self._elites, key=lambda e: e.cost))

    def _close(self, pose: np.ndarray, e: Elite) -> bool:
        """True when the candidate is inside the diversity rule of this
        elite — nearer than ``d_min`` AND fewer than ``rot_frac``
        rotations different."""
        return (
            mean_displacement(pose, e.pose) < self.d_min
            and rotations_differ(pose, e.pose) < self.rot_frac
        )

    def admit(self, pose: np.ndarray, cost: float) -> bool:
        """Spec's admission rule. Returns True on a change.

        Read as an invariant: no two stored elites may ever violate the
        diversity rule — so the crowding replacement happens only inside
        the set of elites the candidate is too close to (``offenders``);
        the nearest offender is the spec's "nearest elite" (every
        non-offender is further away by definition)."""
        pose = np.asarray(pose, dtype=np.float64)
        if not self._elites:
            self._elites.append(Elite(pose.copy(), cost))
            return True
        offenders = [e for e in self._elites if self._close(pose, e)]
        if not offenders:
            worst = max(self._elites, key=lambda e: e.cost)
            if cost < worst.cost:
                if len(self._elites) >= self.size:
                    # list.remove(e) is unusable — Elite equality walks
                    # an ndarray; identity filter instead.
                    self._elites = [e for e in self._elites if e is not worst]
                self._elites.append(Elite(pose.copy(), cost))
                return True
            return False
        # crowding: take the offenders' slots iff the candidate beats
        # EVERY one — evicting only the nearest would leave the
        # candidate sitting inside a remaining offender's radius, which
        # is exactly the pairwise violation the rule exists to forbid.
        if all(cost < e.cost for e in offenders):
            self._elites = [
                e for e in self._elites if all(e is not o for o in offenders)
            ]
            self._elites.append(Elite(pose.copy(), cost))
            return True
        return False


__all__ = ["Elite", "EliteArchive", "mean_displacement", "rotations_differ"]
