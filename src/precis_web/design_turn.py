"""The workbench turn engine — one tool-less chat turn against an ``se`` or
``structure`` design (the design-workbench build, slice 3 (2026-09-18)).

Pure library, no routes: the ``/se/{slug}`` and ``/structure/{slug}`` chat
POSTs call :func:`run_turn` (under ``asyncio.to_thread``) and render the
:class:`TurnResult`; the human Apply button on a proposal calls
:func:`apply_proposal`. The shape is ``workers/job_types/structure_propose``'s,
run inline: the prompt carries a text **digest** of the design
(:func:`build_digest`) plus the handles the operator clicked, the model
returns ``{"ops": [...], "rationale": "..."}`` and nothing else, and every op
is vetted against the kind's real roster before anything is dry-run or
written. The model never holds a tool: ``LlmRequest(tools_needed=False)``,
no MCP config — so a reply that *narrates* a ``put(...)`` is just prose that
fails to parse, never a write.

**Apply policy by op class** (the spec's "Apply policy, by op class"):

* non-destructive pure se ops (:func:`precis_se.ops.known_ops`, L0–L2,
  minus :data:`DESTRUCTIVE_SE_OPS`) — dry-run on a deep copy of the tree,
  then **auto-applied** through ``SeHandler.edit`` as one revision whose
  ``turn`` is this turn's transcript handle;
* destructive pure se ops (:data:`DESTRUCTIVE_SE_OPS` — currently
  ``remove_block``, ``remove_port``, ``remove_measure``, ``remove_bom``,
  ``remove_note``, ``remove_threading``, ``disconnect``), store-aware se
  ops (:data:`precis_se.atomic.apply.HANDLER_LEVEL_OPS`), and every
  ``structure`` atom op — dry-run on a copy, returned as a **proposal**
  (``applied=False``, ``proposal=ops``, ``valid``), nothing written; the
  human's Apply is :func:`apply_proposal`, which goes through
  ``StructureHandler.edit`` (version in place, never ``derive``) or
  ``SeHandler.edit``.

**Rejection is whole-turn, with one repair round.** An unknown op name,
an op smuggling raw coordinates (:func:`_raw_coordinate_key`), a reply
with no JSON object, or a dry run that fails → the validator's message is
fed back to the model ONCE (:func:`build_repair_prompt`: the original
prompt + the rejected reply + the error, same tool-less contract) and the
second reply is vetted from scratch. Still bad → **no design writes** —
no revision, no ref of the kind a narrated ``put(...)`` named (acceptance
S3): a parse/roster failure or a failed dry run of auto-apply ops is
``TurnResult(applied=False, error=...)`` tagged ``rejected``; a failed dry
run of proposal-class ops stays an INVALID proposal (as before — Apply
re-vets). A transport failure on the repair call keeps the first verdict
and says so in the ``repair`` note. The first prod use (2026-09-19) rejected 2 of 4 turns on
op-shape errors the validator named exactly (a fused block name, a
``set_load`` with no target); the repair round exists to convert those.

**Transcript.** One ``conv`` ref per design, slug
:func:`conv_slug` (``design-chat-<slug>``), linked ``related-to`` the
design once (``Store.add_link`` is idempotent on the edge). One block per
turn that reached the model *and got a reply* — applied, proposal,
**rejected** and **no-op** ("cannot be expressed as ops") alike; only a
model transport failure writes nothing — user message, clicked handles,
model rationale, ops, an optional ``repair:`` line (the first attempt's
error when a repair round ran) and the ``outcome:`` tag, appended through
``ConversationHandler.put`` with ``msg_id`` = the turn handle. Rejected
and no-op turns were lost before 2026-09-19: two of the four first-use
turns were informational answers the operator only saw as a flash. The turn handle stamped onto the revision row is
``<conv-slug>~<block ordinal>``: the address ``get(kind='conv',
id='<slug>~N')`` resolves and ``precis.utils.mentions`` parses (the
computed ``format_handle`` form is a *chunk-id* handle, which a reader
cannot map back to "turn N of this design's chat" without a lookup). The
ordinal is fixed *before* the edit so the revision row can name it; the
block is written right after — the two are not one transaction (each
handler owns its own), which is acceptable for a single operator.

The read side of that convention lives here too: :func:`transcript`
parses the blocks back into :class:`TranscriptTurn` rows for the chat
panel, and :func:`pending_proposal` is "the last turn was a proposal and
no revision carries its handle yet" — the panel's Apply button needs no
session state, only the store.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from psycopg.errors import IntegrityError

from precis.design import history as design_history
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.conversation import ConversationHandler
from precis.handlers.structure import StructureHandler
from precis.structure import apply_ops as structure_apply_ops
from precis.structure.ops import _OPS as _STRUCTURE_OPS
from precis.structure.ops import OpError as StructureOpError
from precis.structure.probe import toc as structure_toc
from precis.utils.llm.json_reply import extract_json_object
from precis.utils.llm.router import LlmRequest, Tier, route
from precis_se import persist as se_persist
from precis_se.atomic.apply import HANDLER_LEVEL_OPS, all_op_names
from precis_se.atomic.bind import bind_structure, unbind_structure
from precis_se.atomic.generate import prepare_generate
from precis_se.handler import (
    SeHandler,
    _binding_line,
    _render_ports,
    _render_topology,
    _render_tree,
)
from precis_se.ops import OpError as SeOpError
from precis_se.ops import SeTree, effective_ports
from precis_se.ops import apply_ops as se_apply_ops
from precis_se.ops import known_ops as se_known_ops
from precis_se.realize import prepare_realize

log = logging.getLogger(__name__)

DesignKind = Literal["se", "structure"]

#: ``prompt -> reply text``. The default is the router at ``Tier.BIG``
#: (:func:`_router_call`); tests inject a stub and never reach the router.
ModelCall = Callable[[str], str]

#: Atom rows past this many are elided from a structure digest (with a
#: note) — the model reasons over the table, and a 2 000-row table is
#: neither readable nor cheap.
MAX_DIGEST_ATOMS = 400

#: The friendly reply to a ``design_revisions`` ``UNIQUE(ref_id, rev)``
#: race (:class:`psycopg.errors.IntegrityError`): two saves computed the
#: same next ``rev`` (se numbering is ``len(list_revisions)+1``) and the
#: loser's transaction is already rolled back by the handler's own ``tx()``
#: context by the time either :func:`run_turn` or :func:`apply_proposal`
#: sees this — nothing else to clean up here.
_CONCURRENT_SAVE_ERROR = "another save landed first — reload the page and retry"

#: Op-dict keys that only ever mean "here are raw coordinates" — none of
#: either vocabulary takes them (se poses are ``pose``/``rot``; structure
#: atoms are ``frac``/``cart``, one vector per op). Their presence rejects
#: the turn whatever the op name (the spec: "anything else, including
#: free-form coordinates, rejects the whole turn").
_RAW_COORDINATE_KEYS = frozenset(
    {
        "xyz",
        "coords",
        "coordinates",
        "positions",
        "position",
        "cartesian_coords",
        "atoms_xyz",
        "geometry",
        "points",
    }
)

#: One-line signatures for the pure se ops shown to the model — a
#: hand-kept table (the op functions' docstrings are prose, not
#: signatures), checked by ``tests/test_design_turn.py`` to name only ops
#: in :func:`precis_se.atomic.apply.all_op_names`. A roster op without an
#: entry is still listed, by bare name.
_SE_OP_SIGNATURES: dict[str, str] = {
    "add_block": "add_block{name,parent?,envelope?,pose?:[x,y,z] m,rot?:[rx,ry,rz] rad,desc?,use?,dof?}",
    "instance_block": "instance_block{name,template,parent?,pose?,rot?}",
    "array_block": "array_block{name,template,parent?,linear?:{count,pitch,axis?}|polar?:{count,radius,axis?}}",
    "remove_block": "remove_block{block}",
    "set_pose": "set_pose{block,pose?:[x,y,z] m,rot?:[rx,ry,rz] rad,origin?:user|proposed}",
    "set_envelope": "set_envelope{block,envelope:'box:w..d..h..'|'cyl:r..h..'|…,origin?}",
    "set_desc": "set_desc{block,desc?,use?}",
    "add_port": "add_port{block,name,roles?:[…],direction?:[x,y,z],pose?,rot?,annotations?,expected_element?,expected_hybridization?}",
    "remove_port": "remove_port{block,name}",
    "set_port_pose": "set_port_pose{block,name,pose?,rot?,clear?}",
    "connect": "connect{a:'block.port',b:'block.port',joint?:{class,axis?,mechanism?,params?},kind?:bond|interaction,objectives?}",
    "disconnect": "disconnect{a,b}",
    "set_joint": "set_joint{a,b,joint:{class,axis?,mechanism?,params?}|null}",
    "set_load": "set_load{block|a+b,force?:[N],torque?:[N·m],duty?,cycles?,fixed?,clear?}",
    "add_measure": "add_measure{block,name,value?,min?,max?,unit?:m|count|ratio|deg,relation?,origin?}",
    "set_measure": "set_measure{block,name,value?,min?,max?,relation?,origin?}",
    "remove_measure": "remove_measure{block,name}",
    "set_mode": "set_mode{block,mode:'purchase'|'atomic'|'fdm/<material>'|…|null}",
    "set_binding": "set_binding{block,kind:cad|structure|component|part,design}|{block,clear:true}",
    "add_bom": "add_bom{block|a+b,item_kind:component|part,item,qty?,uom?,why?}",
    "remove_bom": "remove_bom{block|a+b,item_kind,item}",
    "add_note": "add_note{name,kind:question|answer|decision,text,re?,about?:[…],origin?}",
    "remove_note": "remove_note{name}",
    "declare_threading": "declare_threading{a,b}  (a threaded through b)",
    "remove_threading": "remove_threading{a,b}",
    "declare_dof": "declare_dof{block,kind:rotational|translational,axis_ports:[p,q]}",
    "clear_dof": "clear_dof{block}",
    "declare_states": "declare_states{block,states:[{name,ports?:{…}}]}",
    "declare_transitions": "declare_transitions{block,transitions:[{from_state,to_state,driver_kind,driver_ref?,params?}]}",
    "set_current_state": "set_current_state{block,state}",
}

#: The store-aware se ops, shown with the propose-only marker.
_SE_STORE_AWARE_SIGNATURES: dict[str, str] = {
    "bind_structure": "bind_structure{block,design:<structure slug>,ports?:{port: atom label}}",
    "unbind_structure": "unbind_structure{block}",
    "generate": "generate{generator:cnt|fullerene|cone|cyclodextrin|hexfold,params:{…},name,parent?,pose?}",
    "realize": "realize{block,mode}",
}

#: Structure op signatures — ``structure_propose``'s vocabulary widened to
#: the whole :data:`precis.structure.ops._OPS` table (Å throughout).
_STRUCTURE_OP_SIGNATURES: dict[str, str] = {
    "set_cell": "set_cell{a,b,c,alpha?,beta?,gamma?,pbc?:[bool,bool,bool]}",
    "slab": "slab{element,size:[nx,ny,nz],a?,vacuum?,fix_layers?}",
    "add_atom": "add_atom{element,frac|cart:[x,y,z],label?,charge?,magmom?}",
    "add_atom_site": "add_atom_site{element,site:'top:aX'|'bridge:aX,aY'|'hollow:aX,aY,aZ',height?}",
    "add_adsorbate": "add_adsorbate{species:H|O|N|OH|H2O|NH|NH2|NH3,site,height?,rotate?}",
    "set_element": "set_element{atom,element}",
    "vacancy": "vacancy{atom}",
    "displace": "displace{atom,vector:[dx,dy,dz],cartesian?}",
    "add_bond": "add_bond{i,j,order?,kind?,image?}",
    "remove_bond": "remove_bond{i,j}",
    "constrain": "constrain{atoms:[…],kind:none|fixed-x|fixed-y|fixed-z|fixed-all}",
    "eye": "eye{name,atoms:[…],reach?,for?}",
    "measure": "measure{kind:distance|angle|coordination|bond_length,atoms:[…],direction?,goal?,strength?,for?}",
    "unmark": "unmark{name}",
    "remove_measure": "remove_measure{kind,atoms:[…]}",
    "ring": "ring{element,n,aromatic?,center?:[x,y,z],normal?:[x,y,z],bond_length?}",
    "attach": "attach{from,to,order?,distance?,kind?}",
    "from_smiles": "from_smiles{smiles,offset?:[x,y,z],seed?}",
}


@dataclass(frozen=True)
class TurnResult:
    """What one turn did. ``applied`` — ops were written as one revision
    (``revision`` = its number). ``proposal`` — the ops await a human
    Apply (``valid`` = the dry run's verdict, its message in ``error``
    when ``False``). ``error`` with ``ops=[]`` — the reply was rejected
    before any dry run. ``turn`` is the transcript block's handle when
    one was written (every turn the model answered)."""

    applied: bool
    ops: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""
    proposal: list[dict[str, Any]] | None = None
    valid: bool | None = None
    error: str | None = None
    revision: int | None = None
    turn: str | None = None
    #: The first attempt's validator error when a repair round ran (the
    #: second reply is what ``ops``/``error`` describe).
    repair: str | None = None


def conv_slug(slug: str) -> str:
    """The design's transcript ref slug — one ``conv`` per design."""
    return f"design-chat-{slug}"


# ── digest ─────────────────────────────────────────────────────────────


def _store_of(hub: Hub) -> Any:
    if hub.store is None:  # pragma: no cover - the web app always has one
        raise BadInput("design_turn: store required")
    return hub.store


def _design_ref(store: Any, kind: DesignKind, slug: str) -> Any:
    ref = store.get_ref(kind=kind, id=slug)
    if ref is None:
        raise NotFound(f"{kind} design {slug!r} not found")
    return ref


def _load_se_tree(store: Any, ref: Any) -> SeTree:
    tree = se_persist.load_tree(store, ref.id)
    tree.own_slug = str(ref.slug)
    tree.foreign = se_persist.foreign_resolver(store)
    return tree


def _se_identity_lines(tree: SeTree) -> str:
    """uid + realization per block — what ``view='tree'`` leaves out and a
    turn needs (a clicked handle is a uid; a realize decision reads the
    binding)."""
    rows = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        uid = f"#{node.uid}" if node.uid is not None else "(unsaved)"
        ports = ", ".join(sorted(effective_ports(tree, node))) or "—"
        rows.append(f"  {uid} {name}: ports {ports}; {_binding_line(tree, node)}")
    return "\n".join(rows) if rows else "  (none)"


def _se_connect_lines(tree: SeTree) -> str:
    rows = []
    for c in tree.connects:
        extra = []
        if c.joint:
            extra.append(f"joint={json.dumps(c.joint)}")
        if c.kind:
            extra.append(f"kind={c.kind}")
        if c.objectives:
            extra.append(f"objectives={json.dumps(c.objectives)}")
        tail = f"  ({'; '.join(extra)})" if extra else ""
        rows.append(f"  {c.a_block}.{c.a_port} — {c.b_block}.{c.b_port}{tail}")
    return "\n".join(rows) if rows else "  (none)"


def _se_digest(store: Any, ref: Any) -> str:
    tree = _load_se_tree(store, ref)
    description = str((ref.meta or {}).get("description") or "").strip()
    parts = [
        _render_tree(tree, ref.title or str(ref.slug), description),
        "## blocks (uid · ports · realization)\n" + _se_identity_lines(tree),
        "## connects\n" + _se_connect_lines(tree),
        _render_ports(tree),
        _render_topology(tree),
    ]
    if tree.measures:
        parts.append(
            "## measures\n" + "\n".join(f"  {m.block}.{m.name}" for m in tree.measures)
        )
    if tree.notes:
        parts.append(
            "## notes\n"
            + "\n".join(f"  {n.name} [{n.kind}] {n.body}" for n in tree.notes)
        )
    return "\n\n".join(parts)


def _structure_digest(store: Any, ref: Any) -> str:
    scene, _handles = store.structure_load(ref.id)
    t = structure_toc(scene)
    atoms_all = list(scene.atoms.values())
    shown = atoms_all[:MAX_DIGEST_ATOMS]
    atoms = "\n".join(
        f"  {a.label} {a.element} frac=[{a.frac[0]:.3f},{a.frac[1]:.3f},{a.frac[2]:.3f}]"
        f"{' fixed' if a.fixed else ''}"
        for a in shown
    )
    if len(atoms_all) > len(shown):
        atoms += (
            f"\n  … {len(atoms_all) - len(shown)} more atoms not listed "
            f"(digest caps at {MAX_DIGEST_ATOMS}; refer to atoms by label)"
        )
    bonds = (
        "\n".join(
            f"  {b.i}-{b.j} order={b.order} {b.kind}/{b.provenance}"
            for b in scene.bonds[: MAX_DIGEST_ATOMS * 2]
        )
        or "  (none)"
    )
    if len(scene.bonds) > MAX_DIGEST_ATOMS * 2:
        bonds += f"\n  … {len(scene.bonds) - MAX_DIGEST_ATOMS * 2} more bonds"
    marks = (
        "\n".join(
            f"  {m.name or m.kind} [{m.kind}] over {','.join(m.operands)}"
            + (f" for={m.for_!r}" if m.for_ else "")
            for m in scene.measures
        )
        or "  (none)"
    )
    return (
        f"Design {ref.slug!r}: {t['formula']} · {t['natoms']} atoms · pbc {t['pbc']} · "
        f"{t['nbonds']} bonds · {t['nfragments']} fragment(s) · "
        f"version {store.structure_version(ref.id)}\n"
        f"Atoms:\n{atoms}\nBonds:\n{bonds}\nMarkers:\n{marks}"
    )


def op_vocabulary(kind: DesignKind) -> str:
    """The op roster the model may draw from, one signature per line, with
    the apply class of each marked. Built from the live rosters
    (:func:`all_op_names` / :data:`precis.structure.ops._OPS`) so an op
    added to a kind shows up here by name even before its signature is
    written."""
    if kind == "se":
        pure = sorted(se_known_ops())
        lines = ["Auto-applied on a valid reply (pure block-tree ops):"]
        lines += [f"  {_SE_OP_SIGNATURES.get(n, n)}" for n in pure]
        lines.append("Proposal only — a human applies these (store-aware ops):")
        lines += [
            f"  {_SE_STORE_AWARE_SIGNATURES.get(n, n)}" for n in HANDLER_LEVEL_OPS
        ]
        return "\n".join(lines)
    lines = ["Proposal only — a human applies these (atom ops, Å):"]
    lines += [f"  {_STRUCTURE_OP_SIGNATURES.get(n, n)}" for n in sorted(_STRUCTURE_OPS)]
    return "\n".join(lines)


def build_digest(hub: Hub, *, kind: DesignKind, slug: str) -> str:
    """The text the model acts on: the design (se — tree, uids, ports,
    connects, topology, measures, notes; structure — atom/bond/marker
    tables, capped at :data:`MAX_DIGEST_ATOMS`) plus the op vocabulary."""
    store = _store_of(hub)
    ref = _design_ref(store, kind, slug)
    body = _se_digest(store, ref) if kind == "se" else _structure_digest(store, ref)
    return (
        f"# Current design ({kind})\n{body}\n\n# Op vocabulary\n{op_vocabulary(kind)}"
    )


# ── prompt + reply ─────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are the design partner in a molecule/mechanism workbench. You "
    "edit a design ONLY by returning typed ops from the listed vocabulary. "
    "You have no tools and cannot read or write anything yourself: a "
    "sentence like put(...) or edit(...) is not an action and will be "
    "rejected. Reply with ONE JSON object and nothing else:\n"
    '{"ops": [{"op": "<name>", ...}, ...], "rationale": "one or two '
    'sentences on what these ops do and why"}\n'
    "If the request cannot be expressed with the listed ops, reply "
    '{"ops": [], "rationale": "cannot be expressed as ops because ..."} '
    "— that is a valid, welcome answer. Never invent op names or keys, "
    "never pass raw coordinate tables; refer to blocks/atoms by the names "
    "and labels in the digest. Do not wrap the JSON in prose or markdown "
    "fences."
)


def build_prompt(
    digest: str, *, kind: DesignKind, message: str, handles: list[str]
) -> str:
    """One prompt string (the ``claude_*`` transports take ``prompt``; the
    local transport turns it into a single user turn)."""
    clicked = ", ".join(h.strip() for h in handles if h.strip()) or "(none)"
    return (
        f"{SYSTEM_PROMPT}\n\n{digest}\n\n"
        f"# Clicked handles\n{clicked}\n\n"
        f"# Message\n{message.strip()}\n\n"
        "# Output contract\n"
        'ONE JSON object: {"ops": [...], "rationale": "..."} — ops only from '
        f"the {kind} vocabulary above."
    )


#: A rejected reply (and the validator error, which can echo model
#: text) is quoted back to the model at most this long.
_REPAIR_REPLY_MAX = 4000
_REPAIR_ERROR_MAX = 2000


def build_repair_prompt(prompt: str, *, reply: str, error: str) -> str:
    """The one repair round: the original prompt, the reply the validator
    refused, and its message verbatim — the model fixes exactly that or
    says why it cannot."""
    return (
        f"{prompt}\n\n# Your previous reply was rejected\n"
        f"{(reply or '').strip()[:_REPAIR_REPLY_MAX] or '(empty)'}\n\n"
        f"# Validator error\n{error[:_REPAIR_ERROR_MAX]}\n\n"
        "Fix exactly that and reply again with ONE JSON object under the same "
        'contract. If it cannot be fixed with the listed ops, reply {"ops": [], '
        '"rationale": "cannot be expressed as ops because ..."}.'
    )


def parse_reply(text: str) -> tuple[list[dict[str, Any]], str]:
    """``(ops, rationale)`` out of the model's reply — tolerates a fence or
    surrounding prose (:func:`extract_json_object`). ``ValueError`` when no
    JSON object with an ``ops`` list is there (prose, a bare list, a
    ``put(...)`` narration)."""
    obj = extract_json_object(text or "")
    if obj is None:
        raise ValueError('the reply is not a JSON object {"ops": [...], ...}')
    ops = obj.get("ops")
    if not isinstance(ops, list):
        raise ValueError("the reply's JSON has no 'ops' list")
    for op in ops:
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise ValueError(f"each op must be an object with an 'op' name: {op!r}")
    return ops, str(obj.get("rationale") or "").strip()


def _is_vec3(v: Any) -> bool:
    return (
        isinstance(v, (list, tuple))
        and len(v) == 3
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
    )


def _raw_coordinate_key(value: Any, path: str = "") -> str | None:
    """The path of the first raw-coordinate payload in ``value``: a key in
    :data:`_RAW_COORDINATE_KEYS` at any depth, or a list of ≥ 1 numeric
    3-vectors (a coordinate table — no op in either vocabulary takes one;
    single vectors like ``pose``/``frac``/``vector`` are fine)."""
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else str(k)
            if str(k).lower() in _RAW_COORDINATE_KEYS:
                return here
            found = _raw_coordinate_key(v, here)
            if found is not None:
                return found
        return None
    if isinstance(value, list):
        if value and all(_is_vec3(x) for x in value):
            return path or "<list>"
        for i, v in enumerate(value):
            found = _raw_coordinate_key(v, f"{path}[{i}]")
            if found is not None:
                return found
    return None


def roster(kind: DesignKind) -> frozenset[str]:
    return all_op_names() if kind == "se" else frozenset(_STRUCTURE_OPS)


def vet_ops(ops: list[dict[str, Any]], *, kind: DesignKind) -> str | None:
    """The whole-turn gate: every op name in the kind's roster and no raw
    coordinates anywhere. Returns the rejection message, or ``None``."""
    names = roster(kind)
    for i, op in enumerate(ops):
        name = op.get("op")
        if name not in names:
            return (
                f"op {i + 1}: unknown op {name!r} for {kind}; known: "
                f"{', '.join(sorted(names))}"
            )
        where = _raw_coordinate_key({k: v for k, v in op.items() if k != "op"})
        if where is not None:
            return (
                f"op {i + 1} ({name}): raw coordinates at {where!r} are not part "
                f"of the {kind} vocabulary — express geometry through the op's "
                "own fields, or say the request cannot be expressed as ops"
            )
    return None


#: The destructive slice of :func:`precis_se.ops.known_ops` — computed by
#: prefix rule off the live roster, never hand-listed, so a future op
#: earns the propose gate by naming convention alone. Currently:
#: ``remove_block``, ``remove_port``, ``remove_measure``, ``remove_bom``,
#: ``remove_note``, ``remove_threading``, ``disconnect`` (no roster op
#: today starts ``delete_``/``unbind``). These claim nothing physical the
#: other pure ops don't, but undo a decision a human may not have meant to
#: throw away — module docstring's apply-policy paragraph.
DESTRUCTIVE_SE_OPS: frozenset[str] = frozenset(
    name
    for name in se_known_ops()
    if name.startswith(("remove_", "delete_", "unbind", "disconnect"))
)


def is_auto_apply(ops: list[dict[str, Any]], *, kind: DesignKind) -> bool:
    """Non-destructive pure se ops auto-apply; a destructive pure se op
    (:data:`DESTRUCTIVE_SE_OPS`), a store-aware se op, or any structure op
    makes the whole turn a proposal."""
    if kind != "se":
        return False
    pure = se_known_ops()
    return all(
        op.get("op") in pure and op.get("op") not in DESTRUCTIVE_SE_OPS for op in ops
    )


# ── dry runs ───────────────────────────────────────────────────────────


def dry_run_se(
    store: Any, tree: SeTree, ops: list[dict[str, Any]], *, design_slug: str
) -> str | None:
    """Walk ``ops`` on a deep copy of ``tree``: pure ops through
    :func:`precis_se.ops.apply_ops`; the store-aware ones through their
    read-only/in-memory halves only (``bind_structure``/``unbind_structure``
    never write; ``generate``/``realize`` run ``prepare_*`` and skip
    ``finish_*``, the half that mints designs). Returns the first error
    message, or ``None``."""
    scratch = copy.deepcopy(tree)
    try:
        for op in ops:
            name = op["op"]
            if name == "bind_structure":
                bind_structure(store, scratch, op)
            elif name == "unbind_structure":
                unbind_structure(scratch, op)
            elif name == "generate":
                prepare_generate(store, scratch, op, design_slug)
            elif name == "realize":
                prepare_realize(store, scratch, op, design_slug)
            else:
                se_apply_ops(scratch, [op])
    except (SeOpError, BadInput, NotFound, ValueError) as exc:
        return str(exc)
    return None


def dry_run_structure(scene: Any, ops: list[dict[str, Any]]) -> str | None:
    """``structure_propose.dry_run``'s shape: apply to a deep copy."""
    try:
        structure_apply_ops(copy.deepcopy(scene), ops)
    except (StructureOpError, ValueError) as exc:
        return f"op error: {exc}"
    return None


# ── transcript ─────────────────────────────────────────────────────────


def _next_turn_handle(store: Any, slug: str) -> str:
    """The handle the NEXT transcript block will get — fixed before the
    edit so the revision row can carry it (module docstring)."""
    cslug = conv_slug(slug)
    ref = store.get_ref(kind="conv", id=cslug)
    if ref is None:
        return f"{cslug}~0"
    existing = store.chunks.list_chunks_for_ref(ref.id)
    return f"{cslug}~{(existing[-1].ord + 1) if existing else 0}"


def _write_transcript(
    hub: Hub,
    *,
    kind: DesignKind,
    design_ref: Any,
    turn: str,
    message: str,
    handles: list[str],
    rationale: str,
    ops: list[dict[str, Any]],
    outcome: str,
    repair: str | None = None,
) -> None:
    store = _store_of(hub)
    cslug = conv_slug(str(design_ref.slug))
    lines = [
        f"**user:** {message.strip()}",
        f"handles: {', '.join(h for h in handles if h.strip()) or '(none)'}",
        f"**model:** {rationale or '(no rationale)'}",
        f"ops: {json.dumps(ops)}",
    ]
    if repair is not None:
        lines.append(f"repair: {' '.join(repair.split())}")
    lines.append(f"outcome: {outcome}")
    body = "\n".join(lines)
    # The meta is what :func:`_parse_turn` reads back; the body text is
    # the human-readable rendering (and the only source for blocks
    # written before ``outcome`` was recorded here, 2026-09-19).
    meta: dict[str, Any] = {
        "design_kind": kind,
        "design": str(design_ref.slug),
        "message": message.strip(),
        "handles": [h.strip() for h in handles if h.strip()],
        "rationale": rationale,
        "ops": ops,
        "outcome": outcome,
    }
    if repair is not None:
        meta["repair"] = repair
    ConversationHandler(hub=hub).put(
        id=cslug,
        text=body,
        author="design-chat",
        msg_id=turn,
        title=f"design chat — {kind} {design_ref.slug}",
        meta=meta,
        ref_meta={"design_kind": kind, "design": str(design_ref.slug)},
    )
    conv_ref = store.get_ref(kind="conv", id=cslug)
    assert conv_ref is not None
    # Idempotent on the edge: one related-to link per design, however
    # many turns.
    store.add_link(
        src_ref_id=conv_ref.id, dst_ref_id=design_ref.id, relation="related-to"
    )


@dataclass(frozen=True)
class TranscriptTurn:
    """One turn read back off the ``conv`` block :func:`_write_transcript`
    wrote. ``revision`` is set when the outcome was "applied as revision
    N"; ``proposal`` when the turn proposed ops for a human Apply
    (``valid``/``error`` = the dry run's verdict, as recorded);
    ``rejected`` when the validator refused the (repaired) reply
    (``error`` = its message); ``noop`` for a "cannot be expressed as
    ops" answer. ``repair`` is the first attempt's error when a repair
    round ran."""

    ord: int
    handle: str
    message: str
    handles: list[str]
    rationale: str
    ops: list[dict[str, Any]]
    outcome: str
    revision: int | None = None
    proposal: bool = False
    valid: bool | None = None
    error: str | None = None
    rejected: bool = False
    noop: bool = False
    repair: str | None = None
    created_at: datetime | None = None


#: The block layout :func:`_write_transcript` emits, in order. Message,
#: rationale and the (last) outcome may span lines (``re.S``); the
#: ``handles``/``ops``/``repair`` lines are single-line by construction.
_TRANSCRIPT_RE = re.compile(
    r"^\*\*user:\*\* (?P<message>.*?)\n"
    r"handles: (?P<handles>[^\n]*)\n"
    r"\*\*model:\*\* (?P<rationale>.*?)\n"
    r"ops: (?P<ops>[^\n]*)\n"
    r"(?:repair: (?P<repair>[^\n]*)\n)?"
    r"outcome: (?P<outcome>.*)$",
    re.S,
)
_APPLIED_RE = re.compile(r"^applied as revision (\d+)")
_PROPOSAL_RE = re.compile(
    r"^proposal \(\d+ op\(s\), (?:(valid)|INVALID: (.*))\)$", re.S
)
_REJECTED_PREFIX = "rejected: "
_NOOP_OUTCOME = "no-op (cannot be expressed as ops)"


def _classify(outcome: str) -> dict[str, Any]:
    """The typed fields an ``outcome:`` tag carries."""
    out: dict[str, Any] = {}
    if applied := _APPLIED_RE.match(outcome):
        out["revision"] = int(applied.group(1))
        out["valid"] = True
    elif prop := _PROPOSAL_RE.match(outcome):
        out["proposal"] = True
        out["valid"] = prop.group(1) is not None
        out["error"] = prop.group(2)
    elif outcome.startswith(_REJECTED_PREFIX):
        out["rejected"] = True
        out["valid"] = False
        out["error"] = outcome[len(_REJECTED_PREFIX) :]
    elif outcome == _NOOP_OUTCOME:
        out["noop"] = True
    return out


def _parse_turn(block: Any, cslug: str) -> TranscriptTurn:
    text = block.text or ""
    meta = block.meta or {}
    ops_meta = meta.get("ops")
    outcome_meta = meta.get("outcome")
    if isinstance(outcome_meta, str):
        # A block written since the meta carries the fields: never parse
        # the body, whose model line may itself contain ``ops:``/
        # ``outcome:``-shaped text (a rejected reply's raw prose).
        handles_meta = meta.get("handles")
        repair_meta = meta.get("repair")
        return TranscriptTurn(
            ord=int(block.ord),
            handle=f"{cslug}~{block.ord}",
            message=str(meta.get("message") or ""),
            handles=(
                [str(h) for h in handles_meta] if isinstance(handles_meta, list) else []
            ),
            rationale=str(meta.get("rationale") or ""),
            ops=list(ops_meta) if isinstance(ops_meta, list) else [],
            outcome=outcome_meta,
            repair=str(repair_meta) if isinstance(repair_meta, str) else None,
            created_at=getattr(block, "created_at", None),
            **_classify(outcome_meta),
        )
    m = _TRANSCRIPT_RE.match(text)
    if m is None:
        # Not a block this module wrote (someone appended by hand) — show
        # it whole rather than drop it from the transcript.
        return TranscriptTurn(
            ord=int(block.ord),
            handle=f"{cslug}~{block.ord}",
            message=text,
            handles=[],
            rationale="",
            ops=list(ops_meta) if isinstance(ops_meta, list) else [],
            outcome="",
            created_at=getattr(block, "created_at", None),
        )
    ops: list[dict[str, Any]]
    if isinstance(ops_meta, list):
        ops = list(ops_meta)
    else:
        try:
            parsed = json.loads(m.group("ops"))
        except json.JSONDecodeError:
            parsed = []
        ops = parsed if isinstance(parsed, list) else []
    handles_raw = m.group("handles").strip()
    handles = (
        []
        if handles_raw == "(none)"
        else [h.strip() for h in handles_raw.split(",") if h.strip()]
    )
    rationale = m.group("rationale").strip()
    if rationale == "(no rationale)":
        rationale = ""
    outcome = m.group("outcome").strip()
    return TranscriptTurn(
        ord=int(block.ord),
        handle=f"{cslug}~{block.ord}",
        message=m.group("message").strip(),
        handles=handles,
        rationale=rationale,
        ops=ops,
        outcome=outcome,
        repair=m.group("repair"),
        created_at=getattr(block, "created_at", None),
        **_classify(outcome),
    )


def transcript(store: Any, slug: str) -> list[TranscriptTurn]:
    """The design's chat so far, oldest first — every block of
    ``design-chat-<slug>`` parsed back; ``[]`` before the first turn."""
    cslug = conv_slug(slug)
    ref = store.get_ref(kind="conv", id=cslug)
    if ref is None:
        return []
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    return [_parse_turn(b, cslug) for b in blocks]


def pending_proposal(
    store: Any,
    *,
    kind: DesignKind,
    slug: str,
    turns: list[TranscriptTurn] | None = None,
) -> TranscriptTurn | None:
    """The proposal awaiting a human Apply: the LAST turn that touched the
    design (rejected and no-op turns change nothing and are skipped), when
    it proposed ops and no ``design_revisions`` row on the design carries
    its handle (Apply stamps the proposing ``turn`` onto the revision it
    writes). A later applied or proposing turn supersedes an unapplied
    one — only the newest is offered."""
    rows = transcript(store, slug) if turns is None else turns
    last = next((t for t in reversed(rows) if not (t.rejected or t.noop)), None)
    if last is None or not last.proposal:
        return None
    ref = store.get_ref(kind=kind, id=slug)
    if ref is None:
        return None
    applied = {r.turn for r in design_history.list_revisions(store, ref.id) if r.turn}
    return None if last.handle in applied else last


# ── the turn ───────────────────────────────────────────────────────────


def _router_call(ref_id: int) -> ModelCall:
    """The default ``model_call``: one tool-less ``Tier.BIG`` route, spend
    attributed to the design. Raises on a transport error."""

    def call(prompt: str) -> str:
        res = route(
            LlmRequest(
                tier=Tier.BIG,
                source="design-chat",
                ref_id=ref_id,
                prompt=prompt,
                tools_needed=False,
                mcp_config=None,
                disallowed_tools=("WebFetch", "WebSearch"),
                timeout_s=120.0,
            )
        )
        if res.error:
            raise RuntimeError(res.error)
        return res.text or ""

    return call


def _revision_of(store: Any, ref_id: int) -> int | None:
    revs = design_history.list_revisions(store, ref_id)
    return revs[-1].rev if revs else None


@dataclass(frozen=True)
class _Vetted:
    """One reply through parse → roster gate → dry run. ``error`` names
    the stage's message; ``stage`` is ``"reply"`` (parse/roster — the ops
    are unusable, ``ops=[]``) or ``"dry_run"`` (well-formed ops the design
    refused)."""

    ops: list[dict[str, Any]]
    rationale: str
    error: str | None = None
    stage: Literal["reply", "dry_run"] | None = None


def _vet_reply(store: Any, ref: Any, *, kind: DesignKind, reply: str) -> _Vetted:
    try:
        ops, rationale = parse_reply(reply)
    except ValueError as exc:
        # Keep what the model said (truncated) so the transcript shows
        # the prose that failed to parse.
        return _Vetted([], (reply or "").strip()[:500], str(exc), "reply")
    rejection = vet_ops(ops, kind=kind)
    if rejection is not None:
        return _Vetted([], rationale, rejection, "reply")
    if not ops:
        return _Vetted([], rationale)
    if kind == "se":
        tree = _load_se_tree(store, ref)
        err = dry_run_se(store, tree, ops, design_slug=str(ref.slug))
    else:
        scene, _handles = store.structure_load(ref.id)
        err = dry_run_structure(scene, ops)
    return _Vetted(ops, rationale, err, "dry_run" if err is not None else None)


def run_turn(
    hub: Hub,
    *,
    kind: DesignKind,
    slug: str,
    message: str,
    handles: list[str] | None = None,
    model_call: ModelCall | None = None,
) -> TurnResult:
    """One workbench turn (module docstring for the policy). Never raises
    on a bad model reply — that is a :class:`TurnResult` with ``error``;
    a missing design or store does raise."""
    store = _store_of(hub)
    ref = _design_ref(store, kind, slug)
    clicked = list(handles or [])
    prompt = build_prompt(
        build_digest(hub, kind=kind, slug=slug),
        kind=kind,
        message=message,
        handles=clicked,
    )
    call = model_call or _router_call(ref.id)
    try:
        reply = call(prompt)
    except Exception as exc:
        return TurnResult(applied=False, error=f"model call failed: {exc}")
    vetted = _vet_reply(store, ref, kind=kind, reply=reply)
    repair: str | None = None
    if vetted.error is not None:
        # The one repair round: same contract, the validator's message
        # quoted back. A transport failure here keeps the first verdict.
        repair = vetted.error
        try:
            reply = call(build_repair_prompt(prompt, reply=reply, error=repair))
        except Exception as exc:
            log.warning("design-chat repair call failed: %s", exc)
            repair = f"{repair} (repair call failed: {exc})"
        else:
            vetted = _vet_reply(store, ref, kind=kind, reply=reply)
    ops, rationale = vetted.ops, vetted.rationale

    turn = _next_turn_handle(store, str(ref.slug))

    def record(outcome: str) -> None:
        _write_transcript(
            hub,
            kind=kind,
            design_ref=ref,
            turn=turn,
            message=message,
            handles=clicked,
            rationale=rationale,
            ops=ops,
            outcome=outcome,
            repair=repair,
        )

    def rejected(error: str) -> TurnResult:
        record(f"{_REJECTED_PREFIX}{error}")
        return TurnResult(
            applied=False,
            ops=ops,
            rationale=rationale,
            error=error,
            turn=turn,
            repair=repair,
        )

    if vetted.stage == "reply":
        assert vetted.error is not None
        return rejected(vetted.error)
    if not ops:
        # "cannot be expressed as ops" — a valid reply; nothing to apply.
        record(_NOOP_OUTCOME)
        return TurnResult(
            applied=False, ops=[], rationale=rationale, turn=turn, repair=repair
        )
    err = vetted.error
    if kind == "se" and is_auto_apply(ops, kind="se"):
        if err is not None:
            return rejected(err)
        try:
            SeHandler(hub=hub).edit(id=str(ref.slug), ops=ops, turn=turn)
        except (BadInput, NotFound) as exc:
            return rejected(str(exc))
        except IntegrityError:
            return rejected(_CONCURRENT_SAVE_ERROR)
        revision = _revision_of(store, ref.id)
        record(f"applied as revision {revision}")
        return TurnResult(
            applied=True,
            ops=ops,
            rationale=rationale,
            valid=True,
            revision=revision,
            turn=turn,
            repair=repair,
        )

    valid = err is None
    record(f"proposal ({len(ops)} op(s), {'valid' if valid else f'INVALID: {err}'})")
    return TurnResult(
        applied=False,
        ops=ops,
        rationale=rationale,
        proposal=ops,
        valid=valid,
        error=err,
        turn=turn,
        repair=repair,
    )


def apply_proposal(
    hub: Hub,
    *,
    kind: DesignKind,
    slug: str,
    ops: list[dict[str, Any]],
    turn: str | None,
) -> TurnResult:
    """The human Apply on a proposal: the same roster gate, then
    ``StructureHandler.edit`` (version in place — never ``derive``) or
    ``SeHandler.edit``, the revision stamped with the proposing ``turn``."""
    store = _store_of(hub)
    ref = _design_ref(store, kind, slug)
    rejection = vet_ops(ops, kind=kind)
    if rejection is not None:
        return TurnResult(applied=False, error=rejection, turn=turn)
    if not ops:
        return TurnResult(applied=False, error="nothing to apply", turn=turn)
    try:
        if kind == "se":
            SeHandler(hub=hub).edit(id=str(ref.slug), ops=ops, turn=turn)
        else:
            StructureHandler(hub=hub).edit(id=str(ref.slug), ops=ops, turn=turn)
    except (BadInput, NotFound) as exc:
        return TurnResult(applied=False, ops=ops, error=str(exc), turn=turn)
    except IntegrityError:
        return TurnResult(
            applied=False, ops=ops, error=_CONCURRENT_SAVE_ERROR, turn=turn
        )
    return TurnResult(
        applied=True,
        ops=ops,
        valid=True,
        revision=_revision_of(store, ref.id),
        turn=turn,
    )


__all__ = [
    "DESTRUCTIVE_SE_OPS",
    "MAX_DIGEST_ATOMS",
    "SYSTEM_PROMPT",
    "DesignKind",
    "ModelCall",
    "TranscriptTurn",
    "TurnResult",
    "apply_proposal",
    "build_digest",
    "build_prompt",
    "build_repair_prompt",
    "conv_slug",
    "dry_run_se",
    "dry_run_structure",
    "is_auto_apply",
    "op_vocabulary",
    "parse_reply",
    "pending_proposal",
    "roster",
    "run_turn",
    "transcript",
    "vet_ops",
]
