"""The ``hexfold_scene`` generator — several authored fillet feet on one
sheet, planned and relaxed by :func:`~precis_se.atomic.generators.authored_foot.plan_scene`
and minted as one block (docs/backlog/hexfold-ideal-surface-then-tile.md,
S4b). Where :mod:`~precis_se.atomic.generators.hexfold_spec` takes a ``.hx``
text and a free stick relaxation, this takes the *scene*: a sheet, and per
feature a hole cell, an ``(n, 0)`` tube, the fillet radius the author wants
and a top (``open``/``lid``/``ball``/``sphere``); the planner writes the spec, picks
each frustum width ``k`` by measurement and relaxes the whole scene under
the normal tether toward the authored surfaces.

``params`` is ``{"sheet": [w, h], "features": [{"name", "at": [i, j], "n",
"radius", "tube_len", "top"?, "top_R"?, "top_fillet"?}], "extra"?: str,
"k_tether"?: float}``;
``top`` defaults to ``"open"``, ``extra`` (verbatim ``.hx`` lines, for
buds) to ``""``, ``k_tether`` to 1.0. An unknown key at either level is
refused by name, so a typo never silently drops a feature option. The
planner's refusals (a seam-phase hole cell, overlapping features, a top the
build cannot seat) arrive as :class:`GeneratorError`.

**Authored tops.** ``top: "sphere"`` (n a multiple of 6, n >= 12; optional
``top_R`` and ``top_fillet``, Å) and ``top: "lid"`` with ``top_fillet`` (at
most the tube radius) hold the top to an authored surface, planned by
:func:`~precis_se.atomic.generators.authored_foot.plan_top`: a washer
frustum, bulge and lid for the sphere, today's flat lid rounded toward a
hemisphere for the lid. Without ``top_fillet`` a ``lid`` is today's flat lid,
byte for byte, and ``ball`` is unchanged. The stored ``plan["top_plans"]``
carries, per such top, the chosen ``k``/``L``, the realised ``R`` and
fillet, the measured deviation p95 and theta_p max, which of the five bars
were met, the relaxed (tether-off) p95, and every candidate measured. Three
WARNs: ``scene.top.bar`` (a bar missed on the scene), ``scene.top.R_mismatch``
(an authored ``top_R`` more than 0.5 A from the realised R) and
``scene.top.relaxed_shape`` (relaxed p95 over the 0.5 A band; the stored scene
stays the tethered one).

**Plan table.** The default tops are measured once and checked in
(:data:`~precis_se.atomic.generators.authored_foot.TOP_TABLE_PATH`): a sphere
with the default fillet at n = 12, 18, 24, 30 and 36, and a rounded lid with
``top_fillet`` equal to the tube radius rounded down to 0.01 A (4.69 at
n = 12) at n = 12, 18 and 24. A
tabled top builds no candidate, and ``plan["top_plans"]`` says
``planned: table``. The candidate budget in :func:`_normalize` counts only
the tops planned live; the scene-relax ceiling (one ``sphere`` per scene op,
n <= 12) holds for every sphere, because the table removes the planning
time, not the scene's relax.

**Judgement travels with the block.** The coordinates are the tethered
relaxation, not a free one, so the geometry findings are judged on them and
labelled ``relax=tethered`` (:class:`hexfold.check.Relaxed`). On top of
those, two scene WARNs: ``scene.bar`` for a feature whose foot row misses a
planner bar, and ``scene.top.joint`` for a ball top, whose fused ``(6,0)``
neck is a stick geometry only (gr464391). The provenance line carries
``relax=tethered``, the chosen ``k`` per feature and the full text of every
``scene.*`` WARN, so a caller sees them without reading the block.

The persisted record keeps ``topology["scene"]`` (the normalized params, the
regeneration input next to ``topology["spec"]``) and ``topology["plan"]``
(``ks``, the per-feature :class:`~precis_se.atomic.generators.authored_foot.FootRow`
columns, the top joint angles, the relaxation pass count).
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from hexfold.build import build
from hexfold.report import Finding, HexfoldError, Report, Severity
from precis_se.atomic.generators._types import GeneratedBlock, GeneratorError
from precis_se.atomic.generators.authored_foot import (
    RELAXED_P95_A,
    TABLE_LID_N,
    TABLE_SPHERE_N,
    SceneFeature,
    ScenePlan,
    TopPlan,
    TopRow,
    plan_scene,
    table_lid_fillet,
    top_tabled,
)
from precis_se.atomic.generators.hexfold_spec import _block_from_net, _internal_message

_PARAM_KEYS = ("sheet", "features", "extra", "k_tether")
_FEATURE_KEYS = ("name", "at", "n", "radius", "tube_len", "top", "top_R", "top_fillet")
_FEATURE_REQUIRED = ("name", "at", "n", "radius", "tube_len")
# Scene-relax ceiling on every sphere top, tabled or not: the table makes
# planning free, not the tethered relax of the scene around a sphere (a
# scene op with default spheres at n=12 and n=24 relaxed for 476 s).
_MAX_SCENE_SPHERES = 1
_MAX_SCENE_SPHERE_N = 12
# Cost ceilings on the tops the per-n plan table does not hold (see
# _normalize): a live sphere plans 9 candidate builds, a live rounded lid 4
# (identical (n, top_fillet) lids are planned once and cached), and one scene
# op is budgeted 16 in total.  A tabled top costs nothing.  The budget is per
# scene op: a put with several ops sums their times.
_SPHERE_CANDIDATES = 9
_LID_CANDIDATES = 4
_CANDIDATE_BUDGET = 16


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _int_pair(value: object, what: str) -> tuple[int, int]:
    if (
        not isinstance(value, list | tuple)
        or len(value) != 2
        or not all(_is_int(v) for v in value)
    ):
        raise GeneratorError(f"{what} must be a pair of integers [a, b]; got {value!r}")
    return int(value[0]), int(value[1])


def _feature(raw: object, idx: int) -> SceneFeature:
    where = f"features[{idx}]"
    if not isinstance(raw, dict):
        raise GeneratorError(f"{where} must be an object; got {raw!r}")
    unknown = sorted(str(k) for k in raw if k not in _FEATURE_KEYS)
    if unknown:
        raise GeneratorError(
            f"{where}: unknown key(s) {unknown}; known: {list(_FEATURE_KEYS)}"
        )
    missing = [k for k in _FEATURE_REQUIRED if k not in raw]
    if missing:
        raise GeneratorError(f"{where}: missing key(s) {missing}")
    name = raw["name"]
    if not isinstance(name, str) or not name.strip():
        raise GeneratorError(f"{where}.name must be a non-empty string; got {name!r}")
    at = _int_pair(raw["at"], f"{where}.at")
    for key in ("n", "tube_len"):
        if not _is_int(raw[key]):
            raise GeneratorError(f"{where}.{key} must be an integer; got {raw[key]!r}")
    if not _is_number(raw["radius"]):
        raise GeneratorError(f"{where}.radius must be a number; got {raw['radius']!r}")
    top = raw.get("top", "open")
    if not isinstance(top, str):
        raise GeneratorError(f"{where}.top must be a string; got {top!r}")
    top_kw: dict[str, float] = {}
    for key in ("top_R", "top_fillet"):
        if key in raw:
            if not _is_number(raw[key]):
                raise GeneratorError(
                    f"{where}.{key} must be a number; got {raw[key]!r}"
                )
            top_kw[key] = float(raw[key])
    return SceneFeature(
        name=name,
        at=at,
        n=int(raw["n"]),
        radius=float(raw["radius"]),
        tube_len=int(raw["tube_len"]),
        top=top,
        **top_kw,
    )


def _normalize(
    params: dict[str, Any],
) -> tuple[tuple[int, int], tuple[SceneFeature, ...], str, float]:
    unknown = sorted(str(k) for k in params if k not in _PARAM_KEYS)
    if unknown:
        raise GeneratorError(
            f"hexfold_scene: unknown param(s) {unknown}; known: {list(_PARAM_KEYS)}"
        )
    if "sheet" not in params or "features" not in params:
        raise GeneratorError("hexfold_scene needs 'sheet' [w, h] and 'features' [...]")
    sheet = _int_pair(params["sheet"], "sheet")
    raw_features = params["features"]
    if not isinstance(raw_features, list | tuple) or not raw_features:
        raise GeneratorError("features must be a non-empty list of objects")
    features = tuple(_feature(raw, i) for i, raw in enumerate(raw_features))
    k_tether = params.get("k_tether", 1.0)
    if not _is_number(k_tether):
        raise GeneratorError(f"k_tether must be a number; got {k_tether!r}")
    k_tether = float(k_tether)
    live = [
        f
        for f in features
        if f.top in ("sphere", "lid")
        and (f.top == "sphere" or f.top_fillet is not None)
        and not top_tabled(f.n, f.top, f.top_fillet, k_tether)
    ]
    lid_keys = ", ".join(f"{table_lid_fillet(n)} at n={n}" for n in TABLE_LID_N)
    tabled = (
        "tabled tops (a sphere with the default fillet at n="
        f"{'/'.join(map(str, TABLE_SPHERE_N))}; a lid with top_fillet {lid_keys}) "
        "plan from the table and cost nothing"
    )
    all_spheres = [f for f in features if f.top == "sphere"]
    # an n that is not a multiple of 6 is left to the planner's own refusal,
    # which names the real reason
    big_relax = [f for f in all_spheres if f.n > _MAX_SCENE_SPHERE_N and f.n % 6 == 0]
    if len(all_spheres) > _MAX_SCENE_SPHERES or big_relax:
        raise GeneratorError(
            f"hexfold_scene relaxes at most {_MAX_SCENE_SPHERES} top: 'sphere' per "
            f"scene op, with n <= {_MAX_SCENE_SPHERE_N}; got "
            f"{[f'{f.name} (n={f.n})' for f in all_spheres]}. Tabled or not, the "
            "scene's tethered relax around a sphere top is the slow part: one at "
            "n=12 takes about a minute, and spheres at n=12 and n=24 in one scene "
            "op took 476 s, past a client timeout. Put one sphere scene op per "
            "put."
        )
    spheres = [f.name for f in live if f.top == "sphere"]
    lids = {(f.n, f.top_fillet) for f in live if f.top == "lid"}
    cost = _SPHERE_CANDIDATES * len(spheres) + _LID_CANDIDATES * len(lids)
    if cost > _CANDIDATE_BUDGET:
        raise GeneratorError(
            f"hexfold_scene budgets {_CANDIDATE_BUDGET} candidate builds per scene op; "
            f"this scene needs {cost} ({len(spheres)} sphere top x "
            f"{_SPHERE_CANDIDATES} + {len(lids)} distinct rounded lid(s) x "
            f"{_LID_CANDIDATES}; lids with the same n and top_fillet are planned "
            f"once). Split the scene across puts, one round-top scene op per put; "
            f"{tabled}."
        )
    extra = params.get("extra", "")
    if not isinstance(extra, str):
        raise GeneratorError(f"extra must be a string of .hx lines; got {extra!r}")
    return sheet, features, extra, k_tether


_R_MISMATCH_A = 0.5  # an authored top_R this far from the realised R is flagged


def _top_findings(name: str, tp: TopPlan, scene_row: TopRow) -> list[Finding]:
    """The WARNs of an authored top (a sphere or a rounded lid): the five
    bars as re-measured on the scene, an authored ``top_R`` the candidates
    could not meet, and the relaxed-shape band."""
    out: list[Finding] = []
    if scene_row.misses:
        out.append(
            Finding(
                "scene.top.bar",
                Severity.WARN,
                f"{name}: the {tp.kind} top misses {', '.join(scene_row.misses)} "
                f"(tethered deviation p95 {scene_row.dev_p95:.3f} A, theta_p max "
                f"{scene_row.theta_p_max:.1f} deg, top bonds <= "
                f"{scene_row.bond_max:.3f} A, {scene_row.pairs} pairs under 1.34 A)",
            )
        )
    if tp.authored_R is not None and abs(tp.R - tp.authored_R) > _R_MISMATCH_A:
        out.append(
            Finding(
                "scene.top.R_mismatch",
                Severity.WARN,
                f"{name}: top_R {tp.authored_R:g} A asked; the nearest build "
                f"(k={tp.k}, L={tp.length}) has an area-matched R of {tp.R:.2f} A",
            )
        )
    if not tp.relaxed_ok:
        out.append(
            Finding(
                "scene.top.relaxed_shape",
                Severity.WARN,
                f"{name}: a stick-model relax with the tether off leaves the "
                f"{tp.kind} top {tp.chosen.relaxed_p95:.2f} A (p95) from the "
                f"authored surface, band {RELAXED_P95_A:g} A (judged on the "
                "planner's bare trial tube, no sheet; scene.top.bar uses the "
                "scene re-measurement); tethered geometry "
                f"stored; relaxes ~{tp.chosen.relaxed_dz:.1f} A flatter at the "
                "pole in the stick model. A lower bound: the n=12 sphere probe "
                "under MACE-MP and xTB moved about 2 A at the pole",
            )
        )
    return out


def _top_record(tp: TopPlan, scene_row: TopRow) -> dict[str, Any]:
    """The stored plan of one authored top: the chosen build and its
    realised numbers, the same columns re-measured on the scene, and every
    candidate the planner measured."""
    return {
        "kind": tp.kind,
        "planned": tp.source,
        "k": tp.k,
        "L": tp.length,
        "dome_rows": tp.dome_rows,
        "drop": tp.drop,
        "R": tp.R,
        "fillet": tp.fillet,
        "authored_R": tp.authored_R,
        "authored_fillet": tp.authored_fillet,
        "dev_p95": scene_row.dev_p95,
        "theta_p_max": scene_row.theta_p_max,
        "bars_met": not scene_row.misses,
        "bars_missed": list(scene_row.misses),
        "relaxed_p95": tp.chosen.relaxed_p95,
        "relaxed_dz": tp.chosen.relaxed_dz,
        "relaxed_ok": tp.relaxed_ok,
        "trial": dataclasses.asdict(tp.chosen),
        "scene": dataclasses.asdict(scene_row),
        "grid": [dataclasses.asdict(r) for r in tp.rows],
    }


def _scene_findings(
    features: tuple[SceneFeature, ...], plan: ScenePlan
) -> list[Finding]:
    out: list[Finding] = []
    for f in features:
        row = plan.rows[f.name]
        if row.misses:
            out.append(
                Finding(
                    "scene.bar",
                    Severity.WARN,
                    f"{f.name}: misses {', '.join(row.misses)} (fillet mean/max "
                    f"{row.fillet_mean:.3f}/{row.fillet_max:.3f} A, bonds "
                    f"{row.bond_min:.3f}-{row.bond_max:.3f} A)",
                )
            )
        if f.top == "ball":
            out.append(
                Finding(
                    "scene.top.joint",
                    Severity.WARN,
                    f"{f.name}: the fused (6,0)->C60 neck is a stick geometry "
                    "only; MACE-MP small and GFN2-xTB both open 4 of its 6 seam "
                    "bonds to 4.6-4.9 A (gr464391)",
                )
            )
        if f.name in plan.top_plans:
            out.extend(
                _top_findings(f.name, plan.top_plans[f.name], plan.top_rows[f.name])
            )
    return out


def build_hexfold_scene(params: dict[str, Any]) -> GeneratedBlock:
    """``{"sheet": [w, h], "features": [{"name", "at", "n", "radius",
    "tube_len", "top"?, "top_R"?, "top_fillet"?}], "extra"?: str,
    "k_tether"?: float}`` — plan the
    scene, relax it under the tether and mint it (module docstring)."""
    sheet, features, extra, k_tether = _normalize(params)
    try:
        plan = plan_scene(sheet, features, extra=extra, k_tether=k_tether)
        net = build(plan.text, strict=False)
    except ValueError as exc:
        raise GeneratorError(str(exc)) from exc
    except HexfoldError as exc:
        raise GeneratorError(str(exc)) from exc
    except (KeyError, IndexError) as exc:
        raise GeneratorError(_internal_message(exc)) from exc

    scene = _scene_findings(features, plan)
    report = net.report.merge(Report(tuple(plan.findings) + tuple(scene)))
    normalized: dict[str, Any] = {
        "sheet": list(sheet),
        "features": [
            {
                "name": f.name,
                "at": list(f.at),
                "n": f.n,
                "radius": f.radius,
                "tube_len": f.tube_len,
                "top": f.top,
                # present only when authored, so a scene without them
                # stores exactly what it stored before the keywords existed
                **({"top_R": f.top_R} if f.top_R is not None else {}),
                **({"top_fillet": f.top_fillet} if f.top_fillet is not None else {}),
            }
            for f in features
        ],
        "extra": extra,
        "k_tether": k_tether,
    }
    record: dict[str, Any] = {
        "ks": plan.ks,
        "rows": {n: dataclasses.asdict(r) for n, r in plan.rows.items()},
        "tops": {n: list(t) for n, t in plan.tops.items()},
        "passes": plan.passes,
    }
    if plan.top_plans:  # only scenes with an authored top carry the key
        record["top_plans"] = {
            n: _top_record(tp, plan.top_rows[n]) for n, tp in plan.top_plans.items()
        }
    plan_record = json.loads(json.dumps(record))
    tail = f"; relax=tethered ks={plan.ks}" + "".join(
        f"; {f.code}: {f.message}" for f in scene
    )
    return _block_from_net(
        net,
        plan.positions,
        spec=plan.text,
        report=report,
        fidelity="stick",
        extra_topology={"scene": normalized, "plan": plan_record},
        provenance_tail=tail,
    )


__all__ = ["build_hexfold_scene"]
