"""``realize(strategy='simp')`` — the se → :mod:`precis.structsolve.simp` →
cad field-leaf bridge (docs/backlog/structural-solution-space.md "Slice 4
bridge", round A: the solve and the binding; print intents are round B).

The analytic ``realize`` (:mod:`precis_se.realize`) seeds a cad design
from the block's envelope. This strategy instead **solves** for the
block's material: the block's effective envelope is voxelised in its own
LOCAL frame (:func:`precis.cad.relate.component_sdf_np` at ``pitch`` on
an element-centred grid — inside = active), its declared
``objectives.force`` becomes nodal loads and ``objectives.fixed`` becomes
supports, :func:`~precis.structsolve.simp.simp_optimize` minimises
compliance at ``volfrac`` under the AM overhang filter for the chosen
``build_dir``, and the density comes back into the cad kernel as a
sampled-field leaf (:func:`precis.cad.fieldops.from_density` →
``store.put_field`` → a new cad design whose root is ``field:<sha>``),
which the block is then bound to. From there it is ordinary geometry:
``view='print'``/``'fab'``/``'bom'`` see a realized block.

**Two halves, and a job.** A solve is minutes at a real pitch, so the op
never runs it inline (the MCP thread-pool lesson ``pcb_place`` already
learned): :func:`prepare_simp` is the op's pure validation — every
refusal happens here, at ``edit`` time, against the in-memory tree — and
returns a :class:`SimpRequest`; the handler enqueues one ``se_simp`` job
(:mod:`precis_se.simp_job`) per request *after* the tree is saved, and
the job calls :func:`run_simp`, which is also callable in-process (the
tests do). Inside ``run_simp``, :func:`solve_simp` is pure over the tree
and :func:`realize_simp` holds the store writes.

**Where the load and the support sit.** se's ``objectives.force`` is a
vector and ``objectives.fixed`` an axis set — neither carries a position
on the block (:mod:`precis_se.stability` treats a block as a point). A
SIMP domain needs one for each, so the op takes ``load_at=`` and
``fixed_at=``: a **face token** (``x+``/``x-``/``y+``/``y-``/``z+``/
``z-`` — every domain-touching node on that face of the envelope's box,
the force shared equally between them) or a **port name** (a port with a
pose in the block frame — the eight nodes of the nearest active
element). A port without a pose is refused, not guessed. Nothing is
stored back on the objectives: the location is a solve input and lives
in the run summary.

**Passive solid.** Every element touching a load or support node is
pinned at density 1 (``simp_optimize(passive=...)``): a loaded face that
thins to a skin is the classic SIMP artefact.

**Build direction.** The engine's AM filter is ``+z`` only; the bridge
permutes/flips the domain, loads and supports so the requested axis is
``+z`` for the solve and maps the density back. ``build_dir`` defaults
to the envelope's largest face down, read off its bounding box (the
thinnest extent's negative side — ``z+`` on a tie, then ``y+``, ``x+``),
and the op echoes which it chose; it is stored on the block's
``build_frame`` with ``origin='simp'`` so ``view='print'`` verifies that
frame instead of searching (:mod:`precis_se.printing`).

**Re-realize mints a sibling.** A block holds ONE binding
(``se_blocks.bound_kind``/``bound_design``), so a second solve mints a
new cad design (``<design>-<block>``, numeric suffix on collision) and
switches the binding to it; the previous cad design is never touched
and stays reachable two ways — every run in the se ref's
``meta.simp.runs`` names its cad slug, and each minted cad design
carries a ``derived-from`` link to the se design. (``realized-by`` is
not used: that relation targets a procurable ``component`` and
``sync_realized_by`` projects only component bindings.)

Advisory tier throughout: the compliance is a voxel-model estimate; the
run summary carries the engine's own honesty notes and never a bare
number.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.cad import bulk as cad_bulk
from precis.cad import dsl as cad_dsl
from precis.cad import fieldops
from precis.cad.fieldops import open as open_field
from precis.cad.graph import Design as CadDesign
from precis.cad.primitives import Field
from precis.cad.relate import component_sdf_np
from precis.cad.scene import NodeSpec, SceneSpec
from precis.cad.vec import as_vec3
from precis.cad.vec import pose as cad_pose
from precis.structsolve.simp import (
    ENGINE_VERSION,
    SimpResult,
    overhang_violations,
    simp_optimize,
)
from precis_se import capabilities as se_caps
from precis_se import persist
from precis_se.modes import ModeError, parse_mode
from precis_se.ops import (
    OpError,
    SeBlock,
    SeTree,
    apply_ops,
    effective_envelope,
    print_intent,
)
from precis_se.ops import effective_ports as se_effective_ports
from precis_se.realize import _unique_cad_slug, resolve_realize_target

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

log = logging.getLogger(__name__)

#: The job type the handler enqueues (``precis.job_types`` entry point).
JOB_TYPE = "se_simp"
#: The se ref's ``meta`` key the run summaries live under.
META_KEY = "simp"
#: Axis tokens ``build_dir=`` / ``load_at=`` / ``fixed_at=`` accept.
AXIS_TOKENS: tuple[str, ...] = ("x+", "x-", "y+", "y-", "z+", "z-")
#: Optimality-criteria iteration budget: the engine's own default and the
#: cap the op enforces (a run that has not converged by 300 OC steps is
#: oscillating, not descending — the engine's notes say so either way).
DEFAULT_MAX_ITER = 60
MAX_ITER_CAP = 300
#: Element budget per solve — 500k hex elements is ~1.5M DOFs, the top of
#: what the matrix-free PCG finishes in minutes on one node. A finer pitch
#: than this allows is refused with the count, never silently coarsened.
MAX_ELEMENTS = 500_000
#: The capability field the house voxel pitch is read from (millimetres).
PITCH_FIELD = "simp_pitch"
#: ``build_frame.origin`` stamped on a SIMP-realized block.
FRAME_ORIGIN = "simp"

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


class SimpBridgeError(OpError):
    """A refusal from this module — an :class:`~precis_se.ops.OpError`, so
    the op walker maps it to ``BadInput`` like every other op refusal."""


@dataclass(frozen=True)
class SimpRequest:
    """One validated ``realize(strategy='simp')`` — everything the solve
    needs, JSON-round-trippable (:meth:`to_params`/:meth:`from_params`)
    because it travels as the ``se_simp`` job's params. Lengths in
    metres; ``build_dir_source`` records whether the caller gave the
    direction or the largest-face default chose it."""

    block: str
    mode: str
    pitch: float
    volfrac: float
    build_dir: str
    build_dir_source: str
    load_at: str
    fixed_at: str
    round_r: float | None = None
    open_r: float | None = None
    close_r: float | None = None
    max_iter: int = DEFAULT_MAX_ITER

    def to_params(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "mode": self.mode,
            "pitch": self.pitch,
            "volfrac": self.volfrac,
            "build_dir": self.build_dir,
            "build_dir_source": self.build_dir_source,
            "load_at": self.load_at,
            "fixed_at": self.fixed_at,
            "round": self.round_r,
            "open": self.open_r,
            "close": self.close_r,
            "max_iter": self.max_iter,
        }

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> SimpRequest:
        def _opt(key: str) -> float | None:
            v = params.get(key)
            return None if v is None else float(v)

        return cls(
            block=str(params["block"]),
            mode=str(params["mode"]),
            pitch=float(params["pitch"]),
            volfrac=float(params["volfrac"]),
            build_dir=str(params["build_dir"]),
            build_dir_source=str(params.get("build_dir_source") or "given"),
            load_at=str(params["load_at"]),
            fixed_at=str(params["fixed_at"]),
            round_r=_opt("round"),
            open_r=_opt("open"),
            close_r=_opt("close"),
            max_iter=int(params.get("max_iter") or DEFAULT_MAX_ITER),
        )


@dataclass
class SimpSolve:
    """:func:`solve_simp`'s answer: the engine result, the field the block
    will be bound to (after any morphology), and the JSON-safe run
    summary (without the store-assigned ``cad``/``field_sha`` keys, which
    :func:`realize_simp` adds)."""

    result: SimpResult
    field: Field
    summary: dict[str, Any]
    #: Morphology findings (``open`` reporting what it erased), already
    #: rendered to one line each.
    findings: list[str]


@dataclass
class SimpOutcome:
    """:func:`run_simp`'s answer — the echo for the caller plus the
    summary as stored."""

    echo: str
    cad_slug: str
    summary: dict[str, Any]


# --------------------------------------------------------------------------
# geometry plumbing: envelope → domain, tokens → node sets, axis permutation
# --------------------------------------------------------------------------


def _envelope_design(envelope: str) -> tuple[CadDesign, Any]:
    """The block's envelope as a one-component cad design at identity
    pose (the block's own local frame, the frame every se geometry
    consumer samples envelopes in — :func:`precis_se.atomic.validate.
    envelope_fit`'s reasoning)."""
    try:
        prim = cad_dsl.build_config(envelope)
    except cad_dsl.DslError as exc:
        raise SimpBridgeError(f"realize(simp): envelope {envelope!r}: {exc}") from exc
    design = CadDesign()
    identity = cad_pose(as_vec3([0.0, 0.0, 0.0]), as_vec3([0.0, 0.0, 0.0]))
    design.add_component("envelope", design.prim("envelope", prim, identity))
    return design, design.components["envelope"]


def _grid_shape(lo: np.ndarray, hi: np.ndarray, pitch: float) -> tuple[int, int, int]:
    ext = np.maximum(hi - lo, 0.0)
    n = np.maximum(np.ceil(ext / pitch - 1e-9), 1).astype(int)
    return (int(n[0]), int(n[1]), int(n[2]))


def _default_build_dir(lo: np.ndarray, hi: np.ndarray) -> str:
    """Largest face down, read off the bounding box: the thinnest extent's
    axis, printing along its positive direction (plate at the box's
    minimum side). Ties break ``z``, then ``y``, then ``x`` — the order a
    slicer user expects a flat part to land in."""
    ext = hi - lo
    order = (2, 1, 0)
    best = min(order, key=lambda a: (float(ext[a]), order.index(a)))
    return "xyz"[best] + "+"


def _parse_axis_token(token: str, what: str) -> tuple[int, int]:
    """``'x+'`` → ``(axis index, +1|-1)``."""
    t = str(token).strip().lower()
    if len(t) != 2 or t[0] not in _AXIS_INDEX or t[1] not in "+-":
        raise SimpBridgeError(
            f"realize(simp): {what} must be one of {', '.join(AXIS_TOKENS)}, "
            f"got {token!r}"
        )
    return _AXIS_INDEX[t[0]], (1 if t[1] == "+" else -1)


def _touches_active(domain: np.ndarray, node: tuple[int, int, int]) -> bool:
    i, j, k = node
    nx, ny, nz = domain.shape
    sl = (
        slice(max(i - 1, 0), min(i + 1, nx)),
        slice(max(j - 1, 0), min(j + 1, ny)),
        slice(max(k - 1, 0), min(k + 1, nz)),
    )
    return bool(domain[sl].any())


def _face_nodes(
    domain: np.ndarray, token: str, what: str
) -> list[tuple[int, int, int]]:
    axis, sign = _parse_axis_token(token, what)
    shape = domain.shape
    n_nodes = tuple(s + 1 for s in shape)
    fixed_index = shape[axis] if sign > 0 else 0
    out: list[tuple[int, int, int]] = []
    ranges = [range(n_nodes[a]) for a in range(3)]
    ranges[axis] = range(fixed_index, fixed_index + 1)
    for i in ranges[0]:
        for j in ranges[1]:
            for k in ranges[2]:
                if _touches_active(domain, (i, j, k)):
                    out.append((i, j, k))
    return out


def _port_nodes(
    domain: np.ndarray, origin: np.ndarray, pitch: float, point: np.ndarray
) -> list[tuple[int, int, int]]:
    """The eight nodes of the active element whose centre is nearest to
    ``point`` (block-local metres)."""
    idx = np.argwhere(domain)
    centres = origin[None, :] + idx.astype(float) * pitch
    d2 = np.sum((centres - point[None, :]) ** 2, axis=1)
    i, j, k = (int(v) for v in idx[int(np.argmin(d2))])
    return [(i + a, j + b, k + c) for a in (0, 1) for b in (0, 1) for c in (0, 1)]


def _node_set(
    tree: SeTree,
    node: SeBlock,
    token: str,
    domain: np.ndarray,
    origin: np.ndarray,
    pitch: float,
    what: str,
) -> list[tuple[int, int, int]]:
    t = str(token).strip()
    if t.lower() in AXIS_TOKENS:
        nodes = _face_nodes(domain, t, what)
        if not nodes:
            raise SimpBridgeError(
                f"realize(simp): no active element touches the {t!r} face of "
                f"{node.name!r}'s envelope box — {what} has nothing to act on"
            )
        return nodes
    port = se_effective_ports(tree, node).get(t)
    if port is None:
        raise SimpBridgeError(
            f"realize(simp): {what}={t!r} is neither an axis face "
            f"({', '.join(AXIS_TOKENS)}) nor a port of {node.name!r}"
        )
    if port.pose is None:
        raise SimpBridgeError(
            f"realize(simp): port {t!r} on {node.name!r} has no pose in the "
            "block frame — set_port_pose it, or name a face token for "
            f"{what} instead"
        )
    return _port_nodes(domain, origin, pitch, np.asarray(port.pose, dtype=float))


def _elements_touching(
    shape: tuple[int, int, int], nodes: list[tuple[int, int, int]]
) -> np.ndarray:
    out = np.zeros(shape, dtype=bool)
    nx, ny, nz = shape
    for i, j, k in nodes:
        si = slice(max(i - 1, 0), min(i + 1, nx))
        sj = slice(max(j - 1, 0), min(j + 1, ny))
        sk = slice(max(k - 1, 0), min(k + 1, nz))
        out[si, sj, sk] = True
    return out


@dataclass(frozen=True)
class _BuildFrame:
    """The axis permutation that makes ``build_dir`` the engine's ``+z``:
    ``perm[new_axis] = old_axis``; ``flip`` reverses the new z."""

    perm: tuple[int, int, int]
    flip: bool

    @classmethod
    def for_dir(cls, build_dir: str) -> _BuildFrame:
        axis, sign = _parse_axis_token(build_dir, "build_dir")
        perm = {0: (1, 2, 0), 1: (2, 0, 1), 2: (0, 1, 2)}[axis]
        return cls(perm=perm, flip=sign < 0)

    def forward_array(self, a: np.ndarray) -> np.ndarray:
        out = np.transpose(a, self.perm)
        return out[:, :, ::-1] if self.flip else out

    def backward_array(self, a: np.ndarray) -> np.ndarray:
        arr = a[:, :, ::-1] if self.flip else a
        inv = tuple(int(np.argsort(self.perm)[i]) for i in range(3))
        return np.ascontiguousarray(np.transpose(arr, inv))

    def forward_node(
        self, node: tuple[int, int, int], shape: tuple[int, int, int]
    ) -> tuple[int, int, int]:
        n = tuple(node[p] for p in self.perm)
        if self.flip:
            n = (n[0], n[1], shape[self.perm[2]] - n[2])
        return (int(n[0]), int(n[1]), int(n[2]))

    def forward_vec(self, v: np.ndarray) -> tuple[float, float, float]:
        w = [float(v[p]) for p in self.perm]
        if self.flip:
            w[2] = -w[2]
        return (w[0], w[1], w[2])

    def down_local(self) -> list[float]:
        """The block-local build-DOWN unit vector ``view='print'`` frames
        by: the plate sits on the negative side of the build axis."""
        v = [0.0, 0.0, 0.0]
        v[self.perm[2]] = 1.0 if self.flip else -1.0
        return v


# --------------------------------------------------------------------------
# the op's pure half
# --------------------------------------------------------------------------


def _positive_length(op: dict[str, Any], key: str) -> float | None:
    raw = op.get(key)
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError) as exc:
        raise SimpBridgeError(
            f"realize(simp): {key} must be a length in metres, got {raw!r}"
        ) from exc
    if not (v > 0.0 and math.isfinite(v)):
        raise SimpBridgeError(f"realize(simp): {key} must be > 0 m, got {raw!r}")
    return v


def _resolve_pitch(tree: SeTree, node: SeBlock, mode: str, op: dict[str, Any]) -> float:
    given = _positive_length(op, "pitch")
    if given is not None:
        return given
    probe = replace(node, mode=mode)
    resolved = se_caps.resolve(tree, probe, PITCH_FIELD)
    if resolved is None:
        raise SimpBridgeError(
            f"realize(simp) needs pitch= (metres): the house {PITCH_FIELD} "
            f"figure for {mode!r} is null (uncalibrated — se_capabilities.json "
            "says why) and nothing is defaulted in code; pass pitch=, or "
            f"set_process_override(block={node.name!r}, field={PITCH_FIELD!r}, "
            "value=<mm>)"
        )
    return float(resolved.value) / 1000.0


def prepare_simp(tree: SeTree, op: dict[str, Any]) -> tuple[str, SimpRequest]:
    """Validate ``{"op": "realize", "strategy": "simp", "block", "mode",
    "volfrac", "load_at", "fixed_at", "pitch"?, "build_dir"?, "round"?,
    "open"?, "close"?, "max_iter"?}`` against the in-memory tree and
    return ``(echo, request)``. Pure: nothing is mutated or stored — the
    block stays unrealized until the ``se_simp`` job lands.

    Refusals (all :class:`SimpBridgeError`): a bound non-cad block, a
    missing envelope, no ``objectives.force``/``fixed`` (pointing at
    ``set_load``), a null house pitch with no ``pitch=``, ``volfrac``
    outside ``(0, 1)``, an unknown ``build_dir``/``load_at``/``fixed_at``
    token, ``round`` combined with ``open``/``close``, ``max_iter`` beyond
    :data:`MAX_ITER_CAP`, or a grid beyond :data:`MAX_ELEMENTS`."""
    key, node = resolve_realize_target(tree, op)
    if node.bound_kind not in (None, "cad"):
        raise SimpBridgeError(
            f"realize(simp): block {key!r} is bound to a {node.bound_kind} "
            f"({node.bound!r}) — SIMP re-realizes cad-realized or unrealized "
            "blocks only; set_binding(clear=true) first if that is intended"
        )
    mode = op.get("mode")
    if not mode or not str(mode).strip():
        raise SimpBridgeError("realize needs 'mode' (a mode key, e.g. 'fdm/pla')")
    mode = str(mode).strip()
    try:
        parse_mode(mode)
    except ModeError as exc:
        raise SimpBridgeError(f"realize: {exc}") from exc
    envelope = effective_envelope(tree, node)
    if not envelope:
        raise SimpBridgeError(
            f"realize(simp): block {key!r} has no envelope — set_envelope first "
            "(the envelope is the SIMP keep-in domain)"
        )
    force = list((node.objectives or {}).get("force") or [])
    fixed = (node.objectives or {}).get("fixed")
    forced = len(force) == 3 and any(float(x) != 0.0 for x in force)
    if not forced or not fixed:
        lacking: list[str] = []
        missing: list[str] = []
        if not forced:
            lacking.append("load (objectives.force)")
            missing.append("force=[fx, fy, fz] (newtons, non-zero)")
        if not fixed:
            lacking.append("support (objectives.fixed)")
            missing.append("fixed=true (or an axis subset)")
        raise SimpBridgeError(
            f"realize(simp): block {key!r} declares no {' and no '.join(lacking)} "
            f"— SIMP needs both; set_load(block={key!r}, {', '.join(missing)}) "
            "first"
        )

    if isinstance(fixed, list) and set(fixed) != {"x", "y", "z"}:
        # The support holds the declared axes only; a load with a component
        # along a free axis has nothing to react against (a mechanism, not
        # a structure — the FEA would be singular there).
        free = [a for a in "xyz" if a not in fixed]
        if any(float(force["xyz".index(a)]) != 0.0 for a in free):
            raise SimpBridgeError(
                f"realize(simp): block {key!r} is fixed in {''.join(fixed)} only, "
                f"but its force has a component along {'/'.join(free)} — nothing "
                "reacts it; declare fixed=true or drop that component"
            )

    pitch = _resolve_pitch(tree, node, mode, op)
    raw_vf = op.get("volfrac")
    if raw_vf is None:
        raise SimpBridgeError(
            "realize(simp) needs volfrac= (the target material fraction of the "
            "envelope, strictly inside (0, 1))"
        )
    try:
        volfrac = float(raw_vf)
    except (TypeError, ValueError) as exc:
        raise SimpBridgeError(
            f"realize(simp): volfrac must be a number, got {raw_vf!r}"
        ) from exc
    if not (0.0 < volfrac < 1.0):
        raise SimpBridgeError(
            f"realize(simp): volfrac must lie strictly inside (0, 1), got {volfrac} "
            "— 0 is an empty part and 1 is the uncut envelope"
        )

    design, expr = _envelope_design(envelope)
    lo, hi = (np.asarray(v, dtype=float) for v in cad_bulk.expr_aabb(design, expr))
    shape = _grid_shape(lo, hi, pitch)
    n_elements = shape[0] * shape[1] * shape[2]
    if n_elements > MAX_ELEMENTS:
        raise SimpBridgeError(
            f"realize(simp): pitch {pitch:g} m over {key!r}'s envelope box gives "
            f"{shape[0]}x{shape[1]}x{shape[2]} = {n_elements} elements, above "
            f"the {MAX_ELEMENTS} budget one solve is allowed — coarsen pitch"
        )

    raw_dir = op.get("build_dir")
    if raw_dir is None:
        build_dir = _default_build_dir(lo, hi)
        dir_source = "largest-face"
    else:
        _parse_axis_token(str(raw_dir), "build_dir")
        build_dir = str(raw_dir).strip().lower()
        dir_source = "given"

    ports = se_effective_ports(tree, node)
    locations: dict[str, str] = {}
    for what in ("load_at", "fixed_at"):
        raw = op.get(what)
        if raw is None or not str(raw).strip():
            raise SimpBridgeError(
                f"realize(simp) needs {what}= — where on {key!r} the "
                f"{'load acts' if what == 'load_at' else 'support holds'}: an "
                f"envelope face ({', '.join(AXIS_TOKENS)}) or a posed port name"
            )
        token = str(raw).strip()
        if token.lower() in AXIS_TOKENS:
            locations[what] = token.lower()
            continue
        port = ports.get(token)
        if port is None:
            raise SimpBridgeError(
                f"realize(simp): {what}={token!r} is neither an axis face "
                f"({', '.join(AXIS_TOKENS)}) nor a port of {key!r}"
            )
        if port.pose is None:
            raise SimpBridgeError(
                f"realize(simp): port {token!r} on {key!r} has no pose in the "
                f"block frame — set_port_pose it, or give {what} a face token"
            )
        locations[what] = token
    if locations["load_at"] == locations["fixed_at"]:
        raise SimpBridgeError(
            f"realize(simp): load_at and fixed_at both name {locations['load_at']!r} "
            "— a load on its own support does no work"
        )

    round_r = _positive_length(op, "round")
    open_r = _positive_length(op, "open")
    close_r = _positive_length(op, "close")
    if round_r is not None and (open_r is not None or close_r is not None):
        raise SimpBridgeError(
            "realize(simp): round= is open+close at one radius — give either "
            "round=, or open=/close= separately, not both"
        )
    for name, r in (("round", round_r), ("open", open_r), ("close", close_r)):
        if r is not None and r < 0.5 * pitch:
            raise SimpBridgeError(
                f"realize(simp): {name}={r:g} m is below half the pitch "
                f"({pitch:g} m) — the grid cannot resolve it"
            )

    raw_iter = op.get("max_iter")
    max_iter = DEFAULT_MAX_ITER
    if raw_iter is not None:
        try:
            max_iter = int(raw_iter)
        except (TypeError, ValueError) as exc:
            raise SimpBridgeError(
                f"realize(simp): max_iter must be an integer, got {raw_iter!r}"
            ) from exc
        if not (1 <= max_iter <= MAX_ITER_CAP):
            raise SimpBridgeError(
                f"realize(simp): max_iter must lie in 1..{MAX_ITER_CAP}, got {max_iter}"
            )

    request = SimpRequest(
        block=key,
        mode=mode,
        pitch=pitch,
        volfrac=volfrac,
        build_dir=build_dir,
        build_dir_source=dir_source,
        load_at=locations["load_at"],
        fixed_at=locations["fixed_at"],
        round_r=round_r,
        open_r=open_r,
        close_r=close_r,
        max_iter=max_iter,
    )
    dir_note = (
        f"build_dir {build_dir!r} (given)"
        if dir_source == "given"
        else f"build_dir {build_dir!r} (default: largest face of the envelope box down)"
    )
    echo = (
        f"realize({key!r}, strategy='simp'): queued a {JOB_TYPE} job — pitch "
        f"{pitch:g} m over a {shape[0]}x{shape[1]}x{shape[2]} grid, volfrac "
        f"{volfrac:g}, {dir_note}, load at {locations['load_at']!r}, fixed at "
        f"{locations['fixed_at']!r}, max_iter {max_iter}; {key!r} stays "
        f"unrealized until the job lands (advisory tier: the compliance is a "
        "voxel estimate)"
    )
    return echo, request


# --------------------------------------------------------------------------
# the solve (pure over the tree)
# --------------------------------------------------------------------------


def inputs_sha(tree: SeTree, req: SimpRequest) -> str | None:
    """Content hash of everything the solve reads off the tree for
    ``req.block`` — engine version, effective envelope, objectives, pose,
    every posed effective port — plus the request itself. ONE function for
    two consumers so they can never drift: the handler's job idem key
    (a re-submit of the same problem dedupes; a moved ``load_at`` port
    does not) and the job's staleness check before binding (the tree it
    solved on must still hash the same). ``None`` when the block is gone."""
    node = tree.blocks.get(req.block)
    if node is None:
        return None
    payload = {
        "engine": ENGINE_VERSION,
        "envelope": effective_envelope(tree, node),
        "objectives": node.objectives,
        "pose": node.pose,
        "rot": node.rot,
        "ports": {
            name: p.pose
            for name, p in sorted(se_effective_ports(tree, node).items())
            if p.pose is not None
        },
        "request": req.to_params(),
    }
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binarised_overhangs(fld: Field, frame: _BuildFrame) -> int:
    solid = (np.asarray(fld.grid) <= 0.0).astype(float)
    return overhang_violations(frame.forward_array(solid), plate_at_first_solid=True)


def solve_simp(tree: SeTree, req: SimpRequest) -> SimpSolve:
    """Voxelise, load, support, solve, and turn the density into a field —
    the store-free half of :func:`run_simp` (module docstring). Raises
    :class:`SimpBridgeError` for anything :func:`prepare_simp` could not
    see (an envelope that voxelises to nothing at this pitch, a passive
    set that eats the volume budget) with the engine's own wording."""
    node = tree.blocks.get(req.block)
    if node is None:
        raise SimpBridgeError(f"realize(simp): block {req.block!r} no longer exists")
    envelope = effective_envelope(tree, node)
    if not envelope:
        raise SimpBridgeError(f"realize(simp): block {req.block!r} lost its envelope")
    design, expr = _envelope_design(envelope)
    lo, hi = (np.asarray(v, dtype=float) for v in cad_bulk.expr_aabb(design, expr))
    shape = _grid_shape(lo, hi, req.pitch)
    origin = lo + 0.5 * req.pitch  # centre of element [0, 0, 0]
    ii, jj, kk = np.meshgrid(*(np.arange(n) for n in shape), indexing="ij")
    centres = (
        origin[None, :]
        + np.stack([ii.ravel(), jj.ravel(), kk.ravel()], axis=1).astype(float)
        * req.pitch
    )
    sdf = component_sdf_np(design, expr, centres)
    domain = (np.asarray(sdf) <= 0.0).reshape(shape)
    if not domain.any():
        raise SimpBridgeError(
            f"realize(simp): {req.block!r}'s envelope {envelope!r} voxelises to "
            f"no active element at pitch {req.pitch:g} m — the pitch is coarser "
            "than the envelope"
        )

    load_nodes = _node_set(
        tree, node, req.load_at, domain, origin, req.pitch, "load_at"
    )
    fixed_nodes = _node_set(
        tree, node, req.fixed_at, domain, origin, req.pitch, "fixed_at"
    )
    # objectives.force is declared in the world frame (set_load); the domain
    # is the block's local frame — the same inverse-pose rotation
    # printing._local_loads applies.
    xform = cad_pose(as_vec3(node.pose), as_vec3(node.rot))
    force_local = np.asarray(
        xform.inverse().apply_dir(as_vec3(node.objectives["force"])), dtype=float
    )
    fixed_raw = node.objectives.get("fixed")
    axes = "".join(str(a) for a in fixed_raw) if isinstance(fixed_raw, list) else "xyz"
    passive = _elements_touching(shape, load_nodes) | _elements_touching(
        shape, fixed_nodes
    )

    frame = _BuildFrame.for_dir(req.build_dir)
    share = force_local / float(len(load_nodes))
    loads = [
        (frame.forward_node(n, shape), frame.forward_vec(share)) for n in load_nodes
    ]
    supports = [(frame.forward_node(n, shape), axes) for n in fixed_nodes]
    try:
        result = simp_optimize(
            frame.forward_array(domain),
            loads,
            supports,
            volfrac=req.volfrac,
            h=req.pitch,
            max_iter=req.max_iter,
            build_dir="z+",
            passive=frame.forward_array(passive),
        )
    except ValueError as exc:
        raise SimpBridgeError(f"realize(simp): {exc}") from exc
    density = frame.backward_array(result.density)

    fld = fieldops.from_density(density, 0.5, pitch=req.pitch, origin=origin)
    volume_raw = float(np.count_nonzero(np.asarray(fld.grid) <= 0.0)) * req.pitch**3
    findings: list[str] = []
    morphology: dict[str, Any] = {}
    try:
        if req.round_r is not None:
            opened = open_field(fld, req.round_r)
            findings.extend(f.detail for f in opened.findings)
            fld = fieldops.close(opened.field, req.round_r)
            morphology["round_m"] = req.round_r
        if req.open_r is not None:
            opened = open_field(fld, req.open_r)
            findings.extend(f.detail for f in opened.findings)
            fld = opened.field
            morphology["open_m"] = req.open_r
        if req.close_r is not None:
            fld = fieldops.close(fld, req.close_r)
            morphology["close_m"] = req.close_r
    except ValueError as exc:
        raise SimpBridgeError(f"realize(simp): morphology: {exc}") from exc
    volume_final = float(np.count_nonzero(np.asarray(fld.grid) <= 0.0)) * req.pitch**3
    if volume_final <= 0.0:
        raise SimpBridgeError(
            "realize(simp): the morphology erased the whole body ("
            + "; ".join(findings)
            + ") — nothing left to bind; lower the radius or raise volfrac"
        )
    if morphology:
        morphology["volume_before_m3"] = volume_raw
        morphology["volume_after_m3"] = volume_final

    summary: dict[str, Any] = {
        "engine": ENGINE_VERSION,
        "inputs_sha": inputs_sha(tree, req),
        "block": req.block,
        "mode": req.mode,
        "pitch_m": req.pitch,
        "volfrac": req.volfrac,
        "build_dir": req.build_dir,
        "build_dir_source": req.build_dir_source,
        "load_at": req.load_at,
        "fixed_at": req.fixed_at,
        "grid": list(shape),
        "active_elements": int(np.count_nonzero(domain)),
        "passive_elements": int(np.count_nonzero(passive & domain)),
        "load_nodes": len(load_nodes),
        "support_nodes": len(fixed_nodes),
        "force_local_n": [float(x) for x in force_local],
        "compliance_first": float(result.compliance_history[0]),
        "compliance_last": float(result.compliance_history[-1]),
        "volume_fraction": float(result.volume_fraction),
        "iterations": int(result.iterations),
        "converged": bool(result.converged),
        "overhang_violation_count": int(result.overhang_violation_count),
        "overhang_violations_field": _binarised_overhangs(fld, frame),
        "volume_m3": volume_final,
        "morphology": morphology,
        "morphology_findings": findings,
        "max_iter": req.max_iter,
        "notes": list(result.notes),
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return SimpSolve(result=result, field=fld, summary=summary, findings=findings)


# --------------------------------------------------------------------------
# the store half + the whole run
# --------------------------------------------------------------------------


class SimpStale(SimpBridgeError):
    """The design changed between the solve's snapshot and the bind — the
    solved field belongs to a block that no longer exists in that form.
    Nothing is bound; the caller re-runs ``realize``."""


def _require_fresh(tree: SeTree, req: SimpRequest, solve: SimpSolve, when: str) -> None:
    now = inputs_sha(tree, req)
    if now is None:
        raise SimpStale(
            f"realize(simp): block {req.block!r} no longer exists ({when}) — "
            "nothing to bind; re-run realize if it was re-added"
        )
    if now != solve.summary["inputs_sha"]:
        raise SimpStale(
            f"realize(simp): design changed while solving ({when}: {req.block!r}'s "
            "envelope, loads, pose or a port moved since the snapshot) — the "
            "solved field is stale and was NOT bound; re-run realize"
        )


def _load_live(store: Store, ref_id: int, slug: str) -> SeTree:
    tree = persist.load_tree(store, ref_id)
    tree.own_slug = slug
    tree.foreign = persist.foreign_resolver(store)
    return tree


def _record_failure(
    store: Store, ref_id: int, *, cad_slug: str, retired: bool, reason: str
) -> None:
    """``meta.simp.failed`` — the orphaned (or retired) cad design is
    discoverable even when nothing else about the run landed. Best-effort:
    a failure to record a failure must not mask the original."""
    try:
        with persist.tree_mutation(store, ref_id) as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
            existing = dict(((row[0] if row else None) or {}).get(META_KEY) or {})
            existing["failed"] = {
                "cad": cad_slug,
                "retired": retired,
                "reason": reason[:500],
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            store.stamp_ref_meta(ref_id, {META_KEY: existing}, conn=conn)
    except Exception:  # pragma: no cover — defensive
        log.warning("se_simp: could not record the failed run on ref %s", ref_id)


def realize_simp(
    store: Store, ref_id: int, req: SimpRequest, solve: SimpSolve
) -> SimpOutcome:
    """Store the field, mint the cad design rooted at it, then — inside the
    design's :func:`~precis_se.persist.tree_mutation` lock, on a FRESH
    tree — verify the solve inputs still hash to ``solve``'s, bind the
    block (``set_binding`` + ``set_mode`` + ``build_frame``), save the
    tree and stamp the run summary, all in that one transaction. Never
    touches a previous realization (module docstring).

    Ordering is the durability argument: ``put_field``/``cad_save``
    commit on their own (unavoidable — they are the cad store's own
    transactions), so they go first; the binding and the success stamp
    are one tx, so ``meta.simp.last`` can never claim a bind that did not
    land. If anything fails after the cad design was minted — including
    :class:`SimpStale`, the design having changed under the solve — the
    minted design is retired (``cad_delete``) and ``meta.simp.failed``
    names it, then the error re-raises."""
    # Function-local: handler -> atomic.apply -> this module at import time,
    # so the module-level import would be a cycle; the card-text rule is
    # the handler's and is not duplicated here.
    from precis_se.handler import _card_text

    ref = store.get_ref(kind="se", id=ref_id)
    if ref is None:
        raise SimpBridgeError(f"realize(simp): se design ref {ref_id} not found")
    design_slug = str(ref.slug)
    # Cheap optimistic check before minting anything: the common stale
    # case (an edit landed during the minutes of solving) then costs no
    # orphan at all. The authoritative check is the locked one below.
    _require_fresh(_load_live(store, ref_id, design_slug), req, solve, "pre-mint check")

    sha = store.put_field(
        ref_id,
        solve.field,
        provenance={
            "source": JOB_TYPE,
            "se_design": design_slug,
            "block": req.block,
            "build_dir": req.build_dir,
            "inputs_sha": solve.summary["inputs_sha"],
            "engine": ENGINE_VERSION,
        },
    )
    slug, note = _unique_cad_slug(store, f"{design_slug}-{req.block}")
    title = f"{req.block} ({design_slug} realize simp)"
    spec = SceneSpec(
        nodes=[
            NodeSpec(name="body", op="add", config=f"field:{sha}", component="part")
        ],
        components=["part"],
    )
    card_text = (
        f"{title}: SIMP density field at {req.pitch:g} m pitch, volfrac "
        f"{req.volfrac:g}, build_dir {req.build_dir}"
    )
    cad_ref, _created, _n = store.cad_save(
        slug=slug, title=title, spec=spec, card_text=card_text
    )

    summary = dict(solve.summary)
    summary["cad"] = slug
    summary["field_sha"] = sha
    try:
        with persist.tree_mutation(store, ref_id) as conn:
            tree = _load_live(store, ref_id, design_slug)
            _require_fresh(tree, req, solve, "at bind time")
            node = tree.blocks[req.block]
            previous = node.bound if node.bound_kind == "cad" else None
            apply_ops(
                tree,
                [
                    {
                        "op": "set_binding",
                        "block": req.block,
                        "kind": "cad",
                        "design": slug,
                    },
                    {"op": "set_mode", "block": req.block, "mode": req.mode},
                ],
            )
            frame = _BuildFrame.for_dir(req.build_dir)
            intent = print_intent(node)  # a group root's intent is not a pin
            node.build_frame = {
                "down": frame.down_local(),
                "origin": FRAME_ORIGIN,
                "build_dir": req.build_dir,
            }
            if intent is not None:
                node.build_frame["intent"] = intent
            row = conn.execute(
                "SELECT title, meta FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
            meta_now = dict((row[1] if row else None) or {})
            title_now = (row[0] if row else None) or design_slug
            description = str(meta_now.get("description") or "").strip()
            persist.save_tree(
                store,
                ref_id=ref_id,
                tree=tree,
                card_text=_card_text(title_now, description, tree),
                conn=conn,
            )
            summary["previous_cad"] = previous
            existing = dict(meta_now.get(META_KEY) or {})
            runs = [r for r in (existing.get("runs") or []) if isinstance(r, dict)]
            runs.append(summary)
            existing.pop("failed", None)
            existing.update({"last": summary, "runs": runs})
            store.stamp_ref_meta(ref_id, {META_KEY: existing}, conn=conn)
    except Exception as exc:
        retired = False
        try:
            store.cad_delete(int(cad_ref.id))
            retired = True
        except Exception:  # pragma: no cover — defensive
            log.warning("se_simp: could not retire orphaned cad design %s", slug)
        _record_failure(store, ref_id, cad_slug=slug, retired=retired, reason=str(exc))
        raise

    # After the commit, like the handler: derived projections must never
    # roll back a saved bind.
    persist.sync_realized_by(store, ref_id, tree)
    try:
        store.add_link(
            src_ref_id=int(cad_ref.id),
            dst_ref_id=int(ref_id),
            relation="derived-from",
            meta={
                "se_simp": True,
                "block": req.block,
                "inputs_sha": summary["inputs_sha"],
            },
        )
    except Exception:  # pragma: no cover — defensive, mirrors cad's sync
        log.warning("se_simp: derived-from link failed for cad %s", slug)

    res = solve.result
    echo = (
        f"realize({req.block!r}, strategy='simp'): bound to new cad design "
        f"{slug!r} (root field:{sha[:12]}), mode {req.mode!r}, build_dir "
        f"{req.build_dir!r} pinned on the block; compliance "
        f"{res.compliance_history[0]:.4g} -> {res.compliance_history[-1]:.4g}, "
        f"volume fraction {res.volume_fraction:.3f}, {res.iterations} iteration(s), "
        f"{'converged' if res.converged else 'BUDGET EXHAUSTED'}, "
        f"{summary['overhang_violations_field']} overhang voxel(s) on the stored field"
    )
    if note:
        echo += f" ({note})"
    if previous:
        echo += f"; previous realization {previous!r} left in place (meta.simp.runs)"
    if solve.findings:
        echo += "; morphology: " + "; ".join(solve.findings)
    return SimpOutcome(echo=echo, cad_slug=slug, summary=summary)


def run_simp(store: Store, ref_id: int, req: SimpRequest) -> SimpOutcome:
    """The whole job body, callable in-process: snapshot the design's live
    tree, :func:`solve_simp` on it OUTSIDE any lock (minutes), then
    :func:`realize_simp`, which re-loads under the per-ref lock, checks
    the snapshot is still current and binds. A design edited meanwhile
    is never clobbered: an unrelated edit survives and the bind lands on
    top of it; an edit to the solve's inputs makes the job fail with
    :class:`SimpStale` and bind nothing."""
    ref = store.get_ref(kind="se", id=ref_id)
    if ref is None:
        raise SimpBridgeError(f"realize(simp): se design ref {ref_id} not found")
    tree = _load_live(store, ref_id, str(ref.slug))
    solve = solve_simp(tree, req)
    return realize_simp(store, ref_id, req, solve)


__all__ = [
    "AXIS_TOKENS",
    "DEFAULT_MAX_ITER",
    "FRAME_ORIGIN",
    "JOB_TYPE",
    "MAX_ELEMENTS",
    "MAX_ITER_CAP",
    "META_KEY",
    "PITCH_FIELD",
    "SimpBridgeError",
    "SimpOutcome",
    "SimpRequest",
    "SimpSolve",
    "SimpStale",
    "inputs_sha",
    "prepare_simp",
    "realize_simp",
    "run_simp",
    "solve_simp",
]
