"""Explicit net-current precondition for placement and routing."""

from __future__ import annotations

import math
from collections.abc import Iterable

from precis.errors import BadInput


def require_net_currents(
    nets: Iterable[tuple[str, float | None]],
    *,
    board_id: str | int | None = None,
) -> None:
    """Refuse unspecified currents; zero is an explicit declaration.

    Graph/builder rows use None, while the IR uses NaN for missing current.
    Class presets cannot establish a design's load, so none exempt a
    signal, power or ground net from this precondition.
    """
    missing = [
        str(name) for name, current in nets if current is None or math.isnan(current)
    ]
    if not missing:
        return
    shown = ", ".join(repr(name) for name in missing[:10])
    if len(missing) > 10:
        shown += f", +{len(missing) - 10} more"
    if board_id is None:
        repair = f"edit the declaration to b.net({missing[0]!r}, current=0.01)"
    else:
        repair = (
            f"put(kind='pcb', id={board_id!r}, "
            f"args={{'nets': [{{'name': {missing[0]!r}, 'current': 0.01}}]}})"
        )
    raise BadInput(
        "pcb: cannot place/route while net currents are unset",
        next=(
            f"{len(missing)} net(s) without current: {shown}. For each net, "
            f"{repair}; replace 0.01 with its estimated maximum amps "
            "(including power and ground)."
        ),
    )
