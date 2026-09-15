"""The orbit camera — az/el/twist intent parameters, never raw matrices.

An LLM (or a saved figure recipe) sets ``azimuth_deg``/``elevation_deg``/
``twist_deg`` — a spherical orbit around a target point — rather than a 4x4
transform, so the parameters stay legible and independently nameable in a
JSON recipe. :func:`view_basis` turns that intent into a concrete
right-handed camera frame; :func:`project` uses it to map world points to
camera space.

**Convention** (world frame: right-handed, +Z up):

- The eye orbits the (resolved) ``target`` at radius ``distance`` along
  ``eye_dir = (cos(el)*cos(az), cos(el)*sin(az), sin(el))`` — ``azimuth_deg``
  is the angle from +X toward +Y in the world XY-plane (the standard
  right-hand rotation about +Z: increasing azimuth swings the eye from the
  +X side toward the +Y side); ``elevation_deg`` is the angle up from the
  XY-plane toward +Z (positive = eye rises above the content, looking down).
- ``forward`` (the direction the camera looks, eye → target) is
  ``-eye_dir``. ``right``/``up`` complete a right-handed frame against the
  world-+Z reference (falling back to a +Y reference within ~0.01° of the
  poles, where the reference would otherwise be parallel to ``forward``),
  then both are rolled about ``forward`` by ``twist_deg`` (positive twist
  rotates ``up`` toward ``right`` — a clockwise roll as seen by the camera).
- A point sitting exactly at ``target`` always projects to camera-space
  ``(0, 0)`` regardless of az/el/twist/distance — that's the defining
  property callers rely on (:func:`project`'s docstring test).

Camera-space axes are the ordinary math convention: +x right, +y **up**.
SVG's y axis points down, so :mod:`.render` flips y at the last step, not
here — everything in this module stays in the math convention.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

__all__ = ["Camera", "content_bbox_centroid", "project", "view_basis"]

#: World "up" reference for building the right/up frame. Only ``+Z`` is
#: supported as the scene's up axis in this slice (periodic/crystal scenes
#: with a tilted cell are out of scope — see the package docstring).
_WORLD_UP = np.array([0.0, 0.0, 1.0])
_WORLD_UP_FALLBACK = np.array([0.0, 1.0, 0.0])

#: Degenerate-pole guard: forward within this cosine of ``_WORLD_UP`` swaps
#: to the fallback reference so ``right``/``up`` don't degenerate to zero.
_POLE_COS_THRESHOLD = 0.9999


@dataclass(frozen=True)
class Camera:
    """Orbit-camera intent. See the module docstring for az/el/twist sign
    conventions.

    ``target`` is ``None`` until resolved — a bare :class:`Camera` cannot be
    projected from (:func:`view_basis`/:func:`project` raise
    ``ValueError``); the pipeline (:mod:`.render`) resolves it to the
    scene's bbox centroid via :func:`content_bbox_centroid` before use, or a
    caller may set it explicitly to orbit a fixed point.

    ``zoom`` only scales ``ortho`` projection; ``distance``/``fov_deg`` only
    govern ``persp`` framing (``distance`` still positions the eye, and thus
    the painter's-algorithm depth origin, under ``ortho`` too — see
    :func:`project`).
    """

    target: tuple[float, float, float] | None = None
    azimuth_deg: float = 0.0
    elevation_deg: float = 0.0
    twist_deg: float = 0.0
    projection: Literal["ortho", "persp"] = "ortho"
    zoom: float = 1.0
    distance: float = 10.0
    fov_deg: float = 40.0


def content_bbox_centroid(points: NDArray[np.floating]) -> NDArray[np.float64]:
    """The bbox centroid ``(min + max) / 2`` of an ``(N, 3)`` point array —
    the pipeline's default :attr:`Camera.target` for an unset camera.

    Not the centroid of mass: an off-center cluster of points (e.g. one
    dense corner and one sparse arm) still centers the *frame*, not the
    *content*, which is what "camera auto-frames the structure" means.
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    lo = pts.min(axis=0)
    hi = pts.max(axis=0)
    return (lo + hi) * 0.5


def _rotate_about_axis(
    v: NDArray[np.float64], axis: NDArray[np.float64], angle_rad: float
) -> NDArray[np.float64]:
    """Rodrigues' rotation formula — ``v`` rotated by ``angle_rad`` about
    the unit ``axis``, right-hand rule."""
    c = math.cos(angle_rad)
    s = math.sin(angle_rad)
    return v * c + np.cross(axis, v) * s + axis * np.dot(axis, v) * (1.0 - c)


def view_basis(
    camera: Camera,
) -> tuple[
    NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]
]:
    """Resolve ``camera`` to a concrete right-handed frame: ``(eye, right,
    up, forward)``, each a ``(3,)`` world-space vector (``right``/``up``/
    ``forward`` unit length).

    Raises ``ValueError`` if ``camera.target`` is ``None`` (unresolved —
    see :class:`Camera`).
    """
    if camera.target is None:
        raise ValueError(
            "Camera.target is unresolved — the pipeline must set it "
            "(e.g. to content_bbox_centroid(...)) before projecting"
        )
    target = np.asarray(camera.target, dtype=np.float64)
    az = math.radians(camera.azimuth_deg)
    el = math.radians(camera.elevation_deg)
    eye_dir = np.array(
        [math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)]
    )
    eye = target + camera.distance * eye_dir
    forward = -eye_dir
    forward = forward / np.linalg.norm(forward)

    up_ref = _WORLD_UP
    if abs(float(np.dot(forward, _WORLD_UP))) > _POLE_COS_THRESHOLD:
        up_ref = _WORLD_UP_FALLBACK
    right = np.cross(forward, up_ref)
    right = right / np.linalg.norm(right)
    up = np.cross(right, forward)
    up = up / np.linalg.norm(up)

    twist = math.radians(camera.twist_deg)
    if twist:
        right = _rotate_about_axis(right, forward, twist)
        up = _rotate_about_axis(up, forward, twist)

    return eye, right, up, forward


def project(
    camera: Camera, points: NDArray[np.floating]
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Project ``(N, 3)`` world ``points`` to camera space.

    Returns ``(xy, depth)``: ``xy`` is ``(N, 2)`` in the math convention
    (+x right, +y up — see the module docstring on the SVG y-flip);
    ``depth`` is ``(N,)`` and increases monotonically away from the camera
    (a point exactly at ``camera.target`` has ``depth == camera.distance``,
    by construction, for both projections).

    ``ortho``: ``xy`` is the camera-space ``(x, y)`` scaled by
    ``camera.zoom`` — depth-independent, the defining property of a
    parallel projection.

    ``persp``: ``xy`` is the camera-space ``(x, y)`` scaled by
    ``f / depth`` where ``f = 1 / tan(fov_deg / 2)`` — the standard
    perspective divide. Points at or behind the eye (``depth <= 0``) are
    not clipped in this slice (no near-plane clip); such a scene renders
    with an inverted/degenerate projection for those points rather than
    erroring — acceptable because ``ortho`` is the default and the intended
    use (an in-frame scalebar-bearing figure) never places content behind
    the eye.
    """
    eye, right, up, forward = view_basis(camera)
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    rel = pts - eye
    cx = rel @ right
    cy = rel @ up
    depth = rel @ forward

    if camera.projection == "ortho":
        xy = np.stack([cx, cy], axis=-1) * camera.zoom
    else:
        f = 1.0 / math.tan(math.radians(camera.fov_deg) / 2.0)
        safe_depth = np.where(depth == 0.0, np.finfo(np.float64).eps, depth)
        xy = np.stack([cx, cy], axis=-1) * (f / safe_depth[:, None])

    return xy, depth
