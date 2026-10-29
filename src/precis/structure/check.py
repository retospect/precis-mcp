"""The DB-free chemistry check — one composition, two call sites.

``check(scene)`` is what ``Run.finish()`` (``precis_geom.out``) runs over every
atomic block and what the workspace importer runs on arrival
(``precis_se.workspace_import``). It is a pure composition of what exists —
no new validator; a new rule belongs in :mod:`validate` / :mod:`vsepr`:

- :func:`validate.findings_cached` — the exact list ``view='validate'``
  renders (``validate`` error tier + :func:`vsepr.advisories` warn tier);
- :func:`probe.fragments` — a bonded block that falls into several
  connected components (``disconnected_fragments``, warn);
- ``open_valence`` — a bonded main-group atom below its element's valence
  budget that is neither a declared port atom nor a declared radical
  (``Atom.magmom`` set, nonzero).

The finding **text** is the contract: both call sites build it with
:meth:`ChemFinding.text`, from the same scene bytes (the finish path checks what
``out.block`` wrote — bonds normalised the way the ``.bonds.json`` round trip
normalises them), so a run's ``check.json`` and the import echo read the same.
Each finding carries the construction-trace frame of its first atom (the
atom's ``maker``, else the block's own frame) and :func:`locate` resolves that
to the nearest user ``file:line`` — the 2t ``frame`` column convention.

Like :mod:`validate`, Å-native and DB-free; ships in the ``precis-geom`` wheel.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from . import elements, probe
from .scene import Scene
from .validate import findings_cached

#: Elements whose valence is one fixed number, so "below the budget" means an
#: open valence. S/P (hypervalent), metals and noble gases are excluded —
#: their lower valences are legitimate.
_FIXED_VALENCE = frozenset({"H", "B", "C", "N", "O", "F", "Si", "Cl", "Br", "I"})

#: A bond-order deficit below this is float round-off (an exact all-sp²
#: network sums 3 × 4/3), not an open valence.
_OPEN_EPS = 0.25

#: Atoms named in a finding's ``atoms`` / text before "+N more".
_NAMED = 6


class ChemCheckError(Exception):
    """``Run.finish()`` under ``check={'strict': True}`` with error findings."""


@dataclass(frozen=True)
class ChemFinding:
    """One chemistry-check finding (rule-grouped: ``count`` instances)."""

    rule: str
    severity: str  # 'error' | 'warn'
    atoms: tuple[str, ...]
    detail: str
    count: int = 1
    #: Run-local trace frame id (atom ``maker``, else the block's frame).
    frame: int | None = None
    #: ``main.py:<line>`` once :func:`locate` has resolved ``frame``.
    site: str | None = None

    def text(self) -> str:
        more = f" (+{self.count - 1} more)" if self.count > 1 else ""
        at = f" at {self.site}" if self.site else ""
        return f"mol.check {self.rule} [{self.severity}]: {self.detail}{more}{at}"

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rule": self.rule,
            "severity": self.severity,
            "atoms": list(self.atoms),
            "count": self.count,
            "text": self.text(),
        }
        if self.frame is not None:
            out["frame"] = self.frame
        if self.site is not None:
            out["site"] = self.site
        return out


def _port_atoms(ports: Iterable[Any] | None) -> set[str]:
    out: set[str] = set()
    for p in ports or ():
        atom = p.get("atom") if isinstance(p, dict) else getattr(p, "atom", None)
        if atom:
            out.add(str(atom))
    return out


def _open_valence(scene: Scene, ported: set[str]) -> list[tuple[str, str, float]]:
    """``(label, element, open order)`` for bonded atoms below budget."""
    if not scene.bonds:
        return []  # no bond graph at all: open valence is unknowable
    used: dict[str, float] = {}
    for b in scene.bonds:
        for end in (b.i, b.j):
            used[end] = used.get(end, 0.0) + float(b.order)
    out: list[tuple[str, str, float]] = []
    for label, atom in scene.atoms.items():
        if atom.element not in _FIXED_VALENCE or label in ported:
            continue
        if atom.magmom:  # a declared radical
            continue
        mv, known = elements.effective_valence(atom.element, atom.charge)
        if mv is None or not known:
            continue
        deficit = mv - used.get(label, 0.0)
        if deficit >= _OPEN_EPS:
            out.append((label, atom.element, deficit))
    return out


def _normalised(scene: Scene) -> Scene:
    """The scene as the ``.extxyz`` + ``.bonds.json`` round trip reads it
    back: every bond ``declared`` (the sidecar carries no provenance), so an
    in-process scene and its imported copy are checked alike."""
    if all(b.provenance == "declared" for b in scene.bonds):
        return scene
    return dataclasses.replace(
        scene,
        bonds=[dataclasses.replace(b, provenance="declared") for b in scene.bonds],
    )


def _names(labels: Sequence[str]) -> str:
    shown = ", ".join(labels[:_NAMED])
    return shown + (f", +{len(labels) - _NAMED} more" if len(labels) > _NAMED else "")


def check(
    geom_or_scene: Any,
    *,
    ports: Iterable[Any] | None = None,
    block_frame: int | None = None,
) -> list[ChemFinding]:
    """Chemistry findings for one atomic block, rule-grouped and ordered.

    ``geom_or_scene`` is a structure :class:`Scene` or a ``precis_geom``
    ``Geom`` (flattened; its declared ports are used when ``ports`` is
    ``None``). ``ports`` are manifest rows (``{"atom": label, …}``) or port
    objects; a port's atom may carry an open valence. ``block_frame`` is the
    block's trace frame, the fallback location for an atom with no ``maker``.
    """
    scene = geom_or_scene
    if not hasattr(scene, "atoms") and hasattr(scene, "flatten"):
        if ports is None:
            ports = list(getattr(scene, "ports", {}).values())
        scene = scene.flatten()
    scene = _normalised(scene)
    ported = _port_atoms(ports)

    def frame_of(labels: Sequence[str]) -> int | None:
        for lab in labels:
            atom = scene.atoms.get(lab)
            if atom is not None and atom.maker is not None:
                return int(atom.maker)
        return block_frame

    out: list[ChemFinding] = []
    groups: dict[str, list[Any]] = {}
    for issue in findings_cached(scene):
        groups.setdefault(issue.rule, []).append(issue)
    for rule, issues in groups.items():
        first = issues[0]
        out.append(
            ChemFinding(
                rule=rule,
                severity=first.severity,
                atoms=tuple(first.atoms[:_NAMED]),
                detail=first.suggested_fix,
                count=len(issues),
                frame=frame_of(first.atoms),
            )
        )
    opens = _open_valence(scene, ported)
    if opens:
        labels = [lab for lab, _el, _d in opens]
        desc = ", ".join(f"{lab} ({el}, {d:g} open)" for lab, el, d in opens[:_NAMED])
        more = f", +{len(opens) - _NAMED} more" if len(opens) > _NAMED else ""
        out.append(
            ChemFinding(
                rule="open_valence",
                severity="error",
                atoms=tuple(labels[:_NAMED]),
                detail=(
                    f"{len(opens)} atom(s) below their valence budget and "
                    f"neither a port nor a declared radical: {desc}{more} — "
                    "cap them, declare a port on the atom, or set magmom for "
                    "a radical"
                ),
                count=1,
                frame=frame_of(labels),
            )
        )
    if scene.bonds:
        frags = probe.fragments(scene)
        if len(frags) > 1:
            sizes = ", ".join(str(len(f)) for f in frags[:_NAMED])
            out.append(
                ChemFinding(
                    rule="disconnected_fragments",
                    severity="warn",
                    atoms=tuple(f[0] for f in frags[1 : _NAMED + 1]),
                    detail=(
                        f"{len(frags)} bonded fragments (sizes {sizes}"
                        f"{', …' if len(frags) > _NAMED else ''}) in one block — "
                        "a guest/host pair is fine; stray atoms are not"
                    ),
                    frame=frame_of([f[0] for f in frags[1:]]),
                )
            )
    out.sort(key=lambda f: (f.severity != "error", f.rule))
    return out


def site_of(
    frames: Sequence[dict[str, Any]] | None, frame_id: int | None
) -> str | None:
    """``file:line`` for run-local trace frame ``frame_id``: the frame's own
    call-site location (a ``core`` leaf already cites the user line that
    invoked it), else the nearest ``kind='user'`` ancestor's. ``None`` when
    the id is unknown or no frame in the path carries a location."""
    if frame_id is None or not frames:
        return None
    by_id = {f.get("id"): f for f in frames if isinstance(f, dict)}
    by_key = {f.get("key"): f for f in frames if isinstance(f, dict)}
    fr = by_id.get(int(frame_id))
    seen: set[Any] = set()
    while fr is not None and id(fr) not in seen:
        seen.add(id(fr))
        if fr.get("file") and fr.get("line"):
            if fr is by_id.get(int(frame_id)) or fr.get("kind") == "user":
                return f"{fr['file']}:{fr['line']}"
        parent = fr.get("parent")
        fr = by_key.get(parent) if parent is not None else None
    return None


def locate(
    findings: Iterable[ChemFinding], frames: Sequence[dict[str, Any]] | None
) -> list[ChemFinding]:
    """Resolve every finding's ``frame`` to its ``site`` (``main.py:<line>``)."""
    return [
        dataclasses.replace(f, site=site_of(frames, f.frame) or f.site)
        for f in findings
    ]


__all__ = ["ChemCheckError", "ChemFinding", "check", "locate", "site_of"]
