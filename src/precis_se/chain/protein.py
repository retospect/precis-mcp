"""Protein chain import — a C-alpha trace as helix records, on the nucleic domain.

``import_trace`` turns the C-alpha atoms of one chain of a PDB / mmCIF text into
the same ``helix`` ``chain`` records ``declare_helix`` writes, so the shipped
machinery (``layout_chain`` tube, ``chain_bend`` / ``chain_clash`` findings,
``relax_chain``) runs on a protein unchanged. This module is the store-free
half: the numbers, the secondary-structure call and the record builder. The op
that writes the records into a tree is :func:`precis_se.ops._op_import_trace`.

**Motifs** (:data:`MOTIFS`, added to :data:`precis_se.chain.nucleic.MOTIFS` so
a helix record can name them):

- ``alpha-helix`` — rise 0.15 nm per residue, 100 degrees per residue
  (3.6 residues per turn), C-alpha radius 0.23 nm about the axis (Pauling &
  Corey 1951; the textbook values, not a fit to any one structure).
- ``beta-strand`` — rise 0.33 nm per residue, strictly alternating (twist
  180 degrees); its C-alpha radius is the zig-zag half-amplitude that keeps
  the 0.38 nm C-alpha to C-alpha bond: ``sqrt(0.38^2 - 0.33^2) / 2``.
- ``protein-coil`` — 0.38 nm C-alpha to C-alpha contour per residue (the
  trans-peptide bond), no twist, persistence length about 0.5 nm (the
  unfolded-chain literature value, order of magnitude). Its backbone sits
  on the centre line. All three motifs use a separate 0.5 nm default tube.

The alpha-helix persistence length is the ~100 nm order of magnitude from
single-molecule work; it is only read by ``relax_chain`` as a hinge stiffness.

**Steps.** ``per-residue``: one helix block, one unit per residue, the
centre line IS the C-alpha polyline (``protein-coil`` with the chain's own mean
C-alpha spacing as the rise, so unit k sits on residue k whatever the real
bond-length drift). ``per-ss-segment``: one helix block per secondary-structure
run — a helix run on the fitted axis with the ``alpha-helix`` motif and a
``phase0`` that puts unit 0 on its C-alpha, a strand run likewise with
``beta-strand``, a coil run on the C-alpha polyline. Each fitted run uses an
axial rise derived from its measured length and residue count. Coil runs
extend to the next run's first C-alpha to connect their tubes.

**Secondary structure** is called from C-alpha distances only (no DSSP, no
hydrogen bonds): ``i -> i+3`` near 0.5 nm and ``i -> i+4`` near 0.62 nm is
helix, ``i -> i+2`` near or past 0.6 nm is strand; runs shorter than
:data:`MIN_HELIX_RUN` / :data:`MIN_STRAND_RUN` fall back to coil.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from precis_chain.fibre import r0_seed, unit_frames
from precis_chain.motif import Motif
from precis_chain.path import Path, polyline

#: Trans-peptide C-alpha to C-alpha distance, metres.
CA_CA_M = 0.38e-9

#: A C-alpha pair further apart than this is a chain break (a missing
#: residue or a cis-proline-free gap), refused rather than bridged.
MAX_CA_CA_M = 0.45e-9

ALPHA_RISE_M = 0.15e-9
ALPHA_TWIST_RAD = math.radians(100.0)
ALPHA_RADIUS_M = 0.23e-9
BETA_RISE_M = 0.33e-9
BETA_TWIST_RAD = math.pi
BETA_RADIUS_M = 0.5 * math.sqrt(CA_CA_M**2 - BETA_RISE_M**2)
COIL_LP_M = 0.5e-9
ALPHA_LP_M = 100e-9

#: Default tube radius ``layout_chain`` sweeps around the trace, metres.
DEFAULT_TUBE_RADIUS_M = 0.5e-9

#: Tightest C-alpha centre-line bend the protein motifs tolerate. A real
#: alpha-helix's C-alpha trace bends at ~0.26 nm, so the nucleic default
#: would flag every helix; these are below any real trace.
_TRACE_MIN_BEND_M = 0.15e-9

MOTIFS: dict[str, Motif] = {
    "alpha-helix": Motif(
        name="alpha-helix",
        rise=ALPHA_RISE_M,
        twist=ALPHA_TWIST_RAD,
        radius=ALPHA_RADIUS_M,
        min_bend_radius=1e-9,
        persistence_length=ALPHA_LP_M,
        contour_per_unit=CA_CA_M,
    ),
    "beta-strand": Motif(
        name="beta-strand",
        rise=BETA_RISE_M,
        twist=BETA_TWIST_RAD,
        radius=BETA_RADIUS_M,
        min_bend_radius=1e-9,
        persistence_length=COIL_LP_M,
        contour_per_unit=CA_CA_M,
    ),
    "protein-coil": Motif(
        name="protein-coil",
        rise=CA_CA_M,
        twist=0.0,
        radius=0.0,
        min_bend_radius=_TRACE_MIN_BEND_M,
        persistence_length=COIL_LP_M,
        contour_per_unit=CA_CA_M,
    ),
}

#: ``nucleic`` value stamped on a protein helix record (the field names the
#: polymer; the nucleic-only readers default it to DNA when absent).
PROTEIN = "protein"

STEPS = ("per-residue", "per-ss-segment")

MIN_HELIX_RUN = 5
MIN_STRAND_RUN = 3

# C-alpha distance windows, metres (ideal helix: i+3 0.505 nm, i+4 0.62 nm).
_H_I3 = (0.45e-9, 0.56e-9)
_H_I4 = (0.55e-9, 0.70e-9)
_E_I2_MIN = 0.60e-9


class TraceError(ValueError):
    """An unusable trace (no C-alpha atoms, a chain break, a bad step)."""


@dataclass(frozen=True)
class TraceRun:
    """One imported block: its name suffix, stored record and residue range
    (0-based, inclusive, in trace order)."""

    suffix: str
    record: dict[str, Any]
    first: int
    last: int
    ss: str


def check_trace(ca_m: np.ndarray) -> np.ndarray:
    """Vet an ``(n, 3)`` metre trace: at least two residues, finite, and no
    consecutive pair further apart than :data:`MAX_CA_CA_M`."""
    ca = np.asarray(ca_m, dtype=float)
    if ca.ndim != 2 or ca.shape[1] != 3 or ca.shape[0] < 2:
        raise TraceError("a trace needs >= 2 C-alpha atoms, shape (n, 3)")
    if not np.all(np.isfinite(ca)):
        raise TraceError("trace coordinates must be finite")
    gaps = np.linalg.norm(np.diff(ca, axis=0), axis=1)
    if np.any(gaps == 0):
        raise TraceError("consecutive C-alpha positions must be distinct")
    bad = np.flatnonzero(gaps > MAX_CA_CA_M)
    if bad.size:
        k = int(bad[0])
        raise TraceError(
            f"chain break between C-alpha {k} and {k + 1}: "
            f"{gaps[k] * 1e9:.2f} nm apart (a trace step is ~0.38 nm) — "
            "split it with start=/end= and import each piece separately"
        )
    return ca


def assign_ss(ca_m: np.ndarray) -> list[str]:
    """Per-residue ``'H'`` / ``'E'`` / ``'C'`` from C-alpha distances alone."""
    ca = check_trace(ca_m)
    n = ca.shape[0]
    ss = ["C"] * n

    def dist(i: int, k: int) -> float:
        return float(np.linalg.norm(ca[i + k] - ca[i]))

    helix = [False] * n
    for i in range(n - 4):
        if _H_I3[0] <= dist(i, 3) <= _H_I3[1] and _H_I4[0] <= dist(i, 4) <= _H_I4[1]:
            for j in range(i, i + 5):
                helix[j] = True
    for i in range(n - 2):
        if dist(i, 2) >= _E_I2_MIN:
            for j in range(i, i + 3):
                if not helix[j]:
                    ss[j] = "E"
    for i, flag in enumerate(helix):
        if flag:
            ss[i] = "H"
    # Short runs fall back to coil.
    for label, minimum in (("H", MIN_HELIX_RUN), ("E", MIN_STRAND_RUN)):
        for first, last, lab in _runs(ss):
            if lab == label and last - first + 1 < minimum:
                for j in range(first, last + 1):
                    ss[j] = "C"
    return ss


def _runs(ss: list[str]) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    start = 0
    for i in range(1, len(ss) + 1):
        if i == len(ss) or ss[i] != ss[start]:
            out.append((start, i - 1, ss[start]))
            start = i
    return out


def _unit(v: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0.0 else np.array([0.0, 0.0, 1.0])


def _phase0(path: Path, motif: Motif, n: int, ca0: np.ndarray) -> float:
    """The ``phase0`` that puts unit 0's backbone point on ``ca0`` — solved
    from the same frame field :func:`precis_se.chain.layout.helix_geometry`
    will build (same path, same ``r0`` seed)."""
    units = unit_frames(
        path, motif, n, 0.0, r0=r0_seed(path, np.array([1.0, 0.0, 0.0]))
    )
    frame = units.frames[0]
    v = ca0 - units.origins[0]
    return float(math.atan2(float(v @ frame[:, 2]), float(v @ frame[:, 1])))


def _axis_path(ca: np.ndarray, motif: Motif) -> np.ndarray:
    """Centre line for a helix or strand run, from first to last unit origin.

    Helix: each interior C-alpha steps ``motif.radius`` along the bisector of
    its two neighbours (exactly radial on an ideal helix); the two end
    residues continue the axis by one rise. Strand: the midpoints of
    consecutive C-alpha pairs, extrapolated to the terminal residues."""
    n = ca.shape[0]
    if motif.name == "alpha-helix":
        pts = np.empty_like(ca)
        for i in range(1, n - 1):
            pts[i] = ca[i] + motif.radius * _unit(ca[i - 1] + ca[i + 1] - 2 * ca[i])
        pts[0] = pts[1] - motif.rise * _unit(pts[2] - pts[1])
        pts[-1] = pts[-2] + motif.rise * _unit(pts[-2] - pts[-3])
        return pts
    mids = 0.5 * (ca[:-1] + ca[1:])
    return np.vstack(
        [
            mids[0] - 0.5 * (mids[1] - mids[0]),
            mids,
            mids[-1] + 0.5 * (mids[-1] - mids[-2]),
        ]
    )


def _coil_path(
    seg: np.ndarray, nxt: np.ndarray | None, prev: np.ndarray | None
) -> np.ndarray:
    """A coil run's C-alpha polyline plus one trailing point: the next run's
    first C-alpha, or (at the chain end) one bond straight on."""
    if nxt is None:
        before = seg[-2] if seg.shape[0] >= 2 else prev
        if before is None:
            before = seg[-1] - np.array([CA_CA_M, 0.0, 0.0])
        nxt = seg[-1] + _unit(seg[-1] - before) * CA_CA_M
    return np.vstack([seg, nxt])


def _mean_gap(ca: np.ndarray) -> float:
    if ca.shape[0] < 2:
        return CA_CA_M
    return float(np.linalg.norm(np.diff(ca, axis=0), axis=1).mean())


def _record(
    motif_name: str,
    n_units: int,
    path_pts: np.ndarray,
    tube_radius_m: float,
    phase0: float,
    rise_m: float | None,
    ss: str,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "role": "helix",
        "motif": motif_name,
        "nucleic": PROTEIN,
        "path": {"waypoints_m": [[float(v) for v in p] for p in path_pts]},
        "n_units": n_units,
        "phase0": phase0,
        "tube_radius_m": tube_radius_m,
        "ss": ss,
    }
    if rise_m is not None:
        rec["rise_m"] = rise_m
    return rec


def build_runs(
    ca_m: np.ndarray, step: str, tube_radius_m: float = DEFAULT_TUBE_RADIUS_M
) -> list[TraceRun]:
    """The helix records for one chain's C-alpha trace (metres, ``(n, 3)``).

    ``step='per-residue'`` gives one record, ``'per-ss-segment'`` one per
    secondary-structure run (module docstring)."""
    if step not in STEPS:
        raise TraceError(f"step must be one of {' | '.join(STEPS)}, got {step!r}")
    if not tube_radius_m > 0.0 or not math.isfinite(tube_radius_m):
        raise TraceError("tube_radius must be a positive length")
    ca = check_trace(ca_m)
    n = ca.shape[0]
    if step == "per-residue":
        ss = assign_ss(ca)
        rec = _record(
            "protein-coil",
            n,
            ca,
            tube_radius_m,
            0.0,
            _mean_gap(ca),
            "".join(ss),
        )
        return [TraceRun("", rec, 0, n - 1, "".join(ss))]
    out: list[TraceRun] = []
    for k, (first, last, label) in enumerate(_runs(assign_ss(ca))):
        seg = ca[first : last + 1]
        count = last - first + 1
        if label in ("H", "E"):
            motif = MOTIFS["alpha-helix" if label == "H" else "beta-strand"]
            pts = _axis_path(seg, motif)
            path = polyline(pts)
            motif = replace(motif, rise=path.length / (count - 1))
            rec = _record(
                motif.name,
                count,
                pts,
                tube_radius_m,
                _phase0(path, motif, count, seg[0]),
                motif.rise,
                label,
            )
        else:
            pts = _coil_path(
                seg,
                ca[last + 1] if last + 1 < n else None,
                ca[first - 1] if first > 0 else None,
            )
            rec = _record(
                "protein-coil",
                count,
                pts,
                tube_radius_m,
                0.0,
                _mean_gap(seg),
                "C",
            )
        out.append(TraceRun(f".ss{k}", rec, first, last, label))
    return out
