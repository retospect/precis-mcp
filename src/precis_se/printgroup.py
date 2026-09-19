"""Print groups — one build frame and one 3MF for an fdm ancestor block
(docs/backlog/structural-solution-space.md §Slice 4 bridge, the print
``intent`` table; round B1 = ``model``).

A **print group** is an ancestor block in an fdm mode that carries a print
intent (``set_mode(block=, mode='fdm/<material>', intent='model')`` —
stored on the block's ``build_frame`` record, :func:`precis_se.ops.
print_intent`); its **members** are the blocks below it in the tree,
membership derived from ``parent`` edges at read time — no schema, no
membership table. **A print group ends where the next group root
begins**: a descendant that is itself a group root (fdm mode + intent —
the unicycle's fork group and wheel group under one assembly root) owns
its own subtree; the outer group neither places nor exports it and lists
it as one ``nested group … printed separately`` line, and every block
maps to its NEAREST group-root ancestor (:func:`grouped_blocks`). A group
root with no intent is not a group: every member reads as its own
``view='print'`` section exactly as before.

**``intent='model'`` — a fit-test model, any scale.** Every printed member
prints. Every *purchase* member prints as a **stand-in**: the analytic
catalog solid (:mod:`precis.cad.catalog`, via :func:`precis.cad.scene.
part_spec` — the same ``part <family>:<code>`` source a cad design would
place, threads dropped, drive recess absent) when the bound ``component``
was minted from a series the catalog reads (its ``meta.series``/
``meta.size``), else a solid from its spec dims (a bearing = outer
cylinder minus bore; anything else the catalog-derived envelope,
:mod:`precis_se.catalog`). Joints with DOF and rigid joints alike stay
separate parts — fasteners print too, because the fit is the point. The
mating hole in a printed member keeps its printed-hole compensation, since
it is stamped by the ordinary :mod:`precis_se.printsolid` path. A purchase
member that yields no solid is a ``no_stand_in`` finding naming what it
needs (a ``component`` binding, or the spec dims the generator asked for)
— never a silent skip. A stand-in never re-enters the kernel as a mesh: it
is cad primitives, so export goes through the ordinary
:func:`precis.cad.export._component_meshes` fold.

**One build frame for the group**, scored on the union of the member
meshes in their world poses (:func:`precis.cad.printability.orient` with
the root's rules/policy — the root names the process). Precedence: the
root's own ``set_build_frame`` pin; else a SIMP-realized member
(``build_frame.origin == 'simp'``) pins the group to its baked
``build_dir`` and the search is **skipped and said so** — two SIMP
members disagreeing is a ``simp_frame_conflict`` finding (the first by
name wins, the finding names both), never silence; else the search. Every
member's frame-dependent findings (overhang, bridge, bed contact,
``layer_vs_load``) are then judged at THAT frame, each member on its own
footprint the way a slicer's drop-to-bed would place it, rather than at
the member's own best orientation.

**Output**: one 3MF per group, one ``<object>`` per member (stand-ins are
objects too), every member in its world pose within the group frame —
one rotation, one shared bed offset (:func:`precis.cad.printability.
rotate_all_to_frame`). STL is refused for a group: it has no objects.

``intent='manufacture'`` (cavities, in-place gaps + ``min_clearance``,
fusion + ``blend`` at the seam, fastener elision) is round B2 — the enum
value exists so the arg shape is stable, and ``set_mode`` refuses it as
not built yet. Loads are never scaled here; nothing in this module
touches objectives.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.cad import printability as cad_printability
from precis.cad.catalog import family_for_series
from precis.cad.export import (
    _MM_PER_M,
    ExportError,
    _component_meshes,
    _scaled_for_export,
    _solid_mesh,
    _write_3mf,
    manifold_available,
)
from precis.cad.printability import BuildCandidate, rotate_all_to_frame
from precis.cad.scene import NodeSpec, SceneError, SceneSpec, part_spec
from precis.cad.vec import Transform, Vec3, as_vec3
from precis.cad.vec import pose as cad_pose
from precis.utils.units import format_dsl_number
from precis_se import capabilities as se_caps
from precis_se import catalog as se_catalog
from precis_se import modes as se_modes
from precis_se.ops import SeBlock, SeTree, pinned_down, print_intent
from precis_se.printing import (
    SIMP_FRAME_ORIGIN,
    PrintUnsupported,
    _rules_for,
    _simp_field_overhangs,
    format_down,
    frame_findings,
    frame_free_findings,
)
from precis_se.printsolid import PrintedSolid, printed_solid
from precis_se.validate import ValidationIssue

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

#: Member roles a :class:`GroupMember` can carry.
PRINTED = "printed"
STAND_IN = "stand-in"
UNREALIZED = "unrealized"
SKIPPED = "skipped"
NESTED = "nested"


@dataclass
class StandIn:
    """A purchase member's printed stand-in: an analytic cad spec in the
    member's own block frame (metres) plus one line saying where it came
    from — ``catalog part 'csk:m4x12'`` or ``spec dims (bearing)``."""

    spec: SceneSpec
    source: str


@dataclass
class GroupMember:
    """One block of a print group. ``spec`` (block frame, metres) is what
    exports — a printed member's cut solid or a stand-in — ``None`` for a
    member with nothing to place (``unrealized``/``skipped``); ``xform``
    is the block's world pose. ``findings`` are this member's own; the
    group report concatenates them."""

    block: str
    role: str
    xform: Transform
    spec: SceneSpec | None = None
    printed: PrintedSolid | None = None
    stand_in: StandIn | None = None
    note: str = ""
    findings: list[ValidationIssue] = field(default_factory=list)


@dataclass
class GroupPrintReport:
    """Everything ``view='print'``/``view='fab'`` render for one group —
    the group-level twin of :class:`precis_se.printing.BlockPrintReport`."""

    root: str
    intent: str
    mode: str
    members: list[GroupMember]
    candidates: list[BuildCandidate]
    chosen_down: Vec3 | None
    chosen_score: cad_printability.Score | None
    pinned: bool
    best_other: str | None
    search_skipped: str | None
    pitch: float | None
    findings: list[ValidationIssue] = field(default_factory=list)

    @property
    def exportable(self) -> list[GroupMember]:
        return [m for m in self.members if m.spec is not None]

    def count(self, role: str) -> int:
        return sum(1 for m in self.members if m.role == role)

    @property
    def member_count(self) -> int:
        """Members of THIS group — a nested group's boundary line is not one."""
        return sum(1 for m in self.members if m.role != NESTED)


# ── membership ──────────────────────────────────────────────────────────


def is_group_root(tree: SeTree, name: str) -> bool:
    """A non-instance block in an fdm-family mode carrying a print intent
    (module docstring) — the one membership predicate."""
    node = tree.blocks.get(name)
    if node is None or node.template is not None:
        return False
    fam = se_modes.family_of(node.mode)
    return fam is not None and fam.key == "fdm" and print_intent(node) is not None


def group_roots(tree: SeTree) -> list[str]:
    """Every group root, sorted by name."""
    return sorted(name for name in tree.blocks if is_group_root(tree, name))


def _walk(tree: SeTree, root: str) -> tuple[list[str], list[str]]:
    """``(members, nested_roots)`` below ``root`` by ``parent`` edges: the
    walk descends until it meets a block that is itself a group root —
    that block and everything under it belong to the inner group (module
    docstring's nesting rule), so it is recorded as nested and NOT
    descended into. Both lists sorted by name; ``root`` itself excluded."""
    members: list[str] = []
    nested: list[str] = []
    frontier = [root]
    while frontier:
        here = frontier.pop()
        for name, node in tree.blocks.items():
            if node.parent != here:
                continue
            if is_group_root(tree, name):
                nested.append(name)
            else:
                members.append(name)
                frontier.append(name)
    return sorted(members), sorted(nested)


def descendants(tree: SeTree, root: str) -> list[str]:
    """The blocks that are ``root``'s OWN group members — every block below
    it by ``parent`` edges, transitively, stopping where the next group
    root begins (:func:`nested_roots` names those); instances and arrays
    included (the report says what it does with them), ``root`` itself
    excluded. Sorted by name."""
    return _walk(tree, root)[0]


def nested_roots(tree: SeTree, root: str) -> list[str]:
    """The group roots directly inside ``root``'s group — the boundary
    blocks :func:`descendants` stopped at — each printed by its own group,
    listed by the outer report so the hand-off is never silent."""
    return _walk(tree, root)[1]


def grouped_blocks(tree: SeTree) -> dict[str, str]:
    """``{block: nearest group-root ancestor}`` for every block that
    belongs to a group — a root maps to itself; a nested root's subtree
    maps to the nested root, never the outer one — so the per-block
    renders (``view='print'`` no-args, ``view='fab'``) can leave each
    group's members to that group's one row."""
    roots = set(group_roots(tree))
    out: dict[str, str] = {}
    for name, node in tree.blocks.items():
        here: str | None = name
        seen: set[str] = set()
        while here is not None and here not in seen:
            if here in roots:
                out[name] = here
                break
            seen.add(here)
            parent = tree.blocks.get(here)
            here = parent.parent if parent is not None else None
    return out


# ── stand-ins ───────────────────────────────────────────────────────────


def _no_stand_in(block: str, needs: str) -> ValidationIssue:
    return ValidationIssue(
        rule="no_stand_in",
        subject=block,
        detail=(
            f"purchase member {block!r} has no solid to print a stand-in from — {needs}"
        ),
        severity="warn",
        suggested_fix=(
            f"set_binding(block={block!r}, kind='component', design=<slug>) to "
            "a component minted from a series (put(kind='component', "
            "series=..., size=...)) or one carrying its geometry specs"
        ),
    )


def _shifted(spec: SceneSpec, dz: float) -> SceneSpec:
    """``spec`` with every node moved ``dz`` along the block's own ``+z``."""
    if dz == 0.0:
        return spec
    return SceneSpec(
        nodes=[replace(n, loc=(n.loc[0], n.loc[1], n.loc[2] + dz)) for n in spec.nodes],
        components=list(spec.components),
        meta=dict(spec.meta),
        field_loader=spec.field_loader,
    )


def _catalog_stand_in(node: SeBlock, meta: dict[str, Any]) -> StandIn | None:
    """The cad catalog's analytic solid for a series-minted component, in
    the se block frame. The catalog draws a screw with its under-head plane
    at ``z=0`` and the head at ``-z``; the se catalog envelope
    (:mod:`precis_se.catalog`) runs head-top-at-origin to thread-end at
    ``+z``, so a proud head is shifted by its own height to land where the
    block's envelope (and the fastening pass's stack walk) already put
    it. A countersunk head, a nut, a washer, an insert and a set screw
    start at the same plane in both conventions."""
    series = meta.get("series")
    size = meta.get("size")
    if not series or not size:
        return None
    family = family_for_series(str(series))
    if family is None:
        return None
    code = f"{family}:{str(size).strip().lower()}"
    try:
        spec = part_spec(code)
    except SceneError:
        return None
    specs = getattr(node.derived, "specs", None) or {}
    dz = 0.0
    if se_catalog.fastener_form(specs) == "screw" and (
        str(specs.get("head_form") or "") != "countersunk"
    ):
        dz = se_catalog.head_height(specs) or 0.0
    return StandIn(
        spec=_shifted(spec, dz),
        source=f"catalog part {code!r} (threads dropped, no drive recess)",
    )


def _dims_stand_in(node: SeBlock, category: str | None) -> StandIn | None:
    """A stand-in from the component's own spec dims when no catalog
    family reads its series: a bearing is its outer cylinder minus the
    bore; anything else the catalog-derived L1 envelope — a bounding
    volume, which the note says."""
    derived = node.derived
    if derived is None or not derived.ok or not derived.envelope:
        return None
    specs = derived.specs or {}
    nodes = [
        NodeSpec(name="body", op="add", config=str(derived.envelope), component="body")
    ]
    if category == "bearing":
        bore = specs.get("bore_diameter_bearing") or specs.get("inner_diameter")
        width = specs.get("width")
        if bore and width:
            r = format_dsl_number(float(bore) / 2.0)
            h = format_dsl_number(float(width) * 1.2)
            nodes.append(
                NodeSpec(
                    name="bore",
                    op="cut",
                    config=f"cyl:r{r}h{h}",
                    component="body",
                    loc=(0.0, 0.0, -float(width) * 0.1),
                )
            )
            what = "spec dims (bearing: outer cylinder minus bore)"
        else:
            what = "spec dims (bearing outer cylinder, bore unknown)"
    else:
        what = f"spec dims ({category or 'uncategorised'}: the catalog envelope)"
    return StandIn(spec=SceneSpec(nodes=nodes, components=["body"]), source=what)


def stand_in_for(
    node: SeBlock, *, cad_store_reader: Store
) -> tuple[StandIn | None, ValidationIssue | None]:
    """A purchase member's stand-in, or the ``no_stand_in`` finding saying
    what it needs (module docstring's order: catalog part → spec dims →
    finding). Exactly one of the pair is set."""
    block = node.name
    if node.bound_kind == "part":
        return None, _no_stand_in(
            block,
            f"a part binding ({node.bound!r}) carries no dimensions; a stand-in "
            "needs a component binding with spec dims",
        )
    if node.bound_kind != "component" or not node.bound:
        return None, _no_stand_in(
            block, "it is not bound to a component (nothing says what it is)"
        )
    meta: dict[str, Any] = {}
    category: str | None = None
    get_ref = getattr(cad_store_reader, "get_ref", None)
    if get_ref is not None:
        ref = get_ref(kind="component", id=node.bound)
        if ref is not None:
            meta = dict(ref.meta or {})
            category = meta.get("category")
    cat = _catalog_stand_in(node, meta)
    if cat is not None:
        return cat, None
    dims = _dims_stand_in(node, category)
    if dims is not None:
        return dims, None
    why = getattr(node.derived, "why_not", None) or (
        f"component {node.bound!r} has no geometry specs"
    )
    return None, _no_stand_in(
        block,
        f"{why}; no catalog family reads its series "
        f"({meta.get('series') or 'none recorded'})",
    )


# ── the group report ────────────────────────────────────────────────────


def _world(node: SeBlock) -> Transform:
    return cad_pose(as_vec3(node.pose), as_vec3(node.rot))


def _world_mesh(
    member: GroupMember, pitch: float | None
) -> tuple[np.ndarray, np.ndarray]:
    assert member.spec is not None
    verts, tris = _solid_mesh(member.spec, pitch=pitch)
    return verts @ member.xform.R.T + member.xform.t, tris


def _member(tree: SeTree, name: str, *, cad_store_reader: Store) -> GroupMember:
    """Classify one block below the root (or the root itself) and collect
    what it contributes — module docstring's roles."""
    node = tree.blocks[name]
    xform = _world(node)
    if node.template is not None:
        return GroupMember(
            block=name,
            role=SKIPPED,
            xform=xform,
            note=f"instance of {node.template!r}",
            findings=[
                ValidationIssue(
                    rule="member_skipped",
                    subject=name,
                    detail=(
                        f"{name!r} is an instance/array of {node.template!r}; a "
                        "group places ordinary blocks only today — the template "
                        "prints once through its own view='print'"
                    ),
                    severity="info",
                )
            ],
        )
    family = se_modes.family_of(node.mode)
    if family is not None and family.key == "fdm":
        printed = printed_solid(tree, name, cad_store_reader=cad_store_reader)
        if printed is None or printed.volume_after_m3 <= 0.0:
            findings = list(printed.findings) if printed is not None else []
            findings.append(
                ValidationIssue(
                    rule="unrealized",
                    subject=name,
                    detail=(
                        f"{name!r} declares mode {node.mode!r} but has "
                        + (
                            "no bound cad design yet"
                            if printed is None
                            else "a net-empty solid"
                        )
                        + " — nothing to place in the group"
                    ),
                    severity="info",
                    suggested_fix=f"realize(block={name!r}, mode={node.mode!r})",
                )
            )
            return GroupMember(
                block=name,
                role=UNREALIZED,
                xform=xform,
                printed=printed,
                note="unrealized",
                findings=findings,
            )
        return GroupMember(
            block=name,
            role=PRINTED,
            xform=xform,
            spec=printed.spec,
            printed=printed,
            note=(
                f"printed, {printed.volume_after_m3 * 1e9:.4g} mm³, "
                f"{len(printed.features)} stamped feature(s)"
            ),
            findings=list(printed.findings),
        )
    if (family is not None and family.key == "purchase") or node.bound_kind in (
        "component",
        "part",
    ):
        stand_in, finding = stand_in_for(node, cad_store_reader=cad_store_reader)
        if stand_in is None:
            assert finding is not None
            return GroupMember(
                block=name,
                role=SKIPPED,
                xform=xform,
                note="no stand-in",
                findings=[finding],
            )
        return GroupMember(
            block=name,
            role=STAND_IN,
            xform=xform,
            spec=stand_in.spec,
            stand_in=stand_in,
            note=f"stand-in — {stand_in.source}",
            findings=[
                ValidationIssue(
                    rule="stand_in",
                    subject=name,
                    detail=(
                        f"{name!r} prints as a stand-in ({stand_in.source}) — "
                        "a fit-test proxy for the bought part, not the part"
                    ),
                    severity="info",
                )
            ],
        )
    why = (
        "has no manufacturing mode"
        if node.mode is None
        else f"is a {family.key!r}-family block"
        if family is not None
        else f"has an unknown mode {node.mode!r}"
    )
    return GroupMember(
        block=name,
        role=SKIPPED,
        xform=xform,
        note=f"skipped ({why})",
        findings=[
            ValidationIssue(
                rule="member_skipped",
                subject=name,
                detail=f"{name!r} {why}; an fdm group prints fdm and purchase members",
                severity="info",
                suggested_fix=(
                    f"set_mode(block={name!r}, mode='fdm/<material>') or 'purchase'"
                ),
            )
        ],
    )


def _simp_pins(tree: SeTree, members: list[GroupMember]) -> list[tuple[str, Vec3, str]]:
    """``(member, world_down, build_dir)`` for every SIMP-realized member —
    its baked local ``down`` carried into the world frame by its pose."""
    out: list[tuple[str, Vec3, str]] = []
    for m in members:
        node = tree.blocks[m.block]
        frame = node.build_frame or {}
        if frame.get("origin") != SIMP_FRAME_ORIGIN or not frame.get("down"):
            continue
        world_down = m.xform.apply_dir(as_vec3(frame["down"]))
        out.append((m.block, world_down, str(frame.get("build_dir") or "?")))
    return out


def report_for(
    tree: SeTree, root: str, *, cad_store_reader: Store
) -> GroupPrintReport | None:
    """The group report for ``root``, or ``None`` when ``root`` is not a
    group root (:func:`is_group_root`). Raises :class:`PrintUnsupported`
    when a solid needs tessellating and ``manifold3d`` is missing."""
    if not is_group_root(tree, root):
        return None
    root_node = tree.blocks[root]
    intent = print_intent(root_node) or ""
    mode = root_node.mode or ""
    names, nested = _walk(tree, root)
    if root_node.bound_kind == "cad" and root_node.bound:
        names = [root, *names]  # a root with its own solid is a member too
    members = [_member(tree, n, cad_store_reader=cad_store_reader) for n in names]
    # A nested group root and its subtree print as their own group: one
    # line here, never a member, never in this group's 3MF.
    members.extend(
        GroupMember(
            block=n,
            role=NESTED,
            xform=_world(tree.blocks[n]),
            note=f"nested group {n!r} — printed separately, see its own row",
        )
        for n in nested
    )
    findings: list[ValidationIssue] = []
    for m in members:
        findings.extend(m.findings)

    rules = _rules_for(tree, root_node)
    policy = se_caps.orientation_policy(mode) or {}
    pitch = rules.get("layer_height")
    placed = [m for m in members if m.spec is not None]
    if not placed:
        findings.append(
            ValidationIssue(
                rule="group_empty",
                subject=root,
                detail=(
                    f"print group {root!r} (intent {intent!r}) places nothing — "
                    "no realized fdm member and no stand-in"
                ),
                severity="info",
            )
        )
        return GroupPrintReport(
            root=root,
            intent=intent,
            mode=mode,
            members=members,
            candidates=[],
            chosen_down=None,
            chosen_score=None,
            pinned=False,
            best_other=None,
            search_skipped=None,
            pitch=pitch,
            findings=findings,
        )
    if not manifold_available():
        raise PrintUnsupported(
            "view='print' on a group needs the manifold3d backend (core "
            "dependency — a broken venv?)"
        )

    meshes = {m.block: _world_mesh(m, pitch) for m in placed}
    verts_all: list[np.ndarray] = []
    tris_all: list[np.ndarray] = []
    offset = 0
    for m in placed:
        v, t = meshes[m.block]
        verts_all.append(v)
        tris_all.append(t + offset)
        offset += len(v)
    union = (np.vstack(verts_all), np.vstack(tris_all))
    # set_load declares forces in the world frame — the group's frame.
    loads: list[Vec3] = [
        as_vec3(tree.blocks[m.block].objectives["force"])
        for m in members
        if (tree.blocks[m.block].objectives or {}).get("force")
    ]

    simp_pins = _simp_pins(tree, members)
    root_pin = pinned_down(root_node)
    pinned = root_pin is not None
    search_skipped: str | None = None
    candidates: list[BuildCandidate] = []
    chosen_down: Vec3 | None = None
    if simp_pins:
        first_name, first_down, first_dir = simp_pins[0]
        disagreeing = [
            (n, d, bd)
            for n, d, bd in simp_pins[1:]
            if not np.allclose(d, first_down, atol=1e-6)
        ]
        if disagreeing:
            others = ", ".join(
                f"{n!r} ({bd}, world down={format_down(d)})" for n, d, bd in disagreeing
            )
            findings.append(
                ValidationIssue(
                    rule="simp_frame_conflict",
                    subject=root,
                    detail=(
                        f"SIMP-realized members disagree on the build direction: "
                        f"{first_name!r} ({first_dir}, world down="
                        f"{format_down(first_down)}) vs {others}; the group frame "
                        f"follows {first_name!r} and the others print against "
                        "the direction their AM filter assumed"
                    ),
                    severity="error",
                    suggested_fix=(
                        "re-realize the disagreeing member(s) with build_dir= "
                        "matching, or split them into their own group"
                    ),
                )
            )
        search_skipped = (
            f"orientation search skipped: member {first_name!r} was SIMP-realized "
            f"with build_dir {first_dir!r} baked in by the AM filter; the group "
            f"frame follows it (world down={format_down(first_down)})"
        )
        chosen_down = first_down
        if root_pin is not None and not np.allclose(root_pin, first_down, atol=1e-6):
            findings.append(
                ValidationIssue(
                    rule="simp_frame_overridden",
                    subject=root,
                    detail=(
                        f"the group's pinned down={format_down(as_vec3(root_pin))} "
                        f"overrides SIMP member {first_name!r}'s baked "
                        f"{first_dir} (world down={format_down(first_down)})"
                    ),
                    severity="warn",
                    suggested_fix=f"clear_build_frame(block={root!r})",
                )
            )
    elif policy:
        candidates = cad_printability.orient(union, rules, policy, loads)
    if root_pin is not None:
        chosen_down = as_vec3(root_pin)
    elif chosen_down is None and candidates:
        chosen_down = candidates[0].down

    chosen_score: cad_printability.Score | None = None
    best_other: str | None = None
    if chosen_down is not None and policy:
        chosen_score = cad_printability.score(union, chosen_down, rules, policy, loads)
        if (
            pinned
            and candidates
            and not np.allclose(chosen_down, candidates[0].down, atol=1e-6)
        ):
            best = candidates[0]
            best_other = (
                f"down={format_down(best.down)} scores {best.score:.4g} vs the "
                f"pinned down={format_down(chosen_down)}'s "
                f"{chosen_score.total:.4g} ({chosen_score.total - best.score:+.4g})"
            )

    # Per-member DRC at the group's one frame (module docstring).
    for m in placed:
        node = tree.blocks[m.block]
        member_rules = _rules_for(tree, node) if m.role == PRINTED else rules
        if chosen_down is not None and m.printed is not None:
            # A SIMP member printing in its own baked frame: the 45° voxel
            # rule on the stored field replaces the mesh overhang rule, as
            # printing.report_for does for the block alone (a marching-
            # cubes staircase has facets on both sides of 45° regardless).
            frame = node.build_frame or {}
            if (
                frame.get("origin") == SIMP_FRAME_ORIGIN
                and frame.get("down")
                and np.allclose(
                    m.xform.apply_dir(as_vec3(frame["down"])), chosen_down, atol=1e-6
                )
            ):
                build_dir = str(frame.get("build_dir") or "?")
                voxels = _simp_field_overhangs(m.printed, build_dir, cad_store_reader)
                if voxels is not None:
                    member_rules = {
                        k: v for k, v in member_rules.items() if k != "max_overhang"
                    }
                    if voxels > 0:
                        findings.append(
                            ValidationIssue(
                                rule="overhang",
                                subject=m.block,
                                detail=(
                                    f"{voxels} voxel(s) of {m.block!r}'s stored SIMP "
                                    "field are unsupported under the 45° rule in "
                                    f"its baked build frame ({build_dir})"
                                ),
                                severity="warn",
                                measured=f"{voxels} unsupported voxel(s)",
                                expected="0 (the AM filter's own rule)",
                                suggested_fix=(
                                    "re-realize without open=/close=, or coarser"
                                ),
                            )
                        )
        if chosen_down is not None:
            member_loads = (
                [as_vec3(node.objectives["force"])]
                if (node.objectives or {}).get("force")
                else []
            )
            member_score = (
                cad_printability.score(
                    meshes[m.block], chosen_down, member_rules, policy, member_loads
                )
                if policy
                else None
            )
            findings.extend(
                frame_findings(
                    m.block,
                    meshes[m.block],
                    chosen_down,
                    member_rules,
                    score=member_score,
                    best_other=best_other if m.block == root else None,
                )
            )
        if m.role == PRINTED and m.printed is not None:
            findings.extend(
                frame_free_findings(tree, node, m.block, m.printed, member_rules)
            )

    return GroupPrintReport(
        root=root,
        intent=intent,
        mode=mode,
        members=members,
        candidates=candidates,
        chosen_down=chosen_down,
        chosen_score=chosen_score,
        pinned=pinned,
        best_other=best_other,
        search_skipped=search_skipped,
        pitch=pitch,
        findings=findings,
    )


# ── export ──────────────────────────────────────────────────────────────


def write_group_mesh(report: GroupPrintReport, out_path: str | Path) -> Path:
    """Write the group as one 3MF — one object per member (a multi-
    component member contributes ``<member>/<component>`` objects), every
    member in its world pose, rotated so the group's build-down is ``-z``
    with the union's lowest point at ``z = 0``, millimetres. Reuses the
    cad export seam exactly as :func:`precis_se.printing.write_mesh` does
    (mm-scale first, fold, then rotate the finished meshes)."""
    if report.chosen_down is None:
        raise ExportError(
            f"print group {report.root!r} has no build frame to export in "
            "(nothing placed)"
        )
    if not manifold_available():
        raise PrintUnsupported(
            "view='print' export needs the manifold3d backend (core "
            "dependency — a broken venv?)"
        )
    mm_pitch = None if report.pitch is None else report.pitch * _MM_PER_M
    names: list[str] = []
    verts: list[np.ndarray] = []
    tris: list[np.ndarray] = []
    for m in report.exportable:
        assert m.spec is not None
        parts = _component_meshes(_scaled_for_export(m.spec), pitch=mm_pitch)
        t_mm = m.xform.t * _MM_PER_M
        for comp, v, t in parts:
            names.append(m.block if len(parts) == 1 else f"{m.block}/{comp}")
            verts.append(v @ m.xform.R.T + t_mm)
            tris.append(t)
    framed = rotate_all_to_frame(verts, report.chosen_down)
    out = Path(out_path)
    _write_3mf(out, list(zip(names, framed, tris, strict=True)))
    return out
