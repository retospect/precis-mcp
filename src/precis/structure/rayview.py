"""Text for atomic observation-ray hits (structure-workspaces 2r) — the
one renderer the ``structure`` kind's ``~ray:<name>`` / ``view='ray'``
and the se kind's atomic leg (:mod:`precis_se.rays`) share, so a hit
reads the same on both kinds.

Pure over :class:`~precis.structure.probe.RayHit`; Å throughout (the
package's unit enclave). ``prefix`` is what an atom handle starts with
(``p#`` for an se block, ``''`` on a bare structure), ``block`` what a
``pi:`` selector names (``p`` for an se block — ``pi:p#0`` — and ``''``
for a bare structure, where the selector is ``pi:0``).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .probe import RayHit, RayTrace, ray_hits
from .scene import Measure, Scene

#: Hits per read before the overflow line (``args={'from': …}``).
MAX_HITS = 40


def fmt_s_A(s_A: float) -> str:
    return f"{s_A:6.1f} Å"


def _deg(angle: float | None) -> str:
    return f"∠ {angle:.0f}°" if angle is not None else "∠ —"


def hit_text(
    h: RayHit, *, prefix: str, block: str, frame_texts: dict[str, str] | None = None
) -> str:
    """One hit's body — everything after the ``s`` column."""
    pi_sel = f"pi:{block}#" if block else "pi:"
    if h.field == "pass":
        text = f"pass {prefix}{h.subject}  d {h.data['d']:.2f}  {_deg(h.data['angle'])}"
        if "q" in h.data:
            text += f"  q {h.data['q']:+.2f} e"
        return text
    if h.field == "charge":
        return (
            f"esp {h.data['V']:+.2f} V ({h.data['tag']})  near "
            f"{prefix}{h.data['near']} {h.data['q']:+.2f} e"
        )
    if h.field == "pi":
        if h.subject.startswith("ring:"):
            what = f"ring r{h.data['ring']}"
            if h.event == "enter":
                arom = "aromatic" if h.data.get("aromatic") else "non-aromatic"
                what += f" ({h.data['size']}, {arom}) · {pi_sel}{h.data['system']}"
        else:
            what = f"system {h.data['system']}"
            if h.event == "enter":
                what += f" ({h.data['size']} atoms) · {pi_sel}{h.data['system']}"
        return f"pi.{h.event} {what}"
    if h.field == "frame":
        return f"frame {(frame_texts or {}).get(h.subject, h.subject)}"
    if h.field == "relax":
        return f"relax {h.subject}"
    return f"region.{h.event} {h.subject}"


def _dir_text(d: np.ndarray) -> str:
    for i, ax in enumerate("xyz"):
        if abs(abs(float(d[i])) - 1.0) < 1e-9:
            return ("+" if d[i] > 0 else "−") + ax
    return "[" + " ".join(f"{float(x):.3g}" for x in d) + "]"


def structure_ray(scene: Scene, m: Measure, pi_data: dict[str, Any] | None) -> RayTrace:
    """Evaluate a persisted ``kind='ray'`` marker (its ``spec``: origin,
    direction, length, fields, step, reach — Å) on ``scene``. A bare
    structure carries no trace and no stored charges, so ``frame``,
    ``relax`` and ``charge`` are absent; ``pi`` reads ``pi_data``."""
    spec = m.spec or {}
    return ray_hits(
        scene,
        np.asarray(spec.get("origin") or [0.0, 0.0, 0.0], dtype=float),
        np.asarray(spec.get("direction") or [0.0, 0.0, 1.0], dtype=float),
        length=float(spec.get("length") or 0.0),
        fields=spec.get("fields"),
        step=float(spec.get("step") or 0.5),
        reach=float(spec.get("reach") or 4.0),
        pi_data=pi_data,
    )


def render_structure_ray(
    scene: Scene,
    m: Measure,
    pi_data: dict[str, Any] | None,
    *,
    handle: str,
    start: float | None = None,
) -> list[str]:
    """``ray <name> · <handle> · [o] Å +z · L Å · fields … · n hits`` then
    the hits (capped at :data:`MAX_HITS`) and the ``end`` line."""
    spec = m.spec or {}
    tr = structure_ray(scene, m, pi_data)
    o = spec.get("origin") or [0.0, 0.0, 0.0]
    d = np.asarray(spec.get("direction") or [0.0, 0.0, 1.0], dtype=float)
    head = (
        f"ray {m.name} · {handle} · [{' '.join(f'{float(x):g}' for x in o)}] Å "
        f"{_dir_text(d / np.linalg.norm(d))} · {float(spec.get('length') or 0):g} Å · "
        f"fields {','.join(tr.fields) or '—'} · {len(tr.hits)} hits"
    )
    rows = [h for h in tr.hits if start is None or h.s >= start]
    lines = [head]
    lines += [
        f"  {fmt_s_A(h.s)}  {hit_text(h, prefix='', block='')}" for h in rows[:MAX_HITS]
    ]
    if len(rows) > MAX_HITS:
        lines.append(
            f"  … +{len(rows) - MAX_HITS} more — args={{'name': {m.name!r}, "
            f"'from': {rows[MAX_HITS].s:.1f}}}"
        )
    if tr.end is not None:
        lines.append(f"  end {tr.end:.1f} Å (last atom within reach)")
    return lines
