"""The IS/FS endpoint pair check — ``view='pair'`` (gr512126, slice 2).

A read-only, zero-compute gate table telling an agent *why* an initial/final
structure pair is not NEB-ready before any NEB exists. One row per gate in
the spec's Validation table (``docs/backlog/neb-structure-builder.md``):
the measured input, the threshold, ``pass``/``fail``/``warn``/``n/a``, and a
working ``next`` for every failed gate. A ``fail`` blocks a later
``mode='neb'`` mint (slice 3); a ``warn`` is advisory and never blocks.

Two deliberate core-only approximations of the spec's autocatpath wording
(``autocatpath`` is a GPU-node extra this package may not import — the
``struct_neb`` job lazy-imports it, slice 3):

- "max adsorbate displacement after ``align_endpoints``" is measured as the
  per-atom **MIC displacement by label** in the shared cell. The real
  aligner's per-fragment image choice can only *raise* an atom's
  displacement relative to its own minimum image, so the MIC figure
  under-measures — conservative for the "same basin" floor: a pair that
  fails here would also fail after alignment.
- "equal ``trust.fingerprint``" is :func:`cache.structure_sha` equality —
  the label/bond-free geometry content address; identical geometry hashes
  identically whatever the design or labelling.

"One elementary step" diffs the design's *stored* bond graph (the authored
intent an endpoint derive maintains — ``transfer``/``move_group`` mint and
drop declared bonds); a pair carrying no stored bonds at all (a CIF import)
falls back to ``probe.detect_bonds`` restricted to edges touching
non-slab atoms, so a pure site hop still reads as a change. The slab split
uses ``ops._largest_metal_component`` — the largest connected metal
component over combined adjacency, so a dopant inside the slab counts as
slab (the dominant-element heuristic ``preflight`` still carries gets
this wrong on PdCu).

Unit enclave (package docstring): Å-native.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import cache, probe, validate
from .ops import _combined_adjacency, _largest_metal_component
from .scene import Scene

#: Verdict values one gate row can carry.
PASS = "pass"
FAIL = "fail"
WARN = "warn"
NA = "n/a"

#: ``same cell``: max |Δlattice| (Å) below which two cells count as equal —
#: matches ``cache.structure_sha``'s float-jitter rounding.
_CELL_TOL_A = 1e-6
#: ``distinct basins``: below this max adsorbate move the pair is one basin.
_BASIN_MOVE_A = 0.3
#: ``no cross-cell drag``: an adsorbate atom moving farther than this
#: between endpoints is a placement smell, not a path.
_DRAG_A = 5.0
#: ``one elementary step``: a step makes/breaks at most this many bonds.
_STEP_MAX_BONDS = 2
#: ``relaxed``: the rung an endpoint must have converged at (spec: ``ml``
#: only — no GPAW relax has ever completed in prod).
_RELAXED_FIDELITY = "ml"


@dataclass
class PairGate:
    """One gate-table row: what was measured, against what, the verdict."""

    gate: str
    verdict: str
    detail: str
    threshold: str
    next: str | None = None


@dataclass
class PairVerdict:
    """The whole gate table for one IS/FS pair."""

    is_slug: str
    fs_slug: str
    gates: list[PairGate] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Every gate passed or warned — the pair is NEB-ready."""
        return not any(g.verdict == FAIL for g in self.gates)

    @property
    def failed(self) -> list[PairGate]:
        return [g for g in self.gates if g.verdict == FAIL]


def _relax_edit(slug: str, *, model: str | None = None) -> str:
    ops = "{'op': 'relax', 'fidelity': 'ml', 'steps': 300"
    if model:
        ops += f", 'model': {model!r}"
    ops += "}"
    return f"edit(kind='structure', id={slug!r}, ops=[{ops}])"


def _derive_next(is_slug: str) -> str:
    return (
        "derive the FS from the IS so labels carry over — "
        "put(kind='structure', id='<new-slug>', "
        f"args={{'derive_from': {is_slug!r}, 'ops': [...]}})"
    )


def _ml_models(runs: Iterable[Mapping[str, Any]], version: int) -> set[str]:
    """Models of a design's converged ``ml`` runs on its CURRENT version."""
    return {
        str(r.get("model"))
        for r in runs
        if r.get("fidelity") == _RELAXED_FIDELITY
        and r.get("converged")
        and r.get("status") == "succeeded"
        and int(r.get("on_version") or 0) == version
    }


def _slab(scene: Scene) -> set[str]:
    """The slab label set — largest connected metal component (spec rule)."""
    return _largest_metal_component(scene, _combined_adjacency(scene))


def _mic_displacements(
    is_scene: Scene, fs_scene: Scene, labels: Iterable[str]
) -> dict[str, float]:
    """Per-label MIC displacement (Å) IS→FS in the shared cell."""
    return {
        la: fs_scene.cell.mic(is_scene.atoms[la].frac, fs_scene.atoms[la].frac)[0]
        for la in labels
    }


def _bond_changes(is_scene: Scene, fs_scene: Scene, is_ads: set[str]) -> int:
    """Broken+formed bond count IS→FS — the stored intent graph, or, for a
    bond-less pair (CIF imports), inferred bonds touching an adsorbate."""
    if is_scene.bonds or fs_scene.bonds:
        b0 = {frozenset((b.i, b.j)) for b in is_scene.bonds}
        b1 = {frozenset((b.i, b.j)) for b in fs_scene.bonds}
        return len(b0 ^ b1)

    def _inferred(scene: Scene) -> set[frozenset[str]]:
        return {
            frozenset((b.i, b.j))
            for b in probe.detect_bonds(scene)
            if b.i in is_ads or b.j in is_ads
        }

    return len(_inferred(is_scene) ^ _inferred(fs_scene))


def evaluate_pair(
    is_scene: Scene,
    fs_scene: Scene,
    *,
    is_version: int = 0,
    fs_version: int = 0,
    is_runs: Iterable[Mapping[str, Any]] = (),
    fs_runs: Iterable[Mapping[str, Any]] = (),
    is_slug: str = "<is-slug>",
    fs_slug: str = "<fs-slug>",
) -> PairVerdict:
    """The pair gate table, pure: two hydrated scenes + their run history.

    ``is_runs``/``fs_runs`` are the designs' ``struct_runs`` rows (the
    ``store.structure_runs`` shape — the caller's only DB read); everything
    else comes off the scenes. Gates that need a shared label set or a
    shared cell report ``n/a`` when an upstream gate already failed, rather
    than measuring garbage across mismatched designs.
    """
    out = PairVerdict(is_slug=is_slug, fs_slug=fs_slug)
    g = out.gates.append
    is_labels, fs_labels = set(is_scene.atoms), set(fs_scene.atoms)

    # ── same atoms ──────────────────────────────────────────────────
    added = sorted(fs_labels - is_labels)
    removed = sorted(is_labels - fs_labels)
    el_diff = [
        la
        for la in sorted(is_labels & fs_labels)
        if is_scene.atoms[la].element != fs_scene.atoms[la].element
    ]
    ch_diff = [
        la
        for la in sorted(is_labels & fs_labels)
        if is_scene.atoms[la].charge != fs_scene.atoms[la].charge
    ]
    same_atoms = not (added or removed or el_diff or ch_diff)
    if same_atoms:
        detail = f"{len(is_labels)} labels · elements · charges identical"
    else:
        parts = []
        if added or removed:
            parts.append(
                f"+{len(added)}/-{len(removed)} labels "
                f"({', '.join((added + removed)[:4])})"
            )
        if el_diff:
            parts.append(
                f"element differs on {', '.join(el_diff[:4])} "
                f"(e.g. {el_diff[0]}: {is_scene.atoms[el_diff[0]].element} vs "
                f"{fs_scene.atoms[el_diff[0]].element})"
            )
        if ch_diff:
            parts.append(f"charge differs on {', '.join(ch_diff[:4])}")
        detail = " · ".join(parts)
    g(
        PairGate(
            gate="same atoms",
            verdict=PASS if same_atoms else FAIL,
            detail=detail,
            threshold="labels · element per label · charge identical",
            next=None if same_atoms else _derive_next(is_slug),
        )
    )

    # ── same cell ───────────────────────────────────────────────────
    dlat = float(np.abs(is_scene.cell.lattice - fs_scene.cell.lattice).max())
    same_pbc = tuple(is_scene.cell.pbc) == tuple(fs_scene.cell.pbc)
    same_cell = dlat <= _CELL_TOL_A and same_pbc
    g(
        PairGate(
            gate="same cell",
            verdict=PASS if same_cell else FAIL,
            detail=(
                f"|Δlattice| {dlat:.3g} Å · pbc "
                f"{list(is_scene.cell.pbc)} vs {list(fs_scene.cell.pbc)}"
            ),
            threshold=f"|Δlattice| ≤ {_CELL_TOL_A:g} Å · pbc identical",
            next=None
            if same_cell
            else "the FS must share the IS's cell — " + _derive_next(is_slug),
        )
    )
    comparable = same_atoms and same_cell

    # ── same fixed set ──────────────────────────────────────────────
    if not same_atoms:
        g(
            PairGate(
                gate="same fixed set",
                verdict=NA,
                detail="labels differ — per-label comparison impossible",
                threshold="fixed flags identical per label",
            )
        )
    else:
        fx_diff = [
            la
            for la in sorted(is_labels)
            if is_scene.atoms[la].fixed != fs_scene.atoms[la].fixed
        ]
        if not fx_diff:
            g(
                PairGate(
                    gate="same fixed set",
                    verdict=PASS,
                    detail=f"fixed flags identical on all {len(is_labels)} labels",
                    threshold="fixed flags identical per label",
                )
            )
        else:
            by_kind: dict[int, list[str]] = {}
            for la in fx_diff:
                by_kind.setdefault(int(is_scene.atoms[la].fixed), []).append(la)
            op_strs = []
            for mask, labels in sorted(by_kind.items()):
                kind = "fixed-all" if mask else "none"
                op_strs.append(
                    f"{{'op': 'constrain', 'atoms': {labels!r}, 'kind': {kind!r}}}"
                )
            g(
                PairGate(
                    gate="same fixed set",
                    verdict=FAIL,
                    detail=(
                        f"fixed flags differ on {len(fx_diff)} label(s): "
                        f"{', '.join(fx_diff[:6])}"
                    ),
                    threshold="fixed flags identical per label",
                    next=(
                        "match the IS's fixed flags on the FS — "
                        f"edit(kind='structure', id={fs_slug!r}, "
                        f"ops=[{', '.join(op_strs)}])"
                    ),
                )
            )

    # ── slab anchored ───────────────────────────────────────────────
    is_slab, fs_slab = _slab(is_scene), _slab(fs_scene)
    anchored = [
        (slug, scene, slab)
        for slug, scene, slab in (
            (is_slug, is_scene, is_slab),
            (fs_slug, fs_scene, fs_slab),
        )
        if slab and not any(scene.atoms[la].fixed for la in slab)
    ]
    if not (is_slab or fs_slab):
        g(
            PairGate(
                gate="slab anchored",
                verdict=PASS,
                detail="no metal slab (molecule/cluster pair) — nothing to anchor",
                threshold="≥1 fixed atom in the slab",
            )
        )
    elif not anchored:
        n_fixed = sum(1 for la in is_slab if is_scene.atoms[la].fixed)
        g(
            PairGate(
                gate="slab anchored",
                verdict=PASS,
                detail=f"{n_fixed}/{len(is_slab)} slab atoms fixed",
                threshold="≥1 fixed atom in the slab",
            )
        )
    else:
        names = " and ".join(f"{slug} (0/{len(slab)})" for slug, _s, slab in anchored)
        g(
            PairGate(
                gate="slab anchored",
                verdict=FAIL,
                detail=f"no fixed atom in the slab on {names}",
                threshold="≥1 fixed atom in the slab",
                next=(
                    "freeze the slab's bottom layer(s) — "
                    "edit(ops=[{'op': 'constrain', 'atoms': "
                    "['<bottom-layer labels>'], 'kind': 'fixed-all'}]); "
                    "a new slab gets them for free with "
                    "{'op': 'slab', 'fix_layers': 2}"
                ),
            )
        )

    # ── valid ───────────────────────────────────────────────────────
    is_errs = [f for f in validate(is_scene) if f.severity == "error"]
    fs_errs = [f for f in validate(fs_scene) if f.severity == "error"]
    if not (is_errs or fs_errs):
        g(
            PairGate(
                gate="valid",
                verdict=PASS,
                detail="0 validator errors on both designs",
                threshold="0 validator errors on both designs",
            )
        )
    else:
        fixes = [
            f"{slug}: {errs[0].suggested_fix}"
            for slug, errs in ((is_slug, is_errs), (fs_slug, fs_errs))
            if errs
        ]
        g(
            PairGate(
                gate="valid",
                verdict=FAIL,
                detail=(
                    f"{is_slug}: {len(is_errs)} error(s) · "
                    f"{fs_slug}: {len(fs_errs)} error(s)"
                ),
                threshold="0 validator errors on both designs",
                next=" · ".join(fixes),
            )
        )

    # ── relaxed ─────────────────────────────────────────────────────
    is_models = _ml_models(is_runs, is_version)
    fs_models = _ml_models(fs_runs, fs_version)
    shared = is_models & fs_models
    if shared:
        g(
            PairGate(
                gate="relaxed",
                verdict=PASS,
                detail=(
                    f"{is_slug}: {sorted(is_models)}@v{is_version} · "
                    f"{fs_slug}: {sorted(fs_models)}@v{fs_version}"
                ),
                threshold="converged ml run, same model, current version, both",
            )
        )
    else:
        edits = []
        if not is_models:
            model = sorted(fs_models)[0] if fs_models else None
            edits.append(_relax_edit(is_slug, model=model))
        if not fs_models:
            model = sorted(is_models)[0] if is_models else None
            edits.append(_relax_edit(fs_slug, model=model))
        if not edits and is_models and fs_models:
            edits.append(_relax_edit(fs_slug, model=sorted(is_models)[0]))
        g(
            PairGate(
                gate="relaxed",
                verdict=FAIL,
                detail=(
                    f"{is_slug}: {sorted(is_models) or '—'}@v{is_version} · "
                    f"{fs_slug}: {sorted(fs_models) or '—'}@v{fs_version} — "
                    "no shared converged ml model"
                ),
                threshold="converged ml run, same model, current version, both",
                next=" then ".join(edits),
            )
        )

    # ── distinct basins + cross-cell drag (one displacement pass) ────
    ads_labels = sorted(is_labels - is_slab) if same_atoms else []
    if same_atoms and not ads_labels:
        ads_labels = sorted(is_labels)  # pure-slab pair: judge every atom
    if not comparable:
        skip = "same atoms" if not same_atoms else "same cell"
        for gate, threshold in (
            ("distinct basins", f"max adsorbate move ≥ {_BASIN_MOVE_A:g} Å"),
            ("no cross-cell drag", f"max adsorbate move ≤ {_DRAG_A:g} Å"),
        ):
            g(
                PairGate(
                    gate=gate,
                    verdict=NA,
                    detail=f"needs the {skip} gate first",
                    threshold=threshold,
                )
            )
    else:
        same_sha = cache.structure_sha(is_scene) == cache.structure_sha(fs_scene)
        disp = _mic_displacements(is_scene, fs_scene, ads_labels)
        moved = sorted(disp.items(), key=lambda kv: kv[1], reverse=True)
        top_label, top_move = moved[0] if moved else ("—", 0.0)
        distinct = not same_sha and top_move >= _BASIN_MOVE_A
        g(
            PairGate(
                gate="distinct basins",
                verdict=PASS if distinct else FAIL,
                detail=(
                    "identical geometry (equal structure fingerprint)"
                    if same_sha
                    else f"max adsorbate move {top_move:.2f} Å ({top_label})"
                ),
                threshold=(
                    f"max adsorbate move ≥ {_BASIN_MOVE_A:g} Å · geometry differs"
                ),
                next=None
                if distinct
                else (
                    "change the FS — the pair sits in one basin, so an NEB "
                    "has no barrier to find (move the reacting atoms with "
                    "'transfer'/'move_group' or pick a different site)"
                ),
            )
        )
        drag = top_move > _DRAG_A
        g(
            PairGate(
                gate="no cross-cell drag",
                verdict=WARN if drag else PASS,
                detail=(
                    f"max adsorbate move {top_move:.2f} Å ({top_label})"
                    + (" — over the drag limit" if drag else "")
                ),
                threshold=f"max adsorbate move ≤ {_DRAG_A:g} Å",
                next=None
                if not drag
                else (
                    "check the FS placement — an endpoint atom crossing "
                    "half the cell usually means a wrong site or a torn "
                    "fragment, not a real elementary step"
                ),
            )
        )

    # ── one elementary step ─────────────────────────────────────────
    if not same_atoms:
        g(
            PairGate(
                gate="one elementary step",
                verdict=NA,
                detail="labels differ — bond diff impossible",
                threshold=f"1–{_STEP_MAX_BONDS} bond changes IS→FS",
            )
        )
    else:
        changes = _bond_changes(is_scene, fs_scene, set(ads_labels))
        if changes == 0:
            verdict, nxt = (
                FAIL,
                (
                    "the FS must break or form a bond — 'transfer' is the "
                    "elementary unit (a pure site hop with no connectivity "
                    "change is not a reaction step)"
                ),
            )
        elif changes > _STEP_MAX_BONDS:
            verdict, nxt = (
                WARN,
                (
                    "split into two steps — an elementary step makes/breaks "
                    f"≤{_STEP_MAX_BONDS} bonds; derive an intermediate and "
                    "run two edges"
                ),
            )
        else:
            verdict, nxt = PASS, None
        g(
            PairGate(
                gate="one elementary step",
                verdict=verdict,
                detail=f"{changes} bond change(s) IS→FS",
                threshold=f"1–{_STEP_MAX_BONDS} bond changes IS→FS",
                next=nxt,
            )
        )

    return out
