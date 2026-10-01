"""hexfold.catalogue: environment-keyed rows for seam/bulk geometry (SPEC
§26 "cache", §25.3 `view='catalogue'`; `docs/backlog/hexfold-integration.md`
ruling step 6; `docs/backlog/hexfold-seam-type-catalogue.md`).

Resolution is keyed to the local environment, not the instance
(`docs/backlog/diamondoid-pattern-language.md`): a *rim type* (zigzag/
armchair) at a given dangling count ``N``, on a given relax rung, decays
the same way wherever it occurs, so `compose`'s seam radius and leak
threshold can be looked up rather than baked into module constants.  This
module is that lookup, plus the library function that fills a row by
measurement (the seam-decay experiment of
`tests/test_hexfold_seam_decay.py`, generalised).

Numpy + hexfold only (`tests/test_hexfold_import_boundary.py`); the store
here (:class:`MemoryStore`) is process-local -- a DB-backed store keyed the
same way is `precis_se/atomic/catalogue.py` (slice 2).  :func:`compose`
(``join.py``) stays read-only on whatever store it is given; only
:func:`measure_environment` (called explicitly, e.g. by the se join op's
first-use warm-up, slice 2) produces new rows.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import numpy as np

from . import __version__, join
from .build import Net, build
from .lattice import Lattice, n_cells, translation_vector, tube_radius
from .stick import stick

#: `Relaxer(elements, coords, bonds, rings, pinned_mask) -> coords`, same
#: seam as `join.Relaxer` -- re-exported so a caller measuring a row never
#: needs `hexfold.join` directly.
Relaxer = join.Relaxer

#: seam-decay tube length (periods) `measure_environment` uses when the
#: caller does not pin one, by rim type -- long enough for an interior
#: shell to exist beyond the pinned table radius on the stick rung, short
#: enough to stay well under :data:`MEASURE_ATOM_CAP`.
MEASURE_LEN: dict[str, int] = {"z": 6, "a": 4}

#: above this projected atom count (free + fused, both tubes),
#: `measure_environment` refuses rather than building and relaxing a net
#: that large -- a catalogue fill is meant to be a cheap, synchronous,
#: first-use operation (SPEC §26 "fill and read"), not a batch job.
MEASURE_ATOM_CAP = 800

#: noise floor for the seam-radius decision and the guard-band leak
#: threshold, by rung -- a relax that happens to converge unusually
#: quietly (e.g. a short, stiff tube) must never yield a near-zero
#: threshold that fires on ordinary numerical noise.  Stick: 10x the
#: `join._LEAK_THRESH`/`SEAM_RADIUS` measurement's own noise floor
#: (`join.py`'s docstring: shells 9-10 of an (8,0) len=3 tube show
#: |dl| < 2e-15 A pinned-guard noise).  Geo: the same numbers
#: `tests/test_hexfold_seam_decay.py` already pins (0.002 A / 0.15 deg).
_MEASURE_FLOOR: dict[str, tuple[float, float]] = {
    "stick": (1e-4, 0.01),
    "geo": (0.002, 0.15),
}

#: gr456641: the `coverage="unstable"` test.  A free relax that has not
#: converged at the far, unfused rim leaves that end drifting more than the
#: settled interior, so the per-shell displacement bottoms out in the
#: interior and then climbs back toward the far rim instead of decaying
#: monotonically -- and the measured `seam_radius` tracks the tube length
#: rather than a physical decay length (`seam_radius` grew 4/14/22/30/34 as
#: the tube grew 6/8/10/12/13 periods, all silently reported "full").  The
#: stick rung has no convergence check at all (its own docstring); the geo
#: rung's `trace.converged` is not threaded back to this function; this
#: reads the symptom directly off `max_disp` over the core shells (the
#: free-rim guard band is already excluded) and is therefore rung-
#: independent.  A profile is flagged when the largest post-minimum core
#: displacement is BOTH this factor over the interior minimum (a real
#: rise, not a flat monotone decay whose minimum sits at its last core
#: shell) AND above the floor -- the floor keeps a short, genuinely-settled
#: tube whose far rim barely moves from being flagged: the lower-bound
#: tubes `tests/test_hexfold_seam_decay.py` pins settle to ~0.002 A at the
#: far rim (a converged geo len=3 (8,0) rises to 0.0018 A there), an order
#: of magnitude below the >=0.027 A the stick runaway reaches by len=8.
_STABILITY_RISE_FACTOR = 2.0
_STABILITY_RISE_FLOOR = 0.01  # A

_PINNED_DATE = "2026-09-27"


class CatalogueError(ValueError):
    """A catalogue request that cannot be satisfied: a
    `measure_environment` call over :data:`MEASURE_ATOM_CAP`, a mixed
    (non-pure) rim (no catalogue entry is ever measured for one -- see
    `join.SEAM_RADIUS`'s own mixed-rim fallback), or malformed JSON on
    `MemoryStore.from_json`."""


def _rim_type_of(n: int, m: int) -> str | None:
    """SPEC 10 rim type of a pure tube roll-up: ``"z"`` (``m == 0``),
    ``"a"`` (``n == m``), ``None`` (chiral/mixed -- not catalogued)."""
    if n <= 0 and m <= 0:
        return None
    if m == 0:
        return "z"
    if n == m:
        return "a"
    return None


# ---------- environment key ----------


@dataclass(frozen=True)
class EnvKey:
    """The local environment a catalogue row describes -- everything that
    changes the physics, nothing that names an instance.  ``nm`` is
    canonicalised ``(n, m) -> (n, m)`` with ``m <= n`` (mirror roll-ups are
    the same physical tube up to a reflection this catalogue does not
    distinguish); ``rim_type``/``N`` describe an edge zone, ``kind``/``nm``
    a bulk zone, ``seam`` a seam zone (``(type_a, type_b, N, k)``).
    :meth:`hash` (sha256 of :meth:`canonical_json`) is the table key both
    stores use."""

    zone: str  # "bulk" | "edge" | "seam"
    lattice: str = "sp2-hex"
    sigma: str = "1.42"
    kind: str | None = None  # bulk: "tube" | "cap" | "sheet"
    nm: tuple[int, int] | None = None  # bulk (n, m), canonicalised m <= n
    rim_type: str | None = None  # edge/seam: "z" | "a" (mixed not catalogued)
    N: int | None = None  # edge dangling count; None = wildcard (pinned table)
    seam: tuple[str, str, int, int] | None = None  # (type_a, type_b, N, k)
    rung: str = "stick"
    relaxer: str = "stick@0.2.0"  # relaxer code version

    def __post_init__(self) -> None:
        if self.nm is not None and self.nm[1] > self.nm[0]:
            object.__setattr__(self, "nm", (self.nm[1], self.nm[0]))

    def canonical_json(self) -> str:
        d: dict[str, Any] = {
            "zone": self.zone,
            "lattice": self.lattice,
            "sigma": self.sigma,
            "kind": self.kind,
            "nm": list(self.nm) if self.nm is not None else None,
            "rim_type": self.rim_type,
            "N": self.N,
            "seam": list(self.seam) if self.seam is not None else None,
            "rung": self.rung,
            "relaxer": self.relaxer,
        }
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    def hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _key_to_dict(key: EnvKey) -> dict[str, Any]:
    return {
        "zone": key.zone,
        "lattice": key.lattice,
        "sigma": key.sigma,
        "kind": key.kind,
        "nm": list(key.nm) if key.nm is not None else None,
        "rim_type": key.rim_type,
        "N": key.N,
        "seam": list(key.seam) if key.seam is not None else None,
        "rung": key.rung,
        "relaxer": key.relaxer,
    }


def _key_from_dict(d: dict[str, Any]) -> EnvKey:
    return EnvKey(
        zone=str(d["zone"]),
        lattice=str(d.get("lattice", "sp2-hex")),
        sigma=str(d.get("sigma", "1.42")),
        kind=d.get("kind"),
        nm=tuple(d["nm"]) if d.get("nm") is not None else None,
        rim_type=d.get("rim_type"),
        N=d.get("N"),
        seam=tuple(d["seam"]) if d.get("seam") is not None else None,
        rung=str(d.get("rung", "stick")),
        relaxer=str(d.get("relaxer", "stick@0.2.0")),
    )


# ---------- rows ----------


@dataclass(frozen=True)
class BulkCell:
    """The interior of a bulk zone (a tube's own lattice, away from any
    rim): radius/pitch/bond lengths/mean angle, measured from a free
    relax's interior atoms (shell beyond the edge motif's own
    ``seam_radius`` on both rims)."""

    key: EnvKey
    radius_A: float
    pitch_A: float
    bond_axial_A: float
    bond_circ_A: float
    angle_mean_deg: float
    atoms_per_period: int
    measured_on: str
    source: str  # "pinned-2026-09-27" | "measured" | "forced" | "join"
    hexfold_version: str
    coords_A: tuple[tuple[float, float, float], ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": _key_to_dict(self.key),
            "radius_A": round(self.radius_A, 4),
            "pitch_A": round(self.pitch_A, 4),
            "bond_axial_A": round(self.bond_axial_A, 4),
            "bond_circ_A": round(self.bond_circ_A, 4),
            "angle_mean_deg": round(self.angle_mean_deg, 3),
            "atoms_per_period": self.atoms_per_period,
            "measured_on": self.measured_on,
            "source": self.source,
            "hexfold_version": self.hexfold_version,
            "coords_A": (
                [list(c) for c in self.coords_A] if self.coords_A is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BulkCell:
        coords = d.get("coords_A")
        return cls(
            key=_key_from_dict(d["key"]),
            radius_A=float(d["radius_A"]),
            pitch_A=float(d["pitch_A"]),
            bond_axial_A=float(d["bond_axial_A"]),
            bond_circ_A=float(d["bond_circ_A"]),
            angle_mean_deg=float(d["angle_mean_deg"]),
            atoms_per_period=int(d["atoms_per_period"]),
            measured_on=str(d["measured_on"]),
            source=str(d["source"]),
            hexfold_version=str(d["hexfold_version"]),
            coords_A=(
                tuple((float(c[0]), float(c[1]), float(c[2])) for c in coords)
                if coords is not None
                else None
            ),
        )


@dataclass(frozen=True)
class EdgeMotif:
    """The decay of a fused rim: per-shell ``(max_disp, max_dl, max_dtheta)``
    out to :attr:`shells`, the noise floor and decision threshold that
    picked :attr:`seam_radius`, and the guard-band :attr:`leak_thresh`
    `compose` checks its own re-relax against."""

    key: EnvKey
    profile: dict[int, tuple[float, float, float]]
    noise: tuple[float, float]
    thresh: tuple[float, float]
    seam_radius: int
    leak_thresh: tuple[float, float]
    shells: int
    measured_on: str
    coverage: str  # "full" | "lower-bound" | "unstable"
    source: str
    hexfold_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": _key_to_dict(self.key),
            "profile": {
                str(s): [round(v, 5), round(v2, 5), round(v3, 4)]
                for s, (v, v2, v3) in self.profile.items()
            },
            "noise": [round(x, 5) for x in self.noise],
            "thresh": [round(x, 5) for x in self.thresh],
            "seam_radius": self.seam_radius,
            "leak_thresh": [round(x, 5) for x in self.leak_thresh],
            "shells": self.shells,
            "measured_on": self.measured_on,
            "coverage": self.coverage,
            "source": self.source,
            "hexfold_version": self.hexfold_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EdgeMotif:
        profile_raw = d["profile"]
        return cls(
            key=_key_from_dict(d["key"]),
            profile={
                int(s): (float(v[0]), float(v[1]), float(v[2]))
                for s, v in profile_raw.items()
            },
            noise=(float(d["noise"][0]), float(d["noise"][1])),
            thresh=(float(d["thresh"][0]), float(d["thresh"][1])),
            seam_radius=int(d["seam_radius"]),
            leak_thresh=(float(d["leak_thresh"][0]), float(d["leak_thresh"][1])),
            shells=int(d["shells"]),
            measured_on=str(d["measured_on"]),
            coverage=str(d["coverage"]),
            source=str(d["source"]),
            hexfold_version=str(d["hexfold_version"]),
        )


@dataclass(frozen=True)
class SeamMotif:
    """A realised seam's own census (slice 2 fills these from `compose`'s
    ``seam.rings``/``seam.strain`` findings on first use; kept here so the
    row shapes and their (de)serialisation land in one slice)."""

    key: EnvKey
    motif: str
    rings: dict[int, int]
    strain: tuple[float, float]
    hits: int
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": _key_to_dict(self.key),
            "motif": self.motif,
            "rings": {str(k): v for k, v in self.rings.items()},
            "strain": [round(x, 4) for x in self.strain],
            "hits": self.hits,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SeamMotif:
        return cls(
            key=_key_from_dict(d["key"]),
            motif=str(d["motif"]),
            rings={int(k): int(v) for k, v in d["rings"].items()},
            strain=(float(d["strain"][0]), float(d["strain"][1])),
            hits=int(d["hits"]),
            source=str(d["source"]),
        )


Row = BulkCell | EdgeMotif | SeamMotif


# ---------- store ----------


class CatalogueStore(Protocol):
    def get(self, key: EnvKey) -> Row | None: ...

    def put(self, row: Row, *, force: bool = False) -> None: ...

    def rows(self, zone: str | None = None) -> list[Row]: ...


@dataclass
class MemoryStore:
    """Process-local :class:`CatalogueStore`: a dict keyed by
    :meth:`EnvKey.hash`.  ``put`` is first-wins unless ``force=True``.
    ``to_json``/``from_json`` are the CLI dump/load format only -- not a
    persistence layer (`precis_se/atomic/catalogue.py`'s DB store is,
    slice 2)."""

    _rows: dict[str, Row] = field(default_factory=dict)

    def get(self, key: EnvKey) -> Row | None:
        return self._rows.get(key.hash())

    def put(self, row: Row, *, force: bool = False) -> None:
        h = row.key.hash()
        if h in self._rows and not force:
            return
        self._rows[h] = row

    def rows(self, zone: str | None = None) -> list[Row]:
        vals = list(self._rows.values())
        if zone is None:
            return vals
        return [r for r in vals if r.key.zone == zone]

    @classmethod
    def seeded(cls) -> MemoryStore:
        """A store pre-loaded with :func:`seed_rows` -- the ordinary
        starting point for any hexfold-only caller (CLI, tests)."""
        store = cls()
        for row in seed_rows():
            store.put(row)
        return store

    def to_json(self) -> str:
        kinds: dict[type, str] = {
            BulkCell: "bulk",
            EdgeMotif: "edge",
            SeamMotif: "seam",
        }
        rows = [{"type": kinds[type(r)], **r.to_dict()} for r in self._rows.values()]
        return json.dumps({"rows": rows}, sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> MemoryStore:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CatalogueError(f"malformed catalogue JSON: {exc}") from exc
        loaders: dict[str, Any] = {
            "bulk": BulkCell.from_dict,
            "edge": EdgeMotif.from_dict,
            "seam": SeamMotif.from_dict,
        }
        store = cls()
        for d in data.get("rows", []):
            loader = loaders.get(d.get("type"))
            if loader is None:
                raise CatalogueError(f"unknown catalogue row type {d.get('type')!r}")
            store.put(loader(d))
        return store


def seed_rows() -> list[EdgeMotif]:
    """The six wildcard rows -- rim type ``z``/``a``/``None`` (mixed, the
    same conservative zigzag-fallback `join._thresh_for`/`side_radius`
    already use) x rung ``stick``/``geo`` -- built FROM :data:`join.
    SEAM_RADIUS`, :data:`join._LEAK_THRESH` and :data:`join.
    LEAK_THRESH_GEO` (those constants stay exported and authoritative;
    this is a restatement, not a second source of truth -- a test pins
    the two equal)."""
    rows: list[EdgeMotif] = []
    for rim_type in ("z", "a", None):
        # `rim_type is None` is the mixed-rim wildcard; SEAM_RADIUS is keyed by
        # real rim types only, so the fallback IS the None case.
        radius = (
            join.SEAM_RADIUS.get(rim_type, join._SEAM_RADIUS_DEFAULT)
            if rim_type is not None
            else join._SEAM_RADIUS_DEFAULT
        )
        for rung, table in (
            ("stick", join._LEAK_THRESH),
            ("geo", join.LEAK_THRESH_GEO),
        ):
            thresh = join._thresh_for(
                (rim_type, 0) if rim_type is not None else None, table
            )
            rows.append(
                EdgeMotif(
                    key=EnvKey(
                        zone="edge",
                        rim_type=rim_type,
                        N=None,
                        rung=rung,
                        relaxer=f"pinned-{_PINNED_DATE}",
                    ),
                    profile={},
                    noise=(0.0, 0.0),
                    thresh=thresh,
                    seam_radius=radius,
                    leak_thresh=thresh,
                    shells=radius,
                    measured_on=_PINNED_DATE,
                    coverage="full",
                    source="pinned-2026-09-27",
                    hexfold_version=__version__,
                )
            )
    return rows


def resolve_edge(
    store: CatalogueStore,
    rim_type: str | None,
    N: int,
    rung: str,
    relaxer: str | None = None,
    sigma: float | None = None,
) -> tuple[EdgeMotif | None, str]:
    """The :class:`EdgeMotif` :func:`hexfold.join.compose` should use for
    one side of a join: exact ``(rim_type, N)`` -> nearest measured
    ``N' >= N`` of the same type (conservative -- a wider tube's decay
    radius over-covers a narrower one, never the reverse, so lookup never
    goes down) -> the wildcard pinned row -> ``None``.  No interpolation.
    ``sigma`` (the block's own bond-length assumption, part of
    :class:`EnvKey` and its hash) is matched when given -- two rows
    measured under different sigma for the same ``(rim_type, N, rung)``
    must not compete; ``None`` skips the filter (a caller with no sigma
    of its own, e.g. a bare lookup test, sees every row regardless).
    ``relaxer`` is accepted (matches the DB store's signature, slice 2,
    which filters measured rows by an exact relaxer-version match) but
    unused here: ``MemoryStore`` never holds two measurements of the same
    environment under different relaxer versions within one process."""
    del relaxer
    sigma_str = f"{sigma:g}" if sigma is not None else None
    rows = [
        r
        for r in store.rows("edge")
        if isinstance(r, EdgeMotif)
        and r.key.rim_type == rim_type
        and r.key.rung == rung
        and (sigma_str is None or r.key.sigma == sigma_str)
    ]
    label_type = rim_type if rim_type is not None else "x"
    for r in rows:
        if r.key.N == N:
            return r, f"exact {label_type}{N}"
    measured = [r for r in rows if r.key.N is not None and r.key.N >= N]
    measured.sort(key=lambda r: r.key.N if r.key.N is not None else 0)
    if measured:
        r = measured[0]
        return r, f"nearest {label_type}{r.key.N}"
    for r in rows:
        if r.key.N is None:
            return r, f"pinned {label_type}"
    return None, "none"


# ---------- measurement ----------


def _kabsch_align(x: np.ndarray, ref: np.ndarray) -> tuple[np.ndarray, float]:
    """Best-fit rotation+translation of ``x`` onto ``ref`` (no reflection);
    returns the aligned copy and the RMSD -- the same convention
    `precis.structure.georelax.kabsch_align` and `tests/hexfold/
    test_join.py`'s local `_kabsch_rmsd` use, restated numpy-only so
    `measure_environment` never imports `precis`."""
    xc = x - x.mean(axis=0)
    yc = ref - ref.mean(axis=0)
    h = xc.T @ yc
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T))
    r = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    aligned = xc @ r + ref.mean(axis=0)
    rmsd = float(np.sqrt(((aligned - ref) ** 2).sum(axis=1).mean()))
    return aligned, rmsd


def measure_environment(
    nm: tuple[int, int],
    *,
    rung: str,
    relax: Relaxer | None = None,
    relaxer: str | None = None,
    length: int | None = None,
    sigma: float = 1.42,
) -> tuple[EdgeMotif, BulkCell]:
    """The seam-decay experiment (`tests/test_hexfold_seam_decay.py`) as a
    library function: build ``nm`` free and fused tip-to-tip onto an
    identical copy of itself at ``k=0``, relax both with ``relax`` (default
    :func:`hexfold.join._stick_relaxer`, called over the *whole* net with
    an all-zero pinned mask -- a free relax, not a seam sub-relax), and
    read the per-shell decay off instance ``a``'s own atoms.  ``seam_radius``
    is the smallest shell beyond which every measured shell is below
    ``max(3x an interior noise sample, a rung floor)`` -- but only once
    the tube reaches well past the known table radius for this rim type
    (below that, a short tube's own far free rim would otherwise be
    mistaken for a settled interior); short of that depth, or when no
    shell satisfies the threshold, the report is half the tube's own
    measured depth, flagged ``coverage="lower-bound"``.  Whatever the
    depth verdict, if the per-shell ``max_disp`` does not settle past the
    seam but climbs back toward the far, unfused rim -- the signature of a
    free relax that did not converge there, on either rung (gr456641) --
    the report is downgraded to ``coverage="unstable"`` rather than quoting
    a ``seam_radius`` that is really tracking the tube length
    (:data:`_STABILITY_RISE_FACTOR`).  One more
    `join.compose` at that radius (fusing the same free-relaxed block onto
    itself) reads the guard-band's own leak maxima, doubled and floored,
    for :attr:`EdgeMotif.leak_thresh` -- the same derivation
    `join._LEAK_THRESH`'s docstring describes by hand.  The :class:`BulkCell`
    comes from the same free relax's interior (shells beyond
    ``seam_radius + 2`` from both rims).  Pure ``z``/``a`` tubes only
    (:func:`_rim_type_of`); refuses (:class:`CatalogueError`) above
    :data:`MEASURE_ATOM_CAP` projected atoms, before building anything."""
    n, m = nm
    rim_type = _rim_type_of(n, m)
    if rim_type is None:
        raise CatalogueError(
            f"tube({n},{m}) is not a pure zigzag/armchair rim; "
            "measure_environment only measures pure rims"
        )
    length_ = length if length is not None else MEASURE_LEN[rim_type]
    projected = n_cells(n, m) * 2 * length_ * 3  # free (1x) + fused (2x tubes)
    if projected > MEASURE_ATOM_CAP:
        raise CatalogueError(
            f"tube({n},{m},len={length_}): ~{projected} atoms projected, over "
            f"MEASURE_ATOM_CAP={MEASURE_ATOM_CAP}"
        )

    relaxer_label = relaxer if relaxer is not None else f"{rung}@{__version__}"
    relax_fn: Relaxer = relax if relax is not None else join._stick_relaxer(sigma)

    free = build(f"hexfold 0.2\na: tube({n},{m}, len={length_})\n")
    fused = build(
        f"hexfold 0.2\na: tube({n},{m}, len={length_})\n"
        f"b: tube({n},{m}, len={length_})\na.out --fuse k=0--> b.in\n"
    )

    def _relax_free(net: Net) -> np.ndarray:
        coords0 = stick(net).astype(np.float64)
        elements = [a.element for a in net.atoms]
        bonds = list(net.bonds)
        rings = list(net.rings)
        pinned = np.zeros(len(elements), dtype=np.float64)
        return np.asarray(
            relax_fn(elements, coords0, bonds, rings, pinned), dtype=np.float64
        )

    cf = _relax_free(free)
    cg = _relax_free(fused)

    free_a = {a.path: a.ord for a in free.atoms if a.instance == "a"}
    fused_a = {a.path: a.ord for a in fused.atoms if a.instance == "a"}
    if free_a != fused_a:
        raise CatalogueError(
            "instance 'a' ordinal mismatch between the free and fused nets"
        )
    ords = sorted(free_a.values())

    adj = join._adjacency(tuple(free.bonds))
    out_atoms = set(dict(free.ports)["out"].atoms)
    in_atoms = set(dict(free.ports)["in"].atoms)
    dist_out = join._shells_from(out_atoms, adj)
    dist_in = join._shells_from(in_atoms, adj)

    aligned, _rmsd = _kabsch_align(cg[ords], cf[ords])
    disp = np.linalg.norm(aligned - cf[ords], axis=1)

    profile: dict[int, list[float]] = {}
    #: per shell (by distance from the fused "out" rim), the shallowest
    #: distance any of its atoms sit from the tube's OWN opposite ("in")
    #: rim -- a shell close to that free, unfused end carries that rim's
    #: own reconstruction noise (an unconstrained 2-bond boundary wobbles
    #: under a free relax independent of any join elsewhere), which would
    #: otherwise masquerade as un-decayed seam signal this far from the
    #: join.  Shells within the guard band of that free end (same
    #: ``+2`` convention `_seam_subgraph`/the bulk-cell interior cut use)
    #: are excluded from both the noise estimate and the radius scan.
    shell_min_dist_in: dict[int, int] = {}
    for k, o in enumerate(ords):
        s = dist_out[o]
        row = profile.setdefault(s, [0.0, 0.0, 0.0])
        row[0] = max(row[0], float(disp[k]))
        shell_min_dist_in[s] = min(
            shell_min_dist_in.get(s, 1 << 30), dist_in.get(o, 1 << 30)
        )
        nbrs = adj.get(o, [])
        if nbrs:
            th_f = join._angles_at(cf, o, nbrs)
            th_g = join._angles_at(cg, o, nbrs)
            row[2] = max(
                row[2], max(abs(x - y) for x, y in zip(th_f, th_g, strict=True))
            )
    for i, j, *_rest in free.bonds:
        s = min(dist_out.get(i, 1 << 30), dist_out.get(j, 1 << 30))
        if s not in profile:
            continue
        dl = abs(
            float(np.linalg.norm(cf[i] - cf[j])) - float(np.linalg.norm(cg[i] - cg[j]))
        )
        profile[s][1] = max(profile[s][1], dl)

    shells_sorted = sorted(profile)
    core_shells = [s for s in shells_sorted if shell_min_dist_in.get(s, 0) > 2]
    floor_dl, floor_dtheta = _MEASURE_FLOOR.get(rung, _MEASURE_FLOOR["stick"])
    #: the innermost few core shells (never shell 0 -- the rim itself is
    #: the signal being measured, not noise), as a noise sample: on the
    #: geo rung (a real convergence check, `relax_graph`'s own `tol`) this
    #: is already near the rung floor, so the floor dominates, same as
    #: `tests/test_hexfold_seam_decay.py` always used a fixed threshold.
    #: On the stick rung (fixed-iteration gradient descent, no
    #: convergence check) the interior never fully stops drifting, so
    #: this sample is itself the rung's own achievable precision, not
    #: true zero.
    #:
    #: DEVIATION from the plan (nanomachine-slice-6-catalogue.md §3):
    #: it specifies a 10x margin over this noise sample, calibrated
    #: against the *pinned-guard* stick measurement's near-machine-epsilon
    #: noise (join.py's `_LEAK_THRESH` docstring). Applied literally to
    #: THIS free-relax noise sample (measured ~0.01-0.03 deg across the
    #: stick interior, not machine epsilon -- a free relax has no pin to
    #: damp drift the way a guard-band re-relax does), 10x sits above
    #: every shell's own decaying signal (shell 1 here measures ~0.067
    #: deg) and the scan below never triggers before shell 0, always
    #: reporting seam_radius=0.  Measured, not guessed: this was the
    #: actual failure mode this multiplier was tuned against (see the
    #: session's own dispatch notes and `git log` on this file).  3x
    #: keeps the same "margin over measured noise" intent while still
    #: discriminating the real fused-rim perturbation from the flat
    #: interior; it is a slice-1 calibration choice, not re-derived from
    #: first principles, and the four `seam_radius` numbers it produces
    #: are pinned by exact equality in `tests/hexfold/test_catalogue.py`
    #: and `tests/test_hexfold_seam_decay.py` so a future change to this
    #: constant is visible, not silent.
    noise_candidates = [s for s in core_shells if s > 0]
    tail = noise_candidates[-3:]
    noise_dl = float(np.median([profile[s][1] for s in tail])) if tail else 0.0
    noise_dtheta = float(np.median([profile[s][2] for s in tail])) if tail else 0.0
    thresh = (max(3.0 * noise_dl, floor_dl), max(3.0 * noise_dtheta, floor_dtheta))

    #: a "full" verdict needs the tube to reach well past the *known*
    #: table radius for this rim type on both sides -- not just enough
    #: shells for the scan below to find *some* candidate, but enough
    #: depth that a short measurement tube's own far, free (unfused) rim
    #: cannot be mistaken for a settled interior (module docstring of
    #: `tests/test_hexfold_seam_decay.py`: a short tube pins a lower
    #: bound, a long one the real decay length).  Below that depth, skip
    #: the scan and report the deepest shell reached as a lower bound.
    table_radius = join.SEAM_RADIUS.get(rim_type, join._SEAM_RADIUS_DEFAULT)
    min_trusted_depth = 2 * (table_radius + 2)
    seam_radius: int | None = None
    if shells_sorted and shells_sorted[-1] >= min_trusted_depth:
        for s in core_shells:
            beyond = [x for x in core_shells if x > s]
            if beyond and all(
                profile[x][1] < thresh[0] and profile[x][2] < thresh[1] for x in beyond
            ):
                seam_radius = s
                break
    if seam_radius is None:
        # conservative, not "however far the scan above got": a tube that
        # failed the depth gate is too short to trust anywhere near its
        # own far end either (the same free-rim-proximity problem the
        # noise sample above excludes), so the lower bound is half its
        # own measured depth, never the near-full-depth `core_shells[-1]`
        # a short, mostly-core-shells tube would otherwise report.
        seam_radius = shells_sorted[-1] // 2 if shells_sorted else 0
        coverage = "lower-bound"
    else:
        coverage = "full"

    # gr456641: neither rung's convergence signal reaches this function (the
    # stick relaxer runs a fixed iteration count and drops its own max_force;
    # the geo relaxer's `trace.converged` is asserted only by the injecting
    # caller), so an unconverged free relax whose far, unfused rim is still
    # drifting was silently reported as a confident "full"/"lower-bound"
    # decay.  Read the symptom directly off `max_disp`: `core_shells` already
    # excludes the free-rim guard band, so a genuine monotone decay bottoms
    # out at its last core shell (its tail equals its minimum), whereas an
    # unsettled tube bottoms out in the interior and climbs back toward the
    # far end (see _STABILITY_RISE_FACTOR / _STABILITY_RISE_FLOOR).
    core_disps = [profile[s][0] for s in core_shells if s > 0]
    if len(core_disps) >= 2:
        min_disp = min(core_disps)
        tail_max = max(core_disps[core_disps.index(min_disp) :])
        if tail_max >= _STABILITY_RISE_FACTOR * min_disp and (
            tail_max >= _STABILITY_RISE_FLOOR
        ):
            coverage = "unstable"

    free_block = join.block_from_net(free, cf)
    tiny = (1e-12, 1e-12)
    comp = join.compose(
        free_block,
        free_block.ports["out"],
        free_block,
        free_block.ports["in"],
        0,
        seam_radius={rim_type: seam_radius},
        leak_thresholds={rim_type: tiny},
        relax=relax_fn,
    )
    maxima_dl = 0.0
    maxima_dtheta = 0.0
    for f in comp.findings:
        if f.code == "seam.leak":
            d = dict(f.data)
            maxima_dl = max(maxima_dl, float(d["max_dl"]))
            maxima_dtheta = max(maxima_dtheta, float(d["max_dtheta"]))
    leak_thresh = (
        max(2.0 * maxima_dl, floor_dl),
        max(2.0 * maxima_dtheta, floor_dtheta),
    )

    interior = [
        o
        for o in range(len(free.atoms))
        if dist_in.get(o, 1 << 30) > seam_radius + 2
        and dist_out.get(o, 1 << 30) > seam_radius + 2
    ]
    # radius/pitch are combinatorial facts of (n, m) (SPEC §3 lattice
    # arithmetic), not something a relax can improve on measuring --
    # `lattice.tube_radius`/`translation_vector` give the exact bulk value
    # a defect-free interior sits at (the axial span between the two rims'
    # own dangling-atom centroids is *not* `length_ * pitch`: the dangling
    # rim is only a partial period at each end, so that naive measurement
    # is systematically short by close to a full bond length per end).
    # The relax-dependent facts -- bond lengths, angle -- genuinely need
    # measuring, so those still come from the interior atoms below.
    lat = Lattice(sigma_A=sigma)
    radius_A = tube_radius(n, m, lat)
    t_vec = translation_vector(n, m)
    pitch_A = lat.a * math.sqrt(
        t_vec[0] * t_vec[0] + t_vec[1] * t_vec[1] + t_vec[0] * t_vec[1]
    )

    origin = cf[list(in_atoms)].mean(axis=0)
    axis_vec = cf[list(out_atoms)].mean(axis=0) - origin
    axis_len = float(np.linalg.norm(axis_vec))
    axis_unit = axis_vec / axis_len if axis_len else np.array([0.0, 0.0, 1.0])

    interior_set = set(interior)
    bond_axial: list[float] = []
    bond_circ: list[float] = []
    for i, j, *_rest in free.bonds:
        if i in interior_set and j in interior_set:
            d_vec = cf[i] - cf[j]
            bond_len = float(np.linalg.norm(d_vec))
            if bond_len == 0.0:
                continue
            ax_comp = abs(float(d_vec @ axis_unit)) / bond_len
            (bond_axial if ax_comp > 0.5 else bond_circ).append(bond_len)
    bond_axial_A = float(np.mean(bond_axial)) if bond_axial else sigma
    bond_circ_A = float(np.mean(bond_circ)) if bond_circ else sigma

    angles: list[float] = []
    for o in interior:
        angles.extend(join._angles_at(cf, o, adj.get(o, [])))
    angle_mean_deg = float(np.mean(angles)) if angles else 120.0

    atoms_per_period = n_cells(n, m) * 2
    today = datetime.now(UTC).date().isoformat()

    edge_key = EnvKey(
        zone="edge",
        rim_type=rim_type,
        N=n + m,
        rung=rung,
        relaxer=relaxer_label,
        sigma=f"{sigma:g}",
    )
    bulk_key = EnvKey(
        zone="bulk",
        kind="tube",
        nm=(n, m),
        rung=rung,
        relaxer=relaxer_label,
        sigma=f"{sigma:g}",
    )
    edge = EdgeMotif(
        key=edge_key,
        profile={
            s: (round(v[0], 5), round(v[1], 5), round(v[2], 4))
            for s, v in profile.items()
        },
        noise=(round(noise_dl, 5), round(noise_dtheta, 4)),
        thresh=(round(thresh[0], 5), round(thresh[1], 4)),
        seam_radius=seam_radius,
        leak_thresh=(round(leak_thresh[0], 5), round(leak_thresh[1], 4)),
        shells=shells_sorted[-1] if shells_sorted else 0,
        measured_on=today,
        coverage=coverage,
        source="measured",
        hexfold_version=__version__,
    )
    bulk = BulkCell(
        key=bulk_key,
        radius_A=round(radius_A, 4),
        pitch_A=round(pitch_A, 4),
        bond_axial_A=round(bond_axial_A, 4),
        bond_circ_A=round(bond_circ_A, 4),
        angle_mean_deg=round(angle_mean_deg, 3),
        atoms_per_period=atoms_per_period,
        measured_on=today,
        source="measured",
        hexfold_version=__version__,
    )
    return edge, bulk
