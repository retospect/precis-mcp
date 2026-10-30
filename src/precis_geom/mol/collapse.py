"""``mol.relax`` — the cached collapse check (molecular-workspace-code
slice 3, Reto 2026-10-29: "see if it collapses quickly and caching is
good").

``relax(geom, rung='auto', *, steps=200, fmax=None, max_disp=2.0)`` relaxes
a Geom on an *energy* rung and compares the bond graph the relaxed positions
perceive (:func:`precis.structure.probe.detect_bonds`) with the graph the
Geom declares — positions move, ``scene.bonds`` do not, so
``probe.diff`` alone would see nothing. It returns ``(relaxed Geom,
CollapseCheck)``:

- **Rungs.** ``uff`` (ours, RDKit UFF on the declared graph, finite only),
  ``emt`` (periodic-capable, H/C/N/O and the fcc metals), ``xtb`` (GFN2,
  where tblite installs) and ``geo`` (a geometry repair on the declared
  graph: it cannot break a bond, so its verdict covers only displacement and
  the ring census, and it takes no ``fmax`` and no early exit). ``auto``:
  finite -> ``uff``; periodic -> ``xtb`` if installed, else ``emt`` when
  every element is in ``EMT_ELEMENTS``, else a refusal naming the way out.
- **Verdict.** ``collapsed`` when a declared bond breaks, a contact forms, a
  ring of the declared graph is lost, the fragment count changes or the
  largest atomic move exceeds ``max_disp`` Å (default 2.0); else ``ok``. Each
  changed bond is named by both atoms' maker frames resolved to the user's
  ``main.py:<line>``, e.g. ``junction#3 <- main.py:9 lost aC12-aC13 (1.42 ->
  2.31 A)``.
- **Early exit.** Every 10 optimiser steps the bonds are re-perceived
  (``on_step``) and the run stops at the first change not already there at
  step 0 (a contact the input started with is judged at the end, not
  mistaken for a collapse the relax caused).
- **Cached.** One ``@steps.step`` whose key carries the resolved rung,
  ``steps``, ``fmax`` and ``max_disp``; a rejected recipe later reverted is
  a hit. The check rides ``Geom.meta['collapse']`` (JSON; frame names are
  resolved per call, against the live trace) and ``Geom.meta['relaxed_by']``
  (``{rung, verdict, key, pos_sha}``), which ``out.block`` turns into the
  manifest row's ``relaxed_by: {rung, key, verdict}`` while the block's
  coordinates are still the relaxed ones.
- **Checkpoint.** Every probe also writes ``checkpoint.json`` through
  :func:`precis_geom.steps.checkpoint`; a re-run of the same key after a
  crash resumes from the last one.

The relaxed Geom keeps every atom's ``maker`` (relax moves atoms, it never
re-makes them) and drops ``meta['_net']``: the Net's cached coordinates no
longer describe the atoms, so ``mol.cap`` falls back to the scene path.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib
import importlib.util
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis.structure import check as _chem
from precis.structure import probe
from precis_geom import steps as _steps
from precis_geom import trace as _trace
from precis_geom.assembly import Geom

#: Rungs ``rung=`` accepts (``auto`` resolves to one of the others).
RUNGS = ("auto", "uff", "emt", "xtb", "geo")
#: Energy rungs (they have forces, so ``fmax`` and the early exit apply).
_ENERGY = ("uff", "emt", "xtb")
#: Default force target (eV/Å) of the energy rungs.
FMAX = 0.1
#: Default displacement bound (Å): a larger move is a collapse of its own.
MAX_DISP_A = 2.0
#: Optimiser steps between bond re-perceptions / checkpoints.
PROBE_EVERY = 10
#: Changed bonds / lost rings named in the summary before "+N more".
_NAMED = 4
#: Bump when the relax rungs / verdict change in a way the step's own source
#: does not show (a force-field parameter, an optimiser setting): old cached
#: verdicts then miss instead of serving a stale one.
_REV = 2

_relax = importlib.import_module("precis.structure.relax")


class RelaxCollapsed(RuntimeError):
    """``CollapseCheck.raise_if_collapsed()`` — the relaxed structure's bond
    graph differs from the declared one. The message names each changed bond
    by its atoms' maker frames (``main.py:<line>``); ``out.run``'s ``except``
    path records the failed run. ``check`` is the :class:`CollapseCheck`."""

    diag_code = "relax.collapsed"

    def __init__(self, message: str, check: CollapseCheck | None = None) -> None:
        super().__init__(message)
        self.check = check


@dataclass(frozen=True)
class BondChange:
    """One declared bond that broke, or one contact that formed.

    ``frames`` names the maker frame of each end (``junction#3 <-
    main.py:9``; both ends when they differ); ``at_start`` marks a change
    the input geometry already showed before any relax step."""

    kind: str  # 'broken' | 'formed'
    a: str
    b: str
    d_before: float
    d_after: float
    at_start: bool = False
    frames: str = ""

    def text(self) -> str:
        verb = "lost" if self.kind == "broken" else "formed"
        who = f"{self.frames} " if self.frames else ""
        start = " (already so at step 0)" if self.at_start else ""
        return (
            f"{who}{verb} {self.a}–{self.b} "
            f"({self.d_before:.2f} → {self.d_after:.2f} Å){start}"
        )


@dataclass(frozen=True)
class CollapseCheck:
    """The collapse check's verdict (see the module docstring).

    ``verdict`` is ``'ok'`` or ``'collapsed'``; ``broken`` / ``formed`` the
    :class:`BondChange` lists (empty for ``geo``, whose verdict is limited to
    displacement and rings); ``rings_lost`` the declared rings (<= 8 atoms)
    the relaxed geometry no longer closes; ``max_disp_A`` the largest atomic
    move; ``energy_drop`` the energy fall in eV (``None`` for ``geo`` and
    for a resumed run); ``steps`` the optimiser steps taken; ``early_exit``
    the step the run stopped at on a change (``None`` = ran on);
    ``resumed_from`` the checkpoint step it resumed from; ``key`` the step
    key (what ``relaxed_by`` cites)."""

    verdict: str
    rung: str
    steps: int
    max_disp_A: float
    broken: tuple[BondChange, ...] = ()
    formed: tuple[BondChange, ...] = ()
    rings_lost: int = 0
    energy_drop: float | None = None
    fragments: tuple[int, int] = (1, 1)
    converged: bool = False
    early_exit: int | None = None
    resumed_from: int | None = None
    input_mismatch: int = 0
    limited: bool = False
    reasons: tuple[str, ...] = ()
    key: str | None = None
    ring_atoms: tuple[tuple[str, ...], ...] = field(default=(), repr=False)

    @property
    def collapsed(self) -> bool:
        return self.verdict == "collapsed"

    def summary(self) -> str:
        """One line: the verdict and what drove it."""
        head = f"mol.relax {self.rung} {self.verdict}"
        if not self.collapsed:
            drop = (
                f", energy -{self.energy_drop:.3g} eV"
                if self.energy_drop is not None
                else ""
            )
            fixed = (
                f"; input had {self.input_mismatch} bond-graph mismatch(es) "
                "(contact or stretched bond), settled by the relax"
                if self.input_mismatch
                else ""
            )
            return (
                f"{head} after {self.steps} step(s): max displacement "
                f"{self.max_disp_A:.2f} Å{drop}{fixed}"
            )
        at = f" at step {self.early_exit}" if self.early_exit is not None else ""
        return f"{head}{at}: " + "; ".join(self.reasons)

    def text(self) -> str:
        """The summary plus each named change (first few, "+N more")."""
        lines = [self.summary()]
        for group in (self.broken, self.formed):
            lines += [f"  {c.text()}" for c in group[:_NAMED]]
            if len(group) > _NAMED:
                lines.append(f"  +{len(group) - _NAMED} more {group[0].kind}")
        for ring in self.ring_atoms[:_NAMED]:
            lines.append(f"  ring lost: {'–'.join(ring)}")
        return "\n".join(lines)

    def raise_if_collapsed(self) -> CollapseCheck:
        """Raise :class:`RelaxCollapsed` (message = :meth:`text`) when the
        verdict is ``collapsed``; return ``self`` otherwise."""
        if self.collapsed:
            raise RelaxCollapsed(self.text(), self)
        return self


# ── the verdict, frame-free (this is what the step caches) ───────────────


def _pair(b: Any) -> frozenset[str]:
    return frozenset((b.i, b.j))


def _pos_sha(scene: Any) -> str:
    """The goldens' rule (a 1e-6 grid, packed float64) over the atoms in
    scene order — ``out.block`` compares it to still-relaxed coordinates."""
    cart = np.asarray(
        [scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()], dtype=float
    ).reshape(-1, 3)
    return hashlib.sha256((np.round(cart, 6) + 0.0).astype("<f8").tobytes()).hexdigest()


def pos_sha(geom_or_scene: Any) -> str:
    """Coordinate digest of a Geom's (flattened) scene — see
    ``Geom.meta['relaxed_by']['pos_sha']``."""
    scene = (
        geom_or_scene.flatten() if hasattr(geom_or_scene, "flatten") else geom_or_scene
    )
    return _pos_sha(scene)


def _perceived(scene: Any) -> frozenset[frozenset[str]]:
    return frozenset(_pair(b) for b in probe.detect_bonds(scene) if b.i != b.j)


def _graph_scene(scene: Any, bonds: Sequence[Any]) -> Any:
    """``scene`` with ``bonds`` as its graph (for rings / fragments)."""
    return dataclasses.replace(scene, bonds=list(bonds))


#: Largest ring the census counts (``probe.rings``' default).
_RING_MAX = 8


def _rings(scene: Any) -> list[list[str]]:
    """The smallest ring through each declared bond, as ordered atom lists —
    ``probe.rings``' census, but with sorted neighbour order. ``probe.rings``
    breaks ties between equal-length rings by set iteration order, which
    differs between processes (and between two graphs over the same
    edges); a ring count that moves with ``PYTHONHASHSEED`` cannot be a
    verdict."""
    adj: dict[str, list[str]] = {la: [] for la in scene.atoms}
    for b in scene.bonds:
        if b.i != b.j and b.i in adj and b.j in adj and b.j not in adj[b.i]:
            adj[b.i].append(b.j)
            adj[b.j].append(b.i)
    for nbrs in adj.values():
        nbrs.sort()
    found: dict[frozenset[str], list[str]] = {}
    for i in sorted(adj):
        for j in adj[i]:
            if i > j:
                continue
            prev: dict[str, str | None] = {i: None}
            queue = [i]
            hit = False
            for cur in queue:  # BFS i -> j without the i-j edge itself
                for nxt in adj[cur]:
                    if cur == i and nxt == j:
                        continue
                    if nxt == j:
                        prev[j] = cur
                        hit = True
                        break
                    if nxt not in prev:
                        prev[nxt] = cur
                        queue.append(nxt)
                if hit:
                    break
            if not hit:
                continue
            chain = [j]
            while (p := prev.get(chain[-1])) is not None:
                chain.append(p)
            if len(chain) <= _RING_MAX:
                found.setdefault(frozenset(chain), list(reversed(chain)))
    return sorted(found.values(), key=lambda r: (len(r), sorted(r)))


def _dist(scene: Any, a: str, b: str) -> float:
    return float(probe.distance(scene, a, b))


def _judge(
    before: Any,
    after: Any,
    *,
    rung: str,
    steps: int,
    max_disp: float,
    energy_drop: float | None,
    converged: bool,
    early_exit: int | None,
    resumed_from: int | None,
) -> dict[str, Any]:
    """The frame-free collapse document for ``before`` (declared graph,
    input positions) vs ``after`` (the same atoms, relaxed positions)."""
    limited = rung == "geo"
    declared = {_pair(b) for b in before.bonds if b.i != b.j}
    start = _perceived(before)
    end = _perceived(after)
    perceived_bonds = probe.detect_bonds(after)
    moved = probe.diff(before, after).max_disp

    def rows(pairs: AbstractSet[frozenset[str]]) -> list[list[Any]]:
        out = []
        for p in sorted(pairs, key=sorted):
            a, b = sorted(p)
            out.append(
                [
                    a,
                    b,
                    round(_dist(before, a, b), 4),
                    round(_dist(after, a, b), 4),
                    p in (start ^ declared),
                ]
            )
        return out

    broken = rows(declared - end) if not limited else []
    formed = rows(end - declared) if not limited else []
    # a declared ring is lost when the relaxed geometry no longer perceives
    # one of its bonds
    rings_before = _rings(before)
    lost = [
        r
        for r in rings_before
        if any(frozenset((r[k], r[(k + 1) % len(r)])) not in end for k in range(len(r)))
    ]
    frag_before = len(probe.fragments(_graph_scene(before, before.bonds)))
    frag_after = len(probe.fragments(_graph_scene(after, perceived_bonds)))
    reasons: list[str] = []
    if broken:
        reasons.append(f"{len(broken)} declared bond(s) broke")
    if formed:
        reasons.append(f"{len(formed)} contact(s) formed")
    if lost:
        reasons.append(f"{len(lost)} ring(s) lost")
    if not limited and frag_before != frag_after:
        reasons.append(f"fragments {frag_before} → {frag_after}")
    if moved > max_disp:
        reasons.append(f"max displacement {moved:.2f} Å > {max_disp:g} Å")
    return {
        "verdict": "collapsed" if reasons else "ok",
        "rung": rung,
        "steps": steps,
        "max_disp_A": round(float(moved), 4),
        "disp_limit_A": max_disp,
        "energy_drop": None if energy_drop is None else float(energy_drop),
        "broken": broken,
        "formed": formed,
        "rings_before": len(rings_before),
        "rings_lost": len(lost),
        "ring_atoms": [list(r) for r in lost[:_NAMED]],
        "fragments": [frag_before, frag_after] if not limited else [1, 1],
        "converged": bool(converged),
        "early_exit": early_exit,
        "resumed_from": resumed_from,
        "input_mismatch": len(start ^ declared) if not limited else 0,
        "limited": limited,
        "reasons": reasons,
        "pos_sha": _pos_sha(after),
    }


# ── the cached step ─────────────────────────────────────────────────────


def _monitor(
    before: Any,
    work: Any,
    *,
    done0: int,
    ck: _steps.Checkpoint | None,
    seed: dict[str, Any],
) -> Any:
    """The ``on_step`` hook: every :data:`PROBE_EVERY` steps re-perceive the
    bonds at the optimiser's positions and stop at the first change the
    input did not already show; and save the checkpoint."""
    from precis.structure import _pair_kernel

    labels = list(work.atoms)
    index = {la: k for k, la in enumerate(labels)}
    lat, pbc = _pair_kernel.cell_arrays(work.cell)
    radii = _pair_kernel.radii_of(work, labels)
    inv = np.linalg.inv(lat)

    def perceive(frac: np.ndarray) -> frozenset[tuple[int, int]]:
        # the kernel behind probe.detect_bonds, minus its Bond objects
        ii, jj, _d, _img = _pair_kernel.pairs_within(frac, lat, pbc, radii, 1.2, True)
        return frozenset(zip(ii.tolist(), jj.tolist(), strict=True))

    declared = {
        (min(index[b.i], index[b.j]), max(index[b.i], index[b.j]))
        for b in before.bonds
        if b.i != b.j
    }
    start = np.array([a.frac for a in before.atoms.values()], dtype=np.float64)
    start_off = perceive(start) ^ declared

    def hook(n: int, pos: np.ndarray, _curve: list[float]) -> bool:
        if n == 0 or n % PROBE_EVERY:
            return False
        fracs = np.ascontiguousarray(np.asarray(pos, dtype=float) @ inv)
        if ck is not None:
            ck.save(
                {
                    "v": 1,
                    "done": done0 + n,
                    "atoms": labels,
                    "pos": np.asarray(pos, dtype=float).tolist(),
                }
            )
        new = (perceive(fracs) ^ declared) - start_off
        if new:
            seed["early_exit"] = n
            return True
        return False

    return hook


def _resume(ck: _steps.Checkpoint | None, work: Any, steps: int) -> int:
    """Load the checkpoint into ``work`` when it belongs to these atoms;
    returns the steps already done (0 = start fresh)."""
    doc = ck.load() if ck is not None else None
    if not doc or doc.get("v") != 1:
        return 0
    labels = list(work.atoms)
    pos = doc.get("pos")
    done = doc.get("done")
    if (
        doc.get("atoms") != labels
        or not isinstance(done, int)
        or not 0 < done < steps
        or not isinstance(pos, list)
        or len(pos) != len(labels)
    ):
        return 0
    cart = np.asarray(pos, dtype=float)
    fracs = cart @ np.linalg.inv(np.asarray(work.cell.lattice, dtype=float))
    for la, f in zip(labels, fracs, strict=True):
        work.atoms[la].frac = work.cell.wrap(f)
    return done


def _engine(rung: str) -> str:
    """What the rung's numbers depend on beyond the step's own source — the
    step key covers that source, not its callees (steps §Key), so a changed
    force field or optimiser has to show up as a parameter."""
    import ase

    parts = [f"collapse/{_REV}", f"ase {ase.__version__}"]
    if rung == "uff":
        import rdkit

        parts.append(f"rdkit {rdkit.__version__}")
    return "; ".join(parts)


@_steps.step(name="relax")
def _relax_step(
    geom: Geom,
    rung: str,
    steps: int,
    fmax: float | None,
    max_disp: float,
    engine: str,
) -> Geom:
    """Relax + judge. Pure in its arguments (the key); the checkpoint is a
    crash-recovery side file, not an input. ``engine`` only keys the entry
    (:func:`_engine`)."""
    before = geom.flatten()
    work = copy.deepcopy(before)
    ck = _steps.checkpoint()
    done0 = _resume(ck, work, steps)
    seed: dict[str, Any] = {"early_exit": None}
    hook = (
        _monitor(before, work, done0=done0, ck=ck, seed=seed)
        if rung in _ENERGY
        else None
    )
    res = _relax.relax(
        work,
        fidelity=rung,
        steps=steps - done0,
        fmax=fmax,
        on_step=hook,
    )
    drop = (
        res.energy_start - res.energy
        if not done0 and res.energy_start is not None and res.energy is not None
        else None
    )
    doc = _judge(
        before,
        work,
        rung=rung,
        steps=done0 + res.n_steps,
        max_disp=max_disp,
        energy_drop=drop,
        converged=res.converged,
        early_exit=seed["early_exit"],
        resumed_from=done0 or None,
    )
    base = (
        geom
        if geom.scene is not None and not geom.placements and geom.pose is None
        else Geom(findings=geom.findings)
    )
    findings = tuple(base.findings)
    if doc["verdict"] == "collapsed":
        findings = (
            *findings,
            f"relax.collapsed: {rung} — " + "; ".join(doc["reasons"]),
        )
    meta = {k: v for k, v in base.meta.items() if k not in ("_net", "step")}
    meta["relaxed_by"] = {
        "rung": rung,
        "verdict": doc["verdict"],
        "key": _steps.current_key(),
        "pos_sha": doc["pos_sha"],
    }
    meta["collapse"] = doc
    return dataclasses.replace(
        base, scene=work, meta={"units": "A", **meta}, findings=findings
    )


# ── frames: name each changed bond by its atoms' makers ──────────────────


def _frame_name(frames: list[dict[str, Any]] | None, maker: int | None) -> str:
    if maker is None:
        return ""
    fr = next((f for f in frames or () if f.get("id") == maker), None)
    if fr is None:
        return f"frame {maker}"
    label = f"{fr.get('qual')}#{fr.get('occ')}"
    site = _chem.site_of(frames, maker)
    return f"{label} ← {site}" if site else label


def _changes(
    rows: Sequence[Sequence[Any]],
    kind: str,
    scene: Any,
    frames: list[dict[str, Any]] | None,
) -> tuple[BondChange, ...]:
    out = []
    for a, b, d0, d1, at_start in rows:
        names: list[str] = []
        for end in (a, b):
            atom = scene.atoms.get(end)
            nm = _frame_name(frames, atom.maker if atom is not None else None)
            if nm and nm not in names:
                names.append(nm)
        out.append(
            BondChange(
                kind=kind,
                a=a,
                b=b,
                d_before=float(d0),
                d_after=float(d1),
                at_start=bool(at_start),
                frames=" / ".join(names),
            )
        )
    return tuple(out)


def _check_from(geom: Geom, doc: dict[str, Any]) -> CollapseCheck:
    scene = geom.scene
    frames = None
    if doc.get("broken") or doc.get("formed"):
        tr = _trace.current()
        frames = (tr.dump({}) or {}).get("frames") if tr is not None else None
    fr = doc.get("fragments") or (1, 1)
    return CollapseCheck(
        verdict=str(doc["verdict"]),
        rung=str(doc["rung"]),
        steps=int(doc["steps"]),
        max_disp_A=float(doc["max_disp_A"]),
        broken=_changes(doc.get("broken") or (), "broken", scene, frames),
        formed=_changes(doc.get("formed") or (), "formed", scene, frames),
        rings_lost=int(doc.get("rings_lost") or 0),
        energy_drop=doc.get("energy_drop"),
        fragments=(int(fr[0]), int(fr[1])),
        converged=bool(doc.get("converged")),
        early_exit=doc.get("early_exit"),
        resumed_from=doc.get("resumed_from"),
        input_mismatch=int(doc.get("input_mismatch") or 0),
        limited=bool(doc.get("limited")),
        reasons=tuple(doc.get("reasons") or ()),
        key=geom.meta.get("step"),
        ring_atoms=tuple(tuple(r) for r in doc.get("ring_atoms") or ()),
    )


# ── the public call ─────────────────────────────────────────────────────


def _auto_rung(scene: Any) -> str:
    if not any(scene.cell.pbc):
        return "uff"
    if importlib.util.find_spec("tblite") is not None:
        return "xtb"
    if {a.element for a in scene.atoms.values()} <= _relax.EMT_ELEMENTS:
        return "emt"
    raise BadInput(
        "mol.relax(rung='auto'): a periodic scene needs xtb (tblite is not "
        "installed) or emt (covers only "
        f"{sorted(_relax.EMT_ELEMENTS)}), and this scene has other elements",
        next="install precis-mcp[estimate] for rung='xtb', or relax it on "
        "the service (a struct_relax job) and check the result there",
    )


def relax(
    geom: Any,
    rung: str = "auto",
    *,
    steps: int = 200,
    fmax: float | None = None,
    max_disp: float = MAX_DISP_A,
) -> tuple[Geom, CollapseCheck]:
    """Relax ``geom`` on ``rung`` and judge whether its bond graph
    collapses; returns ``(relaxed Geom, CollapseCheck)``. A cached step —
    an identical second call is a hit.

    ``rung`` is ``'auto'`` (finite -> ``uff``; periodic -> ``xtb`` if
    installed, else ``emt`` for H/C/N/O/fcc-metal scenes, else a refusal),
    ``'uff'`` (finite only), ``'emt'``, ``'xtb'`` or ``'geo'``. ``steps``
    caps the optimiser; ``fmax`` is the force target in eV/Å (default
    ``0.1`` on an energy rung; ``geo`` has no forces and refuses it);
    ``max_disp`` the displacement bound in Å. See the module docstring for
    the verdict and the early exit."""
    if rung not in RUNGS:
        raise BadInput(
            f"mol.relax: unknown rung {rung!r}", options=list(RUNGS), next="rung='auto'"
        )
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
        raise BadInput(f"mol.relax: steps must be an integer >= 1, got {steps!r}")
    if max_disp <= 0:
        raise BadInput(f"mol.relax: max_disp must be > 0 Å, got {max_disp!r}")
    if fmax is not None and fmax <= 0:
        raise BadInput(f"mol.relax: fmax must be > 0 eV/Å, got {fmax!r}")
    g = geom if isinstance(geom, Geom) else _as_geom(geom)
    scene = g.flatten()
    if not scene.atoms:
        raise BadInput("mol.relax: the geometry has no atoms")
    if len(scene.atoms) > 1 and not scene.bonds:
        raise BadInput(
            "mol.relax: the geometry declares no bonds — the collapse check "
            "compares the relaxed bond graph with the declared one",
            next="build it with mol.frag / mol.attach / pga.geom(net), or "
            "declare bonds on the Scene",
        )
    if rung == "geo" and fmax is not None:
        raise BadInput(
            "mol.relax: rung 'geo' is a geometry repair on the declared "
            "graph — it has no forces, so no fmax",
            next="drop fmax=, or use rung='uff' (finite), 'emt' or 'xtb'",
        )
    resolved = _auto_rung(scene) if rung == "auto" else rung
    f = None if resolved == "geo" else (FMAX if fmax is None else float(fmax))
    out = _relax_step(g, resolved, int(steps), f, float(max_disp), _engine(resolved))
    return out, _check_from(out, out.meta["collapse"])


def _as_geom(obj: Any) -> Geom:
    from precis_geom import assembly as pga

    return pga.geom(obj)


__all__ = [
    "BondChange",
    "CollapseCheck",
    "RelaxCollapsed",
    "pos_sha",
    "relax",
]
