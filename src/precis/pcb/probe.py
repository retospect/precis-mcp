"""``precis.pcb.probe`` — the board-design probe resolver
(gr522028, shared-core-layers S1c).

The pcb half of :func:`precis_diag.probe`: ask what a design-time
selector resolves to, with **no op run** — probing reads the batch
(:meth:`precis_geom.pcb.Board.to_batch` payload or a bare batch dict),
never calls place/drc/gerbers and never mutates the board.

Selectors (strings):

* ``pads_of('<refdes>')`` — every pad of one instance: the component's
  declared pins plus pins its connections mint, each an entity
  ``(refdes, pin)``. The digest's ids use the
  :func:`~precis.pcb.handles.pad_handle` spelling (``U3.1``).
* ``nets:<pattern>`` — the nets whose name matches the glob
  (``*``/``?``, :func:`fnmatch.fnmatchcase`; no wildcard matches the
  name exactly). Authored ``nets[]`` rows and connection-minted nets
  both count. The digest's ids use
  :func:`~precis.pcb.handles.net_handle` (``net:LED_A``); the entity
  kinds follow :mod:`~precis.pcb.selectables`' catalogue spelling
  (``pad`` / ``net``).

:func:`resolver_for` binds a resolver to one board;
:func:`precis.pcb.local.probe` is the script-facing door — same name
and line shape as ``out.probe`` on the CAD side.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from precis.pcb.handles import net_handle, pad_handle

#: The selector spellings this resolver owns.
_PADS_PREFIX = "pads_of("
_NETS_PREFIX = "nets:"


def _batch_of(board: Any) -> dict[str, Any]:
    """A :class:`precis_geom.pcb.Board` or a bare ``pcb_apply`` batch
    dict → the batch (duck-typed, so this module never imports the
    wheel). Anything else refuses."""
    batch = board.to_batch() if hasattr(board, "to_batch") else board
    if not isinstance(batch, dict):
        raise ValueError(
            f"pcb probe: want a Board or a batch dict, got {type(board).__name__}"
        )
    return batch


class PcbProbeResolver:
    """The pcb :class:`~precis_diag.ProbeResolver`, bound to one board's
    batch. Pure reads throughout — that is the point of a probe."""

    domain = "pcb"

    def __init__(self, board: Any) -> None:
        self._batch = _batch_of(board)

    def can_resolve(self, selector: Any) -> bool:
        return isinstance(selector, str) and (
            (selector.startswith(_PADS_PREFIX) and selector.endswith(")"))
            or selector.startswith(_NETS_PREFIX)
        )

    def resolve(self, selector: Any) -> Any:
        if isinstance(selector, str) and selector.startswith(_PADS_PREFIX):
            if not selector.endswith(")"):
                raise ValueError(f"pcb probe: malformed selector {selector!r}")
            inner = selector[len(_PADS_PREFIX) : -1].strip()
            refdes = inner.strip("'\"")
            if not refdes:
                raise ValueError("pcb probe: pads_of() needs a refdes")
            return {"family": "pads", "items": self._pads_of(refdes)}
        if isinstance(selector, str) and selector.startswith(_NETS_PREFIX):
            return {
                "family": "nets",
                "items": self._nets(selector[len(_NETS_PREFIX) :]),
            }
        raise ValueError(f"pcb probe: cannot resolve {selector!r}")

    def digest(self, entities: Any) -> dict[str, Any]:
        family = str(entities.get("family") or "")
        items = entities.get("items") or []
        if family == "pads":
            ids = [pad_handle(refdes, pin) for refdes, pin, _net in items]
            return {"count": len(items), "kind": "pads", "ids": ids}
        ids = [net_handle(name) for name in items]
        return {"count": len(ids), "kind": "nets", "ids": ids}

    # -- the two selector families ----------------------------------------

    def _pads_of(self, refdes: str) -> list[tuple[str, str, str | None]]:
        """``(refdes, pin, net)`` per pad: declared pins first (the
        component row's authored order), then connection-minted pins not
        in the declaration, each sorted by pin name."""
        components = self._batch.get("components") or []
        row = next((c for c in components if str(c.get("refdes")) == refdes), None)
        if row is None:
            raise ValueError(
                f"pcb probe: unknown refdes {refdes!r} — the board has "
                f"{len(components)} part(s)"
            )
        declared = [str(p.get("name")) for p in row.get("pins") or ()]
        wired = [
            str(c.get("pin"))
            for c in self._batch.get("connections") or ()
            if str(c.get("refdes")) == refdes
        ]
        pins = list(dict.fromkeys(declared + sorted(set(wired) - set(declared))))
        net_of = {
            (str(c.get("refdes")), str(c.get("pin"))): str(c.get("net"))
            for c in self._batch.get("connections") or ()
        }
        return [(refdes, pin, net_of.get((refdes, pin))) for pin in pins]

    def _nets(self, pattern: str) -> list[str]:
        """The net names matching the glob — authored ``nets[]`` rows
        plus connection-minted nets, sorted."""
        names = {str(n.get("name")) for n in self._batch.get("nets") or ()}
        names.update(
            str(c.get("net"))
            for c in self._batch.get("connections") or ()
            if str(c.get("net") or "")
        )
        names.discard("")
        return sorted(n for n in names if fnmatch.fnmatchcase(n, pattern))

    def net_names(self) -> list[str]:
        """Every net name in the batch, sorted — the no-match hint's
        vocabulary."""
        return self._nets("*")


def resolver_for(board: Any) -> PcbProbeResolver:
    """The probe resolver bound to ``board`` (a Board or a batch dict)."""
    return PcbProbeResolver(board)


__all__ = ["PcbProbeResolver", "resolver_for"]
