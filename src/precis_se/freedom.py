"""The design-freedom report backing ``view='freedom'`` — DRC's honest
counterpart (se-kind.md slice 4, set-based design research
``perplexity-reasoning:310975``): DRC says what is *wrong*, freedom says
what is still *undecided, and by whom*. Pure over a loaded
:class:`~precis_se.ops.SeTree`; no store access.

Sections, each an honest inventory rather than a judgment:

- **kinematic freedom** — every moving-class joint (the declared motion),
  and every block no connect touches at all (free in all 6 DOF; a block
  serving only as a template is skipped — an uninstanced definition is
  not a floating part).
- **undecided measures** — pure handles (named, nothing declared),
  interval measures whose point is unchosen (the band IS the declared
  set), and relation-carrying measures whose chain derives nothing.
- **envelope-less blocks** — no authored envelope and no catalog-derived
  one.
- **unevaluated intent** — ``soft`` measures and recorded loads have no
  engaged evaluator until compliance advisories ship (ship-order step 6);
  saying so beats silence (the search-facet lesson).

"By whom": every row carries its ``origin`` where the vocabulary exists —
``proposed`` rows are a propose job's to revise, ``user`` rows are
contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis_se import joints as se_joints
from precis_se.drc import _MOVING_CLASSES
from precis_se.measures import MeasureSpec, declared_band, stackup
from precis_se.ops import SeTree, effective_envelope


@dataclass
class Motion:
    """One moving-class joint — a degree of freedom the design *keeps*."""

    subject: str  # 'a.port—b.port'
    klass: str
    axis: list[float] | None


@dataclass
class UndecidedMeasure:
    """One measure that still names a choice, not a number: ``state`` is
    ``'handle'`` (nothing declared), ``'band'`` (interval declared, point
    unchosen), or ``'underived'`` (relation declared, chain derives
    nothing — see its stack-up row for why)."""

    measure: str  # 'block.name'
    state: str
    unit: str
    origin: str
    band: tuple[float, float] | None = None


@dataclass
class FreedomReport:
    motions: list[Motion] = field(default_factory=list)
    unconnected: list[str] = field(default_factory=list)
    undecided: list[UndecidedMeasure] = field(default_factory=list)
    unenveloped: list[str] = field(default_factory=list)
    #: 'block.name' of every ``soft`` measure (no engaged consumer yet).
    soft_measures: list[str] = field(default_factory=list)
    #: count of blocks/connects carrying loads (no evaluator yet).
    loads_recorded: int = 0
    #: measures by origin: {'user': n, 'proposed': n}.
    measure_origins: dict[str, int] = field(default_factory=dict)
    #: '<block>.<facet>' for every facet stamped ``proposed``.
    proposed_facets: list[str] = field(default_factory=list)


def _decided(m: MeasureSpec, derived: dict[str, bool]) -> UndecidedMeasure | None:
    """The measure's undecided row, or ``None`` when a point exists —
    declared, or derived through its relation (``derived`` is keyed by
    'block.name', True when stack-up produced a point or a band)."""
    key = f"{m.block}.{m.name}"
    if m.value is not None:
        return None
    if m.relation is not None:
        if derived.get(key, False):
            return None
        return UndecidedMeasure(
            measure=key, state="underived", unit=m.unit, origin=m.origin
        )
    band = declared_band(m)
    if band is not None:
        return UndecidedMeasure(
            measure=key, state="band", unit=m.unit, origin=m.origin, band=band
        )
    return UndecidedMeasure(measure=key, state="handle", unit=m.unit, origin=m.origin)


def freedom(tree: SeTree) -> FreedomReport:
    """Assemble the report (module docstring). Malformed stored joints are
    skipped here — they are DRC's ``malformed_joint`` finding, and freedom
    never restates a defect as a liberty."""
    report = FreedomReport()

    connected: set[str] = set()
    for c in tree.connects:
        connected.add(c.a_block)
        connected.add(c.b_block)
        subject = f"{c.a_block}.{c.a_port}—{c.b_block}.{c.b_port}"
        if c.joint is None:
            continue
        try:
            joint = se_joints.validate_joint(c.joint)
        except se_joints.JointError:
            continue
        if joint["class"] in _MOVING_CLASSES:
            axis = joint.get("axis")
            report.motions.append(
                Motion(subject=subject, klass=joint["class"], axis=axis)
            )

    templates_in_use = {
        node.template
        for node in tree.blocks.values()
        if node.template is not None and "#" not in node.template
    }
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        if name not in connected and name not in templates_in_use:
            report.unconnected.append(name)
        if node.template is None and name not in templates_in_use:
            env = effective_envelope(tree, node) or _derived_envelope(node)
            if not env:
                report.unenveloped.append(name)

    derived_ok: dict[str, bool] = {}
    for r in stackup(tree.measures):
        derived_ok[r.measure] = r.derived is not None or r.derived_min is not None
    for m in tree.measures:
        report.measure_origins[m.origin] = report.measure_origins.get(m.origin, 0) + 1
        if m.strength == "soft":
            report.soft_measures.append(f"{m.block}.{m.name}")
        row = _decided(m, derived_ok)
        if row is not None:
            report.undecided.append(row)

    report.loads_recorded = sum(
        1 for node in tree.blocks.values() if node.objectives
    ) + sum(1 for c in tree.connects if c.objectives)

    for name in sorted(tree.blocks):
        for facet, origin in sorted((tree.blocks[name].origins or {}).items()):
            if origin == "proposed":
                report.proposed_facets.append(f"{name}.{facet}")
    return report


def _derived_envelope(node: Any) -> str | None:
    derived = getattr(node, "derived", None)
    if derived is None:
        return None
    return getattr(derived, "envelope", None)
