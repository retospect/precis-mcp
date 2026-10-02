"""se region properties — measurands from the term taxonomy, and the
registry of computers that turn a declared region measure into a realised
value (docs/backlog/se-region-property-layer.md).

**Slice A (this package today):** a measure may name a ``measurand`` — a
``taxon`` node under the ``measurand`` start node (seeded by core migration
``0180_se_measurand_seed.sql``) — instead of, or as well as, the legacy
closed ``unit`` enum. :mod:`precis_se.properties.measurand` resolves it at
write time and snapshots three facts onto the measure row
(:class:`~precis_se.measures.MeasureSpec`): the taxon ref id, the slug, and
the se unit (``MeasureSpec.unit``). Reads need no join.

**The computer registry** (:data:`COMPUTERS`) is the seam slice B fills:
measurand slug → a computer that derives the realised value for a region
(Gasteiger-class partial charges, a group-contribution contact angle, a
point-charge field sum). It is empty here, so every non-geometric measurand
is *declared but unchecked* — :func:`is_checked` says so, and
:func:`precis_se.drc.drc` emits a loud ``measurand_unchecked`` warning for
each such measure rather than staying silent.

A measurand that is one of the legacy enum's four nodes
(:data:`precis_se.measures.LEGACY_MEASURANDS` — length, count, ratio,
angle) counts as checked: it is exactly the legacy ``unit=`` form, which
was never flagged, and ``length`` is computed by the datum evaluator
(:func:`precis_se.datums.evaluate_measure`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from precis_se.measures import LEGACY_MEASURANDS

#: A region computer: ``(tree, measure) -> realised value or None``. The
#: signature is provisional until slice B lands its first computer.
Computer = Callable[[Any, Any], float | None]

#: measurand slug → computer. EMPTY in slice A (module docstring).
COMPUTERS: dict[str, Computer] = {}


def is_checked(measurand: str | None) -> bool:
    """Does anything compute a realised value for this measurand? ``None``
    (a legacy measure with no measurand) is checked by definition — the
    legacy form never carried the advisory."""
    if measurand is None:
        return True
    return measurand in COMPUTERS or measurand in LEGACY_MEASURANDS.values()
