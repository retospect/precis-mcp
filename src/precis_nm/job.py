"""``nm_propose`` job_type — an LLM fills ONE target block of an ``nm`` design
with a **proposed chemical fragment**, without applying it (slice 4b "LLM fill
loop", ``docs/backlog/nm-kind.md`` "4b — LLM fill loop"). This is the L3→L5
bridge from :mod:`precis_nm`'s six-level IR (see this package's
``__init__.py`` docstring): the fragment is chosen and realized here, but
nothing is minted or bound — that is a separate Apply step (unshipped this
round; the backlog names it "mint the structure design, ``derived-from``
lineage, then ``bind_structure``").

Mirrors the ``cad_propose``/``structure_propose`` propose-only pattern
exactly: a **tool-less** ``claude -p`` call (``mcp_config=None`` — the agent
physically cannot mutate anything, it can only return text) whose whole
deliverable is a ``job_result`` chunk holding a JSON proposal, *dry-run
validated before anyone sees it*. Unlike those two — which edit a design
that already exists — ``nm_propose`` targets exactly ONE block (per-block
proposals, small blast radius, never whole-design, per the backlog bullet)
and its output is a NEW fragment: a candidate identity (SMILES, or an
existing ``structure`` slug + provenance note), a ``structure``-kind op
script (``from_smiles``/``ring``/``attach``/…) that realizes it from
scratch, and a port→atom map wiring the block's declared ports to atoms the
ops create.

**Dry run is the load-bearing safety step**: the proposed ops are applied to
a *scratch* (never-persisted) :class:`~precis.structure.scene.Scene`, then
gated through the same machinery a real fragment would face —
:func:`precis.structure.validate.validate` (hard-reject: overlap,
over-coordination, implausible bonds — any ``error`` finding fails the
proposal), :func:`precis.structure.vsepr.advisories` (warn-tier VSEPR/ring/
hybridization findings — never fail the proposal, only annotate it), the
port→atom map itself (every mapped port must resolve on the target block,
every mapped atom must exist in the just-built scratch scene, and a port's
declared ``expected_element`` must match — the exact gate
``NmHandler._bind_structure`` runs at real bind time, run early here for the
same reason ``cad_propose``/``structure_propose`` dry-run before a human
sees anything), and a best-effort **envelope fit-check** (warn-tier — see
:func:`_envelope_fit_warnings`'s docstring for why it can only be
best-effort at propose time, unlike the backlog's separately-scoped
``envelope_fit`` bind-preflight check, which has a real placement to check
against). Like ``structure_propose``, a proposed ``relax`` op is rejected
outright — a proposal is a from-scratch geometry build, never a fidelity-
ladder dispatch (that's compute-heavy and belongs to Apply, not Propose, the
same "propose stays synchronous and cheap" reasoning ``structure_propose``
documents for its own rejection).

**Tier: FRONTIER** (opus-class), unlike ``structure_propose``'s BIG
(sonnet) pin. ``structure_propose``'s round-trip eval showed sonnet ties
opus on translating an already-fully-specified instruction into mechanical
ops — but here the model itself has to *choose* real chemistry (which
fragment, which SMILES, which atoms bind which ports) from a target block's
envelope/ports/objectives alone, a judgment call closer to ``cad_propose``'s
whole-design authoring than to a mechanical translation step. Override via
``PRECIS_NM_PROPOSE_MODEL`` (the same revert knob the other two proposers
expose).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import numpy as np

from precis.cad import dsl as cad_dsl
from precis.cad import relate as cad_relate
from precis.cad.graph import Design as CadDesign
from precis.cad.vec import translation as cad_translation
from precis.structure import apply_ops as structure_apply_ops
from precis.structure.cell import Cell as StructCell
from precis.structure.ops import OpError as StructOpError
from precis.structure.scene import Scene as StructScene
from precis.structure.validate import validate as structure_validate
from precis.structure.vsepr import advisories as structure_advisories
from precis.utils.llm.router import LlmRequest, Tier, route
from precis.workers.job_types import JobTypeSpec
from precis_nm import persist
from precis_nm import validate as nm_validate
from precis_nm.generators.sp2 import VDW_MARGIN_A
from precis_nm.ops import (
    BlockNode,
    BlockTree,
    effective_dof,
    effective_envelope,
    effective_ports,
)

log = logging.getLogger(__name__)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nm_ref_id": {"type": "integer"},
        "slug": {"type": ["string", "null"]},
        "block": {"type": "string", "minLength": 1},
        "steer": {"type": ["string", "null"]},
    },
    "required": ["nm_ref_id", "block"],
    "additionalProperties": True,
}
COMPATIBLE_EXECUTORS = frozenset({"claude_inproc"})
#: Satisfied by EXECUTOR_PROVIDES['claude_inproc'] ⊇ {'claude_bin'}. No
#: mcp_config — the proposal is tool-less on purpose.
REQUIRES = frozenset({"claude_bin"})
DESCRIPTION = (
    "Propose a chemical fragment (SMILES/structure-slug identity + a "
    "structure-kind op script + port->atom map) filling one nm block "
    "(tool-less claude -p; dry-run validated; the human/a later Apply "
    "step mints and binds it separately)."
)

#: A non-periodic scratch cell for the dry-run scene. Generous and fixed
#: (unlike ``NmHandler._generated_cell``, this runs BEFORE any atom exists,
#: so there is no coordinate extent to size it from) — non-periodic axes
#: never wrap (``Cell.wrap``'s docstring), so the only real constraint is
#: avoiding a degenerate lattice, not fitting the eventual fragment inside.
_SCRATCH_CELL_A = 100.0

#: Op vocabulary shown to the model — the fragment-realization subset of
#: ``precis.structure.ops`` (kept in sync with that module's docstrings).
_OP_VOCAB = (
    "from_smiles{smiles,offset?:[x,y,z],seed?} · "
    "ring{element,n,aromatic?,center?:[x,y,z],normal?:[x,y,z],bond_length?} · "
    "attach{from,to,order?,distance?,direction?:[x,y,z],from_direction?:[x,y,z]} · "
    "add_atom{element,frac|cart:[x,y,z],label?} · "
    "add_bond{i,j,order?,kind?} · set_element{atom,element} · "
    "vacancy{atom} · displace{atom,vector:[dx,dy,dz],cartesian?}"
)

#: Fit-check tolerance (Å). Deliberately the SAME constant
#: :func:`precis_nm.validate.envelope_fit` defaults to, imported rather than
#: restated: propose time and bind time answer the same question ("do these
#: atoms fit this envelope") at different moments, so two independently
#: maintained margins would drift and hand the caller two irreconcilable
#: numbers for one fragment. What legitimately differs between the two is the
#: POSE (propose time has none — see :func:`_envelope_fit_warnings`), not the
#: allowance.
_ENVELOPE_FIT_MARGIN_A = VDW_MARGIN_A


def _block_not_found(tree: BlockTree, name: str) -> str:
    base = f"no such block: {name!r}"
    if not tree.blocks:
        return f"{base} — the design has no blocks yet"
    roster = ", ".join(sorted(tree.blocks)[:8])
    more = "" if len(tree.blocks) <= 8 else f", … ({len(tree.blocks)} blocks total)"
    return f"{base}. Available blocks: {roster}{more}"


def _fmt_vec(v: list[float]) -> str:
    return ", ".join(f"{x:g}" for x in v)


def _tree_summary(tree: BlockTree) -> str:
    if not tree.blocks:
        return "  (no blocks)"
    lines = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        env = effective_envelope(tree, node)
        marker = f" (instance of {node.template})" if node.template else ""
        parent = f" parent={node.parent}" if node.parent else ""
        lines.append(f"  {name}{marker} env={env or '—'}{parent}")
    return "\n".join(lines)


def _ports_summary(tree: BlockTree, node: BlockNode) -> str:
    ports = effective_ports(tree, node)
    if not ports:
        return "  (no ports declared)"
    lines = []
    for p in ports.values():
        bound = f"{p.bound_design}:{p.bound_atom}" if p.bound_design else "—"
        direction = f"[{_fmt_vec(p.direction)}]" if p.direction else "—"
        lines.append(
            f"  {p.name}  roles={p.roles or ['(none)']}  direction={direction}  "
            f"expected={p.expected_element or '—'}/{p.expected_hybridization or '—'}"
            f"  bound={bound}"
        )
    return "\n".join(lines)


def _connects_for_block(tree: BlockTree, block_name: str) -> str:
    lines = [
        f"  {c.a_block}.{c.a_port} — {c.b_block}.{c.b_port} [{c.kind}] "
        f"objectives={json.dumps(c.objectives) if c.objectives else '{}'}"
        for c in tree.connects
        if block_name in (c.a_block, c.b_block)
    ]
    return "\n".join(lines) or "  (none)"


def _threading_for_block(tree: BlockTree, block_name: str) -> str:
    lines = [
        f"  {t.a} threaded through {t.b}"
        for t in tree.threading
        if block_name in (t.a, t.b)
    ]
    return "\n".join(lines) or "  (none)"


def _validate_summary(findings: list[Any]) -> str:
    if not findings:
        return "  ✓ no findings"
    lines = [
        f"  [{f.severity}] {f.rule} — {f.subject}: {f.detail}" for f in findings[:20]
    ]
    if len(findings) > 20:
        lines.append(f"  … ({len(findings) - 20} more)")
    return "\n".join(lines)


def build_prompt(
    slug: str,
    tree: BlockTree,
    block_name: str,
    node: BlockNode,
    findings: list[Any],
    steer: str | None,
) -> str:
    """Assemble the propose-only directive prompt (no tools, JSON-only
    reply) for filling ``block_name`` — the design's tree/ports/topology/
    validate views plus the target block's own detail and its connects'
    objective vectors (the backlog bullet's input list)."""
    dof = effective_dof(tree, node)
    return (
        "You are filling ONE block of a nanomachine (molecular-machine) "
        "design with real chemistry. You will PROPOSE a chemical fragment "
        "and a structure-kind op script that realizes it from scratch, "
        "plus a port-to-atom map wiring the block's declared ports to "
        "atoms your ops create. You are NOT applying anything — output a "
        "proposal only; nothing you say is executed with tools.\n\n"
        f"# Design {slug!r} — block tree\n{_tree_summary(tree)}\n\n"
        f"# Target block {block_name!r}\n"
        f"envelope: {effective_envelope(tree, node) or '(none)'}\n"
        f"pose: [{_fmt_vec(node.pose)}] Å   rot: [{_fmt_vec(node.rot)}] deg\n"
        f"desc: {node.descr or '—'}\n"
        f"use: {node.use or '—'}\n"
        f"dof: {json.dumps(dof) if dof else '—'}\n\n"
        f"## ports (map these — other blocks connect at 'block.port')\n"
        f"{_ports_summary(tree, node)}\n\n"
        f"## objective vectors (connects touching this block)\n"
        f"{_connects_for_block(tree, block_name)}\n\n"
        f"## threading touching this block\n"
        f"{_threading_for_block(tree, block_name)}\n\n"
        f"# validate (design-wide L0-L2 findings, for context)\n"
        f"{_validate_summary(findings)}\n\n"
        f"# Caller steer\n{(steer or '(none — use your own chemical judgment)').strip()}\n\n"
        f"# Op vocabulary (realizes atoms into a fresh scratch scene)\n{_OP_VOCAB}\n\n"
        "# Output contract\n"
        "Reply with ONE JSON object and nothing else:\n"
        '{"fragment": {"smiles": "<SMILES string>"} OR '
        '{"structure_slug": "<existing structure design slug>", '
        '"note": "<why this fragment>"}, '
        '"ops": [ {"op": "...", ...}, ... ], '
        '"port_atom_map": {"<port name>": "<atom label your ops create>", ...}, '
        '"rationale": "one or two sentences on the choice"}\n'
        "The ops build the fragment from scratch — reference only atoms "
        "your own ops create (new labels you choose, or the ones "
        "from_smiles/ring mint). Map every port you can plausibly wire; "
        "port_atom_map must not be empty. Do not include a 'relax' op. Do "
        "not wrap the JSON in prose or markdown fences."
    )


def parse_proposal(text: str) -> dict[str, Any]:
    """Extract ``{fragment, ops, port_atom_map, rationale}`` from the
    model's reply.

    Tolerates a stray ```json fence or leading prose by scanning for the
    first balanced ``{ … }``. Raises ``ValueError`` if any required part is
    missing or malformed.
    """
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in the model reply")
    obj = json.loads(raw[start : end + 1])

    fragment = obj.get("fragment")
    if not isinstance(fragment, dict) or not (
        str(fragment.get("smiles") or "").strip()
        or str(fragment.get("structure_slug") or "").strip()
    ):
        raise ValueError(
            "proposal has no 'fragment' identity ('smiles' or 'structure_slug')"
        )
    ops = obj.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ValueError("proposal has no 'ops' list")
    port_atom_map = obj.get("port_atom_map")
    if not isinstance(port_atom_map, dict) or not port_atom_map:
        raise ValueError("proposal has no 'port_atom_map'")
    return {
        "fragment": fragment,
        "ops": ops,
        "port_atom_map": {str(k): str(v) for k, v in port_atom_map.items()},
        "rationale": str(obj.get("rationale") or "").strip(),
    }


def _envelope_fit_warnings(
    tree: BlockTree, node: BlockNode, scene: StructScene
) -> str | None:
    """Best-effort envelope-vs-fragment size check via the ``cad`` SDF —
    warn-tier only, never a dry-run failure.

    **Why this can only be best-effort, unlike the backlog's separately
    scoped ``envelope_fit`` bind-preflight check** (``docs/backlog/
    nm-kind.md`` "4b" — "bound scene's atoms vs the block's envelope +
    margin ... the L1↔L5 agreement check"): that check runs against a
    *bound* scene at a real, chosen placement (the block's own pose/rot, or
    a bind-time transform), so there is a real world frame to test atoms
    against. At propose time there is no such placement yet — Apply hasn't
    run, nothing is bound — so the only thing this can honestly do is
    recentre the envelope's own local bounding-box centre
    (:meth:`~precis.cad.primitives.Primitive.aabb_local`) onto the
    fragment's centroid and check the SDF from there: "is this fragment
    roughly the right SIZE for the envelope", not "does this fragment sit
    where it's supposed to" (a question propose time cannot even ask yet).
    Skips silently when the block has no envelope (nothing to check
    against — ``validate``'s ``blocks_without_envelope`` warn already
    covers that separately) or when the scratch scene has no atoms yet.
    Returns a single warning string, or ``None`` when everything is within
    :data:`_ENVELOPE_FIT_MARGIN_A`."""
    env = effective_envelope(tree, node)
    if not env or not scene.atoms:
        return None
    try:
        prim = cad_dsl.build_config(env)
    except (cad_dsl.DslError, ValueError):
        # Already validated at add_block/generate time — a bad envelope
        # reaching here would be hand-corrupted data; skip rather than
        # crash a proposal over a problem that belongs to a different check.
        return None
    lo, hi = prim.aabb_local()
    env_center = (np.asarray(lo, dtype=float) + np.asarray(hi, dtype=float)) / 2.0
    carts = [scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()]
    centroid = np.mean(np.asarray(carts), axis=0)
    delta = centroid - env_center

    design = CadDesign()
    leaf = design.prim("envelope", prim, cad_translation(*delta.tolist()))
    design.add_component("envelope", leaf)
    component = design.components["envelope"]

    outside = [
        (label, d)
        for label, cart in zip(scene.atoms, carts, strict=True)
        if (d := cad_relate.component_sdf(design, component, cart))
        > _ENVELOPE_FIT_MARGIN_A
    ]
    if not outside:
        return None
    # Report the PROTRUSION (distance past the allowance), not the raw SDF —
    # the same quantity validate.envelope_fit reports, so the propose-time
    # warning and the later bind-time finding for the same fragment agree
    # instead of differing by exactly one margin.
    worst = max(d for _label, d in outside) - _ENVELOPE_FIT_MARGIN_A
    return (
        f"envelope_fit: {len(outside)} atom(s) up to {worst:.2f} Å outside "
        f"envelope {env!r} (best-effort centroid-recentred check — no real "
        "bind pose exists yet at propose time, see this module's "
        "_envelope_fit_warnings docstring)"
    )


def dry_run(
    tree: BlockTree,
    block_name: str,
    ops: list[dict[str, Any]],
    port_atom_map: dict[str, str],
) -> tuple[str | None, list[str]]:
    """Apply ``ops`` to a fresh **scratch** scene (never persisted) and gate
    the result before a human ever sees it. Returns ``(error, warnings)``:
    ``error`` is ``None`` for a clean-or-warn-only proposal (the ops applied,
    every mapped port/atom resolved and passed its element gate, and
    ``structure.validate`` raised no hard finding); any non-``None`` error
    means the proposal is invalid. ``warnings`` collects vsepr advisories
    and an envelope fit note — informational, never load-bearing for
    ``valid``."""
    node = tree.blocks.get(block_name)
    if node is None:
        return _block_not_found(tree, block_name), []
    if node.template is not None:
        return (
            f"block {block_name!r} is an instance (of {node.template!r}) — "
            f"propose against the template {node.template!r} instead (an "
            "instance resolves ports/envelope from its template, the same "
            "rule bind_structure applies)",
            [],
        )
    if any(o.get("op") == "relax" for o in ops):
        return (
            "a proposal may not include a 'relax' op — it builds geometry "
            "from scratch only; a fidelity-ladder relax is compute-heavy "
            "and belongs to a later Apply step, not Propose",
            [],
        )

    scene = StructScene(
        cell=StructCell.from_lengths_angles(
            _SCRATCH_CELL_A,
            _SCRATCH_CELL_A,
            _SCRATCH_CELL_A,
            pbc=(False, False, False),
        )
    )
    try:
        structure_apply_ops(scene, ops)
    except StructOpError as exc:
        return f"op error: {exc}", []

    ports = effective_ports(tree, node)
    for port_name, atom_label in port_atom_map.items():
        port = ports.get(port_name)
        if port is None:
            roster = ", ".join(sorted(ports)) if ports else "(none)"
            return (
                f"port_atom_map: no such port on block {block_name!r}: "
                f"{port_name!r}. Available ports: {roster}",
                [],
            )
        atom = scene.atoms.get(atom_label)
        if atom is None:
            labels = sorted(scene.atoms)
            roster = ", ".join(labels[:8]) if labels else "(none)"
            more = "" if len(labels) <= 8 else f", … ({len(labels)} atoms total)"
            return (
                f"port_atom_map: no such atom in the proposed ops' output: "
                f"{atom_label!r}. Available atoms: {roster}{more}",
                [],
            )
        if port.expected_element and port.expected_element != atom.element:
            return (
                f"port_atom_map: port {block_name}.{port_name} expects "
                f"element {port.expected_element!r}, but atom "
                f"{atom_label!r} is {atom.element!r}",
                [],
            )

    errors = [f for f in structure_validate(scene) if f.severity == "error"]
    if errors:
        detail = "; ".join(
            f"{f.rule} on {f.atoms}: measured {f.measured:g} vs expected {f.expected:g}"
            for f in errors[:5]
        )
        more = "" if len(errors) <= 5 else f" (+{len(errors) - 5} more)"
        return f"structure validate: {detail}{more}", []

    warnings = [
        f"vsepr {f.rule} ({', '.join(f.atoms)}): {f.suggested_fix}"
        for f in structure_advisories(scene)
    ]
    fit_warning = _envelope_fit_warnings(tree, node, scene)
    if fit_warning:
        warnings.append(fit_warning)
    return None, warnings


def _hydrate_bound_scenes(
    store: Any, tree: BlockTree
) -> dict[str, dict[str, str] | None]:
    """The same "assemble in the view path" hydration
    ``NmHandler._render_validate`` runs — duplicated here (rather than
    imported) because this module owns no coupling to ``handler.py``'s
    private renderers: every block/port ``bound_design`` slug referenced
    anywhere in the tree, mapped to ``{atom label: element}`` (or ``None``
    when the slug no longer resolves), feeding :func:`precis_nm.validate.
    validate`'s ``dangling_binding``/``binding_element_mismatch`` checks for
    the design-wide context shown in the prompt."""
    slugs = {n.bound_design for n in tree.blocks.values() if n.bound_design}
    slugs |= {
        p.bound_design
        for n in tree.blocks.values()
        for p in n.ports.values()
        if p.bound_design
    }
    out: dict[str, dict[str, str] | None] = {}
    for slug in slugs:
        ref = store.get_ref(kind="structure", id=slug)
        if ref is None:
            out[slug] = None
            continue
        scene, _handles = store.structure_load(ref.id)
        out[slug] = {label: atom.element for label, atom in scene.atoms.items()}
    return out


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher (claude_inproc): load the target design/block,
    build the prompt, run tool-less claude, parse + dry-run the proposal,
    and write it as a ``job_result`` chunk."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        nm_ref_id = int(params["nm_ref_id"])
        block_name = str(params["block"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"nm_propose: malformed params ({exc})")
        return
    if not block_name:
        ctx.record_failure("nm_propose: empty 'block'")
        return
    steer = params.get("steer")
    steer = str(steer).strip() if steer else None

    try:
        tree = persist.load_tree(ctx.store, nm_ref_id)
    except Exception as exc:  # design vanished / bad id
        ctx.record_failure(f"nm_propose: cannot load design: {exc}")
        return
    node = tree.blocks.get(block_name)
    if node is None:
        ctx.record_failure(f"nm_propose: {_block_not_found(tree, block_name)}")
        return
    if node.template is not None:
        ctx.record_failure(
            f"nm_propose: block {block_name!r} is an instance (of "
            f"{node.template!r}) — propose against the template instead"
        )
        return
    slug = str(params.get("slug") or nm_ref_id)

    bound_scenes = _hydrate_bound_scenes(ctx.store, tree)
    findings = nm_validate.validate(tree, bound_scenes=bound_scenes)

    prompt = build_prompt(slug, tree, block_name, node, findings, steer)
    model = os.environ.get("PRECIS_NM_PROPOSE_MODEL")
    ctx.append_chunk("job_event", f"propose: block={block_name!r}")
    # Routed through the LLM seam: tool-less agent call on FRONTIER (module
    # docstring — chemistry fragment CHOICE is a judgment call, closer to
    # cad_propose's whole-design authoring than structure_propose's
    # mechanical op translation). PRECIS_LLM_BACKEND still switches
    # transport; PRECIS_NM_PROPOSE_MODEL overrides the model id.
    try:
        res = route(
            LlmRequest(
                tier=Tier.FRONTIER,
                source="nm_propose",
                ref_id=nm_ref_id,  # attribute spend to the nm design (gr162130)
                prompt=prompt,
                tools_needed=True,  # the agent wrapper; no MCP tools wired
                model=model,
                mcp_config=None,  # tool-less: the agent cannot mutate anything
                disallowed_tools=("WebFetch", "WebSearch"),
                output_format="stream-json",
                extra_args=("--verbose",),
                log_event=(ctx.store, ctx.ref_id, "nm_propose"),
            )
        )
    except Exception as exc:
        ctx.record_failure(f"nm_propose: agent failed: {exc}")
        return
    if res.error:
        ctx.record_failure(f"nm_propose: agent failed: {res.error}")
        return

    try:
        proposal = parse_proposal(res.text)
    except ValueError as exc:
        ctx.append_chunk("job_event", f"unparseable reply:\n{res.text[:2000]}")
        ctx.record_failure(f"nm_propose: {exc}")
        return

    err, warnings = dry_run(
        tree, block_name, proposal["ops"], proposal["port_atom_map"]
    )
    proposal["valid"] = err is None
    if err is not None:
        proposal["error"] = err
    if warnings:
        proposal["warnings"] = warnings
    proposal["block"] = block_name
    proposal["nm_ref_id"] = nm_ref_id

    ctx.append_chunk("job_result", json.dumps(proposal))
    n = len(proposal["ops"])
    verdict = "valid" if proposal["valid"] else f"INVALID ({err})"
    if warnings and proposal["valid"]:
        verdict += f" ({len(warnings)} warning(s))"
    ctx.append_chunk(
        "job_summary",
        f"Proposed a fragment [{verdict}] for {slug}.{block_name}: "
        f"{proposal['rationale'][:300]}",
    )
    ctx.set_meta(proposed_ops=n, proposal_valid=proposal["valid"])
    ctx.set_status("succeeded")


SPEC = JobTypeSpec(
    name="nm_propose",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


__all__ = ["SPEC", "build_prompt", "dry_run", "parse_proposal"]
