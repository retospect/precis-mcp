"""Query-param filters shared by the graph pages — the ``/graph/`` JSON +
SVG focus page (``docs/backlog/web-graph-navigation.md`` slice 4) and the
``/eye/<handle>`` text ladder (``fisheye-everywhere.md`` in-scope 1).

One parser per axis so the two surfaces can't drift: a comma list is
``a,b``; the kind axis validates against the handle registry (codeless
kinds count — a ring can hold a ``websearch:<slug>`` neighbour); the
``SPACE:`` axis is the closed partition
(:data:`precis.utils.eye_render.SPACE_AXIS`, ``file-mirror.md``
§Pillar-review deltas). Every parser raises ``ValueError`` with the
reason; the caller maps it to its own error shape (JSON 400 on ``/graph``,
the error template on ``/eye``).
"""

from __future__ import annotations

from precis.utils import handle_registry
from precis.utils.eye_render import SPACE_AXIS


def csv_list(raw: str | None) -> list[str] | None:
    """``"a, b"`` → ``["a", "b"]`` — the comma-list shape ``/graph``'s
    ``rels``/``kinds`` params already spoke. ``None`` when empty."""
    vals = [v.strip() for v in (raw or "").split(",") if v.strip()]
    return vals or None


def parse_kind_filter(raw: str | None) -> frozenset[str] | None:
    """``kind=memory,quest`` → the kinds a ring keeps, or ``None`` (axis
    open). Raises ``ValueError`` on a kind the registry doesn't know."""
    vals = csv_list(raw)
    if vals is None:
        return None
    bad = [v for v in vals if not handle_registry.is_known_kind(v)]
    if bad:
        raise ValueError(
            f"unknown kind {bad[0]!r} — kind names like 'memory', 'paper', "
            "'quest' (the ref's own kind, not its handle code)"
        )
    return frozenset(vals)


def parse_space_filter(raw: str | None) -> frozenset[str] | None:
    """``space=repo-dev`` → the ``SPACE:`` values a ring keeps (the
    ``SPACE:`` prefix may be written or omitted), or ``None``. Raises
    ``ValueError`` outside the closed axis."""
    vals = csv_list(raw)
    if vals is None:
        return None
    normed = {v[len("SPACE:") :] if v.upper().startswith("SPACE:") else v for v in vals}
    bad = sorted(normed - SPACE_AXIS)
    if bad:
        raise ValueError(
            f"unknown space {bad[0]!r} — the SPACE: axis is "
            + " | ".join(sorted(SPACE_AXIS))
        )
    return frozenset(normed)
