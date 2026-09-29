"""Write-time vetting for the chain declarations — the shape rules the
migration deliberately does not encode.

Same posture as :mod:`precis_se.atomic.vocab` (the atomic mode's L2
vocabulary) and :mod:`precis_se.fret`'s validators: this module owns the
*shape* of ``se_blocks.chain`` and of a ``kind='domain'``
:class:`~precis_se.chain.vocab.DomainSpec` row, raises :class:`ChainError`
on a bad one, and the ops layer (:data:`precis_se.ops._OPS`) re-raises that
as its own ``OpError`` so a rejected ``declare_helix`` reads like every
other rejected op. Store-free and tree-free by design — it vets payloads,
not designs; a declaration that is *shaped* right but references a block
that is not there is a read-time DRC finding
(``chain_dangling_domain``), never a write-time rejection.

**Lengths and angles come in with units and go out in metres/radians.**
The one boundary conversion lives here, through
:func:`precis.utils.units.parse_quantity` with ``require_unit=True``, so a
bare number raises :class:`~precis.utils.units.UnitRequiredError` — which
this module deliberately does **not** catch or rewrap: its hint ("3 —
state units: '3 nm'? '3 m'?") is the whole point, and burying it inside a
generic op error would lose the one-edit retry. Storage keys carry the
``_m`` suffix wherever the bare key would read as unit-less.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from precis.utils.units import parse_quantity
from precis_se.chain import nucleic

#: ``chain.role`` values. ``helix`` carries geometry, ``strand`` carries
#: the route's chemistry, ``segment`` is a ``layout_chain`` child
#: (``<helix>.s<k>``) and is written by the op, never by a human.
HELIX_ROLE = "helix"
STRAND_ROLE = "strand"
SEGMENT_ROLE = "segment"
ROLES = (HELIX_ROLE, STRAND_ROLE, SEGMENT_ROLE)

#: Keys a stored helix record may carry.
_HELIX_KEYS = frozenset(
    {
        "role",
        "motif",
        "nucleic",
        "path",
        "n_units",
        "phase0",
        "register",
        "min_bend_radius_m",
        "min_gap_m",
    }
)
#: Keys a stored strand record may carry.
_STRAND_KEYS = frozenset({"role", "sequence", "nucleic"})
#: Keys a stored segment record may carry.
_SEGMENT_KEYS = frozenset({"role", "helix", "ord", "start", "end"})
#: Keys a domain row's ``meta`` may carry.
_DOMAIN_META_KEYS = frozenset(
    {
        "ord",
        "forward",
        "start",
        "end",
        "geometry",
        "overrides",
        "loop_before_nt",
        "loop_curve",
    }
)

#: Units per ``layout_chain`` segment when the helix declares no lattice
#: (a lattice's own ``pitch_units`` is the default when it does — "one
#: lattice repeat"). 21 bp is the
#: honeycomb repeat, ~7 nm of duplex: small enough that a curved helix's
#: chord tracks its centre line, big enough that a 7 kb design is a few
#: hundred children rather than thousands.
DEFAULT_SEGMENT_UNITS = 21


class ChainError(ValueError):
    """A malformed chain declaration (an unknown motif, a domain whose
    ``end`` precedes its ``start``).

    Raised by this module's validators; :mod:`precis_se.ops` catches it and
    re-raises as ``OpError``. A :class:`~precis.utils.units.
    UnitRequiredError` is NOT one of these and passes straight through —
    module docstring.
    """


@dataclass
class DomainSpec:
    """One ordered stretch of a strand along a helix — a
    ``se_topology`` row with ``kind='domain'`` (migration
    ``0015_se_chain.sql``).

    ``strand``/``helix`` are block *names* (the subject/object of the
    topology row), resolved to uids only at persist time like every other
    se cross-reference. ``ord`` is the 5'→3' index within the strand and is
    the row's identity together with ``strand`` — a strand crosses the
    same helix twice in any real origami, so the block pair is not unique.

    ``start``/``end`` are helix **offsets** (unit indices) with
    ``start < end``, half-open, exactly scadnano's convention; ``forward``
    says which way the strand runs through them, so the strand enters at
    ``start`` and leaves at ``end - 1`` when forward, and the other way
    round when not. ``loop_before_nt`` is the number of unpaired
    nucleotides between the PREVIOUS domain and this one (``0`` is a real
    answer — a zero-nt crossover is a connection with one bond of reach,
    :mod:`precis_chain.loop`); ``None`` on ``ord == 0``, where there is no
    previous domain. ``geometry`` is a declared Leontis–Westhof family for
    the whole domain and ``overrides`` a per-offset map of the same
    (2026-09-28 decision: per-position geometry is an override, never a
    1-bp domain, so the domain count stays scadnano's).
    """

    strand: str
    helix: str
    ord: int
    forward: bool
    start: int
    end: int
    geometry: str | None = None
    overrides: dict[str, Any] | None = None
    loop_before_nt: int | None = None
    #: The sampled curve of the loop that PRECEDES this domain, points in
    #: metres — written by ``relax_chain`` from the settled backbone exits
    #: (:func:`precis_se.chain.relax.op_relax_chain`) and by nothing else.
    #: **Derived, never authored**: ``add_domain``/``set_domain`` do not
    #: accept it, and a ``set_domain`` edit drops it, because a route that
    #: has moved no longer has the curve that was settled for it. ``None``
    #: means *no placed loop* and is the distinction
    #: ``se-nucleic-realize-export`` reads, so an empty list is never
    #: stored.
    loop_curve: list[list[float]] | None = None

    @property
    def n_units(self) -> int:
        """How many helix offsets this domain occupies."""
        return self.end - self.start

    @property
    def entry_offset(self) -> int:
        """The offset the strand ENTERS this domain at (5' end)."""
        return self.start if self.forward else self.end - 1

    @property
    def exit_offset(self) -> int:
        """The offset the strand LEAVES this domain at (3' end)."""
        return self.end - 1 if self.forward else self.start

    def offsets(self) -> range:
        """Every offset this domain occupies, in 5'→3' order."""
        if self.forward:
            return range(self.start, self.end)
        return range(self.end - 1, self.start - 1, -1)

    def meta(self) -> dict[str, Any]:
        """The ``se_topology.meta`` payload for this row — absent keys
        omitted rather than stored as ``null``, so a row says only what was
        declared."""
        out: dict[str, Any] = {
            "ord": self.ord,
            "forward": self.forward,
            "start": self.start,
            "end": self.end,
        }
        if self.geometry is not None:
            out["geometry"] = self.geometry
        if self.overrides:
            out["overrides"] = dict(self.overrides)
        if self.loop_before_nt is not None:
            out["loop_before_nt"] = self.loop_before_nt
        if self.loop_curve:
            out["loop_curve"] = [[float(v) for v in point] for point in self.loop_curve]
        return out


def _strays(raw: dict[str, Any], allowed: frozenset[str], what: str) -> None:
    stray = sorted(set(raw) - allowed)
    if stray:
        raise ChainError(
            f"{what}: unknown key(s) {', '.join(stray)} — accepted: "
            f"{', '.join(sorted(allowed))}"
        )


def _int(raw: Any, key: str, what: str, *, minimum: int | None = None) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ChainError(f"{what}: {key!r} must be a whole number, got {raw!r}")
    value = float(raw)
    if value != int(value):
        raise ChainError(f"{what}: {key!r} must be a whole number, got {raw!r}")
    out = int(value)
    if minimum is not None and out < minimum:
        raise ChainError(f"{what}: {key!r} must be >= {minimum}, got {out}")
    return out


def coordinate_m(raw: Any, key: str, what: str) -> float:
    """One **coordinate** through the units boundary — metres out, any sign,
    and a bare ``0`` accepted.

    Split from :func:`length_m` because the two guard different things. A
    *size* is positive and its unit is the whole question. A *coordinate* is
    signed, and zero is the one value a unit cannot disambiguate (``0 nm``
    and ``0 m`` are the same point) — so a bare ``0`` is accepted here,
    which is not a hole in the zero-counting/exponent-slip guard the units
    policy exists for: that failure needs a nonzero magnitude to slip.
    """
    if (
        isinstance(raw, (int, float))
        and not isinstance(raw, bool)
        and float(raw) == 0.0
    ):
        return 0.0
    value = parse_quantity(raw, "length", arg_name=f"{what} {key}")
    if not math.isfinite(value):
        raise ChainError(f"{what}: {key!r} must be a finite length, got {raw!r}")
    return value


def length_m(raw: Any, key: str, what: str) -> float:
    """One positive length, through the units boundary — metres out, bare
    numbers refused (module docstring). Public because the ops layer parses
    ``max_seg_len`` the same way for ``layout_chain``, which stores no
    record of its own."""
    value = parse_quantity(raw, "length", arg_name=f"{what} {key}")
    if not value > 0.0:
        raise ChainError(f"{what}: {key!r} must be a positive length, got {raw!r}")
    return value


def _angle_rad(raw: Any, key: str, what: str) -> float:
    return parse_quantity(raw, "angle", arg_name=f"{what} {key}")


def resolve_motif_name(raw: Any, nucleic_name: str, what: str) -> str:
    """The motif name for a declaration — ``motif=`` if given, else the
    default for ``nucleic``."""
    if raw is None:
        return nucleic.MOTIF_FOR_NUCLEIC[nucleic_name]
    name = str(raw).strip()
    if name not in nucleic.MOTIFS:
        raise ChainError(
            f"{what}: unknown motif {name!r}; known: "
            f"{', '.join(sorted(nucleic.MOTIFS))}"
        )
    return name


def resolve_nucleic(raw: Any, what: str) -> str:
    """``'DNA'`` | ``'RNA'``, defaulting to DNA — the overwhelmingly
    common case, and the one every lattice constant is quoted for."""
    if raw is None:
        return "DNA"
    name = str(raw).strip().upper()
    if name not in nucleic.NUCLEICS:
        raise ChainError(
            f"{what}: 'nucleic' must be one of {' | '.join(nucleic.NUCLEICS)}, "
            f"got {raw!r}"
        )
    return name


def vet_sequence(raw: Any, nucleic_name: str, what: str) -> str | None:
    """A strand's sequence, upper-cased and whitespace-stripped, or
    ``None``.

    Optional on purpose: a space plan is suggestive by contract, and a
    24-helix rectangle is a real design long before anybody has picked its
    7 kb of sequence. ``N`` is accepted as an explicit don't-know (it is
    what a designer writes for a region the sequence-designer will fill),
    and every geometry check treats it as unverifiable rather than wrong.
    A letter outside the alphabet is refused: silently dropping it would
    shift every downstream index by one.
    """
    if raw is None:
        return None
    text = "".join(str(raw).split()).upper()
    if not text:
        return None
    alphabet = set("ACGUN") if nucleic_name == "RNA" else set("ACGTN")
    bad = sorted(set(text) - alphabet)
    if bad:
        raise ChainError(
            f"{what}: sequence has letter(s) {', '.join(bad)} outside the "
            f"{nucleic_name} alphabet ({''.join(sorted(alphabet))})"
        )
    return text


def vet_geometry(raw: Any, what: str) -> str | None:
    """A declared Leontis–Westhof family, canonicalised
    (:func:`precis_se.chain.nucleic.canonical_geometry`) — ``None`` when
    absent."""
    if raw is None:
        return None
    family = nucleic.canonical_geometry(str(raw))
    if family is None:
        raise ChainError(
            f"{what}: unknown pair geometry {raw!r} — one of the 12 "
            f"Leontis–Westhof families ({', '.join(nucleic.FAMILIES)}) or an "
            f"alias ({', '.join(sorted(nucleic.GEOMETRY_ALIASES))})"
        )
    return family


def vet_path(raw: Any, what: str) -> dict[str, Any]:
    """A helix's centre line: either ``lattice`` (a straight helix on a
    lattice site) or ``waypoints`` (a free centre line), never both.

    ``{'lattice': {'kind', 'row', 'col'}}`` → stored verbatim; the
    geometry comes from :func:`precis_se.chain.nucleic.site_position`.
    ``{'waypoints': [[x, y, z], …]}`` → stored as ``waypoints_m``, every
    component through the units boundary (so ``[[0, 0, 0], …]`` is a
    :class:`~precis.utils.units.UnitRequiredError`, not a silent
    metre-reading of a nanometre design).
    """
    if not isinstance(raw, dict):
        raise ChainError(
            f"{what}: 'path' must be an object — {{'lattice': {{'kind', "
            "'row', 'col'}}}} or {{'waypoints': [[x, y, z], …]}}, got "
            f"{raw!r}"
        )
    _strays(raw, frozenset({"lattice", "waypoints", "waypoints_m"}), f"{what} path")
    has_lattice = raw.get("lattice") is not None
    waypoints = raw.get("waypoints")
    stored = raw.get("waypoints_m")
    if has_lattice and (waypoints is not None or stored is not None):
        raise ChainError(
            f"{what}: a helix's path is EITHER a lattice site or explicit "
            "waypoints — a lattice site already fixes the centre line"
        )
    if has_lattice:
        return {"lattice": vet_lattice_site(raw["lattice"], what)}
    if waypoints is None and stored is None:
        raise ChainError(f"{what}: 'path' needs 'lattice' or 'waypoints' (>= 2 points)")
    if waypoints is None:
        # A round-trip of this module's own output (a stored row being
        # re-vetted): already metres, so it does NOT go through the units
        # boundary again — the one sanctioned ``require_unit=False`` case
        # (:func:`precis.utils.units.parse_quantity`'s own note).
        points = [[float(v) for v in p] for p in stored or []]
    else:
        if not isinstance(waypoints, (list, tuple)):
            raise ChainError(f"{what}: 'waypoints' must be a list of [x, y, z]")
        points = []
        for i, point in enumerate(waypoints):
            if not isinstance(point, (list, tuple)) or len(point) != 3:
                raise ChainError(
                    f"{what}: waypoint {i} must be [x, y, z], got {point!r}"
                )
            points.append(
                [
                    coordinate_m(component, f"waypoints[{i}][{axis}]", what)
                    for axis, component in enumerate(point)
                ]
            )
    if len(points) < 2:
        raise ChainError(
            f"{what}: 'waypoints' needs >= 2 points to be a centre line, "
            f"got {len(points)}"
        )
    return {"waypoints_m": points}


def vet_lattice_site(raw: Any, what: str) -> dict[str, Any]:
    """``{'kind', 'row', 'col'}`` — a site on one of
    :data:`precis_se.chain.nucleic.LATTICES`."""
    if not isinstance(raw, dict):
        raise ChainError(
            f"{what}: 'lattice' must be {{'kind', 'row', 'col'}}, got {raw!r}"
        )
    _strays(raw, frozenset({"kind", "row", "col"}), f"{what} lattice")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in nucleic.LATTICES:
        raise ChainError(
            f"{what}: unknown lattice {raw.get('kind')!r}; known: "
            f"{', '.join(sorted(nucleic.LATTICES))}"
        )
    return {
        "kind": kind,
        "row": _int(raw.get("row", 0), "row", f"{what} lattice"),
        "col": _int(raw.get("col", 0), "col", f"{what} lattice"),
    }


def vet_register(raw: Any, what: str, *, lattice: str | None) -> dict[str, Any]:
    """The register record — ``{'lattice', 'insertions', 'deletions'}``.

    ``lattice`` names which repeat holds this helix's twist (defaulting to
    the path's own lattice site, when it has one); the insertion/deletion
    lists are the **reserved hook** for retuning register by adding or
    dropping a base. Non-empty is refused rather than stored and ignored:
    the kernel's own hook
    (:func:`precis_chain.register.phase_after`'s ``per_unit_twist``)
    raises for exactly the same reason, and a design whose global twist
    depends on insertions nobody applies would read as checked when it is
    not.
    """
    if raw is None:
        return {"lattice": lattice} if lattice else {}
    if not isinstance(raw, dict):
        raise ChainError(f"{what}: 'register' must be an object, got {raw!r}")
    _strays(raw, frozenset({"lattice", "insertions", "deletions"}), f"{what} register")
    out: dict[str, Any] = {}
    declared = raw.get("lattice")
    name = str(declared).strip().lower() if declared is not None else lattice
    if name is not None:
        if name not in nucleic.LATTICES:
            raise ChainError(
                f"{what}: unknown register lattice {declared!r}; known: "
                f"{', '.join(sorted(nucleic.LATTICES))}"
            )
        out["lattice"] = name
    for key in ("insertions", "deletions"):
        value = raw.get(key)
        if value in (None, [], ()):
            continue
        raise ChainError(
            f"{what}: register {key!r} is a reserved hook with no consumer "
            "yet — the global insertion/deletion twist check is deferred "
            "(``docs/backlog/se-chain-insertions-deletions.md`` owns it); "
            "leave it empty rather than storing a correction nothing applies"
        )
    return out


def build_helix(op: dict[str, Any], *, what: str = "declare_helix") -> dict[str, Any]:
    """The stored ``chain`` record for a helix, from an op payload.

    ``n_units`` is required — a helix with no length has no geometry, no
    register and no segments, and every consumer would have to invent one.
    Everything else has a default: the motif from ``nucleic`` (DNA), the
    path from a ``lattice``/``row``/``col`` shorthand or an explicit
    ``path``, ``phase0`` 0, the register lattice from the path's site, and
    the bend/gap limits from :mod:`precis_se.chain.nucleic`'s coded
    defaults (absent from the record, so the default can move).
    """
    nucleic_name = resolve_nucleic(op.get("nucleic"), what)
    motif_name = resolve_motif_name(op.get("motif"), nucleic_name, what)
    n_units = _int(op.get("n_units"), "n_units", what, minimum=1)
    raw_path = op.get("path")
    if raw_path is None:
        lattice_kind = op.get("lattice")
        if lattice_kind is None:
            raise ChainError(
                f"{what}: needs a centre line — lattice= (+ row=/col=) for a "
                "straight lattice helix, or path={'waypoints': [[x, y, z], …]}"
            )
        raw_path = {
            "lattice": {
                "kind": lattice_kind,
                "row": op.get("row", 0),
                "col": op.get("col", 0),
            }
        }
    path = vet_path(raw_path, what)
    site = path.get("lattice")
    record: dict[str, Any] = {
        "role": HELIX_ROLE,
        "motif": motif_name,
        "nucleic": nucleic_name,
        "path": path,
        "n_units": n_units,
        "phase0": (
            0.0
            if op.get("phase0") is None
            else _angle_rad(op["phase0"], "phase0", what)
        ),
    }
    register = vet_register(
        op.get("register"), what, lattice=site["kind"] if site else None
    )
    if register:
        record["register"] = register
    if op.get("min_bend_radius") is not None:
        record["min_bend_radius_m"] = length_m(
            op["min_bend_radius"], "min_bend_radius", what
        )
    if op.get("min_gap") is not None:
        record["min_gap_m"] = length_m(op["min_gap"], "min_gap", what)
    return record


def build_strand(op: dict[str, Any], *, what: str = "declare_strand") -> dict[str, Any]:
    """The stored ``chain`` record for a strand. The route itself is the
    domain rows (:func:`build_domain`), not part of this record."""
    nucleic_name = resolve_nucleic(op.get("nucleic"), what)
    record: dict[str, Any] = {"role": STRAND_ROLE, "nucleic": nucleic_name}
    sequence = vet_sequence(op.get("sequence"), nucleic_name, what)
    if sequence is not None:
        record["sequence"] = sequence
    return record


def segment_record(helix: str, ord_: int, start: int, end: int) -> dict[str, Any]:
    """The stored ``chain`` record for a ``layout_chain`` child — the
    **realizer seam** this item owes ``se-nucleic-realize-export``:
    ``[start, end]`` is the inclusive unit range this child's envelope
    covers, and one helix's children tile it exactly (no gap, no overlap),
    so a realizer can find the segment covering any offset by comparison
    alone."""
    return {
        "role": SEGMENT_ROLE,
        "helix": helix,
        "ord": ord_,
        "start": start,
        "end": end,
    }


def build_domain(
    op: dict[str, Any],
    *,
    strand: str,
    helix: str,
    ord_: int,
    what: str = "add_domain",
) -> DomainSpec:
    """One :class:`DomainSpec` from an op payload (``strand``/``helix``
    already resolved to block names by the caller)."""
    start = _int(op.get("start"), "start", what, minimum=0)
    end = _int(op.get("end"), "end", what, minimum=0)
    if end <= start:
        raise ChainError(
            f"{what}: 'end' ({end}) must exceed 'start' ({start}) — offsets "
            "are half-open [start, end), and a zero-length domain declares "
            "nothing (an unpaired stretch is a loop, not a domain)"
        )
    forward = op.get("forward")
    if forward is None:
        raise ChainError(
            f"{what}: needs 'forward' (true|false) — which way the strand "
            "runs through the offsets; there is no default direction"
        )
    if not isinstance(forward, bool):
        raise ChainError(f"{what}: 'forward' must be true or false, got {forward!r}")
    loop = op.get("loop_before_nt")
    loop_nt = None if loop is None else _int(loop, "loop_before_nt", what, minimum=0)
    if ord_ == 0 and loop_nt is not None:
        raise ChainError(
            f"{what}: 'loop_before_nt' on the strand's FIRST domain has no "
            "preceding exit to reach from — a 5' overhang is its own "
            "single-occupancy domain, not a loop"
        )
    return DomainSpec(
        strand=strand,
        helix=helix,
        ord=ord_,
        forward=forward,
        start=start,
        end=end,
        geometry=vet_geometry(op.get("geometry"), what),
        overrides=vet_overrides(op.get("overrides"), what, start=start, end=end),
        loop_before_nt=loop_nt,
    )


def vet_overrides(
    raw: Any, what: str, *, start: int, end: int
) -> dict[str, Any] | None:
    """Per-offset pair-geometry overrides — ``{offset: geometry}``, offsets
    inside the domain's own range (2026-09-28 decision: an override, never
    a 1-bp domain). Keys come back as strings, since that is what a jsonb
    object round-trips."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ChainError(
            f"{what}: 'overrides' must be {{offset: geometry}}, got {raw!r}"
        )
    out: dict[str, Any] = {}
    for key, value in raw.items():
        offset = _int(
            int(key)
            if isinstance(key, str) and key.strip().lstrip("-").isdigit()
            else key,
            "overrides key",
            what,
        )
        if not start <= offset < end:
            raise ChainError(
                f"{what}: override offset {offset} is outside this domain's "
                f"range [{start}, {end})"
            )
        out[str(offset)] = vet_geometry(value, f"{what} overrides[{offset}]")
    return out or None


def validate_chain(raw: Any, *, what: str = "chain") -> dict[str, Any]:
    """Re-vet a **stored** ``se_blocks.chain`` record — the read-time
    defence the DRC pass runs before trusting a row (the same posture
    :func:`precis_se.joints.validate_joint` has in
    :func:`precis_se.drc.drc`: a hand-corrected row must surface as a
    finding, never as a crash)."""
    if not isinstance(raw, dict):
        raise ChainError(f"{what} must be a JSON object, got {raw!r}")
    role = str(raw.get("role") or "").strip()
    if role == HELIX_ROLE:
        _strays(raw, _HELIX_KEYS, f"{what} (helix)")
        nucleic_name = resolve_nucleic(raw.get("nucleic"), what)
        resolve_motif_name(raw.get("motif"), nucleic_name, what)
        _int(raw.get("n_units"), "n_units", what, minimum=1)
        vet_path(raw.get("path"), what)
        return raw
    if role == STRAND_ROLE:
        _strays(raw, _STRAND_KEYS, f"{what} (strand)")
        vet_sequence(
            raw.get("sequence"), resolve_nucleic(raw.get("nucleic"), what), what
        )
        return raw
    if role == SEGMENT_ROLE:
        _strays(raw, _SEGMENT_KEYS, f"{what} (segment)")
        for key in ("ord", "start", "end"):
            _int(raw.get(key), key, what, minimum=0)
        return raw
    raise ChainError(
        f"{what}: 'role' must be one of {' | '.join(ROLES)}, got {raw.get('role')!r}"
    )


def chain_role(node: Any) -> str | None:
    """The chain role of a block node, or ``None`` when it declares no
    chain — the one-line read every consumer uses instead of digging into
    the jsonb (``precis_se.validate.envelope_overlaps``'s segment
    exclusion, the views, the DRC pass)."""
    record = getattr(node, "chain", None)
    if not isinstance(record, dict):
        return None
    role = record.get("role")
    return str(role) if role in ROLES else None


#: Sorted-domain key: a strand's route in 5'→3' order.
def domain_sort_key(d: DomainSpec) -> tuple[str, int]:
    return (d.strand, d.ord)


@dataclass
class ChainTables:
    """Domains grouped the two ways every consumer needs them — by strand
    (the route, in ``ord`` order) and by helix (who occupies it).

    Built once per pass (:func:`group_domains`) rather than re-scanned:
    the DRC findings, the pairing derivation and the views all want both
    orders, and a 100-staple origami has ~200 domains.
    """

    by_strand: dict[str, list[DomainSpec]] = field(default_factory=dict)
    by_helix: dict[str, list[DomainSpec]] = field(default_factory=dict)


def group_domains(domains: list[DomainSpec]) -> ChainTables:
    """Group ``domains`` by strand (``ord``-sorted) and by helix."""
    tables = ChainTables()
    for d in domains:
        tables.by_strand.setdefault(d.strand, []).append(d)
        tables.by_helix.setdefault(d.helix, []).append(d)
    for route in tables.by_strand.values():
        route.sort(key=lambda d: d.ord)
    return tables
