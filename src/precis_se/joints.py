"""The se joint vocabulary — kinematic class × mechanism (se-kind.md L2).

A joint is two orthogonal axes of meaning, never conflated (the design
session's correction of the original flat rigid/sliding/rotating list):

- the **kinematic class** says what motion the connection *permits* —
  :data:`KINEMATIC_CLASSES`, with an optional axis (unit 3-vector, world
  frame v1) where the class has one;
- the **mechanism** says how the relation is *physically realized* — a
  separate, optional, suggestive field over :data:`MECHANISMS`, each entry
  carrying its own *implied demands* checked by graph DRC
  (:mod:`precis_se.drc`), never here.

This module owns the stored shape of ``connect.joint`` —
``{"class": ..., "axis"?: ..., "mechanism"?: ..., "params"?: {...}}`` —
and validates it once for both writers (:mod:`precis_se.ops`'s ``connect``/
``set_joint``) and the read-time re-check (:mod:`precis_se.drc` over
whatever is actually stored, including pre-slice-3 free-form joints, which
must surface as findings, never crashes). Unknown top-level keys are
rejected loudly at write time (the ``**_kw`` swallowed-facet lesson);
``params`` is the deliberate open slot for mechanism-specific numbers
(engagement depth, stiffness for ``compliant`` — advisory/descriptive tier
until a real consumer exists, per the annotations contract-class rule).
"""

from __future__ import annotations

import math
from typing import Any

#: What motion the connection permits. ``compliant`` is a DOF with
#: stiffness rather than freedom (TPU living hinge, flexure); ``captive``
#: is interlocked-without-mechanism (a rotaxane) — checked by clearance +
#: connectivity, not by an axis. ``screw`` (the classical helical lower
#: pair — a leadscrew nut) is NOT the ``screw`` *mechanism* below: the
#: class couples rotation to translation by the thread's lead
#: (``params.lead``, m per revolution — descriptive until a kinematic
#: consumer lands); the mechanism means threaded *fastening*.
KINEMATIC_CLASSES: dict[str, str] = {
    "rigid": "no relative motion",
    "revolute": "rotation about the axis only",
    "prismatic": "translation along the axis only",
    "cylindrical": "rotation about + translation along the axis (uncoupled)",
    "screw": "rotation coupled to translation along the axis (lead in params)",
    "planar": "translation in the plane normal to the axis",
    "ball": "rotation about all axes through the joint point",
    "compliant": "motion with stiffness rather than freedom (flexure)",
    "captive": "interlocked, no mechanism (checked by clearance/connectivity)",
}

#: Classes whose meaning includes an axis — for these, a declared ``axis``
#: enables the derived-DOF probe; the others ignore/reject one.
AXIS_CLASSES = frozenset({"revolute", "prismatic", "cylindrical", "screw", "planar"})

#: How the relation is physically realized, each with its implied demand.
#: ``demands_relation`` is the demand graph-DRC can check *today*: a live
#: tolerance relation between measures of the two endpoint blocks (a press
#: fit without an interference relation is folklore, not engineering).
#: ``demands_bom`` is the second checkable demand, live since se_bom
#: (migration 0003): a mechanism realized by a *bought* thing needs a BOM
#: line saying which one — a bearing joint with nothing to buy is a
#: drawing, not a machine. Deferred demands are named in ``deferred`` so
#: they aren't re-derived — reported nowhere until their substrate
#: (flexing-member detection) exists; flagging what a designer cannot yet
#: satisfy is noise.
MECHANISMS: dict[str, dict[str, Any]] = {
    "snap": {
        "demands_relation": "an engagement-depth tolerance relation",
        "deferred": "a flexing member (needs L3 solids)",
    },
    # threaded FASTENING — not the 'screw' kinematic class (helical pair).
    # The fastener is bought; the holes it stamps into the members are
    # rung 3 (se-off-the-shelf-fabrication.md engine 2).
    "screw": {"demands_relation": None, "demands_bom": "the fastener"},
    "press": {
        "demands_relation": "an interference tolerance relation",
    },
    "key": {"demands_relation": None},
    "magnet": {"demands_relation": None, "demands_bom": "the magnet(s)"},
    "bearing": {
        "demands_relation": "bore/OD tolerance relations",
        "demands_bom": "the bearing",
    },
    "bond": {"demands_relation": None},  # atomic scale: one covalent bond
    "integral": {"demands_relation": None},  # print-in-place, same body
}

_JOINT_KEYS = frozenset({"class", "axis", "mechanism", "params"})


class JointError(ValueError):
    """A malformed joint payload (bad class/mechanism/axis/keys)."""


def validate_joint(raw: dict[str, Any]) -> dict[str, Any]:
    """Vet a joint dict into its stored shape, raising :class:`JointError`
    with the legal vocabulary on any miss. Axis normalizes to unit length
    (zero rejected); ``axis`` on a non-axis class is rejected — an axis on
    ``rigid`` or ``ball`` would be a silently meaningless facet."""
    unknown = set(raw) - _JOINT_KEYS
    if unknown:
        raise JointError(
            f"unknown joint key(s): {', '.join(sorted(unknown))} — a joint "
            "is {'class', 'axis'?, 'mechanism'?, 'params'?}. NOTE: "
            "couplings between separately-mounted parts (gear/rack/belt "
            "ratios) are not yet representable — a ratio stored under "
            "'params' is kept but consumed by nothing; note the intent in "
            "'desc'/'reason' prose instead of relying on it"
        )
    klass = str(raw.get("class") or "").strip()
    if klass not in KINEMATIC_CLASSES:
        known = " | ".join(sorted(KINEMATIC_CLASSES))
        raise JointError(f"joint 'class' must be one of {known}; got {klass!r}")
    out: dict[str, Any] = {"class": klass}
    axis_raw = raw.get("axis")
    if axis_raw is not None:
        if klass not in AXIS_CLASSES:
            raise JointError(
                f"joint class {klass!r} takes no 'axis' — only "
                f"{' | '.join(sorted(AXIS_CLASSES))} have one"
            )
        try:
            axis = [float(x) for x in axis_raw]
        except (TypeError, ValueError) as exc:
            raise JointError(
                f"joint 'axis' must be a 3-vector [x, y, z], got {axis_raw!r}"
            ) from exc
        if len(axis) != 3:
            raise JointError(
                f"joint 'axis' must be a 3-vector [x, y, z], got {axis_raw!r}"
            )
        norm = math.sqrt(sum(x * x for x in axis))
        if norm == 0.0:
            raise JointError("joint 'axis' must be a nonzero vector")
        out["axis"] = [x / norm for x in axis]
    mech_raw = raw.get("mechanism")
    if mech_raw is not None:
        mech = str(mech_raw).strip()
        if mech not in MECHANISMS:
            known = " | ".join(sorted(MECHANISMS))
            raise JointError(f"joint 'mechanism' must be one of {known}; got {mech!r}")
        out["mechanism"] = mech
    params_raw = raw.get("params")
    if params_raw is not None:
        if not isinstance(params_raw, dict):
            raise JointError(
                f"joint 'params' must be a JSON object, got {params_raw!r}"
            )
        if params_raw:
            out["params"] = dict(params_raw)
    return out


#: Registered objective (loads) keys — the kind-neutral vocabulary, real
#: units (se-kind.md L2 "Loads"; the pcb objectives posture: intent as
#: physics). Value shape is checked per key by :func:`validate_objectives`.
OBJECTIVE_KEYS: dict[str, str] = {
    "force": "force vector [x, y, z], newtons",
    "torque": "torque vector [x, y, z], newton-metres",
    "duty": "prose duty description ('pushed around a workshop daily')",
    "cycles": "expected load cycles (number ≥ 0)",
}


def validate_objectives(raw: dict[str, Any]) -> dict[str, Any]:
    """Vet a loads/objectives dict into its stored shape. Unknown keys are
    rejected loudly (write time); stored strays surface via DRC instead."""
    unknown = set(raw) - set(OBJECTIVE_KEYS)
    if unknown:
        known = ", ".join(f"{k} ({v})" for k, v in sorted(OBJECTIVE_KEYS.items()))
        raise JointError(
            f"unknown objective key(s): {', '.join(sorted(unknown))} — "
            f"registered keys: {known}"
        )
    out: dict[str, Any] = {}
    for key in ("force", "torque"):
        if raw.get(key) is not None:
            try:
                vec = [float(x) for x in raw[key]]
            except (TypeError, ValueError) as exc:
                raise JointError(
                    f"objective {key!r} must be a 3-vector, got {raw[key]!r}"
                ) from exc
            if len(vec) != 3:
                raise JointError(
                    f"objective {key!r} must be a 3-vector, got {raw[key]!r}"
                )
            out[key] = vec
    if raw.get("duty") is not None:
        duty = str(raw["duty"]).strip()
        if duty:
            out["duty"] = duty
    if raw.get("cycles") is not None:
        try:
            cycles = float(raw["cycles"])
        except (TypeError, ValueError) as exc:
            raise JointError(
                f"objective 'cycles' must be a number ≥ 0, got {raw['cycles']!r}"
            ) from exc
        if not math.isfinite(cycles) or cycles < 0:
            raise JointError(
                f"objective 'cycles' must be a number ≥ 0, got {raw['cycles']!r}"
            )
        out["cycles"] = cycles
    return out
