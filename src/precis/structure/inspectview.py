"""The atomic leg of the one inspect family (molecular-workspace-code
slice 6, docs/backlog/molecular-workspace-code.md §Inspect).

:mod:`precis.cad.inspect`'s four probes — ``ray``, ``section``,
``overlap``, ``thickness`` — marched over the union of van der Waals
spheres of a :class:`~precis.structure.scene.Scene`
(:func:`precis.structure.probe.vdw_field`), plus the pieces the three
callers share: the ``view='inspect'`` argument grammar, atom selection by
``target``, the atomic observation events merged onto a ray, and the
compact text render. Callers: ``precis_geom.inspect`` (scripts, SI metres
at its edge), the ``structure`` kind (Å bare numbers) and the ``se`` kind
(metres / unit strings crossed to Å at its edge).

Å-native, like the rest of this package: every length in, every length
out is Å. The result dataclasses are :mod:`precis.cad.inspect`'s, whose
fields are suffixed ``_m`` for the cad leg; here they hold Å (the working
unit) until a caller crosses to metres with
:func:`precis.cad.inspect.rescale`. Computed on demand; nothing is stored.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from precis.cad import inspect as ci
from precis.errors import BadInput

from . import probe as st_probe
from .rayview import fmt_s_A, hit_text
from .scene import Scene

PROBES = ("ray", "section", "overlap", "thickness")
#: Every key ``view='inspect'`` accepts (the se kind adds ``block``).
INSPECT_ARGS = frozenset(
    {
        "probe",
        "target",
        "with",
        "origin",
        "axis",
        "normal",
        "pitch",
        "radius",
        "length",
        "profile_samples",
        "events",
        "nominal",
        "ratio",
        "method",
        "grid",
        "radii",
    }
)
#: Atomic observation events a ray merges by default — the ones that need
#: no stored data (``charge``/``relax`` need per-atom levels a bare scene
#: does not carry; ``pass`` is ``view='line'``'s answer).
DEFAULT_EVENTS = ("pi", "frame")
#: Section / thickness pitch floor (Å) and grid budget along the long edge.
_PITCH_FLOOR_A = 0.05
_PITCH_GRID = 600
#: A bond this close to perpendicular to the section normal lies in the
#: plane (degrees from the normal), not across it.
_IN_PLANE_DEG = 89.5
#: Default ``profile_samples`` when a beam ``radius`` is asked for.
_BEAM_PROFILE = 200
#: Lines per render list before the overflow line.
_CAP = 8


# ── arguments ────────────────────────────────────────────────────────────


@dataclass
class InspectSpec:
    """A parsed ``view='inspect'`` request. Lengths are Å (the caller
    crossed them); ``None`` = derive from the target's atoms."""

    probe: str
    target: str | None = None
    other: str | None = None
    origin: np.ndarray | None = None
    axis: np.ndarray | None = None
    normal: np.ndarray | None = None
    pitch: float | None = None
    radius: float | None = None
    length: float | None = None
    profile_samples: int | None = None
    events: tuple[str, ...] = DEFAULT_EVENTS
    nominal: float | None = None
    ratio: float = 1.5
    method: str = "ray"
    grid: int = 48
    radii: dict[str, float] | None = None


def require_probe(args: dict[str, Any], *, next_base: str) -> str:
    """The vetted ``probe=`` name, or a refusal listing :data:`PROBES`."""
    probe = str(args.get("probe") or "").strip().lower()
    if probe not in PROBES:
        raise BadInput(
            f"view='inspect' needs args={{'probe': ...}}, got {args.get('probe')!r}",
            options=list(PROBES),
            next=f"{next_base}, args={{'probe': 'section', 'normal': [0, 0, 1]}})",
        )
    return probe


def bare_A(value: Any) -> float:
    """A user length on the Å-native ``structure`` kind: a bare number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadInput(
            f"lengths on the structure kind are bare numbers in Å, got {value!r}",
            next="e.g. pitch=0.05, radius=3.5 (Å); the se kind takes metres or "
            "unit strings",
        )
    return float(value)


def parse_spec(
    args: dict[str, Any],
    *,
    length: Callable[[Any], float],
    next_base: str,
    extra_keys: frozenset[str] = frozenset(),
) -> InspectSpec:
    """Vet ``args`` into an :class:`InspectSpec`. ``length`` converts one
    user length into Å (raising ``BadInput``); ``next_base`` is the
    ``get(...)`` prefix a refusal's ``next=`` extends."""
    unknown = sorted(set(args) - INSPECT_ARGS - extra_keys - {"prefunits"})
    if unknown:
        raise BadInput(
            f"view='inspect' got unknown args {unknown}",
            options=sorted(INSPECT_ARGS | extra_keys),
            next=f"{next_base}, args={{'probe': 'section', ...}})",
        )
    probe = require_probe(args, next_base=next_base)

    def vec(key: str, *, lengths: bool) -> np.ndarray | None:
        raw = args.get(key)
        if raw is None:
            return None
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise BadInput(
                f"{key} must be a 3-vector, got {raw!r}",
                next=f"{key}=[x, y, z]",
            )
        v = np.asarray([length(c) if lengths else float(c) for c in raw], dtype=float)
        if not lengths and float(np.linalg.norm(v)) < 1e-12:
            raise BadInput(
                f"{key} must be a nonzero direction", next=f"{key}=[0, 0, 1]"
            )
        return v

    def num(key: str) -> float | None:
        return None if args.get(key) is None else length(args[key])

    def pos(key: str) -> float | None:
        v = num(key)
        if v is not None and not (math.isfinite(v) and v > 0.0):
            raise BadInput(f"{key} must be > 0, got {args[key]!r}")
        return v

    events = args.get("events")
    ev: tuple[str, ...]
    if events is None:
        ev = DEFAULT_EVENTS
    else:
        ev = tuple(
            str(e) for e in (events if isinstance(events, (list, tuple)) else [events])
        )
        bad = [e for e in ev if e not in st_probe.RAY_FIELDS]
        if bad:
            raise BadInput(
                f"events {bad} are not ray fields",
                options=list(st_probe.RAY_FIELDS),
                next="events=['pi', 'frame'] (or [] for the SDF runs alone)",
            )
    method = str(args.get("method") or "ray")
    if method not in ("ray", "ball"):
        raise BadInput(
            f"method must be 'ray' or 'ball', got {method!r}", options=["ray", "ball"]
        )
    radii = args.get("radii")
    if radii is not None and not (
        isinstance(radii, dict)
        and all(isinstance(v, (int, float)) for v in radii.values())
    ):
        raise BadInput(
            f"radii must be {{element: radius Å}}, got {radii!r}",
            next="radii={'C': 1.7, 'H': 1.2}",
        )
    return InspectSpec(
        probe=probe,
        target=(str(args["target"]).strip() or None) if args.get("target") else None,
        other=(str(args["with"]).strip() or None) if args.get("with") else None,
        origin=vec("origin", lengths=True),
        axis=vec("axis", lengths=False),
        normal=vec("normal", lengths=False),
        pitch=pos("pitch"),
        radius=pos("radius"),
        length=pos("length"),
        profile_samples=(
            int(args["profile_samples"]) if args.get("profile_samples") else None
        ),
        events=ev,
        nominal=pos("nominal"),
        ratio=float(args.get("ratio") or 1.5),
        method=method,
        grid=max(8, min(int(args.get("grid") or 48), 128)),
        radii={str(k): float(v) for k, v in radii.items()} if radii else None,
    )


# ── target selection ─────────────────────────────────────────────────────


def select_atoms(
    scene: Scene,
    target: str | None,
    *,
    frame_atoms: Callable[[str], set[int] | None] | None = None,
    pi_data: dict[str, Any] | None = None,
) -> list[str] | None:
    """The atom labels a ``target`` selects (``None`` = every atom).
    Forms: ``frame:<key>`` (atoms the trace frame's subtree made —
    ``frame_atoms(key)`` returns the maker ids, ``None`` = no such
    frame), ``find:element=<El>[,undercoordinated]``, ``pi:<id>`` (a
    perceived π system), or comma-separated atom labels (``#label`` ok)."""
    if not target:
        return None
    labels = list(scene.atoms)
    if target.startswith("frame:"):
        key = target[len("frame:") :].strip()
        if frame_atoms is None:
            raise BadInput(
                "a bare structure has no trace — frame: needs the se kind on "
                "the block that binds it",
                next="get(kind='se', id='<design>', view='inspect', "
                "args={'block': ..., 'target': 'frame:<key>', ...})",
            )
        ids = frame_atoms(key)
        if ids is None:
            raise BadInput(
                f"no trace frame {key!r}",
                next="get(kind='se', id='<design>', view='trace') lists the frames",
            )
        out = [la for la in labels if scene.atoms[la].maker in ids]
    elif target.startswith("find:"):
        kw: dict[str, Any] = {}
        for p in (x.strip() for x in target[len("find:") :].split(",")):
            if p.startswith("element="):
                kw["element"] = p[len("element=") :]
            elif p == "undercoordinated":
                kw["undercoordinated"] = True
            elif p:
                raise BadInput(
                    f"find: predicate {p!r} is not one of element=<El>, undercoordinated",
                    next="target='find:element=C'",
                )
        out = list(st_probe.find(scene, **kw))
    elif target.startswith("pi:"):
        sid = target[len("pi:") :].strip().split("/")[0].split("#")[-1]
        systems = (pi_data or {}).get("systems") or []
        sysrow = next((s for s in systems if str(s.get("id")) == sid), None)
        if sysrow is None:
            raise BadInput(
                f"no π system {sid!r} on this scene",
                next="target='pi:<id>' — ids are on the eye's pi: line",
            )
        out = [labels[i] for i in sysrow.get("atoms") or [] if i < len(labels)]
    else:
        out = [t.strip().lstrip("#") for t in target.split(",") if t.strip()]
        missing = [la for la in out if la not in scene.atoms]
        if missing:
            raise BadInput(
                f"no atom {missing[0]!r} (labels: {', '.join(labels[:6])}…)",
                next="target='<label>[,<label>…]' | frame:<key> | find:<pred> | pi:<id>",
            )
    if not out:
        raise BadInput(
            f"target {target!r} selects no atoms",
            next="widen the selector, or omit target for every atom",
        )
    return out


def _carts(scene: Scene, labels: Sequence[str] | None) -> np.ndarray:
    names = list(scene.atoms) if labels is None else list(labels)
    return np.asarray(
        [scene.cell.frac_to_cart(scene.atoms[la].frac) for la in names], dtype=float
    ).reshape(-1, 3)


def principal_axis(carts: np.ndarray) -> np.ndarray:
    """The unit principal axis (PCA top eigenvector, sign fixed by its
    largest component) — the default beam / section normal."""
    if len(carts) < 2:
        return np.array([0.0, 0.0, 1.0])
    c = carts.mean(axis=0)
    w, vecs = np.linalg.eigh(np.cov((carts - c).T, bias=True))
    ax = vecs[:, int(np.argmax(w))]
    if ax[int(np.argmax(np.abs(ax)))] < 0:
        ax = -ax
    return ax


# ── probes ───────────────────────────────────────────────────────────────


def _default_pitch(fld: ci.Field) -> float:
    diag = float(np.linalg.norm(np.asarray(fld.aabb[1]) - np.asarray(fld.aabb[0])))
    return max(_PITCH_FLOOR_A, diag / _PITCH_GRID)


def atomic_ray(
    scene: Scene,
    *,
    origin: Any,
    axis: Any,
    t_max: float | None = None,
    profile_samples: int | None = None,
    feature: str | None = None,
    role: str | None = None,
    radius: float | None = None,
    events: Sequence[str] = DEFAULT_EVENTS,
    pi_data: dict[str, Any] | None = None,
    charges: dict[str, float] | None = None,
    labels: Sequence[str] | None = None,
    radii: dict[str, float] | None = None,
    frame_key: Callable[[int | None], str | None] | None = None,
) -> ci.RayInspection:
    """Sphere-trace the vdW union along a ray (:func:`ci.ray_field`); with
    ``profile_samples`` the profile minimum is the channel bottleneck and
    ``pinch_features`` names the frame that pinches it; with ``radius`` the
    swept cylinder is intersected with the part (``swept``); ``events`` are
    :func:`~precis.structure.probe.ray_hits` fields merged onto the report
    (``events`` field, ``s`` in Å)."""
    fld = st_probe.vdw_field(scene, labels=labels, radii=radii, frame_key=frame_key)
    o, d, tmax, _diag = ci.ray_extent(fld, origin, axis, t_max)
    res = ci.ray_field(
        fld,
        origin_m=ci._vec3f(o),
        axis_m=ci._vec3f(d),
        t_max_m=tmax,
        profile_samples=profile_samples,
        feature=feature,
        role=role,
    )
    swept = None
    if radius:
        swept = ci.overlap_fields(fld, ci.cylinder_field(o, d, radius, tmax))
    want = [f for f in events if f in st_probe.RAY_FIELDS]
    hits: tuple[Any, ...] = ()
    if want:
        pi = pi_data
        if pi is None and "pi" in want:
            pi = st_probe.pi_perceive(scene)

        def frame_of(la: str) -> str | None:
            maker = scene.atoms[la].maker
            return frame_key(int(maker)) if frame_key and maker is not None else None

        hits = tuple(
            st_probe.ray_hits(
                scene,
                o,
                d,
                length=tmax,
                fields=want,
                pi_data=pi,
                charges=charges,
                frame_of=frame_of if frame_key else None,
                vdw_radii=st_probe.VDW_RADII_A,
            ).hits
        )
    return replace(res, radius_m=radius, swept=swept, events=hits)


def atomic_section(
    scene: Scene,
    *,
    origin: Any,
    normal: Any,
    pitch: float | None = None,
    labels: Sequence[str] | None = None,
    radii: dict[str, float] | None = None,
    frame_key: Callable[[int | None], str | None] | None = None,
) -> ci.SectionInspection:
    """Marching-squares section of the vdW union (:func:`ci.section_field`)
    plus the bonds crossing the plane (``crossings`` — what stitches the
    layers either side; ``probe.bonds_through_plane``, not a second
    section)."""
    fld = st_probe.vdw_field(scene, labels=labels, radii=radii, frame_key=frame_key)
    res = ci.section_field(
        fld,
        origin_m=ci._vec3f(origin),
        normal_m=ci._vec3f(normal),
        pitch_m=pitch if pitch is not None else _default_pitch(fld),
    )
    keep = None if labels is None else set(labels)
    crossings = tuple(
        ci.PlaneCrossing(
            a=c.i, b=c.j, length_m=c.length, angle_deg=c.angle_to_normal, order=c.order
        )
        for c in st_probe.bonds_through_plane(
            scene, np.asarray(origin, dtype=float), np.asarray(normal, dtype=float)
        )
        if (keep is None or (c.i in keep and c.j in keep))
        # a bond lying IN the plane has endpoints a rounding error either
        # side of it — not a crossing
        and c.angle_to_normal < _IN_PLANE_DEG
    )
    return replace(res, crossings=crossings)


def atomic_thickness(
    scene: Scene,
    *,
    origin: Any,
    normal: Any,
    pitch: float,
    nominal: float | None = None,
    ratio: float = 1.5,
    method: str = "ray",
    feature: str | None = None,
    role: str | None = None,
    labels: Sequence[str] | None = None,
    radii: dict[str, float] | None = None,
    frame_key: Callable[[int | None], str | None] | None = None,
) -> ci.ThicknessInspection:
    """Face-on vdW column map (:func:`ci.thickness_field`): one ray per grid
    node, so keep ``pitch`` at ≥ 0.5 Å on anything but a small molecule."""
    fld = st_probe.vdw_field(scene, labels=labels, radii=radii, frame_key=frame_key)
    return ci.thickness_field(
        fld,
        origin_m=ci._vec3f(origin),
        normal_m=ci._vec3f(normal),
        pitch_m=pitch,
        nominal_m=nominal,
        ratio=ratio,
        method=method,
        feature=feature,
        role=role,
    )


def atomic_overlap(
    scene: Scene,
    other: Scene | None = None,
    *,
    labels: Sequence[str] | None = None,
    other_labels: Sequence[str] | None = None,
    beam: tuple[Any, Any, float, float] | None = None,
    grid: int = 48,
    radii: dict[str, float] | None = None,
    frame_key: Callable[[int | None], str | None] | None = None,
    other_frame_key: Callable[[int | None], str | None] | None = None,
) -> ci.OverlapInspection:
    """Intersection volume of two vdW unions (``scene``/``labels`` against
    ``other``/``other_labels`` — default the same scene, i.e. two atom
    subsets of one block) or of ``scene`` against a cylinder ``beam =
    (origin, axis, radius, length)``. Names the frames on both sides."""
    a = st_probe.vdw_field(scene, labels=labels, radii=radii, frame_key=frame_key)
    if beam is not None:
        b = ci.cylinder_field(*beam)
    else:
        b = st_probe.vdw_field(
            other if other is not None else scene,
            labels=other_labels,
            radii=radii,
            frame_key=other_frame_key if other is not None else frame_key,
        )
    return ci.overlap_fields(a, b, grid=grid)


def run(
    spec: InspectSpec,
    scene: Scene,
    *,
    frame_key: Callable[[int | None], str | None] | None = None,
    frame_atoms: Callable[[str], set[int] | None] | None = None,
    pi_data: dict[str, Any] | None = None,
) -> tuple[Any, int]:
    """Run ``spec`` on ``scene``: ``(result, n_target_atoms)``, Å. Defaults
    come from the target's atoms: origin = centroid (a ray starts 2 Å
    before the projected extent), ``axis``/``normal`` = principal axis."""
    labels = select_atoms(scene, spec.target, frame_atoms=frame_atoms, pi_data=pi_data)
    carts = _carts(scene, labels)
    if not len(carts):
        raise BadInput("the scene has no atoms to probe", next="check the target")
    c = carts.mean(axis=0)
    ax = principal_axis(carts)
    n_atoms = len(carts)
    res: Any
    kw: dict[str, Any] = {"labels": labels, "radii": spec.radii, "frame_key": frame_key}
    if spec.probe == "ray":
        d = spec.axis if spec.axis is not None else ax
        d = d / np.linalg.norm(d)
        if spec.origin is not None:
            o = spec.origin
        else:
            half = float(np.abs((carts - c) @ d).max())
            o = c - (half + 2.0) * d
        prof = spec.profile_samples or (_BEAM_PROFILE if spec.radius else None)
        res = atomic_ray(
            scene,
            origin=o,
            axis=d,
            t_max=spec.length,
            profile_samples=prof,
            radius=spec.radius,
            events=spec.events,
            pi_data=pi_data,
            **kw,
        )
    elif spec.probe == "section":
        res = atomic_section(
            scene,
            origin=spec.origin if spec.origin is not None else c,
            normal=spec.normal if spec.normal is not None else ax,
            pitch=spec.pitch,
            **kw,
        )
    elif spec.probe == "thickness":
        res = atomic_thickness(
            scene,
            origin=spec.origin if spec.origin is not None else c,
            normal=spec.normal if spec.normal is not None else ax,
            pitch=spec.pitch if spec.pitch is not None else 0.5,
            nominal=spec.nominal,
            ratio=spec.ratio,
            method=spec.method,
            **kw,
        )
    else:  # overlap
        if spec.other is not None:
            other_labels = select_atoms(
                scene, spec.other, frame_atoms=frame_atoms, pi_data=pi_data
            )
            res = atomic_overlap(
                scene,
                other_labels=other_labels,
                grid=spec.grid,
                **kw,
            )
        elif spec.radius is not None:
            d = spec.axis if spec.axis is not None else ax
            d = d / np.linalg.norm(d)
            length = spec.length or float(np.ptp((carts - c) @ d)) + 4.0
            start = spec.origin if spec.origin is not None else c - 0.5 * length * d
            res = atomic_overlap(
                scene, beam=(start, d, spec.radius, length), grid=spec.grid, **kw
            )
        else:
            raise BadInput(
                "probe='overlap' needs a second solid: with=<selector> (another "
                "frame/atom set of this scene) or radius= (+ origin/axis/length) "
                "for a probe cylinder",
                next="args={'probe': 'overlap', 'target': 'frame:<tube>', "
                "'with': 'frame:<guest>'}",
            )
    return res, n_atoms


# ── render ───────────────────────────────────────────────────────────────


def _num(x: float, spec: str) -> str:
    """``x`` formatted by ``spec``; float dust (``|x| < 1e-6``) reads 0."""
    return "0" if abs(x) < 1e-6 else format(x, spec)


def _g(x: float | None, scale: float = 1.0) -> str:
    return "—" if x is None else _num(x * scale, ".4g")


def _v(v: Sequence[float], scale: float = 1.0) -> str:
    return "(" + ", ".join(_num(float(x) * scale, ".3g") for x in v) + ")"


def _names(refs: Sequence[ci.FeatureRef], limit: int = 3) -> str:
    seen: list[str] = []
    for fr in refs:
        t = fr.label if fr.role == "owner" else f"{fr.label} (blocker)"
        if t and t not in seen:
            seen.append(t)
    if not seen:
        return "—"
    more = f" +{len(seen) - limit}" if len(seen) > limit else ""
    return ", ".join(seen[:limit]) + more


def render(
    probe: str,
    res: Any,
    *,
    head: str,
    scale: float = 1.0,
    unit: str = "Å",
) -> str:
    """Compact text for one result (a section ≤ 400 tokens). Lengths read
    ``scale × value`` in ``unit`` (Å results: 1; metre results: 1e3 mm)."""
    u = unit
    if probe == "section":
        lines = [
            f"inspect section · {head}",
            f"plane o={_v(res.plane_origin_m, scale)} n={_v(res.plane_normal_m)} {u}"
            f" · pitch {_g(res.pitch_m, scale)} {u}",
            f"loops {len(res.loops)} · holes {res.hole_count}",
        ]
        for i, lp in enumerate(res.loops[:_CAP]):
            lines.append(
                f"  L{i} {'material' if lp.area_m2 >= 0 else 'void'} "
                f"{_g(abs(lp.area_m2), scale * scale)} {u}² · len "
                f"{_g(lp.length_m, scale)} {u} · {_names(lp.features, 2)}"
            )
        if len(res.loops) > _CAP:
            lines.append(f"  … +{len(res.loops) - _CAP} more loops")
        for i, j, d in res.wall_pairs_m[:4]:
            lines.append(f"  wall L{i}–L{j} {_g(d, scale)} {u}")
        if res.crossings:
            lines.append(
                f"bonds crossing the plane: {len(res.crossings)} — "
                + ", ".join(f"{c.a}–{c.b}" for c in res.crossings[:4])
                + (" …" if len(res.crossings) > 4 else "")
            )
        return "\n".join(lines)
    if probe == "ray":
        lines = [f"inspect ray · {head}"]
        lines.append(f"runs {len(res.hits)} (material along the ray)")
        for h in res.hits[:_CAP]:
            lines.append(
                f"  {_g(h.t_entry_m, scale)}–{_g(h.t_exit_m, scale)} {u}  in "
                f"{_names(h.entry_features, 2)} · out {_names(h.exit_features, 2)}"
            )
        if len(res.hits) > _CAP:
            lines.append(f"  … +{len(res.hits) - _CAP} more runs")
        lines.append(f"closest approach {_g(res.min_distance_m, scale)} {u}")
        if res.pinch_m is not None:
            lines.append(
                f"free radius min {_g(res.pinch_m, scale)} {u} at "
                f"{_g(res.pinch_t_m, scale)} {u} · pinched by {_names(res.pinch_features)}"
            )
        if res.radius_m:
            sw = res.swept
            blocked = sw is not None and sw.hit
            lines.append(
                f"beam r {_g(res.radius_m, scale)} {u}: "
                + (
                    f"blocked — overlap {_g(sw.volume_m3, scale**3)} {u}³ with "
                    f"{_names(sw.features)}"
                    if blocked and sw is not None
                    else "clear"
                )
            )
        for e in res.events[:_CAP]:
            lines.append(
                f"  {fmt_s_A(e.s * scale)}  {hit_text(e, prefix='', block='')}"
            )
        if len(res.events) > _CAP:
            lines.append(f"  … +{len(res.events) - _CAP} more events")
        return "\n".join(lines)
    if probe == "overlap":
        lines = [
            f"inspect overlap · {head}",
            f"hit {'yes' if res.hit else 'no'} · volume {_g(res.volume_m3, scale**3)} {u}³",
        ]
        if res.hit:
            lo, hi = res.aabb_m
            lines.append(f"box {_v(lo, scale)} – {_v(hi, scale)} {u}")
            lines.append(f"frames {_names(res.features, 6)}")
        return "\n".join(lines)
    ts = [s.thickness_m for s in res.samples]
    lines = [
        f"inspect thickness · {head}",
        f"plane o={_v(res.plane_origin_m, scale)} n={_v(res.plane_normal_m)} {u}"
        f" · pitch {_g(res.pitch_m, scale)} {u} · {len(ts)} columns",
    ]
    if ts:
        top = max(res.samples, key=lambda s: s.thickness_m)
        lines.append(
            f"column min {_g(min(ts), scale)} · mean {_g(sum(ts) / len(ts), scale)} · "
            f"max {_g(max(ts), scale)} {u} at {_v(top.position_m, scale)} · "
            f"{_names(top.entry_features + top.exit_features)}"
        )
        if res.nominal_m is not None:
            risks = res.sink_risks()
            lines.append(
                f"sink risks (> {res.ratio:g} × {_g(res.nominal_m, scale)} {u}): "
                f"{len(risks)}"
            )
    return "\n".join(lines)
