"""precis_geom.pcb — typed Python builder for pcb workspace scripts
(docs/backlog/pcb-workspace-code.md §Q1; slice 0: "Builder + goldens, no
container, no DB").

A board script builds ``Board``/``Net``/``Part``/``Pin``/``Footprint``
objects purely in memory and calls :meth:`Board.to_batch` to get the
same dict shape ``store.PcbMixin.pcb_apply`` already accepts
(``components``/``nets``/``connections``/``features``/``footprints``/
``net_classes``/``roles``/``generators``/``measures``/``meta``/
``profile``) — see that method's docstring for the per-key field
contract. ``to_batch`` emits only what the script said: a key the script
never set is absent, not defaulted, so a converted fixture is byte-equal
to the dict it came from (``examples/boards/*/main.py`` are checked that
way by ``tests/workspaces/test_pcb_board.py``). This module never imports
``precis``/the store/the MCP; the trusted-side importer (§Import
boundary) and place/route as cached services (§Q2) are later slices.

Order is authored order everywhere (parts, nets, connections, features,
footprints) — the batch is a list-shaped document and the importer diffs
it against the previous run, so a stable order is part of the contract.

**Tracing.** Every construction primitive is decorated with
``precis_geom.trace.traced`` (structure-workspaces slice 2, on the same
base): ``with precis_geom.trace.run(): build()`` records one ``core``
frame per call (``pcb.Board.part`` …) with the user frames between them.

**Intent markers on pins** (``unused``/``nc``/``must_terminate``) emit the
store's bool shorthand (``{"name": "2", "unused": true}``,
``_pcb_insert_pins`` §1j), not a tag — the same bytes the batch put
authors wrote.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis_geom import pcb_shape as _shape
from precis_geom import trace as _trace
from precis_geom.pcb_shape import BoardShape, FitResult
from precis_geom.trace import traced

#: ``Net.domain`` vocabulary: the store routes electrical nets only
#: (``handlers/pcb.py::_reject_non_electrical``); ``None`` = the schema
#: default, ``'electrical'`` the only explicit value. Fluidic/thermal are
#: schema-reserved and refused here at build time, as the put refuses them.
_ELECTRICAL_DOMAINS = frozenset({None, "electrical"})

#: The pin intent markers the store reads as bool keys (§1j).
_PIN_MARKERS = ("unused", "nc", "must_terminate")

MANIFEST_SCHEMA = 1

#: The one not-checked cause ``check()`` can see offline (gr513667): the part
#: has neither a catalog part nor an authored footprint, so the store binds a
#: synthesized landpattern at import. Prefix of each ``not_checked_groups``
#: key (``"<reason>: <refdes letters> <value hint>"``).
_NOT_CHECKED_REASON = "no lcsc, no authored footprint (synthesized landpattern)"


class PcbBuilderError(ValueError):
    """A builder-time misuse: a reused refdes/prefix, a module touching a
    net it was not handed, a non-electrical net domain, an unknown
    footprint name."""


#: Layer counts of the named stackup presets, for index→name resolution
#: in ``plane()``/``keepout()``. Mirrors the authority,
#: ``precis.pcb.STACKUP_PRESETS`` — this module is stdlib-only and
#: cannot read it, so keep in sync (2l → 2, both 4l presets → 4).
_PRESET_LAYER_COUNTS = {"2l": 2, "4l-two-sided": 4, "4l-top-only": 4}

#: Copper-layer count when ``stackup=`` is omitted — the semantic
#: default is ``precis.pcb.DEFAULT_STACKUP`` (4 layers, all routable).
#: ``Board()`` emits no stackup key at all: the store/local kernel
#: defaults the same way and a hard-coded list would desync goldens.
_DEFAULT_LAYER_COUNT = 4


def _layer_name(index: int, n_layers: int) -> str:
    """Index → standard copper name — a byte-identical mirror of
    :func:`precis.pcb.ir.layer_name` (the authority), duplicated because
    this module stays stdlib-only. ``0`` → ``"F.Cu"``,
    ``n_layers - 1`` → ``"B.Cu"``, else ``"In<i>.Cu"`` on the normalised
    index; negative indexes count from the bottom."""
    if isinstance(index, bool) or not isinstance(index, int):
        raise PcbBuilderError(f"layer index {index!r} is not an int")
    i = index + n_layers if index < 0 else index
    if i < 0 or i >= n_layers:
        raise PcbBuilderError(
            f"layer index {index!r} out of range for a {n_layers}-layer board"
        )
    if i == 0:
        return "F.Cu"
    if i == n_layers - 1:
        return "B.Cu"
    return f"In{i}.Cu"


# --------------------------------------------------------------------------
# Pins, parts
# --------------------------------------------------------------------------


class Pin:
    """One named pin on a :class:`Part`. Created lazily by ``part[name]``;
    it appears in the batch only when declared (``Board.part(pins=)``) or
    annotated (a marker, a tag, a fact) — pins the connections merely
    reference stay implicit, as in an authored batch."""

    def __init__(self, *, part: Part, name: str) -> None:
        self.part = part
        self.name = name
        self.tags: list[str] = []
        self.meta: dict[str, Any] = {}
        self.markers: dict[str, bool] = {}
        #: Passthrough TOP-LEVEL pin keys the store reads — builder-less
        #: ones (``pad``/``description``/``note``) plus ``etype`` (the
        #: verb writes it here: the store tags only a top-level etype).
        self.extra: dict[str, Any] = {}

    def etype(self, kind: str) -> Pin:
        # The store's top-level key — an etype under `meta` rides meta
        # verbatim and earns no tag (store rule; gr514460 seam review).
        self.extra["etype"] = kind
        return self

    def current_role(self, kind: str) -> Pin:
        self.meta["current_role"] = kind
        return self

    def nc(self) -> Pin:
        self.markers["nc"] = True
        return self

    def unused(self) -> Pin:
        self.markers["unused"] = True
        return self

    def must_terminate(self) -> Pin:
        self.markers["must_terminate"] = True
        return self

    def tag(self, *tags: str) -> Pin:
        for t in tags:
            if t not in self.tags:
                self.tags.append(t)
        return self

    def client_of(self, role: Role) -> Pin:
        self.meta.setdefault("client_of", []).append(role.name)
        return self

    @property
    def handle(self) -> str:
        return f"{self.part.refdes}.{self.name}"

    @property
    def annotated(self) -> bool:
        return bool(self.tags or self.meta or self.markers or self.extra)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        for marker in _PIN_MARKERS:
            if self.markers.get(marker):
                d[marker] = True
        if self.tags:
            d["tags"] = list(self.tags)
        if self.meta:
            d["meta"] = dict(self.meta)
        d.update(copy.deepcopy(self.extra))
        return d


class Part:
    """One component instance, created by :meth:`Board.part` (refdes is
    already module-qualified by the time this object exists)."""

    def __init__(
        self,
        *,
        board: Board,
        refdes: str,
        lcsc: str | None = None,
        label: str | None = None,
        footprint: str | None = None,
        value: str | None = None,
        height_mm: float | None = None,
        roles: Iterable[str] = (),
        note: str | None = None,
    ) -> None:
        self.board = board
        self.refdes = refdes
        self.lcsc = lcsc
        self.label = label
        self.footprint = footprint
        self.value = value
        self.height_mm = height_mm
        self.roles: list[str] = list(dict.fromkeys(roles))
        self.note = note
        self._pins: dict[str, Pin] = {}
        self._pin_order: list[str] = []
        self._declared_pins: set[str] = set()
        self.x: float | None = None
        self.y: float | None = None
        self.rot: float = 0.0
        self.side: str = "top"
        self.fixed: str | None = None
        self.max_travel_mm: float | None = None
        self.edge_mate: dict[str, Any] | None = None
        self.group: str | None = None
        self.group_offset: dict[str, Any] | None = None
        self.pattern: str | None = None
        self.pattern_instance: str | None = None

    def __getitem__(self, key: str | int) -> Pin:
        name = str(key)
        if name not in self._pins:
            self._pins[name] = Pin(part=self, name=name)
            self._pin_order.append(name)
        return self._pins[name]

    def pins(self, *names: str | int) -> list[Pin]:
        return [self[n] for n in names]

    def declare(self, *names: str | int) -> Part:
        """Declare pins that must appear in the batch even if nothing
        annotates them (a part with no catalog part and no footprint has
        only these as its pin list)."""
        for n in names:
            self._declared_pins.add(self[n].name)
        return self

    def supplies(self, role: Role, pins: Iterable[str] = ()) -> Part:
        if role.name not in self.roles:
            self.roles.append(role.name)
        for name in pins:
            self[name].meta.setdefault("supplies", []).append(role.name)
        return self

    def at(
        self,
        x: float,
        y: float,
        rot: float = 0.0,
        side: str = "top",
        fixed: str = "both",
    ) -> Part:
        self.x, self.y, self.rot, self.side, self.fixed = x, y, rot, side, fixed
        return self

    def leash(self, max_travel_mm: float) -> Part:
        self.max_travel_mm = max_travel_mm
        return self

    def on_edge(
        self, face: str, at: float | None = None, require: str = "required"
    ) -> Part:
        mate: dict[str, Any] = {"face": face, "require": require}
        if at is not None:
            mate["at"] = at
        self.edge_mate = mate
        return self

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"refdes": self.refdes}
        if self.label is not None:
            d["label"] = self.label
        if self.lcsc is not None:
            d["part"] = self.lcsc
        if self.footprint is not None:
            d["footprint"] = self.footprint
        if self.value is not None:
            d["value"] = self.value
        if self.height_mm is not None:
            d["height_mm"] = self.height_mm
        if self.roles:
            d["roles"] = list(self.roles)
        d["pins"] = [
            self._pins[n].to_dict()
            for n in self._pin_order
            if n in self._declared_pins or self._pins[n].annotated
        ]
        if self.note is not None:
            d["note"] = self.note
        if self.x is not None:
            d["x"] = self.x
            d["y"] = self.y
            d["rot"] = self.rot
            d["layer"] = self.side
        if self.fixed is not None:
            d["fixed"] = self.fixed
        if self.max_travel_mm is not None:
            d["max_travel_mm"] = self.max_travel_mm
        if self.edge_mate is not None:
            d["edge_mate"] = dict(self.edge_mate)
        if self.group is not None:
            d["group"] = self.group
        if self.group_offset is not None:
            d["group_offset"] = dict(self.group_offset)
        if self.pattern is not None:
            d["pattern"] = self.pattern
        if self.pattern_instance is not None:
            d["pattern_instance"] = self.pattern_instance
        return d


# --------------------------------------------------------------------------
# Nets, net classes, roles
# --------------------------------------------------------------------------


class Net:
    """One electrical net, created by :meth:`Board.net`."""

    def __init__(
        self,
        *,
        board: Board,
        name: str,
        cls: str | None = None,
        current: float | None = None,
        voltage: float | None = None,
        impedance: float | None = None,
        edge_rate: float | None = None,
        function: str | None = None,
        note: str | None = None,
        domain: str | None = None,
    ) -> None:
        self.board = board
        self.name = name
        self.cls = cls
        self.current = current
        self.voltage = voltage
        self.impedance = impedance
        self.edge_rate = edge_rate
        self.function = function
        self.note = note
        self.domain = domain

    def tie(self, *pins: Pin, note: str | None = None) -> Net:
        for p in pins:
            self.board._connect(self, p, note)
        return self

    def pins(self) -> list[tuple[str, str]]:
        """``(refdes, pin)`` for every connection on this net —
        ``board.pins_on(self.name)``."""
        return self.board.pins_on(self.name)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.cls is not None:
            d["class"] = self.cls
        if self.current is not None:
            d["current"] = self.current
        if self.voltage is not None:
            d["working_voltage_v"] = self.voltage
        if self.impedance is not None:
            d["impedance"] = self.impedance
        if self.edge_rate is not None:
            d["edge_rate"] = self.edge_rate
        if self.function is not None:
            d["function"] = self.function
        if self.domain is not None:
            d["domain"] = self.domain
        if self.note is not None:
            d["note"] = self.note
        return d


class Role:
    """A supply role (pcb-lazy-netlist-and-checks §1): a part ``supplies``
    it, a pin ``client_of`` it."""

    def __init__(self, name: str, voltage: float | None = None) -> None:
        self.name = name
        self.voltage = voltage

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.voltage is not None:
            d["voltage"] = self.voltage
        return d


# --------------------------------------------------------------------------
# Footprints
# --------------------------------------------------------------------------


def pad(
    pin: str,
    shape: str,
    x: float,
    y: float,
    *,
    w: float | None = None,
    h: float | None = None,
    poly: list[list[float]] | None = None,
    drill: float | None = None,
    role: str | None = None,
    mask: Any = None,
    paste: Any = None,
) -> dict[str, Any]:
    """One footprint pad (``precis.pcb.rows.normalize_pad``
    shape: ``{pin, shape, x, y, w?, h?, poly?, drill?, role?, mask?,
    paste?}``)."""
    d: dict[str, Any] = {"pin": pin, "shape": shape, "x": x, "y": y}
    if w is not None:
        d["w"] = w
    if h is not None:
        d["h"] = h
    if poly is not None:
        d["poly"] = poly
    if drill is not None:
        d["drill"] = drill
    if role is not None:
        d["role"] = role
    if mask is not None:
        d["mask"] = mask
    if paste is not None:
        d["paste"] = paste
    return d


class Footprint:
    """An authored (design-local) footprint — ``pcb_apply``'s
    ``footprints[]`` entry. Register it with :meth:`Board.footprint` (or
    pass the instance as ``Board.part(..., footprint=fp)``, which
    registers on first use); parts then name it by ``name``."""

    def __init__(
        self,
        name: str,
        pads: list[dict[str, Any]],
        *,
        pin_map: dict[str, Any] | None = None,
        courtyard: Any = None,
        centroid: Any = None,
        note: str | None = None,
    ) -> None:
        self.name = name
        self.pads = pads
        self.pin_map = pin_map
        self.courtyard = courtyard
        self.centroid = centroid
        self.note = note

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "pads": copy.deepcopy(self.pads)}
        if self.pin_map is not None:
            d["pin_map"] = dict(self.pin_map)
        if self.courtyard is not None:
            d["courtyard"] = self.courtyard
        if self.centroid is not None:
            d["centroid"] = self.centroid
        if self.note is not None:
            d["note"] = self.note
        return d


# --------------------------------------------------------------------------
# Module frames (b.use)
# --------------------------------------------------------------------------


@dataclass
class _ModuleFrame:
    prefix: str
    chain: str
    allowed_net_names: set[str]
    qualname: str


def module[F: Callable[..., Any]](fn: F) -> F:
    """Marks a sub-circuit function for :meth:`Board.use` (pcb-meta-blocks
    level 1, as code). An identity marker today; the placed-module cache
    (slice 3) is what gives it behavior."""
    return fn


# --------------------------------------------------------------------------
# Check findings
# --------------------------------------------------------------------------


@dataclass
class CheckFinding:
    code: str
    message: str
    refdes: str | None = None
    net: str | None = None


@dataclass
class CheckResult:
    findings: list[CheckFinding]
    not_checked: list[str]
    #: ``not_checked`` grouped by ``"<reason>: <refdes letters> <value
    #: hint>"`` — the actionable form (gr513667): one key per cause+kind,
    #: largest group first, refdes in board order.
    not_checked_groups: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"{len(self.findings)} finding(s), "
            f"{len(self.not_checked)} NOT-CHECKED (no catalog part, no footprint)"
        ]
        for f_ in self.findings:
            where = f_.refdes or f_.net or ""
            lines.append(f"  {f_.code} {where}: {f_.message}")
        reasons: list[str] = []
        for key, refdes in self.not_checked_groups.items():
            reason, _, label = key.partition(": ")
            if reason not in reasons:
                reasons.append(reason)
            shown = ", ".join(refdes[:6])
            if len(refdes) > 6:
                shown += f", +{len(refdes) - 6} more"
            lines.append(f"  {len(refdes)} x {label}: {shown}")
        if reasons:
            lines.append(
                f"  cause: {'; '.join(reasons)}; "
                "fix: pcb.catalog / b.footprint(...) / lcsc="
            )
        elif self.not_checked:
            # a CheckResult built without groups keeps the flat list
            lines.append("  NOT-CHECKED: " + ", ".join(self.not_checked))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Catalog stub, outline helper
# --------------------------------------------------------------------------


@dataclass
class CatalogPart:
    """``pcb.catalog(lcsc)`` — the catalog footprint a board script reads.

    Resolution goes through the sandbox's read MCP: under
    ``precis_access: read`` the runtime stages a stdlib
    ``/work/precis_client.py`` whose ``call("get", kind="part", id=lcsc,
    view="footprint")`` returns the ``part_footprints`` row as JSON. With
    no client (a plain laptop run) the part stays ``resolved=False`` —
    legitimate (Decision 25); the manifest then carries no row for it. A
    client that errors or returns an unreadable row is a different story:
    the part stays unresolved, ``error`` says why, and the manifest
    records it ``{provenance: 'unresolved', error}``."""

    lcsc: str
    resolved: bool = False
    pins: list[str] = field(default_factory=list)
    package: str | None = None
    pads: list[dict[str, Any]] = field(default_factory=list)
    pin_map: dict[str, Any] = field(default_factory=dict)
    courtyard: Any = None
    centroid: Any = None
    holes: list[dict[str, Any]] = field(default_factory=list)
    #: ``pn<ref>`` when the part already has a ref, else None.
    part_id: str | None = None
    #: sha256 of the canonical-JSON footprint row actually read.
    footprint_sha: str | None = None
    #: UTC isoformat timestamp of the read.
    read_at: str | None = None
    #: Why the read failed when a read path existed (an unreachable or
    #: misbehaving ``precis_client``). ``None`` when the part resolved or
    #: there was no read path at all (Decision 25 — no client is not an
    #: error).
    error: str | None = None


#: Pending :func:`catalog` reads made before ANY board existed, keyed by
#: lcsc — claimed once by the next manifest written (a pre-board read has
#: no owner to bind to). Once a board exists, module-level reads bind at
#: read time to ``_LAST_BOARD`` instead of racing manifests.
_CATALOG_READS: dict[str, dict[str, Any]] = {}

#: The board a module-level :func:`catalog` read binds to — the most
#: recently constructed one, i.e. the board a script is presumably
#: building when it reads. ``None`` before the first :class:`Board`;
#: :meth:`Board.catalog` always binds to its own board regardless.
_LAST_BOARD: Board | None = None


def _manifest_entry(part: CatalogPart) -> dict[str, Any]:
    """One manifest ``parts`` row — the resolved read's ids+sha+when, or
    the failure's provenance+error (R-e: the manifest records what was
    ACTUALLY read, failures included)."""
    if part.resolved:
        return {
            "lcsc": part.lcsc,
            "part_id": part.part_id,
            "footprint_sha": part.footprint_sha,
            "read_at": part.read_at,
        }
    return {"lcsc": part.lcsc, "provenance": "unresolved", "error": part.error}


def _call_json(raw: Any) -> dict[str, Any]:
    """The ``client.call`` payload as a dict — plain JSON, a JSON head
    followed by a ``Next:`` footer (the handler Response's hint text), or
    a one-key envelope (``{"result"|"body"|"text"|"content": …}``)
    wrapping either form. Anything else raises — the caller records the
    error verbatim."""
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode("utf-8")
    payload = raw
    if not isinstance(payload, dict):
        text = str(payload)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            try:
                payload, _ = json.JSONDecoder().raw_decode(text.lstrip())
            except json.JSONDecodeError as exc:
                if "more(cursor=" in text:
                    raise ValueError("paged response, more() not followed") from exc
                raise ValueError(
                    f"catalog read: unparseable response {text[:200]!r}"
                ) from exc
    for key in ("result", "body", "text", "content"):
        if isinstance(payload, dict) and set(payload) == {key}:
            return _call_json(payload[key])
    if not isinstance(payload, dict):
        raise ValueError(f"catalog read: unparseable response {str(raw)[:200]!r}")
    return payload


def _catalog_read(lcsc: str) -> CatalogPart:
    """One ``get(kind='part', view='footprint')`` read through the staged
    client — the CatalogPart populated from the row, or ``error`` set on
    any read failure. Records nothing; the caller binds the manifest
    entry."""
    part = CatalogPart(lcsc=lcsc)
    try:
        client = importlib.import_module("precis_client")
    except ImportError:
        return part
    try:
        row = _call_json(client.call("get", kind="part", id=lcsc, view="footprint"))
    except Exception as exc:
        part.error = str(exc)
        return part
    if "pads" not in row:
        part.error = str(
            row.get("error")
            or row.get("detail")
            or "get(view='footprint') returned no footprint row"
        )
        return part
    part.resolved = True
    part.pads = list(row.get("pads") or [])
    part.pin_map = dict(row.get("pin_map") or {})
    part.courtyard = row.get("courtyard")
    part.centroid = row.get("centroid")
    part.holes = list(row.get("holes") or [])
    part.package = row.get("package")
    part.part_id = row.get("part_id")
    part.footprint_sha = row.get("sha")
    part.read_at = datetime.now(UTC).isoformat()
    part.pins = [
        str(v.get("name") or k) if isinstance(v, dict) else str(v or k)
        for k, v in sorted(part.pin_map.items(), key=lambda kv: str(kv[0]))
    ] or [str(p.get("number")) for p in part.pads]
    return part


def catalog(lcsc: str) -> CatalogPart:
    """Read a catalog footprint and bind its manifest entry to the board
    under construction (``_LAST_BOARD``), or to the pending pool when no
    board exists yet — claimed once by the next manifest written."""
    part = _catalog_read(lcsc)
    if part.resolved or part.error is not None:
        board = _LAST_BOARD
        if board is not None:
            board._catalog_reads[part.lcsc] = _manifest_entry(part)
        else:
            _CATALOG_READS[part.lcsc] = _manifest_entry(part)
    return part


def rect(w: float, h: float, *, corner_mm: float | None = None) -> dict[str, Any]:
    """A closed rectangular outline geometry for :meth:`Board.outline`
    (origin corner, ``w`` along x, ``h`` along y)."""
    geom: dict[str, Any] = {"path": [[0, 0], [w, 0], [w, h], [0, h], [0, 0]]}
    if corner_mm is not None:
        geom["corner_radius_mm"] = corner_mm
    return geom


# --------------------------------------------------------------------------
# Board
# --------------------------------------------------------------------------


class Board:
    """A pcb design built in memory; ``to_batch()`` is the
    ``pcb_apply``-shaped payload. ``meta`` is the batch's free-form
    ``meta`` dict (the script writes into it directly).

    ``stackup=`` is a copper-layer COUNT (``stackup=4``) or a preset
    name (``'2l'``, ``'4l-two-sided'``, ``'4l-top-only'``). Omitted means
    the semantic default, 4 layers — ``precis.pcb.DEFAULT_STACKUP``:
    F.Cu/In1.Cu/In2.Cu/B.Cu, all routable — and emits no stackup key.
    A list of names is refused: say ``stackup=4`` or a preset; the dict
    form belongs to ``op='stackup'`` (R-d, gr514459). Copper layers are
    addressed by INDEX everywhere in code — ``plane(1, …)``,
    ``keepout(layers=[0])``; the standard names are what export emits."""

    def __init__(
        self,
        slug: str,
        *,
        profile: str | None = None,
        stackup: str | int | None = None,
        sides: str = "both",
        outline: dict[str, Any] | list[tuple[float, float]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.slug = slug
        self.profile = profile
        self.stackup = stackup
        self.sides = sides
        self.meta: dict[str, Any] = dict(meta or {})
        self._parts: dict[str, Part] = {}
        self._part_order: list[str] = []
        self._nets: dict[str, Net] = {}
        self._net_order: list[str] = []
        self._connections: list[dict[str, Any]] = []
        self._features: list[dict[str, Any]] = []
        self._footprints: dict[str, Footprint] = {}
        self._footprint_order: list[str] = []
        self._net_classes: dict[str, dict[str, Any]] = {}
        self._roles: dict[str, Role] = {}
        self._generators: list[dict[str, Any]] = []
        self._measures: list[dict[str, Any]] = []
        self._catalog_reads: dict[str, dict[str, Any]] = {}
        self._used_prefixes: set[str] = set()
        self._frame_stack: list[_ModuleFrame] = []
        global _LAST_BOARD
        _LAST_BOARD = self

        if isinstance(stackup, list):
            raise PcbBuilderError(
                f"stackup= takes a layer count or a preset name, not a list "
                f"({stackup!r}) — use stackup={len(stackup)}"
            )
        if isinstance(stackup, bool) or (
            stackup is not None and not isinstance(stackup, int | str)
        ):
            raise PcbBuilderError(
                f"stackup {stackup!r} is not a layer count or preset name"
            )
        if isinstance(stackup, int) and stackup < 2:
            raise PcbBuilderError(
                f"stackup {stackup} — a board has at least the two outer "
                f"copper layers (F.Cu/B.Cu)"
            )
        if isinstance(stackup, int):
            self.meta["stackup_layers"] = [
                _layer_name(i, stackup) for i in range(stackup)
            ]
        elif stackup is not None:
            self.meta["stackup_preset"] = stackup
        if sides != "both":
            self.meta["placement_sides"] = sides
        if outline is not None:
            self.outline(outline)

    def catalog(self, lcsc: str) -> CatalogPart:
        """:func:`catalog` bound to THIS board — the read's manifest
        ``parts`` row lands here and only here; a pending module-level
        read of the same part number is NOT consumed (it belongs to the
        board that owns it, or the next manifest if none does)."""
        part = _catalog_read(lcsc)
        if part.resolved or part.error is not None:
            self._catalog_reads[part.lcsc] = _manifest_entry(part)
        return part

    # -- naming (module frames) ------------------------------------------

    @property
    def _frame(self) -> _ModuleFrame | None:
        return self._frame_stack[-1] if self._frame_stack else None

    def _qualify(self, name: str, sep: str) -> str:
        frame = self._frame
        return f"{frame.chain}{sep}{name}" if frame else name

    # -- construction primitives -------------------------------------------

    @traced
    def footprint(self, fp: Footprint) -> Footprint:
        """Register an authored footprint (authored order is batch order).
        Re-registering the same instance is a no-op; a different instance
        under a used name is refused."""
        prev = self._footprints.get(fp.name)
        if prev is None:
            self._footprints[fp.name] = fp
            self._footprint_order.append(fp.name)
        elif prev is not fp:
            raise PcbBuilderError(
                f"footprint {fp.name!r} already registered on board {self.slug!r}"
            )
        return fp

    @traced
    def part(
        self,
        refdes: str,
        *,
        lcsc: str | None = None,
        label: str | None = None,
        footprint: str | Footprint | None = None,
        value: str | None = None,
        height_mm: float | None = None,
        roles: Iterable[str] = (),
        pins: Iterable[Any] = (),
        note: str | None = None,
    ) -> Part:
        qualified = self._qualify(refdes, ".")
        if qualified in self._parts:
            raise PcbBuilderError(
                f"refdes {qualified!r} already used on board {self.slug!r}"
            )
        fp_name = (
            self.footprint(footprint).name
            if isinstance(footprint, Footprint)
            else footprint
        )
        p = Part(
            board=self,
            refdes=qualified,
            lcsc=lcsc,
            label=label,
            footprint=fp_name,
            value=value,
            height_mm=height_mm,
            roles=roles,
            note=note,
        )
        for entry in pins:
            if isinstance(entry, dict):
                pin = p[entry["name"]]
                for marker in _PIN_MARKERS:
                    if entry.get(marker):
                        pin.markers[marker] = True
                pin.tag(*entry.get("tags", ()))
                pin.meta.update(entry.get("meta") or {})
                pin.extra.update(
                    {
                        k: v
                        for k, v in entry.items()
                        if k not in ("name", "tags", "meta", *_PIN_MARKERS)
                    }
                )
            else:
                pin = p[entry]
            p._declared_pins.add(pin.name)
        self._parts[qualified] = p
        self._part_order.append(qualified)
        return p

    @traced
    def net(
        self,
        name: str,
        *,
        cls: str | None = None,
        current: float | None = None,
        voltage: float | None = None,
        impedance: float | None = None,
        edge_rate: float | None = None,
        function: str | None = None,
        note: str | None = None,
        domain: str | None = None,
    ) -> Net:
        if domain not in _ELECTRICAL_DOMAINS:
            raise PcbBuilderError(
                f"net {name!r}: domain must be electrical (omit it or pass "
                f"'electrical'), got {domain!r}"
            )
        qualified = self._qualify(name, "/")
        if qualified in self._nets:
            raise PcbBuilderError(
                f"net {qualified!r} already declared on board {self.slug!r}"
            )
        n = Net(
            board=self,
            name=qualified,
            cls=cls,
            current=current,
            voltage=voltage,
            impedance=impedance,
            edge_rate=edge_rate,
            function=function,
            note=note,
            domain=domain,
        )
        self._nets[qualified] = n
        self._net_order.append(qualified)
        frame = self._frame
        if frame is not None:
            frame.allowed_net_names.add(qualified)
        return n

    def _connect(self, net: Net, pin: Pin, note: str | None = None) -> None:
        frame = self._frame
        if frame is not None and net.name not in frame.allowed_net_names:
            raise PcbBuilderError(
                f"module {frame.qualname!r} (prefix {frame.prefix!r}) touched "
                f"net {net.name!r} it was not handed as a port"
            )
        entry: dict[str, Any] = {
            "net": net.name,
            "refdes": pin.part.refdes,
            "pin": pin.name,
        }
        if note:
            entry["note"] = note
        self._connections.append(entry)

    @traced
    def wire(self, pin_a: Pin, pin_b: Pin, name: str | None = None) -> Net:
        """Two-pin net. Without ``name`` the net is minted
        ``<refdes>_<pin>__<refdes>_<pin>`` (the free-ended-net name rule,
        §1d) and recorded like any other."""
        if name is None:
            name = f"{pin_a.part.refdes}_{pin_a.name}__{pin_b.part.refdes}_{pin_b.name}"
            name = name.replace(".", "_").replace("/", "_")
        net = self._nets.get(self._qualify(name, "/")) or self.net(name)
        net.tie(pin_a, pin_b)
        return net

    @traced
    def net_class(self, name: str, **rules: Any) -> dict[str, Any]:
        self._net_classes[name] = rules
        return rules

    @traced
    def role(self, name: str, voltage: float | None = None) -> Role:
        r = Role(name=name, voltage=voltage)
        self._roles[name] = r
        return r

    # -- copper layers by index (R-d, gr514459) ----------------------------

    def _layer_count(self) -> int:
        """Copper layers for index→name resolution: an int ``stackup``
        is the count, a preset reads ``_PRESET_LAYER_COUNTS`` (mirroring
        ``precis.pcb.STACKUP_PRESETS``), no stackup means the semantic
        default — ``_DEFAULT_LAYER_COUNT``."""
        if isinstance(self.stackup, int):
            return self.stackup
        if isinstance(self.stackup, str):
            n = _PRESET_LAYER_COUNTS.get(self.stackup)
            if n is None:
                raise PcbBuilderError(
                    f"unknown stackup preset {self.stackup!r} — known: "
                    f"{sorted(_PRESET_LAYER_COUNTS)}"
                )
            return n
        return _DEFAULT_LAYER_COUNT

    @property
    def layer_names(self) -> list[str]:
        """The resolved stackup's copper names, top→bottom."""
        n = self._layer_count()
        return [_layer_name(i, n) for i in range(n)]

    def _layer_at(self, layer: Any, what: str) -> str:
        """The standard name for a copper layer given by INDEX — a
        string is refused with its index equivalent named."""
        n = self._layer_count()
        if isinstance(layer, str):
            names = [_layer_name(i, n) for i in range(n)]
            hint = (
                f" — {layer!r} is index {names.index(layer)}" if layer in names else ""
            )
            raise PcbBuilderError(
                f"{what}() takes a layer index (0 = top … {n - 1} = bottom), "
                f"not a name{hint}"
            )
        return _layer_name(layer, n)

    @traced
    def plane(self, layer: int, *, net: Net, priority: int = 0) -> dict[str, Any]:
        """A whole-layer pour for ``net`` on copper layer INDEX ``layer``
        (``0`` = top … n-1 = bottom; ``-1`` counts from the bottom)."""
        entry = {
            "ftype": "plane_net",
            "layer": self._layer_at(layer, "plane"),
            "net": net.name,
            "priority": priority,
        }
        self._features.append(entry)
        return entry

    @traced
    def outline(
        self,
        geom: dict[str, Any] | list[tuple[float, float]],
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """The board outline (one per board): a :func:`rect` geometry or a
        point list (closed by the caller if it should be)."""
        if any(f_["ftype"] == "outline" for f_ in self._features):
            raise PcbBuilderError(f"board {self.slug!r} already has an outline")
        if not isinstance(geom, dict):
            geom = {"path": [[x, y] for x, y in geom]}
        f_: dict[str, Any] = {"ftype": "outline", "geom": geom}
        if note is not None:
            f_["note"] = note
        self._features.append(f_)
        return f_

    @traced
    def hole(
        self,
        x: float,
        y: float,
        *,
        drill: float,
        ring: float,
        plated: bool = True,
        note: str | None = None,
    ) -> dict[str, Any]:
        f_: dict[str, Any] = {
            "ftype": "mounting_hole",
            "x": x,
            "y": y,
            "geom": {"diameter": drill, "ring_dia_mm": ring, "plated": plated},
        }
        if note is not None:
            f_["note"] = note
        self._features.append(f_)
        return f_

    @traced
    def fiducial(self, x: float, y: float) -> dict[str, Any]:
        f_ = {"ftype": "fiducial", "x": x, "y": y}
        self._features.append(f_)
        return f_

    @traced
    def testpoint(
        self, net: Net, x: float | None = None, y: float | None = None
    ) -> dict[str, Any]:
        f_: dict[str, Any] = {"ftype": "testpoint", "net": net.name}
        if x is not None:
            f_["x"] = x
        if y is not None:
            f_["y"] = y
        self._features.append(f_)
        return f_

    @traced
    def corner_nuts(self) -> dict[str, Any]:
        f_ = {"ftype": "corner_nuts"}
        self._features.append(f_)
        return f_

    @traced
    def keepout(
        self,
        poly: list[list[float]],
        layers: Iterable[int] = (),
        reason: str | None = None,
    ) -> dict[str, Any]:
        f_: dict[str, Any] = {"ftype": "keepout", "geom": {"poly": poly}}
        if layers:
            f_["layers"] = [self._layer_at(layer, "keepout") for layer in layers]
        if reason is not None:
            f_["reason"] = reason
        self._features.append(f_)
        return f_

    @traced
    def feature(self, ftype: str, **fields: Any) -> dict[str, Any]:
        """An arbitrary ``features[]`` row (``_pcb_insert_feature`` shape)
        for feature types without a dedicated primitive."""
        f_: dict[str, Any] = {"ftype": ftype, **fields}
        self._features.append(f_)
        return f_

    def near(self, a: Part, b: Part, within_mm: float) -> dict[str, Any]:
        return self.measure("near", [a, b], bound=within_mm)

    def apart(self, a: Part, b: Part, at_least_mm: float) -> dict[str, Any]:
        return self.measure("apart", [a, b], bound=at_least_mm)

    def align(self, a: Part, b: Part, axis: str) -> dict[str, Any]:
        m = self.measure("align", [a, b])
        m["axis"] = axis
        return m

    @traced
    def measure(
        self,
        metric: str,
        operands: Iterable[Any],
        *,
        bound: float | None = None,
        goal: float | None = None,
        hard: bool = False,
        weight: float = 1.0,
    ) -> dict[str, Any]:
        m: dict[str, Any] = {
            "metric": metric,
            "operands": [getattr(o, "refdes", o) for o in operands],
            "hard": hard,
            "weight": weight,
        }
        if bound is not None:
            m["bound"] = bound
        if goal is not None:
            m["goal"] = goal
        self._measures.append(m)
        return m

    @traced
    def generate(self, name: str, *, generator: str, **params: Any) -> dict[str, Any]:
        g = {"name": name, "generator": generator, "params": params}
        self._generators.append(g)
        return g

    @traced
    def group(
        self,
        name: str,
        parts: Iterable[Part],
        *,
        rigid: bool = True,
        pattern: str | None = None,
        offsets: Iterable[tuple[float, float, float]] | None = None,
    ) -> list[Part]:
        """A rigid group (instance meta ``group``/``group_offset``);
        ``offsets`` are per-part ``(x, y, rot)`` in the group frame."""
        parts = list(parts)
        offs = list(offsets) if offsets is not None else None
        if offs is not None and len(offs) != len(parts):
            raise PcbBuilderError(
                f"group {name!r}: {len(offs)} offsets for {len(parts)} parts"
            )
        for i, p in enumerate(parts):
            p.group = name
            if offs is not None:
                x, y, rot = offs[i]
                p.group_offset = {"x": x, "y": y, "rot": rot}
            if pattern is not None:
                p.pattern = pattern
        return parts

    @traced
    def use(self, module_fn: Callable[..., Any], *, prefix: str, **ports: Any) -> Any:
        """Instantiate a module: its parts become ``<prefix>.<local>``, its
        own nets ``<prefix>/<name>``; the ``Net`` arguments are the only
        outside nets it may touch."""
        if prefix in self._used_prefixes:
            raise PcbBuilderError(
                f"prefix {prefix!r} already used on board {self.slug!r}"
            )
        self._used_prefixes.add(prefix)
        allowed = {v.name for v in ports.values() if isinstance(v, Net)}
        chain = ".".join([f.prefix for f in self._frame_stack] + [prefix])
        frame = _ModuleFrame(
            prefix=prefix,
            chain=chain,
            allowed_net_names=allowed,
            qualname=getattr(
                module_fn,
                "__qualname__",
                getattr(module_fn, "__name__", str(module_fn)),
            ),
        )
        self._frame_stack.append(frame)
        try:
            return module_fn(self, **ports)
        finally:
            self._frame_stack.pop()

    # -- inspection ---------------------------------------------------------

    def nets_of(self, refdes: str) -> list[str]:
        """The sorted, de-duplicated net names of the connections naming
        ``refdes`` — "which nets does U1 touch" (gr513764: the name reads
        as the net list, so that is what it returns). For the per-pin
        ``{pin name: net name}`` map see :meth:`pin_nets`."""
        if refdes not in self._parts:
            raise PcbBuilderError(
                f"nets_of: unknown refdes {refdes!r} — board {self.slug!r} "
                f"has {len(self._part_order)} parts"
            )
        return sorted({c["net"] for c in self._connections if c["refdes"] == refdes})

    def pin_nets(self, refdes: str) -> dict[str, str]:
        """``{pin name: net name}`` — the pin → net map for every
        connection naming ``refdes`` (module-qualified), in the part's
        pin order. (Not ``pin_map``: that name is the footprint
        pad→pin-name map in the ``pcb_apply`` payload.)"""
        if refdes not in self._parts:
            raise PcbBuilderError(
                f"pin_nets: unknown refdes {refdes!r} — board {self.slug!r} "
                f"has {len(self._part_order)} parts"
            )
        order = {n: i for i, n in enumerate(self._parts[refdes]._pin_order)}
        pairs = [
            (c["pin"], c["net"]) for c in self._connections if c["refdes"] == refdes
        ]
        pairs.sort(key=lambda kv: order.get(kv[0], len(order)))
        return dict(pairs)

    def pins_on(self, net: str | Net) -> list[tuple[str, str]]:
        """``(refdes, pin)`` for every connection on ``net``, in authored
        connection order. Accepts the net's name or the :class:`Net`.
        The part-side directions are :meth:`nets_of` (a part's net names)
        and :meth:`pin_nets` (its pin → net map)."""
        name = net.name if isinstance(net, Net) else str(net)
        if name not in self._nets:
            raise PcbBuilderError(
                f"pins_on: unknown net {name!r} — board {self.slug!r} "
                f"has {len(self._net_order)} nets"
            )
        return [(c["refdes"], c["pin"]) for c in self._connections if c["net"] == name]

    def check(self) -> CheckResult:
        """Pure, offline L0 checks (§Q1): parts with no catalog part and no
        authored footprint are NOT-CHECKED (a synthesized landpattern
        bound); a declared net with zero connections, a connection to a
        part the board never made, and a part naming an unregistered
        footprint are findings. No geometry, no DRC — that stays
        server-side (§DRC)."""
        not_checked = [
            r
            for r in self._part_order
            if self._parts[r].lcsc is None and self._parts[r].footprint is None
        ]
        groups: dict[str, list[str]] = {}
        for r in not_checked:
            p = self._parts[r]
            m = re.match(r"[A-Za-z]+", r.rsplit(".", 1)[-1])
            label = m.group(0) if m else r
            if p.value is not None:
                label += f" {p.value}"
            groups.setdefault(f"{_NOT_CHECKED_REASON}: {label}", []).append(r)
        groups = dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])))
        findings: list[CheckFinding] = []
        for r in self._part_order:
            fp = self._parts[r].footprint
            if fp is not None and fp not in self._footprints:
                findings.append(
                    CheckFinding(
                        code="footprint.unknown",
                        refdes=r,
                        message=f"footprint {fp!r} is not registered on this board",
                    )
                )
        connected = {c["net"] for c in self._connections}
        findings += [
            CheckFinding(
                code="net.unconnected", net=n, message=f"net {n!r} has no connections"
            )
            for n in self._net_order
            if n not in connected
        ]
        findings += [
            CheckFinding(
                code="net.undeclared",
                net=c["net"],
                message=f"connection {c['refdes']}.{c['pin']} names an undeclared net",
            )
            for c in self._connections
            if c["net"] not in self._nets
        ]
        return CheckResult(
            findings=findings,
            not_checked=not_checked,
            not_checked_groups=groups,
        )

    # -- the 3-D shape (slice 0b) -------------------------------------------

    def _local_extent_mm(self, part: Part) -> tuple[float, float, float, float] | None:
        """The part's footprint-local ``[x0, y0, x1, y1]`` from its
        registered :class:`Footprint` — the authored ``courtyard.bbox``
        first, else the pad bbox (the store's own normalization order,
        ``_pcb_ops``). ``None`` for a catalog/named footprint the script
        never authored — an honest point column, not a guess."""
        fp = self._footprints.get(str(part.footprint or ""))
        if fp is None:
            return None
        courtyard = fp.courtyard
        if isinstance(courtyard, dict):
            bbox = courtyard.get("bbox")
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                return (
                    float(bbox[0]),
                    float(bbox[1]),
                    float(bbox[2]),
                    float(bbox[3]),
                )
        xs: list[float] = []
        ys: list[float] = []
        for p_ in fp.pads:
            if p_.get("poly"):
                xs += [float(v[0]) for v in p_["poly"]]
                ys += [float(v[1]) for v in p_["poly"]]
            else:
                w = float(p_.get("w") or 0.0)
                h = float(p_.get("h") or 0.0)
                xs += [float(p_["x"]) - w / 2.0, float(p_["x"]) + w / 2.0]
                ys += [float(p_["y"]) - h / 2.0, float(p_["y"]) + h / 2.0]
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def shape(self, *, thickness_mm: float | None = None) -> BoardShape:
        """The board's 3-D mechanical shape (slice 0b) as data: the
        outline polygon, one :class:`~precis_geom.pcb_shape.Prism` per
        placed part (resolved ``height_mm`` + the ``origin`` honesty
        flag), mounting-hole centres/diameters, keepouts — mm, board
        frame, z = 0 at the bottom copper face. The derivation is
        :func:`precis_geom.pcb_shape.derive_shape` over the same
        :func:`~precis_geom.pcb_shape.mechanical_profile` the trusted
        side serves — there is no second derivation. ``shape.fits(…)``
        is the callable half. Parts with no pose get no prism and are
        named in ``shape.unplaced``; ``thickness_mm`` overrides the
        default 1.6 until board thickness is real data (pass A)."""
        parts = [self._parts[r] for r in self._part_order]
        instances = [
            {
                "refdes": p.refdes,
                "x": p.x,
                "y": p.y,
                "layer": p.side,
                "height_mm": p.height_mm,
            }
            for p in parts
        ]
        profile = _shape.mechanical_profile(
            instances,
            self._features,
            thickness_mm=(
                thickness_mm
                if thickness_mm is not None
                else _shape.DEFAULT_THICKNESS_MM
            ),
        )
        details = {
            p.refdes: {
                "footprint": p.footprint,
                "extent_mm": self._local_extent_mm(p),
                "rot_deg": p.rot,
            }
            for p in parts
        }
        unplaced = [p.refdes for p in parts if p.x is None or p.y is None]
        return _shape.derive_shape(profile, details=details, unplaced=unplaced)

    def to_batch(self) -> dict[str, Any]:
        """The ``pcb_apply``-shaped batch dict (see that method's
        docstring for the per-key field contract). Optional sections are
        present only when the script authored them."""
        out: dict[str, Any] = {
            "components": [self._parts[r].to_dict() for r in self._part_order],
            "nets": [self._nets[n].to_dict() for n in self._net_order],
            "connections": [dict(c) for c in self._connections],
        }
        if self._features:
            out["features"] = [copy.deepcopy(f_) for f_ in self._features]
        if self._footprints:
            out["footprints"] = [
                self._footprints[n].to_dict() for n in self._footprint_order
            ]
        if self._net_classes:
            out["net_classes"] = {k: dict(v) for k, v in self._net_classes.items()}
        if self._roles:
            out["roles"] = {k: r.to_dict() for k, r in self._roles.items()}
        if self._generators:
            out["generators"] = [dict(g) for g in self._generators]
        if self._measures:
            out["measures"] = [dict(m) for m in self._measures]
        if self.meta:
            out["meta"] = dict(self.meta)
        if self.profile is not None:
            out["profile"] = self.profile
        return out

    def manifest(self, requests: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
        """The workspace run's ``out/manifest.json`` document (§Import
        boundary): ``{schema, kind: 'pcb', board, requests}`` plus the
        run's provenance — ``parts`` records every catalog footprint
        :func:`catalog` resolved (R-e: the ids read + a sha + when — and
        ``{provenance: 'unresolved', error}`` when the read failed) and
        ``precis_geom`` the wheel's own version."""
        # Unattached reads bind to this board now — claimed once, so they
        # cannot leak into a second board's manifest.
        for lcsc in sorted(_CATALOG_READS):
            self._catalog_reads.setdefault(lcsc, _CATALOG_READS.pop(lcsc))
        return {
            "schema": MANIFEST_SCHEMA,
            "kind": "pcb",
            "board": self.to_batch(),
            "requests": [dict(r) for r in requests],
            "parts": [self._catalog_reads[k] for k in sorted(self._catalog_reads)],
            "precis_geom": {"version": _wheel_version()},
        }


def fit(
    shape: BoardShape,
    other: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    clearance_mm: float = 0.0,
) -> FitResult:
    """``b.shape().fits(other)`` spelled as a module call — the multi-solid
    fit check (slice 0b); see :func:`precis_geom.pcb_shape.fit`."""
    return _shape.fit(shape, other, clearance_mm=clearance_mm)


#: Counterparty box-spec constructors for ``fit``/``BoardShape.fits`` —
#: ``box(...)`` is an se part to keep clear of, ``enclosure(...)`` is the
#: interior cavity the whole board must fit inside.
box = _shape.box
enclosure = _shape.enclosure


def _wheel_version() -> str:
    """The installed ``precis-geom`` version for the manifest; ``0+unknown``
    when running from a source tree (no dist metadata)."""
    try:
        return importlib.metadata.version("precis-geom")
    except Exception:
        return "0+unknown"


def canonical_json(batch: dict[str, Any]) -> str:
    """The byte form the goldens hash and the request key is built from."""
    return json.dumps(batch, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(batch: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(batch).encode("utf-8")).hexdigest()


def write_manifest(
    board: Board,
    out_dir: str | Path,
    *,
    requests: Iterable[dict[str, Any]] = (),
) -> Path:
    """Write ``manifest.json`` (and ``trace.json`` when a tracer is live)
    under ``out_dir``; returns the manifest path."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "manifest.json"
    path.write_text(
        json.dumps(board.manifest(requests), sort_keys=True, indent=1) + "\n",
        encoding="utf-8",
    )
    doc = _trace.dump()
    if doc is not None:
        (out / "trace.json").write_text(
            json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8"
        )
    return path


def _kernel() -> Any:
    """``precis.pcb.local``, resolved lazily — the geometric kernel behind
    the offline ``place``/``route``/``drc``/``gerbers`` calls (the
    ``precis-geom[pcb-kernel]`` extra). A missing kernel raises with the
    install instruction, not a bare module error."""
    import importlib

    try:
        return importlib.import_module("precis.pcb.local")
    except ImportError as exc:  # pragma: no cover - depends on install
        raise ImportError(
            "pcb.local needs the geometric kernel — install "
            f"precis-geom[pcb-kernel] (missing: {exc.name or exc})"
        ) from exc


if TYPE_CHECKING:
    # The lazy-alias pattern (gr514458): a lazy submodule whose target
    # can't be imported at all — this module is stdlib-only and
    # ``test_pcb_board._imports`` walks ``TYPE_CHECKING`` blocks too, so
    # a ``from precis.pcb import local`` here would trip the belt even
    # though it never runs — is *declared* for checkers instead. With
    # this binding ``from precis_geom.pcb import local`` type-checks and
    # lints clean in an agent's own script; ``__getattr__`` below stays
    # the runtime resolver. ``local`` stays out of ``__all__`` on
    # purpose: a star-import would call ``_kernel()`` and raise the
    # install hint on a wheel without ``[pcb-kernel]``.
    local: Any


def __getattr__(name: str) -> Any:
    """``pcb.local`` resolves to :mod:`precis.pcb.local` on demand —
    ``pcb.local.place(b)`` reads like the spec's table while the builder
    itself stays dependency-light (a wheel without ``[pcb-kernel]`` can
    still run ``to_batch``/``check``/``write_manifest``). The next lazy
    alias copies the ``TYPE_CHECKING`` declaration above, never an
    import."""
    if name == "local":
        return _kernel()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run(
    board: Board, out_dir: str | Path = "out", argv: list[str] | None = None
) -> Path:
    """The ``main.py`` entry — writes the §Q3 output set under ``out_dir``
    and returns the directory. Always: ``manifest.json`` (+``trace.json``
    when tracing is live) and ``check.txt``. ``--place``/``--route`` in
    ``argv`` (defaults to ``sys.argv[1:]``) add the kernel outputs
    (``netlist.net``, ``schematic.svg``, ``ratsnest.svg``, and for route
    ``placement.json``/``copper.json``/``drc.txt``/``gerbers.zip``/
    ``board.svg``/``bom.csv``/``cpl.csv``) via :func:`local`. A missing
    kernel refuses the flagged run and writes the base set for a plain
    one."""
    import sys

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_manifest(board, str(out))
    check = board.check()
    (out / "check.txt").write_text(check.summary() + "\n", encoding="utf-8")
    args = list(sys.argv[1:] if argv is None else argv)
    want_place = "--place" in args or "--route" in args
    want_route = "--route" in args
    try:
        mod = _kernel()
    except ImportError:
        if want_place or want_route:
            raise
        print(
            "precis-geom[pcb-kernel] not installed — wrote manifest.json "
            "+ check.txt only"
        )
        return out
    mod.run(
        board,
        str(out),
        do_place=want_place,
        do_route=want_route,
        name=str(board.slug),
    )
    return out


__all__ = [
    "MANIFEST_SCHEMA",
    "Board",
    "BoardShape",
    "CatalogPart",
    "CheckFinding",
    "CheckResult",
    "FitResult",
    "Footprint",
    "Net",
    "Part",
    "PcbBuilderError",
    "Pin",
    "Role",
    "box",
    "canonical_json",
    "catalog",
    "content_hash",
    "enclosure",
    "fit",
    "module",
    "pad",
    "rect",
    "run",
    "write_manifest",
]
