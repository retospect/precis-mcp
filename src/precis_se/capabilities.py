"""Process capability rows — what a (process, material) can actually do
(se-kind.md "Manufacturing modes"; the pcb capability architecture
transferred).

**Seeded narrowly, widened in rung 1.** ``hole_diameter_compensation``/
``min_wall``/``min_boss_wall`` are the three fields
``se-off-the-shelf-fabrication.md`` rung 3c could not proceed without.
``se-print-implementer.md`` rung 1 widens every ``fdm`` row with the
process-figure set the printed-solid/orientation/process-DRC implementer
needs: ``layer_height``, ``line_width``, ``max_overhang``, ``max_bridge``,
``min_bed_contact``, ``min_feature``, ``min_hole``, ``strength_z_ratio``,
``max_build``. Field names stay bare (no ``_mm`` suffix) — a field's
optional ``unit`` key (``'mm'`` default | ``'deg'`` | ``'ratio'``) carries
what a suffix used to imply.

**Two tiers.** ``physical`` is the floor/ceiling the process cannot beat
whatever anyone declares; ``house`` is what we build to, at a margin
inside it. Consumers read the house figure through :func:`capability` or
— once a block enters the picture — :func:`resolve`, which layers a
per-block override and clamps to the physical figure.

An unknown mode, or a field with no recorded figure, returns ``None`` from
:func:`capability` — never a default, and never a neighbour's number. A
capability figure invented for a process nobody characterized is exactly
the number that would print.

**The resolver** (``se-kind.md`` L5's "One resolver" bullet, the
``pcb.rules.resolve_net_rules`` pattern transferred): :func:`resolve`
chains a block's own ``process_overrides`` (migration
``0011_se_process_overrides.sql``; written through
:func:`precis_se.ops.set_process_override`/``clear_process_override``)
over a load-derived slot (named, unimplemented — no producer exists yet)
over :func:`capability`'s house tier, and always clamps the result to the
physical figure for a ``min_*``/``max_*``-named field (the direction the
name encodes; a field named neither way carries ``physical`` as
information only, never a guessed clamp direction).

**Orientation weights** (Engine 2, ``precis/cad/printability.py``) are a
*family*-level judgment, not a per-material measured figure, so they live
outside the per-material row shape entirely — the top-level
``orientation`` block in the JSON file, read through
:func:`orientation_policy`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis_se.ops import SeBlock, SeTree

_PACKAGED_DATA = "precis.data"
_FILE = "se_capabilities.json"

#: Length units :func:`house_mm`/:func:`house_m` may legally convert.
_LENGTH_UNIT = "mm"


@dataclass(frozen=True)
class Capability:
    """One field of one capability row. ``house``/``physical`` are the
    field's own unit (:attr:`unit`, ``'mm'`` default | ``'deg'`` |
    ``'ratio'``) — both optional, since a field this row's source did not
    characterize records that honestly as ``None`` rather than a borrowed
    or invented figure. ``house_mm``/``house_m`` stay named for the reason
    they always were (this file's original and still most common unit)
    and now raise :class:`ValueError` on a non-mm field instead of
    silently handing back a degree or a ratio as if it were a length."""

    mode: str
    field: str
    unit: str
    house: float | None
    physical: float | None
    confidence: str
    source: str

    def _require_mm(self, accessor: str) -> None:
        if self.unit != _LENGTH_UNIT:
            raise ValueError(
                f"{self.mode}.{self.field} is a {self.unit!r}-unit field, "
                f"not millimetres — {accessor} only converts lengths"
            )

    @property
    def house_mm(self) -> float:
        self._require_mm("house_mm")
        if self.house is None:  # pragma: no cover — capability() never
            # returns a Capability with house=None; defensive only, for a
            # hand-built Capability (a test constructing one directly).
            raise ValueError(f"{self.mode}.{self.field} has no house figure")
        return self.house

    @property
    def house_m(self) -> float:
        return self.house_mm / 1000.0


@dataclass(frozen=True)
class Resolved:
    """What :func:`resolve` answers for one field of one block: the
    ``value`` in its ``unit``, which ``tier`` supplied it
    (``'override'`` | ``'house'`` — the load-derived slot names a v1
    no-op, so it never surfaces as a tier yet), whether the physical
    figure clamped it, the :class:`Capability` row it came from (``None``
    only when nothing in the chain had one, which also means ``clamped``
    can never be ``True``), and a one-line ``note`` for a rendered view."""

    value: float
    unit: str
    tier: str
    clamped: bool
    capability: Capability | None
    note: str


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    text = resources.files(_PACKAGED_DATA).joinpath(_FILE).read_text(encoding="utf-8")
    return dict(json.loads(text))


@lru_cache(maxsize=1)
def _rows() -> dict[str, dict[str, Any]]:
    return {str(r["mode"]): r for r in _data().get("rows") or []}


def capability(mode: str | None, field: str) -> Capability | None:
    """One field of one row, or ``None`` when the mode, the field, or the
    field's house figure is uncharacterized — which a caller reports by
    name rather than substituting a neighbour's figure (the file's own
    rule). A field present in the row but recorded with a null ``house``
    (an honestly uncharacterized facet, e.g. ``max_build`` in every
    generic row) reads exactly the same as a field absent from the row
    entirely: nothing to report."""
    if not mode:
        return None
    row = _rows().get(str(mode).strip())
    if row is None:
        return None
    raw = (row.get("fields") or {}).get(field)
    if raw is None:
        return None
    house = raw.get("house")
    if house is None:
        return None
    physical = raw.get("physical")
    return Capability(
        mode=str(row["mode"]),
        field=field,
        unit=str(raw.get("unit") or _LENGTH_UNIT),
        house=float(house),
        physical=None if physical is None else float(physical),
        confidence=str(raw.get("confidence") or "unknown"),
        source=str(raw.get("source") or ""),
    )


def known_fields(mode: str | None) -> frozenset[str]:
    """Every field key any row in ``mode``'s *family* defines — the
    accepted-fields roster :func:`~precis_se.ops._op_set_process_override`
    rejects an unknown field name against. Family-wide, not per-material:
    a field null in this exact material's own row (``max_bridge`` on
    ``fdm/petg``, say) is still a real field an override may fill in —
    se-kind.md's "the model overrides if it wants" posture applies to a
    gap in the data, not just a margin on a published figure. Empty for
    an unset mode or a family with no rows at all."""
    if not mode:
        return frozenset()
    family = str(mode).split("/", 1)[0].strip()
    if not family:
        return frozenset()
    fields: set[str] = set()
    for key, row in _rows().items():
        if key.split("/", 1)[0] == family:
            fields.update((row.get("fields") or {}).keys())
    return frozenset(fields)


def orientation_policy(mode: str | None) -> dict[str, Any] | None:
    """The family-level orientation-search weights (Engine 2,
    ``precis/cad/printability.py``) for ``mode``'s family — a plain dict
    (``{"weights": {...}, "sweep_deg": ..., "source": ..., "confidence":
    ...}``) copied out of the top-level ``orientation`` block, or ``None``
    for an unset mode or a family with no policy entry (today: everything
    but ``fdm``). A *family*-level judgment, not a per-material measured
    figure, so it lives outside :class:`Capability` entirely; ``orient()``
    takes the returned dict as a per-call argument and holds no defaults
    of its own."""
    if not mode:
        return None
    family = str(mode).split("/", 1)[0].strip()
    if not family:
        return None
    entry = (_data().get("orientation") or {}).get(family)
    return dict(entry) if isinstance(entry, dict) else None


def hole_compensation_m(mode: str | None) -> tuple[float, Capability | None]:
    """How much to **add** to a modelled hole diameter so the printed hole
    comes out at the size the design asked for.

    Returns ``(metres, the row it came from)``; ``(0.0, None)`` for a mode
    with no characterization — a zero that means "nobody has measured
    this", which the caller says out loud rather than passing off as a
    calibrated machine."""
    cap = capability(mode, "hole_diameter_compensation")
    return (0.0 if cap is None else cap.house_m), cap


def _load_derived(tree: SeTree, block: SeBlock, field: str) -> float | None:
    """The load-derived resolution slot se-kind.md's L5 "one resolver"
    bullet names: a producer that would read a block's declared loads
    (:attr:`~precis_se.ops.SeBlock.objectives`, or an incident connect's)
    and compute a field's figure from them — e.g. a required wall
    thickness implied by a declared force. No such producer exists in v1:
    this always returns ``None``, so :func:`resolve`'s chain has a named
    home to grow into later without a shape change, instead of the slot
    being silently absent."""
    return None


def resolve(tree: SeTree, block: SeBlock, field: str) -> Resolved | None:
    """Chain a block's own override over the (unimplemented) load-derived
    slot over :func:`capability`'s house tier, clamped to the physical
    figure — the resolver every rung-2+ consumer (the implementer,
    process DRC) reads instead of :func:`capability` directly, once a
    concrete block is in hand.

    ``None`` when nothing in the chain has an answer: no override, no
    load-derived producer, and no capability row for ``field`` on the
    block's mode — never an invented figure the way a fallback default
    would be.

    **Clamp direction is read off the field's name.** A ``max_*`` field's
    physical figure is a ceiling the process cannot exceed; a ``min_*``
    field's is a floor it cannot go under. A field named neither way
    (``layer_height``, ``line_width``, ``hole_diameter_compensation``,
    ``strength_z_ratio``) carries ``physical`` as information only —
    resolving it never guesses which direction an unprefixed name's floor
    points, so no clamp is applied even when a physical figure is on
    file.
    """
    mode = getattr(block, "mode", None)
    cap = capability(mode, field)
    overrides = getattr(block, "process_overrides", None) or {}
    if field in overrides and overrides[field] is not None:
        value = float(overrides[field])
        tier = "override"
        note = f"block override for {field}"
    else:
        derived = _load_derived(tree, block, field)
        if derived is not None:  # pragma: no cover — no producer exists yet
            value = derived
            tier = "house"  # a future producer would need its own tier name
            note = f"derived from {getattr(block, 'name', block)}'s declared loads"
        elif cap is not None and cap.house is not None:
            value = cap.house
            tier = "house"
            note = f"house tier ({cap.mode})"
        else:
            return None
    unit = cap.unit if cap is not None else _LENGTH_UNIT
    clamped = False
    if cap is not None and cap.physical is not None:
        if field.startswith("max_") and value > cap.physical:
            value = cap.physical
            clamped = True
            note = f"{note}; clamped to the physical ceiling {cap.physical:g} {unit}"
        elif field.startswith("min_") and value < cap.physical:
            value = cap.physical
            clamped = True
            note = f"{note}; clamped to the physical floor {cap.physical:g} {unit}"
    return Resolved(
        value=value,
        unit=unit,
        tier=tier,
        clamped=clamped,
        capability=cap,
        note=note,
    )
