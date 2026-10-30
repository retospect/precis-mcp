"""``precis_geom.inspect`` (``pg.inspect``) — one probe family at both scales
(molecular-workspace-code slice 6, docs/backlog/molecular-workspace-code.md
§Inspect; the probes' design is docs/backlog/cad-inspect.md).

``ray`` / ``section`` / ``overlap`` / ``thickness`` take a :class:`Geom` and
dispatch on :attr:`Geom.atomic`: a macro object marches the folded cad SDF
(:mod:`precis.cad.inspect`), an atomic one the union of its atoms' van der
Waals spheres (:mod:`precis.structure.inspectview`) — the same four probes,
the same result dataclasses. SI metres throughout (bare numbers, or a unit
string the shared grammar resolves: ``"2 nm"``, ``"[0 0 -1] nm"``), like
:class:`~precis_geom.assembly.Ray`; the atomic leg marches in Å and
:func:`precis.cad.inspect.rescale` reports metres.

- **Beam radius.** ``ray(g, ..., radius=r)`` sweeps a cylinder: its
  intersection with the part (``.swept``, an :class:`OverlapInspection`) is
  the "does the guest fit" answer. With ``profile_samples=`` the profile's
  minimum (``.pinch_m``) is the free radius to the nearest wall — at Å the
  channel bottleneck — and ``.pinch_features`` names the construction frame
  that pinches it.
- **Atomic events.** ``events=('pi', 'frame', …)`` merges
  :func:`~precis.structure.probe.ray_hits` events (``.events``, ``s`` in
  metres) beside the SDF runs; ``()`` skips them.
- **Attribution.** An atom's feature label is its ``maker`` frame's trace key
  when ``frames`` resolves it — ``None`` (default) reads the live tracer of
  the running script, a ``trace.dump()`` document or a ``maker → key``
  callable overrides — else the atom label.
- **Section** also lists the bonds crossing the plane (``.crossings``).

Results are plain data: a script asserts on them or attaches a finding
string to the block.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import numpy as np

from precis.cad import inspect as _ci
from precis.cad.vec import Transform
from precis.errors import BadInput
from precis.structure import inspectview as _iv
from precis_geom import assembly as _pga
from precis_geom import trace as _trace

FeatureRef = _ci.FeatureRef
RayInspection = _ci.RayInspection
SectionInspection = _ci.SectionInspection
OverlapInspection = _ci.OverlapInspection
ThicknessInspection = _ci.ThicknessInspection

_M_PER_A = 1e-10
_A_PER_M = 1e10

FrameKeys = Callable[[int | None], str | None]


# ── plumbing ─────────────────────────────────────────────────────────────


def _key_lookup(doc: dict[str, Any]) -> FrameKeys:
    """``maker id → frame key`` over a trace document (a collapsed span
    ``id..id_end`` answers for every id it covers)."""
    exact: dict[int, str] = {}
    spans: list[tuple[int, int, str]] = []
    for f in doc.get("frames") or []:
        lo, hi = int(f["id"]), int(f.get("id_end", f["id"]))
        if hi == lo:
            exact.setdefault(lo, str(f["key"]))
        else:
            spans.append((lo, hi, str(f["key"])))

    def key(maker: int | None) -> str | None:
        if maker is None:
            return None
        if maker in exact:
            return exact[maker]
        for lo, hi, k in spans:
            if lo <= maker <= hi:
                return k
        return None

    return key


def _frame_keys(frames: Any) -> FrameKeys | None:
    if callable(frames):
        return frames
    doc = frames if isinstance(frames, dict) else _trace.dump()
    return _key_lookup(doc) if doc else None


def _vec_a(value: Any, what: str) -> np.ndarray:
    return np.asarray(_pga._ray_origin_m(value), dtype=float) * _A_PER_M


def _len_a(value: Any, what: str) -> float:
    if isinstance(value, str):
        return _pga._ray_length_m(value) * _A_PER_M
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise BadInput(
            f"{what} must be a number (metres) or a unit string, got {value!r}",
            next="e.g. '2 nm' or 2e-9 (metres)",
        ) from exc
    if n <= 0.0:
        raise BadInput(f"{what} must be > 0, got {value!r}")
    return n * _A_PER_M


def _dir(value: Any, what: str) -> np.ndarray:
    return _pga._dir_vec(value, what)


def _scene(g: _pga.Geom) -> Any:
    if any((not c.atomic and c.design is not None) for c, _x, _f in g.placements):
        raise BadInput(
            "inspect: a mixed atomic + macro assembly has no single field",
            next="probe the atomic and macro children separately "
            "(inspect.ray(assembly_child, ...))",
        )
    return g.flatten()


def _macro(g: _pga.Geom, component: str | None) -> tuple[Any, Any]:
    design = g.design
    if design is None:
        raise BadInput(
            "inspect: this Geom has no geometry to probe (symbolic template/array)",
            next="pass a built Geom — atomic (scene) or macro (cad design)",
        )
    if component is not None:
        if component not in design.components:
            raise BadInput(
                f"no component {component!r} on this design",
                options=sorted(design.components),
            )
        return design, design.components[component]
    return design, design.whole()


def _atomic_scale(res: Any) -> Any:
    return _ci.rescale(res, _M_PER_A)


# ── the four probes ──────────────────────────────────────────────────────


def ray(
    geom: Any,
    *,
    origin: Any,
    axis: Any,
    t_max: Any = None,
    profile_samples: int | None = None,
    feature: str | None = None,
    role: str | None = None,
    radius: Any = None,
    events: tuple[str, ...] = _iv.DEFAULT_EVENTS,
    charges: dict[str, float] | None = None,
    component: str | None = None,
    frames: Any = None,
    max_steps: int = _ci._MAX_RAY_STEPS,
) -> RayInspection:
    """Sphere-trace ``geom`` along a ray (module docstring). ``origin`` and
    ``t_max`` are metres or unit strings, ``axis`` a direction; ``radius``
    sweeps a cylinder (``.swept``); ``events`` and ``charges`` are the
    atomic observation fields; ``component`` picks one macro component;
    ``max_steps`` raises the march budget when a long grazing ray stalls
    (``.stalled``)."""
    g = _pga.geom(geom)
    d = _dir(axis, "ray axis")
    if g.atomic:
        res = _iv.atomic_ray(
            _scene(g),
            origin=_vec_a(origin, "ray origin"),
            axis=d,
            t_max=None if t_max is None else _len_a(t_max, "t_max"),
            profile_samples=profile_samples,
            feature=feature,
            role=role,
            radius=None if radius is None else _len_a(radius, "radius"),
            events=events,
            charges=charges,
            frame_key=_frame_keys(frames),
            max_steps=max_steps,
        )
        return _atomic_scale(res)
    design, expr = _macro(g, component)
    o = _pga._ray_origin_m(origin)
    tm = None if t_max is None else _pga._ray_length_m(t_max)
    res = _ci.ray(
        design,
        expr,
        origin_m=o,
        axis_m=_ci._vec3f(d),
        t_max_m=tm,
        profile_samples=profile_samples,
        feature=feature,
        role=role,
        max_steps=max_steps,
    )
    if radius is None:
        return res
    rad = _pga._ray_length_m(radius)
    _o, du, tmax, _diag = _ci.ray_extent(_ci.cad_field(design, expr), o, d, tm)
    beam = _ci.probe_solid(
        design,
        "inspect-beam",
        f"cyl:r{rad * 1e3:.9g}mmh{tmax * 1e3:.9g}mm",
        _beam_transform(np.asarray(o), du),
    )
    return replace(res, radius_m=rad, swept=_ci.overlap(design, expr, beam))


def _beam_transform(origin: np.ndarray, axis: np.ndarray) -> Transform:
    """Cylinder frame: base disc at ``origin``, +z along ``axis``."""
    w = axis / np.linalg.norm(axis)
    seed = np.array([1.0, 0.0, 0.0]) if abs(w[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(w, seed)
    u = u / np.linalg.norm(u)
    v = np.cross(w, u)
    return Transform(R=np.column_stack([u, v, w]), t=np.asarray(origin, dtype=float))


def section(
    geom: Any,
    *,
    origin: Any,
    normal: Any,
    pitch: Any = None,
    component: str | None = None,
    frames: Any = None,
) -> SectionInspection:
    """Marching-squares plane section of ``geom`` (loops, ``hole_count``,
    wall pairs; atomic adds the bonds crossing the plane)."""
    g = _pga.geom(geom)
    n = _dir(normal, "section normal")
    if g.atomic:
        res = _iv.atomic_section(
            _scene(g),
            origin=_vec_a(origin, "section origin"),
            normal=n,
            pitch=None if pitch is None else _len_a(pitch, "pitch"),
            frame_key=_frame_keys(frames),
        )
        return _atomic_scale(res)
    design, expr = _macro(g, component)
    return _ci.section(
        design,
        expr,
        origin_m=_pga._ray_origin_m(origin),
        normal_m=_ci._vec3f(n),
        pitch_m=None if pitch is None else _pga._ray_length_m(pitch),
    )


def overlap(
    geom: Any,
    probe: Any,
    *,
    grid: int = 48,
    component: str | None = None,
    frames: Any = None,
    probe_frames: Any = None,
) -> OverlapInspection:
    """Intersection volume of ``geom`` with ``probe``. Atomic: ``probe`` is
    another atomic Geom (a guest in a host — both frames are named) or a
    beam ``{'origin', 'axis', 'radius', 'length'}`` (metres / unit
    strings). Macro: a cad probe expression on ``geom.design``
    (:func:`probe_solid`)."""
    g = _pga.geom(geom)
    if g.atomic:
        fk = _frame_keys(frames)
        if isinstance(probe, dict):
            beam = (
                _vec_a(probe["origin"], "beam origin"),
                _dir(probe["axis"], "beam axis"),
                _len_a(probe["radius"], "beam radius"),
                _len_a(probe["length"], "beam length"),
            )
            res = _iv.atomic_overlap(_scene(g), beam=beam, grid=grid, frame_key=fk)
        else:
            other = _pga.geom(probe)
            if not other.atomic:
                raise BadInput(
                    "inspect.overlap: an atomic geom needs an atomic probe geom "
                    "or a beam dict",
                    next="overlap(host, guest) | overlap(host, {'origin':…, "
                    "'axis':…, 'radius':…, 'length':…})",
                )
            res = _iv.atomic_overlap(
                _scene(g),
                _scene(other),
                grid=grid,
                frame_key=fk,
                other_frame_key=_frame_keys(probe_frames if probe_frames else frames),
            )
        return _atomic_scale(res)
    design, expr = _macro(g, component)
    return _ci.overlap(design, expr, probe, grid=grid)


def probe_solid(geom: Any, name: str, config: str, xf: Transform) -> Any:
    """Register a macro probe primitive (``cyl:r1.7mmh13.4mm``) on
    ``geom.design`` and return its expression for :func:`overlap`."""
    design, _expr = _macro(_pga.geom(geom), None)
    return _ci.probe_solid(design, name, config, xf)


def thickness(
    geom: Any,
    *,
    origin: Any,
    normal: Any,
    pitch: Any,
    nominal: Any = None,
    ratio: float = 1.5,
    method: str = "ray",
    feature: str | None = None,
    role: str | None = None,
    component: str | None = None,
    frames: Any = None,
) -> ThicknessInspection:
    """Face-on thickness map of ``geom`` — one ray per grid node, so keep
    ``pitch`` at wall scale (atomic: ≥ 0.5 Å)."""
    g = _pga.geom(geom)
    n = _dir(normal, "thickness normal")
    if g.atomic:
        res = _iv.atomic_thickness(
            _scene(g),
            origin=_vec_a(origin, "thickness origin"),
            normal=n,
            pitch=_len_a(pitch, "pitch"),
            nominal=None if nominal is None else _len_a(nominal, "nominal"),
            ratio=ratio,
            method=method,
            feature=feature,
            role=role,
            frame_key=_frame_keys(frames),
        )
        return _atomic_scale(res)
    design, expr = _macro(g, component)
    return _ci.thickness(
        design,
        expr,
        origin_m=_pga._ray_origin_m(origin),
        normal_m=_ci._vec3f(n),
        pitch_m=_pga._ray_length_m(pitch),
        nominal_m=None if nominal is None else _pga._ray_length_m(nominal),
        ratio=ratio,
        method=method,
        feature=feature,
        role=role,
    )


__all__ = [
    "FeatureRef",
    "OverlapInspection",
    "RayInspection",
    "SectionInspection",
    "ThicknessInspection",
    "overlap",
    "probe_solid",
    "ray",
    "section",
    "thickness",
]
