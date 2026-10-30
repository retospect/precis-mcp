"""precis_geom.pcb — typed Python builder for pcb workspace scripts
(docs/backlog/pcb-workspace-code.md §Q1; slice 0: "Builder + goldens, no
container, no DB").

A board script builds ``Board``/``Net``/``Part``/``Pin``/``Footprint``
objects purely in memory and calls :meth:`Board.to_batch` to get the
same dict shape ``store.PcbMixin.pcb_apply`` already accepts
(``components``/``nets``/``connections``/``features``/``footprints``/``planes``/
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
Parts and nets retain their creating frame's run-local ``maker`` id;
``made`` counts belong to the primitive that actually creates the entity
(so ``wire`` delegates net creation to ``net`` without double-counting).
A traced build stamps each component/net batch row ``frame`` with its
``maker`` — the entity→frame bindings the store persists through the
``meta`` whitelists (gr521596). An untraced build emits no ``frame``
key, so authored batches stay byte-stable.

**Intent markers on pins** (``unused``/``nc``/``must_terminate``) emit the
store's bool shorthand (``{"name": "2", "unused": true}``,
``_pcb_insert_pins`` §1j), not a tag — the same bytes the batch put
authors wrote.

**Plane assignments.** :meth:`Board.plane_assignment` carries named routing
layer/net assignments in ``planes``. They contain no copper geometry and
remain separate from :meth:`Board.plane`, the existing whole-layer pour
feature API, so importing assignment rows cannot create copper pours.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import importlib.metadata
import json
import math
import re
import warnings
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
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

#: The checkable provenance handle ``source=`` takes (gr521603):
#: ``ds:<datasheet ref>~<chunk selector>`` — ``ref`` is a ``da<N>``
#: handle or cite-key slug, the ``~`` suffix the same selector syntax
#: the paper/datasheet handlers resolve.
_SOURCE_HANDLE_RE = re.compile(r"^ds:([^\s~]+)~([^\s~]+)$")

MANIFEST_SCHEMA = 1


def _source_failure_reason(exc: BaseException) -> str | None:
    """Classify a failed ``precis_client`` read of a ``ds:`` handle:
    ``None`` for a clear not-found reply (the ``[error:NotFound]`` /
    ``[error:Gone]`` envelope — a ``source.unresolvable`` finding),
    else the one-line reason the read failed with, which a
    ``source.unchecked`` row names. The staged client raises
    ``RuntimeError`` carrying the tool's ``[error:<Kind>]`` text for
    tool errors (a kind refusal reads ``[error:BadInput] … is not
    served by the sandbox read-only MCP``) and ``precis MCP …`` for
    HTTP/transport ones; a bare ``TimeoutError``/``OSError`` keeps its
    own message."""
    text = str(exc)
    if "[error:NotFound]" in text or "[error:Gone]" in text:
        return None
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first[:200] if first else type(exc).__name__


#: The one not-checked cause ``check()`` can see offline (gr513667): the part
#: has neither a catalog part nor an authored footprint, so the store binds a
#: synthesized landpattern at import. Prefix of each ``not_checked_groups``
#: key (``"<reason>: <refdes letters> <value hint>"``).
_NOT_CHECKED_REASON = "no lcsc, no authored footprint (synthesized landpattern)"


class PcbBuilderError(ValueError):
    """A builder-time misuse: a reused refdes/prefix, a module touching a
    net it was not handed, a non-electrical net domain, an unknown
    footprint name.

    ``refs=`` names the entities the error is about — the
    ``part:<refdes>``/``pad:<refdes>/<pin>``/``net:<name>`` vocabulary
    ``precis.pcb.drc._object_identity`` uses — and ``code=`` overrides
    the derived ``pcb.<type-slug>`` code. Both ride ``diag_refs``/
    ``diag_code`` (gr522027): :func:`precis_geom.out._error_entry`
    serializes them onto a failed manifest's ``error`` for the Diag
    adapter, and ``precis.pcb.diag.op`` reads them when stamping
    ``exc.diag`` — named ``diag_*`` so they can never collide with a
    caller's own attribute."""

    def __init__(
        self, msg: str, *, refs: tuple[str, ...] = (), code: str | None = None
    ) -> None:
        super().__init__(msg)
        self.diag_refs = tuple(refs)
        self.diag_code = code or (
            f"pcb.{_DIAG_CODES.get(type(self).__name__, 'builder-error')}"
        )


class PinConflict(PcbBuilderError):
    """A pin claim that cannot stand (pcb-topo-place Decision 12): a
    fixed pin claimed under two different names, or a flexible slot left
    with no feasible pin at solve time. The message names the signals,
    the pins and the claim sites."""


#: ``PcbBuilderError`` subclass → its ``pcb.`` Diag code slug.
_DIAG_CODES = {"PinConflict": "pin-conflict"}


_AMPS_PER_UNIT = {"a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6, "na": 1e-9}


def _parse_amps(text: str | float | int) -> float:
    """``"20mA"``/``"1.5A"``/``"500uA"`` → amperes; a bare number is
    already amperes."""
    if isinstance(text, int | float):
        return float(text)
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zµA-Z]+)\s*", str(text))
    if not m:
        raise PcbBuilderError(f"current bound {text!r} — expected e.g. '20mA'")
    unit = m.group(2).lower()
    if unit not in _AMPS_PER_UNIT:
        raise PcbBuilderError(
            f"current bound {text!r}: unknown unit {m.group(2)!r} — use A, mA, uA or nA"
        )
    return float(m.group(1)) * _AMPS_PER_UNIT[unit]


@dataclass(frozen=True, slots=True)
class Bound:
    """A checkable electrical requirement on a tie (Decision 15): ``Max``
    is "this path must carry at most this much", ``Min`` "at least".
    Carries the authoring ``text`` verbatim for the manifest."""

    kind: str  # "max" | "min"
    amps: float
    text: str


def Max(text: str | float | int) -> Bound:
    """Upper current bound: ``net.tie(…, current=pcb.Max("20mA"))``."""
    return Bound("max", _parse_amps(text), str(text))


def Min(text: str | float | int) -> Bound:
    """Lower current bound."""
    return Bound("min", _parse_amps(text), str(text))


# ---------------------------------------------------------------------------
# Kwarg contract (gr520905, the push half): a bad or missing kwarg on a
# public builder method quotes that param's doc line, so a board-script
# mistake surfaces the contract at the moment it is made.
# ---------------------------------------------------------------------------

_ARG_ENTRY = re.compile(r"^(\*{0,2}[^\s(:]+)\s*(?:\([^)]*\))?\s*:\s*(.*)$")
_ARG_SECTIONS = ("Args:", "Arguments:", "Parameters:")


@functools.cache
def _param_docs(fn: Callable[..., Any]) -> dict[str, str]:
    """``{param: doc line}`` read off `fn`'s Google ``Args:`` block.

    Runtime half of the params contract: the index-time half is
    ``precis.python_index.docparams`` (docstring_parser, three styles).
    This module stays stdlib-only, so the same parse is re-done here in
    the Google style this file's own docstrings use — the D417 ratchet
    on ``precis_geom.pcb`` keeps the two halves agreeing. A wrapped
    entry's continuation lines join with a space; ``*args``/``**kwargs``
    doc names shed their stars. ``__doc__`` never changes after def, so
    the parse is cached on the function object.
    """
    import inspect

    doc = inspect.getdoc(fn) or ""
    lines = doc.split("\n")
    docs: dict[str, str] = {}
    i = 0
    while i < len(lines):
        if lines[i].strip() not in _ARG_SECTIONS:
            i += 1
            continue
        # The first nonblank line after the header sets the entry indent;
        # a dedent below it ends the section.
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            break
        base = len(lines[j]) - len(lines[j].lstrip())
        cur: str | None = None
        while j < len(lines):
            raw, text = lines[j], lines[j].strip()
            indent = len(raw) - len(raw.lstrip())
            if text and indent < base:
                break
            if not text:
                cur = None
            elif indent == base:
                m = _ARG_ENTRY.match(text)
                if m is not None:
                    cur = m.group(1).lstrip("*")
                    docs[cur] = m.group(2)
                else:
                    cur = None
            elif cur is not None:
                docs[cur] += " " + text
            j += 1
        i = j
    return docs


def _bad_kwarg(
    fn: Callable[..., Any],
    param: str,
    msg: str,
    *,
    refs: tuple[str, ...] = (),
) -> PcbBuilderError:
    """`PcbBuilderError` about `param` on `fn`, with that param's doc
    line appended — the contract surfaces where it was broken. `msg`
    passes through unquoted when `fn`'s docstring has no Args entry for
    it, so an undocumented param degrades to today's message. `refs`
    names the entities the refusal is about (gr522027)."""
    doc = _param_docs(fn).get(param)
    return PcbBuilderError(f"{msg} — {param}: {doc}" if doc else msg, refs=refs)


@functools.cache
def _script_dir() -> Path:
    """The directory the entry script lives in — the anchor claim sites
    are made relative against (the sandbox runs
    ``python /work/main.py``). ``sys.argv[0]`` when it names a real
    file, else ``__main__``'s ``__file__``, else the cwd. Cached: it is
    syscalls on every pin claim and argv0 does not change mid-run. Any
    filesystem fault degrades to ``Path('.')`` — a relative anchor that
    no resolved path is under, so the caller falls back to basename."""
    import sys

    try:
        argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    except (OSError, ValueError):
        argv0 = None
    if argv0 is not None and argv0.is_file():
        return argv0.parent
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if main_file:
        try:
            return Path(main_file).resolve().parent
        except (OSError, ValueError):
            pass
    try:
        return Path.cwd()
    except OSError:
        return Path(".")


def _frame_site(f: Any) -> str:
    """``file:line`` for one frame — relative to the calling script's
    directory or the cwd (``basename:line`` when the frame lives outside
    both), so a site never carries an absolute path (gr520324)."""
    raw = Path(f.f_code.co_filename)
    try:
        path = raw.resolve()
        anchors = (_script_dir(), Path.cwd())
    except (OSError, ValueError):
        # resolve()/cwd() can fail (NUL in the name, a deleted cwd) —
        # the site contract is "never absolute", so basename it is.
        return f"{raw.name}:{f.f_lineno}"
    for anchor in anchors:
        try:
            return f"{path.relative_to(anchor)}:{f.f_lineno}"
        except ValueError:
            continue
    return f"{path.name}:{f.f_lineno}"


def _caller_site(depth: int = 2) -> str:
    """``file:line`` of the board-script line that called the API —
    recorded on pin claims so a PinConflict can name both sites."""
    import inspect

    f = inspect.currentframe()
    try:
        for _ in range(depth):
            if f is None or f.f_back is None:
                return "?:0"
            f = f.f_back
        if f is None:
            return "?:0"
        return _frame_site(f)
    finally:
        del f


def _script_site() -> str:
    """``file:line`` of the first non-``precis_geom`` frame above this
    call — the board-script line behind the API call no matter how many
    builder internals sit between (``Net.tie`` reached through
    ``Board.wire`` still names the ``b.wire`` line, and the ``@traced``
    wrapper is a ``precis_geom`` frame too). This is the stamp
    :class:`CheckFinding` ``site`` carries (gr521599)."""
    import inspect

    f = inspect.currentframe()
    try:
        while f is not None:
            f = f.f_back
            if f is None:
                return "?:0"
            mod = f.f_globals.get("__name__") or ""
            if mod != "precis_geom" and not mod.startswith("precis_geom."):
                return _frame_site(f)
        return "?:0"
    finally:
        del f


class FlexPin:
    """A solver-assigned pin — what ``part.pin(any_of=[...], name=...)``
    returns. Not a concrete :class:`Pin`: the solver picks one of
    ``pins`` and the write-back records the choice (Decisions 11, 14,
    24). :meth:`Net.tie` binds the net the slot's wire belongs to."""

    def __init__(
        self,
        *,
        part: Part,
        pins: tuple[str, ...],
        name: str | None,
        site: str,
    ) -> None:
        self.part = part
        self.pins = pins
        self.name = name
        self.site = site
        #: Filled by :meth:`Net.tie`.
        self.net: str | None = None
        self.requirement: Bound | None = None
        self.source: str | None = None
        #: ``file:line`` of the :meth:`Net.tie` that bound the slot.
        self.tie_site: str | None = None
        #: ``file:line`` of the :meth:`Net.tie` that set ``source``.
        self.source_site: str | None = None

    @property
    def handle(self) -> str:
        return f"{self.part.refdes}.<{self.name or '|'.join(self.pins)}>"

    def to_decl(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "pins": list(self.pins),
            "net": self.net,
            "site": self.site,
        }
        if self.requirement is not None:
            d["requirement_a"] = self.requirement.amps
            d["requirement_kind"] = self.requirement.kind
            d["requirement"] = self.requirement.text
        if self.source is not None:
            d["source"] = self.source
        return d


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

    def __init__(self, *, part: Part, name: str, site: str | None = None) -> None:
        """Create the pin — normally via ``part[name]``, not directly.

        Args:
            part (Part): The owning part.
            name (str): Pin name/number as the footprint knows it.
            site (str | None): ``file:line`` the pin was first named on
                — computed from the calling script frame when omitted.
        """
        self.part = part
        self.name = name
        #: ``file:line`` of the script line that first named this pin.
        self.site = site if site is not None else _script_site()
        self.tags: list[str] = []
        self.meta: dict[str, Any] = {}
        self.markers: dict[str, bool] = {}
        #: Passthrough TOP-LEVEL pin keys the store reads — builder-less
        #: ones (``pad``/``description``/``note``) plus ``etype`` (the
        #: verb writes it here: the store tags only a top-level etype).
        self.extra: dict[str, Any] = {}

    def etype(self, kind: str) -> Pin:
        """Set the pin's electrical type — the store's top-level
        ``etype`` key, which is what earns a tag at write time.

        Args:
            kind (str): Electrical type, e.g. ``"power_in"``.
        """
        # The store's top-level key — an etype under `meta` rides meta
        # verbatim and earns no tag (store rule; gr514460 seam review).
        self.extra["etype"] = kind
        return self

    def current_role(self, kind: str) -> Pin:
        """Declare the pin's role in a current path
        (``meta.current_role``) — e.g. ``"series"`` marks a limiter for
        the LED check.

        Args:
            kind (str): Role name, e.g. ``"series"`` or ``"shunt"``.
        """
        self.meta["current_role"] = kind
        return self

    def nc(self) -> Pin:
        """Mark the pin not-connected (the store's ``nc`` bool shorthand)."""
        self.markers["nc"] = True
        return self

    def unused(self) -> Pin:
        """Mark the pin unused (the store's ``unused`` bool shorthand)."""
        self.markers["unused"] = True
        return self

    def must_terminate(self) -> Pin:
        """Mark the pin must-terminate (the store's bool shorthand)."""
        self.markers["must_terminate"] = True
        return self

    def tag(self, *tags: str) -> Pin:
        """Add free-form tags (deduplicated, order kept).

        Args:
            tags (str): Tag strings to add.
        """
        for t in tags:
            if t not in self.tags:
                self.tags.append(t)
        return self

    def client_of(self, role: Role) -> Pin:
        """Declare the pin a client of a supply :class:`Role`
        (``meta.client_of``).

        Args:
            role (Role): The supply role this pin draws from.
        """
        self.meta.setdefault("client_of", []).append(role.name)
        return self

    @property
    def handle(self) -> str:
        return f"{self.part.refdes}.{self.name}"

    @property
    def annotated(self) -> bool:
        return bool(self.tags or self.meta or self.markers or self.extra)

    def to_dict(self) -> dict[str, Any]:
        """The pin's batch row — ``name`` plus whatever was annotated."""
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
        part_meta: dict[str, Any] | None = None,
        supplies: Any = None,
        pin_swap_groups: Any = None,
        site: str | None = None,
    ) -> None:
        """Create the part — normally via :meth:`Board.part`, not directly.

        Args:
            board (Board): The owning board.
            refdes (str): Module-qualified reference designator.
            lcsc (str | None): Catalog part number.
            label (str | None): Display label override.
            footprint (str | None): Registered footprint name.
            value (str | None): Component value, e.g. ``"10k"``.
            height_mm (float | None): Component height, millimetres.
            roles (Iterable[str]): Supply roles the part provides.
            note (str | None): Free-text note on the batch row.
            part_meta (dict | None): Catalog-identity blob carried
                verbatim as ``part_meta``.
            supplies (Any): Authored ``supplies`` declaration list,
                carried verbatim.
            pin_swap_groups (Any): Authored swap groups, carried verbatim.
            site (str | None): ``file:line`` the part was declared on —
                computed from the calling script frame when omitted.
        """
        self.board = board
        self.refdes = refdes
        self.maker = _trace.leaf_id()
        #: ``file:line`` of the ``Board.part`` script line.
        self.site = site if site is not None else _script_site()
        self.lcsc = lcsc
        self.label = label
        self.footprint = footprint
        self.value = value
        self.height_mm = height_mm
        self.roles: list[str] = list(dict.fromkeys(roles))
        self.note = note
        #: The import's catalog-identity blob (``{value, mpn, mfr,
        #: supplier, supplier_part}``) — carried verbatim as the
        #: component's ``part_meta`` batch key.
        self.part_meta = part_meta
        #: The authored ``supplies`` declaration list — stored under a
        #: different attr name because :meth:`supplies` is the §1b
        #: role-annotation method.
        self.supply_blocks = supplies
        self.pin_swap_groups = pin_swap_groups
        self._pins: dict[str, Pin] = {}
        self._pin_order: list[str] = []
        self._declared_pins: set[str] = set()
        #: topo-place 1b declarations (Decisions 11–13): named fixed-pin
        #: claims keyed by pin name, the flex slots, swap groups and the
        #: per-design pin function table — emitted as the component's
        #: ``pin_decls`` batch key.
        self._pin_claims: dict[str, dict[str, Any]] = {}
        self._flex_slots: list[FlexPin] = []
        self._swap_groups: list[dict[str, Any]] = []
        self._pin_functions: dict[str, dict[str, Any]] = {}
        self._pin_functions_source: str | None = None
        #: The :class:`CatalogPart` this part was built from
        #: (``part=``/``part="lcsc"``), else ``None`` — ``check`` reads
        #: its resolved ``pins`` as the pin universe.
        self._catalog: CatalogPart | None = None
        #: ``file:line`` each ``_pin_functions`` key was first written on
        #: (first-wins per key) — the site a ``pin.unknown`` finding on a
        #: typo'd table key points at.
        self._pin_function_sites: dict[str, str] = {}
        #: ``file:line`` of the ``pin_functions`` call that set
        #: ``_pin_functions_source``.
        self._pin_functions_source_site: str | None = None
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
        """Lazy pin access: ``part[5]``/``part["A1"]`` creates or returns
        the :class:`Pin`.

        Args:
            key (str | int): Pin name/number.
        """
        name = str(key)
        if name not in self._pins:
            self._pins[name] = Pin(part=self, name=name)
            self._pin_order.append(name)
        return self._pins[name]

    def pins(self, *names: str | int) -> list[Pin]:
        """Several pins at once: ``part.pins(1, 2, "SDA")``.

        Args:
            names (str | int): Pin names/numbers.
        """
        return [self[n] for n in names]

    def pin(
        self,
        number: str | int | None = None,
        *,
        name: str | None = None,
        any_of: Iterable[str | int] | None = None,
    ) -> Pin | FlexPin:
        """Declare how this part's pin is chosen (pcb-topo-place
        Decision 11). ``part.pin(5)`` fixes pin 5 — the plain
        :class:`Pin`. ``part.pin(any_of=[1, 4, 9], name="BTN")`` is a
        flexible slot the solver fills; ``name`` feeds the firmware pin
        map. A fixed pin claimed twice under different names raises
        :class:`PinConflict` naming both claim sites; the same name
        again is a no-op.

        Args:
            number (str | int | None): Pin to fix outright — pass this
                OR ``any_of=``, not both.
            name (str | None): Firmware pin-map name for the claim.
            any_of (Iterable[str | int] | None): Candidate pins (≥1, no
                repeats) — a flexible slot the solver picks one of.
        """
        site = _caller_site()
        if any_of is not None:
            if number is not None:
                raise _bad_kwarg(
                    Part.pin,
                    "any_of",
                    f"{self.refdes}.pin() takes a pin number OR any_of=, not both",
                )
            pins = tuple(str(p) for p in any_of)
            if not pins:
                raise _bad_kwarg(
                    Part.pin, "any_of", f"{self.refdes}.pin(any_of=…) needs ≥1 pin"
                )
            if len(set(pins)) != len(pins):
                raise _bad_kwarg(
                    Part.pin,
                    "any_of",
                    f"{self.refdes}.pin(any_of={pins}) repeats a pin",
                )
            # Candidates are live pins: the solver picks one, so each
            # must reach the IR (a declared pin with no wire lands in
            # `unconnected` with a real pad position).
            for cand in pins:
                self._declared_pins.add(self[cand].name)
            fp = FlexPin(part=self, pins=pins, name=name, site=site)
            self._flex_slots.append(fp)
            return fp
        if number is None:
            raise _bad_kwarg(
                Part.pin,
                "number",
                f"{self.refdes}.pin() needs a pin number or any_of=",
            )
        p = self[number]
        self._declared_pins.add(p.name)
        if name is not None:
            prev = self._pin_claims.get(p.name)
            if prev is not None and prev["name"] != name:
                raise PinConflict(
                    f"{self.refdes} pin {p.name}: claimed as "
                    f"{prev['name']!r} ({prev['site']}) and as {name!r} ({site})",
                    refs=(f"part:{self.refdes}", f"pad:{self.refdes}/{p.name}"),
                )
            self._pin_claims.setdefault(p.name, {"name": name, "site": site})
        return p

    def pin_functions(
        self,
        table: Mapping[str, Any],
        *,
        source: str | None = None,
    ) -> Part:
        """The per-design pin function table the LLM writes from the
        datasheet (Decision 16): ``{"PA1": ["GPIO", "INT1"], …}`` — a
        value may instead be ``{"functions": [...], "max_current_a": …}``
        when the datasheet also rates the pin's drive. ``source`` is the
        datasheet chunk id (``"ds:<id>~<chunk>"``) and is checkable
        provenance, not a comment.

        Args:
            table (Mapping[str, Any]): ``{pin label: [functions]}`` — or
                ``{"functions": [...], "max_current_a": …}`` per pin.
            source (str | None): Datasheet chunk id
                (``"ds:<id>~<chunk>"``).
        """
        site = _script_site()
        for pin, entry in table.items():
            self._pin_function_sites.setdefault(str(pin), site)
            if isinstance(entry, Mapping):
                funcs = [str(f) for f in (entry.get("functions") or ())]
                extra = {k: v for k, v in entry.items() if k != "functions"}
            else:
                funcs = [str(f) for f in entry]
                extra = {}
            rec = self._pin_functions.setdefault(str(pin), {"functions": []})
            for f_ in funcs:
                if f_ not in rec["functions"]:
                    rec["functions"].append(f_)
            rec.update(extra)
        if source is not None:
            self._pin_functions_source = source
            self._pin_functions_source_site = site
        return self

    def pins_with(self, func: str) -> list[str]:
        """Pin labels whose :meth:`pin_functions` entry lists ``func`` —
        feeds ``pin(any_of=u1.pins_with("GPIO"))``.

        Args:
            func (str): Function name to match, e.g. ``"GPIO"``.
        """
        return [p for p, rec in self._pin_functions.items() if func in rec["functions"]]

    def swap_group(
        self,
        members: Iterable[Any],
        *,
        tuple: bool = False,
    ) -> Part:
        """Declare pins the solver may permute (Decision 13). A flat pin
        list (``rn1.swap_group([1, 2, 3, 4])``) lets those pins permute
        freely; ``tuple=True`` takes a list of pin tuples moved as units
        (``[(1, 8), (2, 7), …]`` — a buffer channel's ``in_i``/``out_i``
        always move together).

        Args:
            members (Iterable): Pins (or pin tuples with ``tuple=True``)
                the solver may permute — ≥2 members.
            tuple (bool): ``members`` is a list of equal-length pin
                tuples moved as units.
        """
        units = [[m] if isinstance(m, str | int) else list(m) for m in members]
        for u in units:
            for m in u:
                self._declared_pins.add(self[m].name)
        if len(units) < 2:
            raise _bad_kwarg(
                Part.swap_group,
                "members",
                f"{self.refdes}.swap_group needs ≥2 members",
            )
        widths = {len(u) for u in units}
        if tuple and len(widths) != 1:
            raise _bad_kwarg(
                Part.swap_group,
                "tuple",
                f"{self.refdes}.swap_group(tuple=True): members must be "
                "equal-length tuples",
            )
        self._swap_groups.append(
            {
                "members": [[str(p) for p in u] for u in units],
                "tuple": bool(tuple),
            }
        )
        return self

    def declare(self, *names: str | int) -> Part:
        """Declare pins that must appear in the batch even if nothing
        annotates them (a part with no catalog part and no footprint has
        only these as its pin list).

        Args:
            names (str | int): Pin names/numbers to declare.
        """
        for n in names:
            self._declared_pins.add(self[n].name)
        return self

    def supplies(self, role: Role, pins: Iterable[str] = ()) -> Part:
        """Declare the part supplies ``role``; ``pins`` marks which of
        its pins feed it (``meta.supplies``).

        Args:
            role (Role): The supply role this part provides.
            pins (Iterable[str]): Pin names that supply the role.
        """
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
        """Place the part on the board (mm, board frame).

        Args:
            x (float): Board-frame x, millimetres.
            y (float): Board-frame y, millimetres.
            rot (float): Rotation, degrees.
            side (str): ``"top"`` or ``"bottom"``.
            fixed (str): What the solver must hold — ``"both"`` fixes
                pose, ``"rot"`` only the rotation.
        """
        self.x, self.y, self.rot, self.side, self.fixed = x, y, rot, side, fixed
        return self

    def leash(self, max_travel_mm: float) -> Part:
        """Cap how far the solver may move the part.

        Args:
            max_travel_mm (float): Max solver travel, millimetres.
        """
        self.max_travel_mm = max_travel_mm
        return self

    def on_edge(
        self, face: str, at: float | None = None, require: str = "required"
    ) -> Part:
        """Declare an edge-mate constraint (the ``edge_mate`` row).

        Args:
            face (str): Board edge face the part mates, e.g. ``"left"``.
            at (float | None): Position along the edge, millimetres.
            require (str): Requirement strength — ``"required"`` or a
                softer ask.
        """
        mate: dict[str, Any] = {"face": face, "require": require}
        if at is not None:
            mate["at"] = at
        self.edge_mate = mate
        return self

    def to_dict(self) -> dict[str, Any]:
        """The component's batch row (a ``components[]`` entry)."""
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
        decls = self._pin_decls()
        if decls:
            d["pin_decls"] = decls
        if self.part_meta is not None:
            d["part_meta"] = copy.deepcopy(self.part_meta)
        if self.supply_blocks is not None:
            d["supplies"] = copy.deepcopy(self.supply_blocks)
        if self.pin_swap_groups is not None:
            d["pin_swap_groups"] = copy.deepcopy(self.pin_swap_groups)
        if self.maker is not None:
            d["frame"] = self.maker
        return d

    def _pin_decls(self) -> dict[str, Any]:
        """The component's ``pin_decls`` batch section — the topo-place
        1b declarations in serialized form. Rides the component row the
        same way ``pin_swap_groups`` does: ``instance_row`` hoists it
        onto the pcb_graph instance, the store carries it in component
        meta, and ``design_from_ir`` turns it into ``TopoDesign.flex`` /
        ``swap_groups``."""
        decls: dict[str, Any] = {}
        if self._pin_claims:
            decls["claims"] = [
                {"pin": p, "name": c["name"], "site": c["site"]}
                for p, c in self._pin_claims.items()
            ]
        if self._flex_slots:
            decls["slots"] = [fp.to_decl() for fp in self._flex_slots]
        if self._swap_groups:
            decls["swap_groups"] = copy.deepcopy(self._swap_groups)
        if self._pin_functions:
            decls["pin_functions"] = copy.deepcopy(self._pin_functions)
            if self._pin_functions_source is not None:
                decls["pin_functions_source"] = self._pin_functions_source
        return decls


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
        width_mm: float | None = None,
        meta: dict[str, Any] | None = None,
        site: str | None = None,
    ) -> None:
        """Create the net — normally via :meth:`Board.net`, not directly.

        Args:
            board (Board): The owning board.
            name (str): Net name — already module-qualified.
            cls (str | None): Net class — the :meth:`Board.net_class`
                name whose rules this net obeys.
            current (float | None): Estimated max current, amps
                (``est_current_a``).
            voltage (float | None): Working voltage, volts
                (``working_voltage_v``).
            impedance (float | None): Controlled-impedance target, ohms.
            edge_rate (float | None): Switching edge rate, V/ns — the
                aggressor annotation (``edge_rate_v_per_ns``).
            function (str | None): Function hint (``function_hint``),
                e.g. ``"i2c_sda"``.
            note (str | None): Free-text note on the net's batch row.
            domain (str | None): Net domain — ``'electrical'`` or omit.
            width_mm (float | None): Trace width hint, millimetres.
            meta (dict | None): Extra keys merged verbatim into the
                net's ``meta`` batch key.
            site (str | None): ``file:line`` the net was declared on —
                computed from the calling script frame when omitted.
        """
        self.board = board
        self.name = name
        self.maker = _trace.leaf_id()
        #: ``file:line`` of the ``Board.net`` script line.
        self.site = site if site is not None else _script_site()
        self.cls = cls
        self.current = current
        self.voltage = voltage
        self.impedance = impedance
        self.edge_rate = edge_rate
        self.function = function
        self.note = note
        self.domain = domain
        self.width_mm = width_mm
        self.meta = meta

    def tie(
        self,
        *pins: Pin | FlexPin,
        note: str | None = None,
        current: Bound | None = None,
        source: str | None = None,
    ) -> Net:
        """Wire pins onto this net. A :class:`FlexPin` ties the SLOT —
        its net is recorded for the solver, no concrete connection is
        emitted (the solver picks the pin). ``current=pcb.Max("20mA")``
        plus ``source="ds:…~chunk…"`` attach a checkable requirement
        with provenance to the connection (Decision 15).

        Args:
            pins (Pin | FlexPin): Pins (or flex slots) to wire onto this
                net.
            note (str | None): Note on each emitted connection row.
            current (Bound | None): ``pcb.Max``/``pcb.Min`` checkable
                current requirement for the connections.
            source (str | None): Provenance chunk id for the requirement
                (``"ds:<id>~<chunk>"``).
        """
        if current is not None and not isinstance(current, Bound):
            raise _bad_kwarg(
                Net.tie,
                "current",
                f"net {self.name!r}: current= takes pcb.Max/pcb.Min, got {current!r}",
            )
        site = _script_site()
        for p in pins:
            if isinstance(p, FlexPin):
                if p.net is not None and p.net != self.name:
                    raise PinConflict(
                        f"slot {p.handle}: tied to net {p.net!r} "
                        f"({p.site}) and again to {self.name!r}",
                        refs=(
                            f"part:{p.part.refdes}",
                            f"net:{p.net}",
                            f"net:{self.name}",
                        ),
                    )
                p.net = self.name
                p.requirement = current or p.requirement
                p.source = source or p.source
                p.tie_site = site
                if source is not None:
                    p.source_site = site
                continue
            self.board._connect(
                self, p, note, current=current, source=source, site=site
            )
        return self

    def pins(self) -> list[tuple[str, str]]:
        """``(refdes, pin)`` for every connection on this net —
        ``board.pins_on(self.name)``."""
        return self.board.pins_on(self.name)

    def to_dict(self) -> dict[str, Any]:
        """The net's batch row (a ``nets[]`` entry)."""
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
        if self.width_mm is not None:
            d["width_mm"] = self.width_mm
        if self.meta is not None:
            d["meta"] = dict(self.meta)
        if self.maker is not None:
            d["frame"] = self.maker
        return d


class Role:
    """A supply role (pcb-lazy-netlist-and-checks §1): a part ``supplies``
    it, a pin ``client_of`` it."""

    def __init__(self, name: str, voltage: float | None = None) -> None:
        """Create the role — normally via :meth:`Board.role`.

        Args:
            name (str): Role name, e.g. ``"3v3"``.
            voltage (float | None): Nominal voltage, volts.
        """
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
        holes: list[dict[str, Any]] | None = None,
        graphics: list[dict[str, Any]] | None = None,
    ) -> None:
        self.name = name
        self.pads = pads
        self.pin_map = pin_map
        self.courtyard = courtyard
        self.centroid = centroid
        self.note = note
        self.holes = list(holes or [])
        self.graphics = list(graphics or [])

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
        if self.holes:
            d["holes"] = copy.deepcopy(self.holes)
        if self.graphics:
            d["graphics"] = copy.deepcopy(self.graphics)
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
    #: Pin name when the finding names one pin — pin-level feedback is
    #: the load-bearing granularity (gr521599).
    pin: str | None = None
    #: ``file:line`` of the board-script line the finding's subject was
    #: declared on — the ``b.net``/``b.part``/``net.tie`` line, not the
    #: check's own.
    site: str | None = None
    #: True when the row reports a rule that could not be evaluated
    #: (e.g. a bound pin on a voltage-less net) — a cannot-check row,
    #: not a defect.
    not_checked: bool = False


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
            where = ".".join(p for p in (f_.refdes, f_.pin) if p) or f_.net or ""
            line = f"  {f_.code}"
            if f_.not_checked:
                line += " [not-checked]"
            line += f" {where}: {f_.message}"
            if f_.site:
                line += f" ({f_.site})"
            lines.append(line)
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
    height_mm: float | None = None
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


#: Pending :func:`catalog` reads made with no board to bind to, keyed by
#: lcsc — claimed once by the next manifest written (gr515495: a read's
#: owner is the ``board=`` argument or the ``with board:`` block it ran
#: in; an unbound read lands here deprecated, and whichever board next
#: writes a manifest claims the pool — reads cannot leak into a SECOND
#: board, but nothing makes the claim order deterministic).
_CATALOG_READS: dict[str, dict[str, Any]] = {}

#: The context-managed "current board" — the ``Board`` a ``with board:``
#: block is inside. A module-level :func:`catalog` read binds to it at
#: read time; :meth:`Board.__exit__` resets the binding on exit (a
#: ContextVar token restore), so a board can never leak its binding past
#: the block that names it — the defect the old ``_LAST_BOARD`` global
#: had (gr515495).
_CURRENT_BOARD: ContextVar[Board | None] = ContextVar(
    "precis_geom_pcb_current_board", default=None
)


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
    hm = row.get("height_mm")
    part.height_mm = float(hm) if hm is not None else None
    part.part_id = row.get("part_id")
    part.footprint_sha = row.get("sha")
    part.read_at = datetime.now(UTC).isoformat()
    part.pins = [
        str(v.get("name") or k) if isinstance(v, dict) else str(v or k)
        for k, v in sorted(part.pin_map.items(), key=lambda kv: str(kv[0]))
    ] or [str(p.get("number")) for p in part.pads]
    return part


def catalog(lcsc: str, *, board: Board | None = None) -> CatalogPart:
    """Read a catalog footprint and bind its manifest entry to a board —
    the ``board=`` argument, else the board whose ``with board:`` block
    this read runs inside (``_CURRENT_BOARD``; gr515495 — binding is
    explicit, never a process-global "board most recently built").

    A read with no board at all lands in the pending pool
    (:data:`_CATALOG_READS`), claimed once by the next manifest written —
    the DEPRECATED implicit path every ``Board(...)``-then-``catalog()``
    script written before ``board=``/``with`` still works through, with a
    warning. :meth:`Board.catalog` binds to its own board regardless."""
    part = _catalog_read(lcsc)
    if part.resolved or part.error is not None:
        target = board if board is not None else _CURRENT_BOARD.get()
        if target is None:
            warnings.warn(
                "pcb.catalog() with no board — pass board=, call "
                "b.catalog(lcsc), or read inside `with b:`; the read is "
                "pending for whatever board next writes a manifest "
                "(implicit binding is deprecated)",
                DeprecationWarning,
                stacklevel=2,
            )
            _CATALOG_READS[part.lcsc] = _manifest_entry(part)
        else:
            target._catalog_reads[part.lcsc] = _manifest_entry(part)
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
    addressed by INDEX for geometry — ``plane(1, …)``,
    ``keepout(layers=[0])``; ``plane_assignment`` carries named routing
    layers verbatim for batch round trips."""

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
        """Create the board in memory; :meth:`to_batch` emits the
        ``pcb_apply`` payload.

        Args:
            slug (str): Board slug — the design's stable name.
            profile (str | None): Profile name emitted as the batch's
                ``profile`` key.
            stackup (str | int | None): Copper-layer count (``4``) or a
                preset name (``'2l'``, ``'4l-two-sided'``,
                ``'4l-top-only'``); omit for the 4-layer default.
            sides (str): ``"both"`` (default); anything else is emitted
                as ``meta.placement_sides``.
            outline (dict | list | None): Board outline — a :func:`rect`
                geometry dict or an ``(x, y)`` point list.
            meta (dict | None): Free-form keys for the batch ``meta``
                dict.
        """
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
        #: ``file:line`` of each ``_connections`` row's ``tie`` — a
        #: parallel list so the site never lands in ``to_batch`` (the
        #: goldens hash those bytes); index-aligned with
        #: ``_connections``.
        self._conn_sites: list[str] = []
        self._features: list[dict[str, Any]] = []
        self._planes: list[dict[str, str]] = []
        self._footprints: dict[str, Footprint] = {}
        self._footprint_order: list[str] = []
        self._net_classes: dict[str, dict[str, Any]] = {}
        self._roles: dict[str, Role] = {}
        self._generators: list[dict[str, Any]] = []
        self._measures: list[dict[str, Any]] = []
        self._catalog_reads: dict[str, dict[str, Any]] = {}
        #: ``source=`` resolution per distinct ``ref~chunk`` — a check()
        #: runs repeatedly and each resolution is a client round-trip.
        self._source_cache: dict[str, tuple[str, str | None]] = {}
        self._used_prefixes: set[str] = set()
        self._frame_stack: list[_ModuleFrame] = []
        self._board_ctx_tokens: list[Any] = []

        if isinstance(stackup, list):
            raise _bad_kwarg(
                Board.__init__,
                "stackup",
                f"stackup= takes a layer count or a preset name, not a list "
                f"({stackup!r}) — use stackup={len(stackup)}",
            )
        if isinstance(stackup, bool) or (
            stackup is not None and not isinstance(stackup, int | str)
        ):
            raise _bad_kwarg(
                Board.__init__,
                "stackup",
                f"stackup {stackup!r} is not a layer count or preset name",
            )
        if isinstance(stackup, int) and stackup < 2:
            raise _bad_kwarg(
                Board.__init__,
                "stackup",
                f"stackup {stackup} — a board has at least the two outer "
                f"copper layers (F.Cu/B.Cu)",
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

    def __enter__(self) -> Board:
        """``with board:`` makes this board the current one — a
        module-level :func:`catalog` read in the block binds here.
        Exiting resets the binding (ContextVar token), so the board's
        reach ends with the block."""
        self._board_ctx_tokens.append(_CURRENT_BOARD.set(self))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _CURRENT_BOARD.reset(self._board_ctx_tokens.pop())

    def catalog(self, lcsc: str) -> CatalogPart:
        """:func:`catalog` bound to THIS board — the read's manifest
        ``parts`` row lands here and only here; a pending module-level
        read of the same part number is NOT consumed (it belongs to the
        board that owns it, or the next manifest if none does).

        Args:
            lcsc (str): Catalog part number to read.
        """
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
        under a used name is refused.

        Args:
            fp (Footprint): The authored footprint to register.
        """
        prev = self._footprints.get(fp.name)
        if prev is None:
            self._footprints[fp.name] = fp
            self._footprint_order.append(fp.name)
            _trace.record(made={"footprints": 1})
        elif prev is not fp:
            raise PcbBuilderError(
                f"footprint {fp.name!r} already registered on board {self.slug!r}",
                refs=(f"footprint:{fp.name}",),
            )
        return fp

    @traced
    def part(
        self,
        refdes: str,
        *,
        lcsc: str | None = None,
        part: CatalogPart | str | None = None,
        label: str | None = None,
        footprint: str | Footprint | None = None,
        value: str | None = None,
        height_mm: float | None = None,
        roles: Iterable[str] = (),
        pins: Iterable[Any] = (),
        note: str | None = None,
        part_meta: dict[str, Any] | None = None,
        supplies: Any = None,
        pin_swap_groups: Any = None,
    ) -> Part:
        """``part=`` is a catalog-ref (gr514215): a :class:`CatalogPart`
        from :meth:`catalog`/``pcb.catalog``, or an lcsc string — read
        through :meth:`catalog` so the manifest records it. The ref
        supplies ``lcsc``, the ``footprint`` name (its ``package``) and
        ``height_mm``; explicit kwargs win, a conflicting ``lcsc=``
        refuses. An unresolved ref still sets ``lcsc`` (it resolves
        server-side on apply); only a resolved one can supply the
        footprint name.

        Args:
            refdes (str): Reference designator — under :meth:`use` it
                qualifies as ``<prefix>.<refdes>``; a used one refuses.
            lcsc (str | None): Catalog part number — must agree with a
                ``part=`` ref when both are given.
            part (CatalogPart | str | None): Catalog ref — a
                :class:`CatalogPart` or an lcsc string read through
                :meth:`catalog`; supplies ``lcsc``/``footprint``/
                ``height_mm`` unless kwargs override.
            label (str | None): Display label override.
            footprint (str | Footprint | None): Registered footprint
                name, or an authored :class:`Footprint` (registered on
                first use).
            value (str | None): Component value, e.g. ``"10k"``.
            height_mm (float | None): Component height, millimetres.
            roles (Iterable[str]): Supply roles the part provides.
            pins (Iterable): Pins to declare — names, or batch-shaped
                ``{"name": …, "tags": …, "meta": …}`` dicts.
            note (str | None): Free-text note on the batch row.
            part_meta (dict | None): Catalog-identity blob carried
                verbatim as ``part_meta``.
            supplies (Any): Authored ``supplies`` declaration list,
                carried verbatim.
            pin_swap_groups (Any): Authored swap groups, carried
                verbatim.
        """
        if isinstance(part, str):
            part = self.catalog(part)
        if part is not None:
            if lcsc is not None and lcsc != part.lcsc:
                raise _bad_kwarg(
                    Board.part,
                    "part",
                    f"part= reads {part.lcsc!r} but lcsc={lcsc!r} was also "
                    f"given — pass one",
                )
            lcsc = part.lcsc
            if footprint is None and part.package:
                footprint = part.package
            if height_mm is None and part.height_mm is not None:
                height_mm = part.height_mm
            # The read's manifest entry binds to this board here too —
            # a CatalogPart made by an unbound module-level ``catalog()``
            # (pending in _CATALOG_READS) must not escape to whatever
            # board next writes a manifest.
            if part.resolved or part.error is not None:
                self._catalog_reads[part.lcsc] = _manifest_entry(part)
                _CATALOG_READS.pop(part.lcsc, None)
        qualified = self._qualify(refdes, ".")
        if qualified in self._parts:
            raise _bad_kwarg(
                Board.part,
                "refdes",
                f"refdes {qualified!r} already used on board {self.slug!r}",
                refs=(f"part:{qualified}",),
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
            part_meta=part_meta,
            supplies=supplies,
            pin_swap_groups=pin_swap_groups,
        )
        p._catalog = part if isinstance(part, CatalogPart) else None
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
        _trace.record(made={"parts": 1})
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
        width_mm: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Net:
        """Declare an electrical net; :meth:`Net.tie` wires pins onto it.
        Re-declaring a ``name`` refuses.

        Args:
            name (str): Net name — under :meth:`use` it qualifies as
                ``<prefix>/<name>``.
            cls (str | None): Net class — the :meth:`net_class` name
                whose rules this net obeys.
            current (float | None): Estimated max current, amps
                (``est_current_a``).
            voltage (float | None): Working voltage, volts
                (``working_voltage_v``).
            impedance (float | None): Controlled-impedance target, ohms.
            edge_rate (float | None): Switching edge rate, V/ns — the
                aggressor annotation (``edge_rate_v_per_ns``).
            function (str | None): Function hint (``function_hint``),
                e.g. ``"i2c_sda"``.
            note (str | None): Free-text note on the net's batch row.
            domain (str | None): Net domain — ``'electrical'`` or omit;
                anything else refuses (only electrical nets route).
            width_mm (float | None): Trace width hint, millimetres.
            meta (dict | None): Extra keys merged verbatim into the
                net's ``meta`` batch key.
        """
        if domain not in _ELECTRICAL_DOMAINS:
            raise _bad_kwarg(
                Board.net,
                "domain",
                f"net {name!r}: domain must be electrical (omit it or pass "
                f"'electrical'), got {domain!r}",
            )
        qualified = self._qualify(name, "/")
        if qualified in self._nets:
            raise _bad_kwarg(
                Board.net,
                "name",
                f"net {qualified!r} already declared on board {self.slug!r}",
                refs=(f"net:{qualified}",),
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
            width_mm=width_mm,
            meta=meta,
        )
        self._nets[qualified] = n
        self._net_order.append(qualified)
        frame = self._frame
        if frame is not None:
            frame.allowed_net_names.add(qualified)
        _trace.record(made={"nets": 1})
        return n

    def _connect(
        self,
        net: Net,
        pin: Pin,
        note: str | None = None,
        current: Bound | None = None,
        source: str | None = None,
        site: str | None = None,
    ) -> None:
        frame = self._frame
        if frame is not None and net.name not in frame.allowed_net_names:
            raise PcbBuilderError(
                f"module {frame.qualname!r} (prefix {frame.prefix!r}) touched "
                f"net {net.name!r} it was not handed as a port",
                refs=(f"net:{net.name}", f"part:{pin.part.refdes}"),
            )
        entry: dict[str, Any] = {
            "net": net.name,
            "refdes": pin.part.refdes,
            "pin": pin.name,
        }
        if note:
            entry["note"] = note
        if current is not None:
            key = "current_max_a" if current.kind == "max" else "current_min_a"
            entry[key] = current.amps
            entry["current"] = current.text
        if source is not None:
            entry["source"] = source
        self._connections.append(entry)
        self._conn_sites.append(site or _script_site())

    @traced
    def wire(self, pin_a: Pin, pin_b: Pin, name: str | None = None) -> Net:
        """Two-pin net. Without ``name`` the net is minted
        ``<refdes>_<pin>__<refdes>_<pin>`` (the free-ended-net name rule,
        §1d) and recorded like any other.

        Args:
            pin_a (Pin): One end of the wire.
            pin_b (Pin): The other end.
            name (str | None): Net name; omit for the auto
                ``<refdes>_<pin>__<refdes>_<pin>`` name.
        """
        if name is None:
            name = f"{pin_a.part.refdes}_{pin_a.name}__{pin_b.part.refdes}_{pin_b.name}"
            name = name.replace(".", "_").replace("/", "_")
        net = self._nets.get(self._qualify(name, "/")) or self.net(name)
        net.tie(pin_a, pin_b)
        return net

    @traced
    def net_class(self, name: str, **rules: Any) -> dict[str, Any]:
        """Declare a net class — the rule set nets join with ``cls=``.

        Args:
            name (str): Net class name.
            rules: Constraint keys (clearances, widths…) carried
                verbatim into ``net_classes``.
        """
        self._net_classes[name] = rules
        _trace.record(made={"net_classes": 1})
        return rules

    @traced
    def role(self, name: str, voltage: float | None = None) -> Role:
        """Declare a supply role (the ``roles`` batch section).

        Args:
            name (str): Role name, e.g. ``"3v3"``.
            voltage (float | None): Nominal voltage, volts.
        """
        r = Role(name=name, voltage=voltage)
        self._roles[name] = r
        _trace.record(made={"roles": 1})
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
                raise _bad_kwarg(
                    Board.__init__,
                    "stackup",
                    f"unknown stackup preset {self.stackup!r} — known: "
                    f"{sorted(_PRESET_LAYER_COUNTS)}",
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
            raise _bad_kwarg(
                getattr(Board, what),
                "layer",
                f"{what}() takes a layer index (0 = top … {n - 1} = bottom), "
                f"not a name{hint}",
            )
        return _layer_name(layer, n)

    @traced
    def plane_assignment(self, net: Net, layer: str) -> dict[str, str]:
        """Assign a net to a named routing layer, without creating copper.

        Args:
            net (Net): Net declared on this board.
            layer (str): Routing-layer name, preserved verbatim in ``planes``.
                This assignment carries no pour or zone geometry.
        """
        if net.board is not self:
            raise _bad_kwarg(
                Board.plane_assignment, "net", "net must belong to this board"
            )
        if not isinstance(layer, str) or not layer.strip():
            raise _bad_kwarg(
                Board.plane_assignment, "layer", "layer must be a nonempty name"
            )
        entry = {"net": net.name, "layer": layer}
        self._planes.append(entry)
        return entry

    @traced
    def plane(self, layer: int, *, net: Net, priority: int = 0) -> dict[str, Any]:
        """A whole-layer pour for ``net`` on copper layer INDEX ``layer``
        (``0`` = top … n-1 = bottom; ``-1`` counts from the bottom).

        Args:
            layer (int): Copper layer index (0 = top … n-1 = bottom;
                ``-1`` counts from the bottom) — never a name.
            net (Net): The net the pour belongs to.
            priority (int): Pour priority when pours overlap.
        """
        entry = {
            "ftype": "plane_net",
            "layer": self._layer_at(layer, "plane"),
            "net": net.name,
            "priority": priority,
        }
        self._features.append(entry)
        _trace.record(made={"features": 1})
        return entry

    @traced
    def outline(
        self,
        geom: dict[str, Any] | list[tuple[float, float]],
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """The board outline (one per board): a :func:`rect` geometry or a
        point list (closed by the caller if it should be).

        Args:
            geom (dict | list): Outline geometry — a :func:`rect` dict
                or an ``(x, y)`` point list.
            note (str | None): Free-text note on the feature row.
        """
        if any(f_["ftype"] == "outline" for f_ in self._features):
            raise PcbBuilderError(f"board {self.slug!r} already has an outline")
        if not isinstance(geom, dict):
            geom = {"path": [[x, y] for x, y in geom]}
        f_: dict[str, Any] = {"ftype": "outline", "geom": geom}
        if note is not None:
            f_["note"] = note
        self._features.append(f_)
        _trace.record(made={"features": 1})
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
        """A mounting hole feature.

        Args:
            x (float): Centre x, millimetres.
            y (float): Centre y, millimetres.
            drill (float): Drill diameter, millimetres.
            ring (float): Annular ring diameter, millimetres.
            plated (bool): Plated barrel (default True).
            note (str | None): Free-text note on the feature row.
        """
        f_: dict[str, Any] = {
            "ftype": "mounting_hole",
            "x": x,
            "y": y,
            "geom": {"diameter": drill, "ring_dia_mm": ring, "plated": plated},
        }
        if note is not None:
            f_["note"] = note
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    @traced
    def fiducial(self, x: float, y: float) -> dict[str, Any]:
        """A fiducial marker feature.

        Args:
            x (float): Marker x, millimetres.
            y (float): Marker y, millimetres.
        """
        f_ = {"ftype": "fiducial", "x": x, "y": y}
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    @traced
    def testpoint(
        self, net: Net, x: float | None = None, y: float | None = None
    ) -> dict[str, Any]:
        """A testpoint feature bound to a net.

        Args:
            net (Net): The net the testpoint probes.
            x (float | None): Pad x, millimetres.
            y (float | None): Pad y, millimetres.
        """
        f_: dict[str, Any] = {"ftype": "testpoint", "net": net.name}
        if x is not None:
            f_["x"] = x
        if y is not None:
            f_["y"] = y
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    @traced
    def corner_nuts(self) -> dict[str, Any]:
        """Mounting nuts at the board corners (``corner_nuts``)."""
        f_ = {"ftype": "corner_nuts"}
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    @traced
    def keepout(
        self,
        poly: list[list[float]],
        layers: Iterable[int] = (),
        reason: str | None = None,
    ) -> dict[str, Any]:
        """A keepout polygon — optionally restricted to copper layers.

        Args:
            poly (list[list[float]]): Polygon vertices ``[[x, y], …]``,
                millimetres.
            layers (Iterable[int]): Copper layer indexes the keepout
                applies to; empty = all layers.
            reason (str | None): Why the area is kept out.
        """
        f_: dict[str, Any] = {"ftype": "keepout", "geom": {"poly": poly}}
        if layers:
            f_["layers"] = [self._layer_at(layer, "keepout") for layer in layers]
        if reason is not None:
            f_["reason"] = reason
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    @traced
    def feature(self, ftype: str, **fields: Any) -> dict[str, Any]:
        """An arbitrary ``features[]`` row (``_pcb_insert_feature`` shape)
        for feature types without a dedicated primitive.

        Args:
            ftype (str): Feature type tag (the row's ``ftype``).
            fields: Feature keys carried verbatim into the row.
        """
        f_: dict[str, Any] = {"ftype": ftype, **fields}
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    # -- non-copper fab layers (gr520329) ----------------------------------
    #
    # Flex/rigid-flex fabrication layers and the bend zone, as typed
    # ``features[]`` rows — the same path ``keepout``/``outline`` take, so
    # ``_pcb_insert_feature`` stores them unchanged and the gerber writer
    # (``precis.pcb.gerber``) emits each to its conventional layer file.
    # Hatched copper pours inside a bend zone are deliberately out of scope
    # for this slice (noted in ``check_bend_zones``).

    @staticmethod
    def _fab_poly(fn: Callable[..., Any], polygon: Any) -> list[list[float]]:
        """A fab-layer region polygon: ≥3 finite ``[x, y]`` mm points."""
        if not isinstance(polygon, list | tuple) or len(polygon) < 3:
            raise _bad_kwarg(
                fn,
                "polygon",
                f"{fn.__name__}() needs a polygon of at least 3 [x, y] points",
            )
        pts: list[list[float]] = []
        for p in polygon:
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError) as exc:
                raise _bad_kwarg(
                    fn,
                    "polygon",
                    f"{fn.__name__}(): polygon point {p!r} is not an [x, y] pair",
                ) from exc
            if not (math.isfinite(x) and math.isfinite(y)):
                raise _bad_kwarg(
                    fn,
                    "polygon",
                    f"{fn.__name__}(): polygon point {p!r} is not finite",
                )
            pts.append([x, y])
        return pts

    @staticmethod
    def _fab_side(fn: Callable[..., Any], side: Any) -> str:
        s = str(side).strip().lower()
        if s not in ("top", "bottom"):
            raise _bad_kwarg(
                fn,
                "side",
                f"{fn.__name__}(): side must be 'top' or 'bottom', got {side!r}",
            )
        return s

    @traced
    def coverlay_opening(self, polygon: list[list[float]], side: str) -> dict[str, Any]:
        """A cut-out in the ``side`` coverlay film (flex boards) — where
        the film is opened for pads/connector lands. Exports to
        ``-F_Coverlay.gbr``/``-B_Coverlay.gbr``.

        Args:
            polygon (list[list[float]]): The opening's outline — ≥3
                ``[x, y]`` mm points.
            side (str): ``'top'`` or ``'bottom'``.
        """
        f_ = {
            "ftype": "coverlay_opening",
            "layer": self._fab_side(Board.coverlay_opening, side),
            "geom": {"polygon": self._fab_poly(Board.coverlay_opening, polygon)},
        }
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    @traced
    def stiffener(
        self,
        polygon: list[list[float]],
        material: str,
        thickness_mm: float,
        side: str,
    ) -> dict[str, Any]:
        """A stiffener (FR4/polyimide/metal) bonded over ``polygon`` on
        ``side`` — where parts may sit on an otherwise bendable area.
        Exports to ``-F_Stiffener.gbr``/``-B_Stiffener.gbr``.

        Args:
            polygon (list[list[float]]): The stiffener's footprint — ≥3
                ``[x, y]`` mm points.
            material (str): Material name (``'fr4'``, ``'polyimide'``,
                …) — exported as a ``G04`` note on the file.
            thickness_mm (float): Bonded thickness — finite and > 0.
            side (str): ``'top'`` or ``'bottom'``.
        """
        material = str(material).strip()
        if not material:
            raise _bad_kwarg(
                Board.stiffener, "material", "stiffener() needs a material name"
            )
        try:
            t = float(thickness_mm)
        except (TypeError, ValueError) as exc:
            raise _bad_kwarg(
                Board.stiffener,
                "thickness_mm",
                f"stiffener(): thickness_mm {thickness_mm!r} is not a number",
            ) from exc
        if not math.isfinite(t) or t <= 0:
            raise _bad_kwarg(
                Board.stiffener,
                "thickness_mm",
                f"stiffener(): thickness_mm {t!r} must be > 0",
            )
        f_ = {
            "ftype": "stiffener",
            "layer": self._fab_side(Board.stiffener, side),
            "geom": {
                "polygon": self._fab_poly(Board.stiffener, polygon),
                "material": material,
                "thickness_mm": t,
            },
        }
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    @traced
    def adhesive(self, polygon: list[list[float]], side: str) -> dict[str, Any]:
        """An adhesive (bondply/PSA) sheet over ``polygon`` on ``side``.
        Exports to ``-F_Adhesive.gbr``/``-B_Adhesive.gbr``.

        Args:
            polygon (list[list[float]]): The sheet's outline — ≥3
                ``[x, y]`` mm points.
            side (str): ``'top'`` or ``'bottom'``.
        """
        f_ = {
            "ftype": "adhesive",
            "layer": self._fab_side(Board.adhesive, side),
            "geom": {"polygon": self._fab_poly(Board.adhesive, polygon)},
        }
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    @traced
    def tape_mask(
        self, polygon: list[list[float]], kind: str = "parylene"
    ) -> dict[str, Any]:
        """A region taped off during a conformal-coat step (``kind``
        names the coat — ``'parylene'`` is the common one). Drawn on the
        ``Dwgs.User`` fab drawing.

        Args:
            polygon (list[list[float]]): The masked region — ≥3
                ``[x, y]`` mm points.
            kind (str): Coat name — ``'parylene'`` by default.
        """
        kind = str(kind).strip()
        if not kind:
            raise _bad_kwarg(Board.tape_mask, "kind", "tape_mask() needs a coat kind")
        f_ = {
            "ftype": "tape_mask",
            "geom": {"polygon": self._fab_poly(Board.tape_mask, polygon), "kind": kind},
        }
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    @traced
    def doc_note(
        self, text: str, at: list[float], layer: str = "Dwgs.User"
    ) -> dict[str, Any]:
        """A text note stroked onto a drawings layer — ``at`` is the
        ``[x, y]`` baseline-left anchor, ``layer`` the target user layer
        (``Dwgs.User`` by default).

        Args:
            text (str): Note text — non-empty.
            at (list[float]): ``[x, y]`` baseline-left anchor, finite mm.
            layer (str): Target drawings layer — ``'Dwgs.User'`` by
                default.
        """
        text = str(text).strip()
        if not text:
            raise _bad_kwarg(Board.doc_note, "text", "doc_note() needs note text")
        layer = str(layer).strip()
        if not layer:
            raise _bad_kwarg(
                Board.doc_note, "layer", "doc_note() needs a drawings-layer name"
            )
        try:
            x, y = float(at[0]), float(at[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise _bad_kwarg(
                Board.doc_note,
                "at",
                f"doc_note(): at {at!r} is not an [x, y] pair",
            ) from exc
        if not (math.isfinite(x) and math.isfinite(y)):
            raise _bad_kwarg(
                Board.doc_note, "at", f"doc_note(): at {at!r} is not finite"
            )
        f_ = {"ftype": "doc_note", "x": x, "y": y, "layer": layer, "note": text}
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    @traced
    def bend_zone(
        self, line: list[list[float]], radius_mm: float, angle_deg: float
    ) -> dict[str, Any]:
        """A flex bend zone: ``line`` is the fold line ``[[x1, y1],
        [x2, y2]]`` across the board, bent by ``angle_deg`` around a
        ``radius_mm`` neutral radius. The zone DRC checks is the fold
        line widened by the bend allowance (``radius_mm * |angle_deg|``
        in radians of arc); vias and parts inside it, and traces
        crossing it far off perpendicular, are findings
        (:func:`precis.pcb.drc.check_bend_zones`). Exports as a
        ``Dwgs.User`` line plus a stroked radius/angle note.

        Args:
            line (list[list[float]]): The two fold-line endpoints
                ``[[x1, y1], [x2, y2]]`` — distinct, finite mm.
            radius_mm (float): Neutral bend radius — finite and > 0.
            angle_deg (float): Bend angle — non-zero, within ±360°.
        """
        if not isinstance(line, list | tuple) or len(line) != 2:
            raise _bad_kwarg(
                Board.bend_zone,
                "line",
                "bend_zone(): line is the two fold-line endpoints [[x1, y1], [x2, y2]]",
            )
        pts: list[list[float]] = []
        for p in line:
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError) as exc:
                raise _bad_kwarg(
                    Board.bend_zone,
                    "line",
                    f"bend_zone(): line endpoint {p!r} is not an [x, y] pair",
                ) from exc
            if not (math.isfinite(x) and math.isfinite(y)):
                raise _bad_kwarg(
                    Board.bend_zone,
                    "line",
                    f"bend_zone(): line endpoint {p!r} is not finite",
                )
            pts.append([x, y])
        if pts[0] == pts[1]:
            raise _bad_kwarg(
                Board.bend_zone, "line", "bend_zone(): the fold line has zero length"
            )
        try:
            r = float(radius_mm)
        except (TypeError, ValueError) as exc:
            raise _bad_kwarg(
                Board.bend_zone,
                "radius_mm",
                f"bend_zone(): radius_mm {radius_mm!r} is not a number",
            ) from exc
        if not math.isfinite(r) or r <= 0:
            raise _bad_kwarg(
                Board.bend_zone,
                "radius_mm",
                f"bend_zone(): radius_mm {r!r} must be > 0",
            )
        try:
            a = float(angle_deg)
        except (TypeError, ValueError) as exc:
            raise _bad_kwarg(
                Board.bend_zone,
                "angle_deg",
                f"bend_zone(): angle_deg {angle_deg!r} is not a number",
            ) from exc
        if not math.isfinite(a) or a == 0 or abs(a) > 360:
            raise _bad_kwarg(
                Board.bend_zone,
                "angle_deg",
                f"bend_zone(): angle_deg {a!r} — expected a non-zero bend up to ±360°",
            )
        f_ = {
            "ftype": "bend_zone",
            "geom": {"line": pts, "radius_mm": r, "angle_deg": a},
        }
        self._features.append(f_)
        _trace.record(made={"features": 1})
        return f_

    def near(self, a: Part, b: Part, within_mm: float) -> dict[str, Any]:
        """Placement measure: keep ``a`` and ``b`` within a distance.

        Args:
            a (Part): First part.
            b (Part): Second part.
            within_mm (float): Max separation, millimetres.
        """
        return self.measure("near", [a, b], bound=within_mm)

    def apart(self, a: Part, b: Part, at_least_mm: float) -> dict[str, Any]:
        """Placement measure: keep ``a`` and ``b`` at least a distance.

        Args:
            a (Part): First part.
            b (Part): Second part.
            at_least_mm (float): Min separation, millimetres.
        """
        return self.measure("apart", [a, b], bound=at_least_mm)

    def align(self, a: Part, b: Part, axis: str) -> dict[str, Any]:
        """Placement measure: align ``a`` and ``b`` on an axis.

        Args:
            a (Part): First part.
            b (Part): Second part.
            axis (str): Axis to align on, e.g. ``"x"`` or ``"y"``.
        """
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
        """A measure row (``measures[]``) — the generic primitive
        :meth:`near`/:meth:`apart`/:meth:`align` wrap.

        Args:
            metric (str): Measure name, e.g. ``"near"``.
            operands (Iterable): Parts (or refdes strings) measured.
            bound (float | None): Bound on the metric (units are the
                metric's, mm for distances).
            goal (float | None): Optimisation goal value.
            hard (bool): A hard constraint, not a soft cost.
            weight (float): Soft-constraint weight (default 1.0).
        """
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
        _trace.record(made={"measures": 1})
        return m

    @traced
    def generate(self, name: str, *, generator: str, **params: Any) -> dict[str, Any]:
        """A generator row (``generators[]``) — a named generator run
        server-side on apply.

        Args:
            name (str): Generator instance name.
            generator (str): Generator kind/id to run.
            params: Generator parameters, carried verbatim.
        """
        g = {"name": name, "generator": generator, "params": params}
        self._generators.append(g)
        _trace.record(made={"generators": 1})
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
        ``offsets`` are per-part ``(x, y, rot)`` in the group frame.

        Args:
            name (str): Group name stamped on each member.
            parts (Iterable[Part]): Members of the group.
            rigid (bool): Rigidity flag. Every persisted group is rigid
                today — ``rigid=False`` stamps NOTHING (the members stay
                independent) instead of silently building a rigid group
                the author never asked for: on taz-leaf the sheet labels
                (``rigid=False``) pinned all 50 parts through
                ``design_from_ir``'s fixed-anchor group rule. A non-rigid
                group is a declarative no-op, kept so board scripts can
                build sheet lists unconditionally.
            pattern (str | None): Pattern name stamped on each member.
            offsets (Iterable[tuple] | None): Per-part ``(x, y, rot)``
                in the group frame — must match ``parts`` 1:1.
        """
        parts = list(parts)
        offs = list(offsets) if offsets is not None else None
        if offs is not None and len(offs) != len(parts):
            raise _bad_kwarg(
                Board.group,
                "offsets",
                f"group {name!r}: {len(offs)} offsets for {len(parts)} parts",
            )
        if not rigid:
            return parts
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
        outside nets it may touch.

        Args:
            module_fn (Callable): The :func:`module`-marked function to
                run — called ``module_fn(self, **ports)``.
            prefix (str): Instance prefix — parts become
                ``<prefix>.<local>``, nets ``<prefix>/<name>``; a used
                prefix refuses.
            ports: Arguments handed to the module — the ``Net`` values
                are the only outside nets it may touch.
        """
        if prefix in self._used_prefixes:
            raise _bad_kwarg(
                Board.use,
                "prefix",
                f"prefix {prefix!r} already used on board {self.slug!r}",
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
        ``{pin name: net name}`` map see :meth:`pin_nets`.

        Args:
            refdes (str): Module-qualified refdes — an unknown one
                refuses.
        """
        if refdes not in self._parts:
            raise _bad_kwarg(
                Board.nets_of,
                "refdes",
                f"nets_of: unknown refdes {refdes!r} — board {self.slug!r} "
                f"has {len(self._part_order)} parts",
            )
        return sorted({c["net"] for c in self._connections if c["refdes"] == refdes})

    def pin_nets(self, refdes: str) -> dict[str, str]:
        """``{pin name: net name}`` — the pin → net map for every
        connection naming ``refdes`` (module-qualified), in the part's
        pin order. (Not ``pin_map``: that name is the footprint
        pad→pin-name map in the ``pcb_apply`` payload.)

        Args:
            refdes (str): Module-qualified refdes — an unknown one
                refuses.
        """
        if refdes not in self._parts:
            raise _bad_kwarg(
                Board.pin_nets,
                "refdes",
                f"pin_nets: unknown refdes {refdes!r} — board {self.slug!r} "
                f"has {len(self._part_order)} parts",
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
        and :meth:`pin_nets` (its pin → net map).

        Args:
            net (str | Net): The net's name or the :class:`Net` — an
                unknown name refuses.
        """
        name = net.name if isinstance(net, Net) else str(net)
        if name not in self._nets:
            raise _bad_kwarg(
                Board.pins_on,
                "net",
                f"pins_on: unknown net {name!r} — board {self.slug!r} "
                f"has {len(self._net_order)} nets",
            )
        return [(c["refdes"], c["pin"]) for c in self._connections if c["net"] == name]

    def check(self) -> CheckResult:
        """Pure, offline L0 checks (§Q1): parts with no catalog part and no
        authored footprint are NOT-CHECKED (a synthesized landpattern
        bound); a declared net with zero connections, a connection to a
        part the board never made, a part naming an unregistered
        footprint, a pin name no known pad answers to, and a declared
        pin left unwired without an intent marker are findings. No
        geometry, no DRC — that stays server-side (§DRC)."""
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
                        site=self._parts[r].site,
                        message=f"footprint {fp!r} is not registered on this board",
                    )
                )
        # A net tied only to solver slots is bound, not floating: the
        # solver wires the concrete pin at assign time (gr516695).
        connected = {c["net"] for c in self._connections} | {
            fp.net
            for r in self._part_order
            for fp in self._parts[r]._flex_slots
            if fp.net is not None
        }
        findings += [
            CheckFinding(
                code="net.unconnected",
                net=n,
                site=self._nets[n].site,
                message=f"net {n!r} has no connections",
            )
            for n in self._net_order
            if n not in connected
        ]
        findings += [
            CheckFinding(
                code="net.undeclared",
                net=c["net"],
                refdes=c["refdes"],
                pin=c["pin"],
                site=site,
                message=f"connection {c['refdes']}.{c['pin']} names an undeclared net",
            )
            for c, site in zip(self._connections, self._conn_sites)
            if c["net"] not in self._nets
        ]
        findings += [
            CheckFinding(
                code="part.unknown",
                refdes=c["refdes"],
                net=c["net"],
                pin=c["pin"],
                site=site,
                message=(
                    f"connection {c['refdes']}.{c['pin']} names a part this "
                    "board never made"
                ),
            )
            for c, site in zip(self._connections, self._conn_sites)
            if c["refdes"] not in self._parts
        ]
        findings += self._pin_name_findings()
        findings += self._unwired_pin_findings()
        findings += self._pin_requirement_findings()
        from precis_geom.pcb_motifs import motif_findings

        findings += motif_findings(self)
        findings += self._over_voltage_findings()
        findings += self._source_findings()
        return CheckResult(
            findings=findings,
            not_checked=not_checked,
            not_checked_groups=groups,
        )

    def _pin_universe(self, part: Part) -> tuple[set[str], str] | None:
        """``(pin names, provenance label)`` a part's pads declare —
        the offline pin universe ``pin.unknown`` checks against
        (gr521597). A registered authored :class:`Footprint` answers
        with ``pin_map``-resolved pad labels — the ``pad_label`` rule:
        the map entry's ``name`` wins, else the pad number
        (``"number"`` key on store-normalized pads, ``"pin"`` on
        builder-authored ones). A resolved :class:`CatalogPart` answers
        with its ``pins``. ``None`` when nothing offline names the
        pins — an lcsc-less/footprint-less part is already
        ``not_checked``, and an un-read catalog ref names nothing."""
        fp = self._footprints.get(str(part.footprint or ""))
        if fp is not None:
            pin_map = fp.pin_map or {}
            names: set[str] = set()
            for pad_ in fp.pads:
                num = pad_.get("number", pad_.get("pin"))
                if num is None:
                    continue
                entry = pin_map.get(str(num))
                if isinstance(entry, dict):
                    names.add(str(entry.get("name") or num))
                elif entry is not None:
                    names.add(str(entry))
                else:
                    names.add(str(num))
            if names:
                return names, f"footprint {fp.name!r} ({len(fp.pads)} pads)"
        cp = part._catalog
        if cp is not None and cp.resolved and cp.pins:
            return {str(p) for p in cp.pins}, f"catalog part {cp.lcsc}"
        return None

    def _pin_name_findings(self) -> list[CheckFinding]:
        """``pin.unknown`` (gr521597): every name the script minted on a
        part — wired, declared, claimed or function-tabled — must
        resolve to a pad while the part's pin universe is known. The
        server catches the same defect as ``pin_name_mismatch`` — only
        late, and only when the footprint is cached; a typo'd
        ``u1['VBD']`` is a build-time finding here."""
        out: list[CheckFinding] = []
        for r in self._part_order:
            part = self._parts[r]
            universe = self._pin_universe(part)
            if universe is None:
                continue
            names, source = universe
            shown = ", ".join(sorted(names)[:8])
            if len(names) > 8:
                shown += f", … (+{len(names) - 8})"
            for name in sorted(set(part._pins) | set(part._pin_functions)):
                if name in names:
                    continue
                pin = part._pins.get(name)
                site = (pin.site if pin is not None else None) or (
                    part._pin_function_sites.get(name)
                )
                out.append(
                    CheckFinding(
                        code="pin.unknown",
                        refdes=r,
                        pin=name,
                        site=site,
                        message=(
                            f"pin {name!r} matches no pad on {source} (known: {shown})"
                        ),
                    )
                )
        return out

    @staticmethod
    def _pin_intent(pin: Pin) -> str | None:
        """The §1j marker a pin carries — ``"nc"``/``"must_terminate"``/
        ``"unused"``/``None``, the same resolution order
        ``precis.pcb.drc._pin_marker`` uses on the store side: the
        builder's ``markers`` bools OR ``meta`` bools OR the word in
        ``tags``; ``nc`` outranks ``must_terminate`` (a no-internal-
        connection pad cannot be terminated), which outranks
        ``unused``."""
        for marker in ("nc", "must_terminate", "unused"):
            if pin.markers.get(marker) or pin.meta.get(marker) or marker in pin.tags:
                return marker
        return None

    def _unwired_pin_findings(self) -> list[CheckFinding]:
        """``pin.unwired`` (gr521598): a pin that will exist in the
        batch — declared (``pins=``/``pin()``/``declare()``/swap group/
        flex candidate) or annotated — but tied to no net needs a stated
        reason. Mirrors the server's ``unconnected_pin`` rule
        (:func:`precis.pcb.drc.check_unconnected_pins`): ``nc`` and
        ``unused`` silence, ``must_terminate`` always complains, and a
        candidate of a *bound* flex slot waits on the solver so it does
        not complain. A slot that was never tied flags once by name —
        its candidates float regardless of which the solver would pick."""
        wired = {(c["refdes"], c["pin"]) for c in self._connections}
        out: list[CheckFinding] = []
        for r in self._part_order:
            part = self._parts[r]
            # Slot candidates never flag individually: a bound slot's
            # candidate waits on the solver, an unbound one's float is
            # the slot finding below — one row per root cause.
            slot_candidates = {cand for fp in part._flex_slots for cand in fp.pins}
            for fp in part._flex_slots:
                if fp.net is not None:
                    continue
                out.append(
                    CheckFinding(
                        code="pin.unwired",
                        refdes=r,
                        pin=fp.name,
                        site=fp.site,
                        message=(
                            f"flex slot {fp.handle} was never tied to a "
                            f"net — its candidate pins {list(fp.pins)} float"
                        ),
                    )
                )
            for name in part._pin_order:
                if name not in part._declared_pins and not part._pins[name].annotated:
                    continue
                if (r, name) in wired or name in slot_candidates:
                    continue
                marker = self._pin_intent(part._pins[name])
                if marker == "must_terminate":
                    message = (
                        f"{r}.{name} is marked must_terminate and has no "
                        "net — a pin that must not float is floating "
                        "(the unused marker does not exempt it)"
                    )
                elif marker is None:
                    message = (
                        f"{r}.{name} is declared but wired to no net — "
                        "wire it, or mark it nc (no internal connection) / "
                        "unused (this board does not use it) / "
                        "must_terminate (it must not float)"
                    )
                else:
                    continue
                out.append(
                    CheckFinding(
                        code="pin.unwired",
                        refdes=r,
                        pin=name,
                        site=part._pins[name].site,
                        message=message,
                    )
                )
        return out

    def _resolve_source(
        self, ref: str, chunk: str, client: Any
    ) -> tuple[str, str | None]:
        """``("ok", part_lcsc | None)`` when ``ds:ref~chunk`` resolves,
        ``("unresolvable", None)`` on a clear not-found reply, and
        ``("unchecked", reason)`` on every other read failure — cached
        in ``_source_cache`` per distinct handle. The part link comes
        from the ``status`` view's ``      part <lcsc>`` row; an
        unlinked datasheet or a failed status read is ``("ok", None)``
        — the handle exists, its ownership just can't be told."""
        key = f"{ref}~{chunk}"
        if key in self._source_cache:
            return self._source_cache[key]
        try:
            client.call("get", kind="datasheet", id=key)
        except Exception as exc:
            reason = _source_failure_reason(exc)
            out: tuple[str, str | None] = (
                ("unresolvable", None) if reason is None else ("unchecked", reason)
            )
            self._source_cache[key] = out
            return out
        lcsc = None
        try:
            status = client.call("search", kind="datasheet", view="status", ids=[ref])
            m = re.search(r"^\s+part\s+(C\d+)", str(status), re.M)
            lcsc = m.group(1) if m else None
        except Exception:
            pass
        out = ("ok", lcsc)
        self._source_cache[key] = out
        return out

    def _source_findings(self) -> list[CheckFinding]:
        """``source=`` provenance (gr521603): every recorded handle —
        a part's ``pin_functions_source``, a connection's ``source``,
        a flex slot's — must parse as ``ds:<ref>~<chunk>`` and resolve
        through the staged ``precis_client``; when the chunk's
        datasheet is linked to a different catalog part than the one
        making the claim it is ``source.wrong_part``. Existence and
        kind are checked; whether the chunk supports the claim stays
        manual. A clear not-found reply is ``source.unresolvable``;
        every other read failure (a kind refusal — the sandbox
        read-only MCP never serves ``datasheet`` — a transport or
        import failure, an empty reply) is ``source.unchecked``
        cannot-check, never a false unresolvable. With no client
        staged (a laptop run) the well-formed handles report ONE
        ``not_checked`` row — cannot-check, not clean."""
        handles: list[tuple[str, str | None, Part | None]] = []
        for r in self._part_order:
            part = self._parts[r]
            if part._pin_functions_source is not None:
                handles.append(
                    (
                        part._pin_functions_source,
                        part._pin_functions_source_site,
                        part,
                    )
                )
            for fp in part._flex_slots:
                if fp.source is not None:
                    handles.append((fp.source, fp.source_site or fp.site, part))
        for c, c_site in zip(self._connections, self._conn_sites):
            if c.get("source") is not None:
                handles.append((str(c["source"]), c_site, self._parts.get(c["refdes"])))
        if not handles:
            return []
        out: list[CheckFinding] = []
        todo: list[tuple[str, str, str | None, Part | None]] = []
        for handle, site, owner in handles:
            m = _SOURCE_HANDLE_RE.match(str(handle))
            if m is None:
                out.append(
                    CheckFinding(
                        code="source.unresolvable",
                        refdes=owner.refdes if owner is not None else None,
                        site=site,
                        message=(
                            f"source {handle!r} does not parse as ds:<ref>~<chunk>"
                        ),
                    )
                )
            else:
                todo.append((m.group(1), m.group(2), site, owner))
        if not todo:
            return out
        try:
            client = importlib.import_module("precis_client")
        except Exception as exc:
            why = (
                "no precis_client is staged on a laptop run"
                if isinstance(exc, ImportError)
                else f"precis_client failed to import — {type(exc).__name__}: {exc}"
            )
            out.append(
                CheckFinding(
                    code="source.unchecked",
                    site=todo[0][2],
                    not_checked=True,
                    message=(
                        f"{len(todo)} source= handle(s) CANNOT be checked — "
                        f"{why}; provenance stays unverified until the "
                        "sandbox resolves it"
                    ),
                )
            )
            return out
        for ref, chunk, site, owner in todo:
            status, info = self._resolve_source(ref, chunk, client)
            if status == "unresolvable":
                out.append(
                    CheckFinding(
                        code="source.unresolvable",
                        refdes=owner.refdes if owner is not None else None,
                        site=site,
                        message=(
                            f"source 'ds:{ref}~{chunk}' does not resolve — "
                            "no such datasheet chunk"
                        ),
                    )
                )
            elif status == "unchecked":
                out.append(
                    CheckFinding(
                        code="source.unchecked",
                        refdes=owner.refdes if owner is not None else None,
                        site=site,
                        not_checked=True,
                        message=(
                            f"source 'ds:{ref}~{chunk}' CANNOT be checked — {info}"
                        ),
                    )
                )
            elif (
                info is not None
                and owner is not None
                and owner.lcsc is not None
                and info != owner.lcsc
            ):
                out.append(
                    CheckFinding(
                        code="source.wrong_part",
                        refdes=owner.refdes,
                        site=site,
                        message=(
                            f"source 'ds:{ref}~{chunk}' is {info}'s "
                            f"datasheet but {owner.refdes} is {owner.lcsc}"
                        ),
                    )
                )
        return out

    def _pin_bound(self, part: Part | None, pin: str, key: str) -> float | None:
        """A pin's declared ``key`` bound — the part's ``pin_functions``
        entry first, then the pin's ``meta`` (the same precedence the
        current-axis ``pin_rating`` keeps). ``None`` when neither names
        it — a bound is never guessed."""
        if part is None:
            return None
        bound = (part._pin_functions.get(pin) or {}).get(key)
        p = part._pins.get(pin)
        if bound is None and p is not None:
            bound = p.meta.get(key)
        return float(bound) if bound is not None else None

    def _over_voltage_findings(self) -> list[CheckFinding]:
        """``over_voltage`` (gr521607) — the voltage-axis mirror the
        server ERC keeps (:func:`precis.pcb.drc.check_over_voltage`),
        same rule name: a pin on a net whose working voltage exceeds
        the pin's declared ``max_voltage_v``. The net's voltage is
        ``b.net(voltage=)`` else its class's ``working_voltage_v``;
        absolute values compare (a −12 V rail is just as over). A bound
        pin on a net nobody declared a voltage for is the per-net
        never-guess ``not_checked`` row — ask the user, never estimate.
        A board with no ``max_voltage_v`` anywhere gets silence, not
        the server rule's vocabulary-coverage row: that row would sit
        permanently on every board that never opted into voltage
        bounds, and an un-actionable line trains the reader to ignore
        the report."""
        out: list[CheckFinding] = []

        def net_voltage(name: str) -> float | None:
            n = self._nets.get(name)
            if n is None:
                return None
            if n.voltage is not None:
                return float(n.voltage)
            rules = self._net_classes.get(n.cls or "")
            v = (
                (rules or {}).get("working_voltage_v")
                if isinstance(rules, dict)
                else None
            )
            return None if v is None else float(v)

        conn_site = {
            (c["refdes"], c["pin"], c["net"]): s
            for c, s in zip(self._connections, self._conn_sites)
        }
        members: dict[str, list[str]] = {}
        nets = {c["net"] for c in self._connections}
        for n in sorted(nets):
            v = net_voltage(n)
            unbounded: list[str] = []
            for r, p in self.pins_on(n):
                bound = self._pin_bound(self._parts.get(r), p, "max_voltage_v")
                if bound is None:
                    continue
                if v is None:
                    unbounded.append(f"{r}/{p}")
                elif abs(bound) < abs(v):
                    out.append(
                        CheckFinding(
                            code="over_voltage",
                            refdes=r,
                            net=n,
                            pin=p,
                            site=conn_site.get((r, p, n)),
                            message=(
                                f"net {n} works at {v:g} V but {r}.{p} is "
                                f"rated {bound:g} V max"
                            ),
                        )
                    )
            members[n] = unbounded
        for r in self._part_order:
            part = self._parts[r]
            for fp in part._flex_slots:
                if fp.net is None:
                    continue
                v = net_voltage(fp.net)
                rated = [
                    (p, b_)
                    for p in fp.pins
                    if (b_ := self._pin_bound(part, p, "max_voltage_v")) is not None
                ]
                if not rated:
                    continue
                if v is None:
                    members.setdefault(fp.net, []).append(f"{r}/{fp.name or fp.handle}")
                elif all(abs(b_) < abs(v) for _, b_ in rated):
                    out.append(
                        CheckFinding(
                            code="over_voltage",
                            refdes=r,
                            net=fp.net,
                            pin=fp.name,
                            site=fp.tie_site or fp.site,
                            message=(
                                f"slot {fp.name or fp.handle}: every rated "
                                f"candidate pin sits under {abs(v):g} V"
                            ),
                        )
                    )
        for n, unbounded in members.items():
            if unbounded:
                net = self._nets.get(n)
                out.append(
                    CheckFinding(
                        code="over_voltage",
                        net=n,
                        site=net.site if net is not None else None,
                        not_checked=True,
                        message=(
                            f"voltage unknown for net {n}: ask the user — "
                            f"pin(s) {', '.join(unbounded)} declare a bound "
                            "nothing on the net answers"
                        ),
                    )
                )
        return out

    def _pin_requirement_findings(self) -> list[CheckFinding]:
        """The topo-place electrical check (Decision 15): a ``current=``
        requirement is checkable, not a comment. A wired or candidate
        pin whose declared ``max_current_a`` (pin meta or the part's
        ``pin_functions`` entry) sits under the requirement is
        ``over_current`` — the same rule name the server ERC uses.
        Current-path topology (``led_without_limit``) lives in the
        netlist motif registry alongside buck and LDO checks."""
        reqs = [
            c
            for c in self._connections
            if c.get("current_max_a") is not None or c.get("current_min_a") is not None
        ]
        slot_reqs = [
            fp
            for r in self._part_order
            for fp in self._parts[r]._flex_slots
            if fp.requirement is not None and fp.net is not None
        ]
        if not reqs and not slot_reqs:
            return []
        out: list[CheckFinding] = []

        def pin_rating(refdes: str, pin: str) -> float | None:
            return self._pin_bound(self._parts.get(refdes), pin, "max_current_a")

        # The site an over_current points at is the tie that declared the
        # bound — the line to fix — not the under-rated pin's own tie.
        req_site: dict[str, str] = {}
        for c, s in zip(self._connections, self._conn_sites):
            if (
                c.get("current_max_a") is not None or c.get("current_min_a") is not None
            ) and c["net"] not in req_site:
                req_site[c["net"]] = s

        # Requirements are per connection but the finding is per pin per
        # net: N ties carrying current= on one net name the same
        # under-rated pin once, against the strictest bound (gr516696).
        net_max: dict[str, float] = {}
        for c in reqs:
            bound = c.get("current_max_a")
            if bound is None:
                continue
            net = c["net"]
            net_max[net] = min(float(bound), net_max.get(net, float("inf")))
        for net, bound in net_max.items():
            for r, p in self.pins_on(net):
                rating = pin_rating(r, p)
                if rating is not None and rating < bound:
                    out.append(
                        CheckFinding(
                            code="over_current",
                            refdes=r,
                            net=net,
                            pin=p,
                            site=req_site.get(net),
                            message=(
                                f"net {net} requires ≤ {bound:g} A "
                                f"but {r}.{p} is rated {rating:g} A"
                            ),
                        )
                    )
        for fp in slot_reqs:
            if fp.requirement is None or fp.requirement.kind != "max":
                continue
            bound = fp.requirement.amps
            ratings = [(p, pin_rating(fp.part.refdes, p)) for p in fp.pins]
            # Unrated pins are usable — Decision 15 filters only "where the
            # library has it". Every RATED candidate under the bound leaves
            # the slot no legal pin.
            if ratings and all(r_ is not None and r_ < bound for _, r_ in ratings):
                out.append(
                    CheckFinding(
                        code="over_current",
                        refdes=fp.part.refdes,
                        net=fp.net,
                        site=fp.tie_site or fp.site,
                        message=(
                            f"slot {fp.name or fp.handle}: every candidate pin "
                            f"is rated under {bound:g} A"
                        ),
                    )
                )

        return out

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
        default 1.6 until board thickness is real data (pass A).

        Args:
            thickness_mm (float | None): Board thickness override, mm —
                default 1.6 until thickness is real data.
        """
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
        if self._planes:
            out["planes"] = [dict(plane) for plane in self._planes]
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

    # -- step-cache round trip (precis_geom.steps) ---------------------------

    def cache_state(self) -> dict[str, Any]:
        """What :meth:`to_batch` leaves out and a rebuilt board still
        needs — the JSON-safe sidecar of a cached board step
        (:mod:`precis_geom.steps`): ``slug``/``stackup``/``sides``, every
        ``file:line`` site (``check`` findings cite them), the catalog
        reads (the manifest's ``parts``) and the resolved
        :class:`CatalogPart` behind each part (``check`` reads its pin
        universe). :meth:`from_batch` is the inverse; the pair is lossless
        for ``to_batch()`` (the pinned guarantee) and for ``check()``."""
        parts: dict[str, Any] = {}
        for refdes, p in self._parts.items():
            cp = p._catalog
            parts[refdes] = {
                "site": p.site,
                "pins": {n: p._pins[n].site for n in p._pin_order},
                "fn_sites": dict(p._pin_function_sites),
                "fn_source_site": p._pin_functions_source_site,
                "catalog": (
                    {f_.name: getattr(cp, f_.name) for f_ in fields(cp)}
                    if cp is not None
                    else None
                ),
                "slots": [
                    {"tie_site": s.tie_site, "source_site": s.source_site}
                    for s in p._flex_slots
                ],
            }
        return {
            "slug": self.slug,
            "stackup": self.stackup,
            "sides": self.sides,
            "parts": parts,
            "nets": {n: self._nets[n].site for n in self._net_order},
            "conn_sites": list(self._conn_sites),
            "catalog_reads": copy.deepcopy(self._catalog_reads),
            "used_prefixes": sorted(self._used_prefixes),
        }

    @classmethod
    def from_batch(
        cls, batch: dict[str, Any], state: dict[str, Any] | None = None
    ) -> Board:
        """Rebuild a board from a :meth:`to_batch` document (+ the
        :meth:`cache_state` sidecar) — the load half of a cached board
        step. ``to_batch()`` of the result equals ``batch`` (key order and
        values; JSON turns tuples into lists, so compare canonical JSON).
        A row's ``frame`` becomes the part/net ``maker``. Without a
        ``state`` the slug is ``"board"`` and every site is ``"?:0"``."""
        st = state or {}
        sites_p: dict[str, Any] = st.get("parts") or {}
        b = cls(str(st.get("slug") or "board"), profile=batch.get("profile"))
        b.stackup = st.get("stackup")
        b.sides = st.get("sides", "both")
        b.meta = copy.deepcopy(batch.get("meta") or {})
        for row in batch.get("components", ()):
            ps = sites_p.get(row["refdes"]) or {}
            p = Part(
                board=b,
                refdes=row["refdes"],
                lcsc=row.get("part"),
                label=row.get("label"),
                footprint=row.get("footprint"),
                value=row.get("value"),
                height_mm=row.get("height_mm"),
                roles=row.get("roles", ()),
                note=row.get("note"),
                part_meta=copy.deepcopy(row.get("part_meta")),
                supplies=copy.deepcopy(row.get("supplies")),
                pin_swap_groups=copy.deepcopy(row.get("pin_swap_groups")),
                site=ps.get("site") or "?:0",
            )
            p.maker = row.get("frame")
            cat = ps.get("catalog")
            if cat is not None:
                p._catalog = CatalogPart(**cat)
            for prow in row.get("pins", ()):
                pin = Pin(
                    part=p,
                    name=str(prow["name"]),
                    site=(ps.get("pins") or {}).get(str(prow["name"])) or "?:0",
                )
                for marker in _PIN_MARKERS:
                    if prow.get(marker):
                        pin.markers[marker] = True
                pin.tags = list(prow.get("tags", ()))
                pin.meta = copy.deepcopy(prow.get("meta") or {})
                pin.extra = copy.deepcopy(
                    {
                        k: v
                        for k, v in prow.items()
                        if k not in ("name", "tags", "meta", *_PIN_MARKERS)
                    }
                )
                p._pins[pin.name] = pin
                p._pin_order.append(pin.name)
                p._declared_pins.add(pin.name)
            if "x" in row:
                p.x, p.y = row["x"], row.get("y")
                p.rot = row.get("rot", 0.0)
                p.side = row.get("layer", "top")
            p.fixed = row.get("fixed")
            p.max_travel_mm = row.get("max_travel_mm")
            p.edge_mate = copy.deepcopy(row.get("edge_mate"))
            p.group = row.get("group")
            p.group_offset = copy.deepcopy(row.get("group_offset"))
            p.pattern = row.get("pattern")
            p.pattern_instance = row.get("pattern_instance")
            decls = row.get("pin_decls") or {}
            for c in decls.get("claims", ()):
                p._pin_claims[str(c["pin"])] = {"name": c["name"], "site": c["site"]}
            slot_sites = ps.get("slots") or []
            for i, slot in enumerate(decls.get("slots", ())):
                extra = slot_sites[i] if i < len(slot_sites) else {}
                fp = FlexPin(
                    part=p,
                    pins=tuple(slot["pins"]),
                    name=slot.get("name"),
                    site=slot.get("site") or "?:0",
                )
                fp.net = slot.get("net")
                if "requirement_kind" in slot:
                    fp.requirement = Bound(
                        slot["requirement_kind"],
                        slot["requirement_a"],
                        slot["requirement"],
                    )
                fp.source = slot.get("source")
                fp.tie_site = extra.get("tie_site")
                fp.source_site = extra.get("source_site")
                p._flex_slots.append(fp)
            p._swap_groups = copy.deepcopy(decls.get("swap_groups") or [])
            p._pin_functions = copy.deepcopy(decls.get("pin_functions") or {})
            p._pin_functions_source = decls.get("pin_functions_source")
            p._pin_function_sites = dict(ps.get("fn_sites") or {})
            p._pin_functions_source_site = ps.get("fn_source_site")
            b._parts[p.refdes] = p
            b._part_order.append(p.refdes)
        sites_n: dict[str, Any] = st.get("nets") or {}
        for row in batch.get("nets", ()):
            n = Net(
                board=b,
                name=row["name"],
                cls=row.get("class"),
                current=row.get("current"),
                voltage=row.get("working_voltage_v"),
                impedance=row.get("impedance"),
                edge_rate=row.get("edge_rate"),
                function=row.get("function"),
                note=row.get("note"),
                domain=row.get("domain"),
                width_mm=row.get("width_mm"),
                meta=copy.deepcopy(row.get("meta")),
                site=sites_n.get(row["name"]) or "?:0",
            )
            n.maker = row.get("frame")
            b._nets[n.name] = n
            b._net_order.append(n.name)
        b._connections = [dict(c) for c in batch.get("connections", ())]
        conn_sites = list(st.get("conn_sites") or [])
        conn_sites += ["?:0"] * (len(b._connections) - len(conn_sites))
        b._conn_sites = conn_sites[: len(b._connections)]
        b._features = copy.deepcopy(batch.get("features") or [])
        b._planes = [dict(pl) for pl in batch.get("planes", ())]
        for row in batch.get("footprints", ()):
            fpr = Footprint(
                row["name"],
                copy.deepcopy(row["pads"]),
                pin_map=copy.deepcopy(row.get("pin_map")),
                courtyard=copy.deepcopy(row.get("courtyard")),
                centroid=copy.deepcopy(row.get("centroid")),
                note=row.get("note"),
                holes=copy.deepcopy(row.get("holes")),
                graphics=copy.deepcopy(row.get("graphics")),
            )
            b._footprints[fpr.name] = fpr
            b._footprint_order.append(fpr.name)
        b._net_classes = copy.deepcopy(batch.get("net_classes") or {})
        for name, rrow in (batch.get("roles") or {}).items():
            b._roles[name] = Role(name=rrow["name"], voltage=rrow.get("voltage"))
        b._generators = copy.deepcopy(batch.get("generators") or [])
        b._measures = copy.deepcopy(batch.get("measures") or [])
        b._catalog_reads = copy.deepcopy(st.get("catalog_reads") or {})
        b._used_prefixes = set(st.get("used_prefixes") or ())
        return b

    def manifest(
        self,
        requests: Iterable[dict[str, Any]] = (),
        *,
        check: CheckResult | None = None,
    ) -> dict[str, Any]:
        """The workspace run's ``out/manifest.json`` document (§Import
        boundary): ``{schema, kind: 'pcb', board, requests}`` plus the
        run's provenance — ``parts`` records every catalog footprint
        :func:`catalog` resolved (R-e: the ids read + a sha + when — and
        ``{provenance: 'unresolved', error}`` when the read failed) and
        ``precis_geom`` the wheel's own version. ``check`` records the
        :meth:`check` outcome — ``{findings, codes, not_checked,
        unverifiable}``, where ``findings``/``codes`` count real rows
        only and ``not_checked`` the cannot-check rows (unresolved
        parts plus ``not_checked`` findings; ``unverifiable`` is the
        finding-level subset) — so a failing board's manifest differs
        from a clean one's and a cannot-check one reads as neither
        (gr521604).

        Args:
            requests (Iterable[dict]): The run's request rows, carried
                into ``requests[]``.
            check (CheckResult | None): A pre-run :meth:`check` result —
                ``None`` records ``check: "not run"`` (``run()`` hands
                its own over so the manifest and ``check.txt`` quote
                the same pass); ``manifest`` never runs :meth:`check`
                itself — a ``source=`` resolution would be a hidden
                client round-trip.
        """
        # Unattached reads bind to this board now — claimed once, so they
        # cannot leak into a second board's manifest.
        for lcsc in sorted(_CATALOG_READS):
            self._catalog_reads.setdefault(lcsc, _CATALOG_READS.pop(lcsc))
        check_block: str | dict[str, Any] = "not run"
        if check is not None:
            real = [f for f in check.findings if not f.not_checked]
            unverifiable = len(check.findings) - len(real)
            check_block = {
                "findings": len(real),
                "codes": list(dict.fromkeys(f.code for f in real)),
                "not_checked": len(check.not_checked) + unverifiable,
                "unverifiable": unverifiable,
            }
        return {
            "schema": MANIFEST_SCHEMA,
            "kind": "pcb",
            "board": self.to_batch(),
            "requests": [dict(r) for r in requests],
            "parts": [self._catalog_reads[k] for k in sorted(self._catalog_reads)],
            "precis_geom": {"version": _wheel_version()},
            "check": check_block,
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
    check: CheckResult | None = None,
) -> Path:
    """Write ``manifest.json`` (and ``trace.json`` when a tracer is live)
    under ``out_dir``; returns the manifest path. ``check`` is a pre-run
    :meth:`Board.check` result carried into the manifest's ``check``
    block — ``None`` records ``"not run"``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "manifest.json"
    path.write_text(
        json.dumps(board.manifest(requests, check=check), sort_keys=True, indent=1)
        + "\n",
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
    ``board.svg``/``bom.csv``/``cpl.csv``) via :func:`local`. ``--route``
    no longer routes — routing is retired, so the run places and reports
    "no router yet". A missing kernel refuses the flagged run and writes
    the base set for a plain one. The check outcome is recorded in the
    manifest's ``check`` block and echoed on stdout — a failing board
    never lands silently (gr521604)."""
    import sys

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    check = board.check()
    write_manifest(board, str(out), check=check)
    check_path = out / "check.txt"
    check_path.write_text(check.summary() + "\n", encoding="utf-8")
    real = [f for f in check.findings if not f.not_checked]
    echo = f"check: {len(real)} finding(s)"
    codes = ", ".join(dict.fromkeys(f.code for f in real))
    if codes:
        echo += f" ({codes})"
    cannot_check = len(check.not_checked) + (len(check.findings) - len(real))
    if cannot_check:
        echo += f", {cannot_check} not-checked"
    print(f"{echo} — {check_path}")
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
    "Bound",
    "CatalogPart",
    "CheckFinding",
    "CheckResult",
    "FitResult",
    "FlexPin",
    "Footprint",
    "Max",
    "Min",
    "Net",
    "Part",
    "PcbBuilderError",
    "Pin",
    "PinConflict",
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
