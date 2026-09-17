"""SeHandler — the ``se`` (structural envelope) kind.

An ``se`` design is a slug-addressed ref whose content is a **block tree**
(:mod:`precis_se.ops`/:mod:`precis_se.persist`) — nested building blocks
with cad-DSL spatial envelopes in **metres**, rough poses, read-time
template instancing, first-class linear/polar **arrays**, and the L2
invariant tier: joints (kinematic class × mechanism —
:mod:`precis_se.joints`), named measures with tolerance relations
(:mod:`precis_se.measures`), and loads; slice 4's interrogation ledger
(:mod:`precis_se.notes`) and design-freedom vocabulary (interval
measures, ``origin: user|proposed``, relation ``scale``, the closed unit
registry — :mod:`precis_se.freedom`). Maps onto the verbs as of this
round (se slices 1-3 + 4's store half, docs/backlog/se-kind.md "Ship
order"):

- ``put``    — create/replace a design from a JSON payload
  ``{description?, ops: [...]}`` (``id=`` the design slug). A re-put
  soft-retires the prior blocks and reinserts the new tree (the
  ``structure`` re-put shape). The store-aware ops (the atomic mode's
  ``bind_structure``/``unbind_structure``/``generate``, plus ``realize``)
  are intercepted before the pure ops table sees them —
  :func:`precis_se.atomic.apply.apply_ops_with_atomic`, the
  ``import_fragment`` precedent.
- ``edit``   — apply more ops (``ops=`` or ``text=`` JSON) to an existing
  design's live tree.
- ``get``    — list designs (no ``id``), a design's nested tree TOC
  (``id=slug``, the default view), one block's full record
  (``view='block'``, ``args={'name': ...}``), every block's ports
  (``view='ports'``), measures + stack-up (``view='measures'`` —
  :mod:`precis_se.measures`), the deterministic datum ranking and which
  measures hang off each datum (``view='datums'`` —
  :mod:`precis_se.datums`), feasibility findings with the
  filled-fraction honesty header (``view='validate'`` —
  :mod:`precis_se.validate`), the signed envelope gap between two blocks
  (``view='clearance'``, ``args={'a': ..., 'b': ...}``, the cad kernel
  at metres — the nm clearance view's design, transferred; omit ``args``
  for an all-pairs digest over the design's CONNECTS, worst gap first),
  "does anything collide in ANY declared state" swept across the cross
  product of every state-carrying block's own states (``view='sweep'`` —
  blocktree slice 2's discrete-domain mirror of cad's continuous-joint
  sweep; :func:`precis_se.validate.envelope_overlaps` re-run per
  combination, combo count budget-bounded and never silently truncated),
  or the
  graph-tier DRC report (``view='drc'`` — :mod:`precis_se.drc`: joint
  contradictions, mechanism-implied demands, unresolvable relations,
  the declared-vs-derived axis-travel probe), the bought-item rollup
  (``view='bom'`` — :mod:`precis_se.bom`: quantities multiplied through
  the tree's array multiplicities, priced and massed from the
  ``component`` kind's own spec values), the interrogation ledger with
  open questions first (``view='interview'`` — :mod:`precis_se.notes`),
  or the what-is-still-undecided report (``view='freedom'`` —
  :mod:`precis_se.freedom`, DRC's honest counterpart). The atomic mode
  adds two (:mod:`precis_se.atomic.render`): advisory L4 mechanics
  ceilings (``view='mechanics'``) and a deterministic paper query over the
  design's own vocabulary (``view='literature'``, optional
  ``args={'block': ...}``).
- ``delete`` — soft-retire a whole design.
- ``search`` — find designs by intent over each design's one
  ``card_combined`` chunk; ``search_hits`` opts into the cross-kind
  fan-out (``kind='*'``).

Ships live: the kind carries no per-plugin dark switch. An operator who
needs it off uses ``PRECIS_KINDS_DISABLED``, the one general control,
rather than a private per-kind switch. Direct construction (as in tests) is
unaffected by the flag; it only gates the registry.

See ``docs/backlog/se-kind.md`` for the full design. The agent-facing
skill lands last, after behavior exists (ship order step 7 — a skill
describing target state misdirects agents).
"""

from __future__ import annotations

import itertools
import json
import math
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from numpy.typing import NDArray
from psycopg.types.json import Jsonb

from precis.blocktree.types import parse_template_ref
from precis.cad import dsl as cad_dsl
from precis.cad import relate as cad_relate
from precis.cad.export import ExportError
from precis.cad.graph import Design as CadDesign
from precis.cad.vec import euler_rad_from_matrix as cad_euler_rad
from precis.cad.vec import rotation as cad_rotation
from precis.design import scenarios as design_scenarios
from precis.design import states as design_states
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.format import render_agent_table
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import SearchHit
from precis.utils.units import format_quantity
from precis_se import bom as se_bom
from precis_se import datums as se_datums
from precis_se import drc as se_drc
from precis_se import fasten as se_fasten
from precis_se import freedom as se_freedom
from precis_se import fret, persist
from precis_se import library as se_library
from precis_se import modes as se_modes
from precis_se import notes as se_notes
from precis_se import printing as se_printing
from precis_se import stability as se_stability
from precis_se import validate as se_validate
from precis_se.atomic import render as se_atomic_render
from precis_se.atomic import validate as se_atomic_validate
from precis_se.atomic.apply import apply_ops_with_atomic
from precis_se.identity import AmbiguousLabel, resolve_block
from precis_se.measures import stackup as se_stackup
from precis_se.ops import (
    PortSpec,
    SeBlock,
    SeTree,
    effective_chromophore,
    effective_dof,
    effective_envelope,
    effective_ports,
    resolve_template,
)


class SeHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="se",
        title="Structural envelope",
        description=(
            "A scale-agnostic structural/space-planner design (precis-se "
            "plugin — the one design kind from nano to macro): nested blocks with "
            "cad-DSL envelopes in METRES, poses, read-time template "
            "instancing, and first-class arrays. put/edit take typed ops "
            "(add_block/instance_block/array_block/set_pose/set_envelope/"
            "remove_block/add_port/remove_port/set_port_pose/connect/disconnect/"
            "set_joint/set_load/add_measure/set_measure/remove_measure/"
            "set_mode/set_binding/add_bom/remove_bom/add_note/"
            "remove_note/formfind/declare_threading/remove_threading/"
            "declare_dof/clear_dof/bind_structure/unbind_structure/"
            "generate/realize/set_build_frame/clear_build_frame/"
            "set_chromophore/set_optical_link/set_optics/"
            "declare_states/declare_transitions/set_current_state); "
            "declare_states block= states=[{'name','envelope'?,"
            "'port_pose_overrides'?,'descr'?}] declares a block's discrete "
            "states (a bistable's {loaded,bonded} or a photoswitch's "
            "{trans,cis}) — port_pose_overrides={port: {'direction'?:"
            "[x,y,z], 'pose'?:[dx,dy,dz], 'rot'?:[rx,ry,rz]}} overrides "
            "that port in the state: direction outright, pose/rot as a "
            "rigid DELTA in the block frame (applied only to a port that "
            "carries a pose of its own); add_port/set_port_pose "
            "pose=[x,y,z] rot=[rx,ry,rz] place the port itself in the "
            "block's local frame (m/rad; rot needs pose); "
            "declare_transitions block= transitions=[{'from_state',"
            "'to_state','driver_kind','driver_ref'?,'params'?}] adds "
            "DIRECTED stimulus-labelled edges (driver_kind: light|"
            "reaction|redox|ph|thermal|mechanical); set_current_state "
            "block= state= PERSISTENTLY poses a block into one of its "
            "declared states. "
            "get lists designs or renders one (view='tree'|'block'|"
            "'ports'|'topology'|'measures'|'datums'|'validate'|'clearance'|'sweep'|"
            "'drc'|'bom'|'interview'|'freedom'|'stability'|'mechanics'|"
            "'literature'|'fret'|'print'|'fab'; block takes "
            "args={'name':...}, clearance takes args={'a':...,'b':...} "
            "and runs the cad kernel's signed-distance gap between two "
            "blocks' posed envelopes, or omit args for an all-pairs "
            "clearance digest over the design's CONNECTS, worst gap "
            "first; view='tree'|'block'|'clearance' additionally take "
            "args={'state': {'<block>':'<state name>'}} — a TRANSIENT "
            "pose override for this read only, never written back (use "
            "set_current_state to persist a pose); view='sweep' takes no "
            "args and checks EVERY combination of every state-carrying "
            "block's declared states at once — does anything collide in "
            "any declared state, reusing the same overlap check as "
            "view='validate', reported per combination, with a hard combo "
            "count budget it names rather than silently truncates); "
            "delete soft-retires; "
            "search finds "
            "by intent, or, with wants=, ranks every library block against "
            "a per-attribute wishlist (never a strict filter — "
            "wants={'stimulus':'light','bistable':True,'joining':'CuAAC'}, "
            "each value a target|[lo,hi]|{'target'?,'min'?,'max'?,'tol'?,"
            "'weight'?}; q= then narrows the candidate designs instead of "
            "filtering). connect wires two 'block.port' endpoints; a "
            "joint= is {'class': rigid|revolute|prismatic|cylindrical|"
            "planar|ball|compliant|captive|axial, 'axis'?, 'mechanism'?: "
            "snap|screw|press|key|magnet|bearing|bond|integral|cable, "
            "'params'?}. 'axial' is a pin-ended member whose params "
            "capacity pair decides tie/strut/rod (tension_capacity/"
            "compression_capacity N, free_length m, rate N/m, preload N "
            "tension-positive); view='stability' runs Maxwell/Calladine "
            "over the axial members (rigid / mechanism / "
            "prestress-stabilized, self-stress state reported) and, when "
            "any member declares a preload, checks the declared preloads "
            "against the self-stress space (implied forces on the rest "
            "completed and vetted). The "
            "formfind op solves force-density form-finding over those "
            "members (anchors = objectives.fixed; q_tie/q_strut/q_rod "
            "role defaults +1/-1/+1, per-member q=[{'a','b','q'}] "
            "overrides) and writes the equilibrium poses back stamped "
            "origin='proposed' — by default only already-proposed poses "
            "move; move=[...]|'all' authorizes more. "
            "Loads (set_load): force/torque 3-vectors (N, N·m), duty, "
            "cycles, on blocks or connects; fixed=true|['x','y','z'...] "
            "on a block grounds its translations (stability supports). "
            "Measures (add_measure; "
            "unit m default | count | ratio | deg) carry tolerance "
            "RELATIONS between named measures ({'source':"
            "'block.measure','scale':<×, default 1>,'offset','tol'} + "
            "hard/soft/gauge); prefer declaring an acceptable SET over "
            "forcing a point: min=/max= declare an interval ('bore ≥ "
            "4mm'), and origin= (user|proposed, also on set_envelope/"
            "set_pose) records whose choice a number is — a proposer "
            "revises its own, treats the user's as contract. "
            "view='measures' shows the stack-up evaluation; "
            "view='freedom' lists what is still undecided and by whom. "
            "add_note keeps the design interview durable (kind=question|"
            "answer|decision, re= links a response to its question, "
            "about= anchors blocks/measures); view='interview' renders "
            "it, open questions first. "
            "view='validate' leads with filled-fraction honesty and "
            "warns on undeclared envelope interpenetration; view='drc' "
            "runs the graph tier (joint contradictions, "
            "mechanism-implied demands, unresolvable relations, "
            "declared-vs-derived axis-travel probe). Envelopes reuse the "
            "cad mini-DSL (e.g. 'cyl:r0.02h0.01' = a 2 cm-radius disc) — "
            "every field beyond a block's name is optional (suggestive "
            "by contract; an empty design reads as unfilled, not done). "
            "A thing BOUGHT is never a block: add_bom hangs a "
            "component/part on a block (block=) or a connect (a=/b=) with "
            "a per-occurrence qty, and view='bom' rolls it up through the "
            "array multiplicities with cost/mass from the component kind. "
            "set_mode assigns how a block gets made ('purchase' and "
            "'atomic' today; fdm/laser/stock-cut/cnc-2.5ax/sla are "
            "recordable intent until their implementers ship); "
            "set_binding points a "
            "block's realization at a cad|structure|component|part "
            "row — a mode and a binding that contradict each other are a "
            "view='drc' finding, never a rejected write. "
            "realize block= mode= mints a block's first implementation: a "
            "stored cad design seeded from its effective envelope (its own "
            "local frame, identity pose), bound (kind='cad') and moded — "
            "deterministic, no LLM guessing; refused on an already-bound "
            "block (unbind first) or an envelope-less one (set_envelope "
            "first); an array/template member realizes through its "
            "template. It never mints fasteners — which hardware to buy "
            "is a design decision. "
            "view='print' is the fdm implementer: omit args for one "
            "section per fdm block (mode, realized/unrealized, proposed/"
            "pinned build frame + score, findings); args={'block':...} "
            "for one block's full orientation-candidate table; "
            "args={'block':...,'fmt':'stl'|'3mf'[,'path':...]} writes the "
            "file in the build frame (manifold3d required). "
            "set_build_frame block= down=[x,y,z] pins a block's print "
            "orientation (view='print' still searches every read and "
            "reports how much worse the pin scores); clear_build_frame "
            "block= removes the pin. view='fab' is the design's whole "
            "fabrication plan — one row per implementation-bearing block "
            "(any source: purchase/fdm/atomic/unimplemented), qty "
            "through the array multiplicities, status and the handle "
            "that fetches the thing. "
            "ATOMIC MODE (the merged nm kind): a block whose realization "
            "is chemistry binds a structure design and states its L2 "
            "explicitly — declare_threading a through b (a macrocycle on "
            "an axle), declare_dof block= kind=rotational|translational "
            "axis_ports=[p,q] (two of the block's own ports), add_port "
            "expected_element=/expected_hybridization= for the atom a "
            "stub will attach to, and connect kind='bond'|'interaction' "
            "(a bond needs both ports to afford the role — 'covalent' by "
            "default, or objectives={'role':...}); view='topology' shows "
            "the threading pairs and declared dof together. "
            "bind_structure block= design= ports={port: atom} maps a "
            "block's ports onto atoms of a real structure design, gated "
            "by each port's expected_element (unbind_structure clears "
            "it); generate runs a parametric block factory "
            "(generator='cnt'|'fullerene'|'cone'|'cyclodextrin', "
            "params={...}, name=<new block>) — deterministic geometry, no "
            "LLM guessing: it mints and binds the structure design "
            "itself. view='mechanics' renders advisory (never-gating) L4 "
            "ceilings: min-cut tensile between bond-connected blocks, "
            "Euler buckling for tube-shaped generated blocks, harmonic "
            "strain energy vs VSEPR ideal angles — defect-free continuum "
            "estimates, never validate findings. view='literature' builds "
            "a deterministic (no-LLM) paper-search query from a block's "
            "desc/use/name + connect objective vocabulary (or the whole "
            "design's, with no block named) and runs it against the paper "
            "corpus. The LLM traverses a block tree, never atoms "
            "directly. "
            "OPTICAL DOMAIN (FRET, precis_se.fret): set_chromophore "
            "block= label= dipole=[x,y,z] (block frame) quantum_yield= "
            "lifetime_s= emission=/absorption=[[wavelength_nm,value],...] "
            "attaches a donor/acceptor card to a block (clear=true "
            "removes it); set_optical_link a= b= min_efficiency= "
            "(0,1) [channel=] [reason=] declares an existing connect's "
            "required transfer efficiency (null clears it) — both "
            "endpoints need a chromophore first; set_optics "
            "medium_index= [excitation_nm=] declares the design's "
            "optical context (clear=true removes it; undeclared falls "
            "back to a stated default). view='fret' solves every "
            "chromophore block as a donor against every other at once "
            "(the competing-acceptor branching ratios share one "
            "denominator — never a per-pair number), checks each "
            "declared link's realised efficiency against its "
            "min_efficiency, and flags orientation-nulled/too-close/"
            "negligible declared links plus undeclared crosstalk. "
            "array_block patterns a template block N times "
            "(linear={'count','pitch','axis'} in metres, or "
            "polar={'count','radius','axis'}, axis default +z) — the "
            "block tree stays canonical, members are derived. The "
            "se_propose job, realization and manufacturing modes land in "
            "later slices. The LLM traverses a block tree, never raw "
            "geometry. link records typed relations from the design "
            "(target='kind:identifier', rel= default related-to; "
            "rel='parent' places it into a folder) and view='links' "
            "shows the graph both directions — e.g. link a design to "
            "the quest it serves (rel='serves') or to its sibling "
            "variant."
        ),
        supports_get=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_search=True,
        supports_search_hits=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        placement="artifact",
        corpus_role="none",
        can_own_jobs=False,
        views=(
            "tree",
            "block",
            "ports",
            "topology",
            "measures",
            "datums",
            "validate",
            "clearance",
            "sweep",
            "drc",
            "bom",
            "fasten",
            "interview",
            "freedom",
            "stability",
            "mechanics",
            "literature",
            "fret",
            "print",
            "fab",
        ),
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("se: store required")
        self.hub = hub
        self.store = hub.store
        self.embedder = hub.embedder

    def _apply(
        self, tree: SeTree, ops: list[dict[str, Any]], *, slug: str
    ) -> str | None:
        """Walk one ``put``/``edit``'s ops list. Delegates to
        :func:`~precis_se.atomic.apply.apply_ops_with_atomic` rather than
        calling :func:`~precis_se.ops.apply_ops` directly: the store-aware
        ops (the atomic mode's ``bind_structure``/``unbind_structure``/
        ``generate``, plus ``realize``) are intercepted before the pure
        table ever sees them, and ``add_block``'s deferred ``dof``
        axis-port check runs once the whole list has been walked (that
        function's docstring for both). Returns the store-aware ops'
        echo, or ``None``."""
        return apply_ops_with_atomic(self.store, tree, ops, design_slug=slug)

    def _foreign_resolver(self) -> Callable[[str], SeTree | None]:
        """One cross-design ``template`` resolver
        (:attr:`~precis.blocktree.types.Tree.foreign`, docs/backlog/
        blocktree-library-build-plan.md slice 1) for ONE put/edit/get call,
        so the same foreign design read from many ports, blocks or
        cycle-check hops within that call costs one fetch, not one per hop.

        The tree ``load_tree`` hands back already carries a resolver of its
        own; this one replaces it for the handler's own trees purely to
        widen the cache's scope from "this tree" to "this call" — a ``put``
        builds its tree from nothing (no load to inherit a resolver from),
        and ``edit`` runs the cycle check over a tree plus whatever
        foreign designs it reaches."""
        return persist.foreign_resolver(self.store)

    # ── put ──────────────────────────────────────────────────────────
    def put(
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        title: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='se') requires id= (the design slug)",
                next="put(kind='se', id='caster1', "
                'text=\'{"ops":[{"op":"add_block","name":"fork",'
                '"envelope":"box:w0.04d0.02h0.08"}]}\')',
            )
        slug = str(id).strip()
        payload = _payload(text, args)
        _vet_put_payload(payload)
        ops = payload.get("ops") or []
        if not isinstance(ops, list):
            raise BadInput("put(kind='se') 'ops' must be a list of typed ops")
        description = str(payload.get("description") or "").strip()
        scenario_id = _vet_scenario(self.store, payload.get("scenario"))
        tree = SeTree()
        # own_slug is known from id= before the ref row even exists — needed
        # for a foreign design's template to recognise a hop back into THIS
        # design (cross-design cycle detection, ops._find_instance_cycle).
        tree.own_slug = slug
        tree.foreign = self._foreign_resolver()
        echo = self._apply(tree, ops, slug=slug)
        ttl = (title or slug).strip() or slug
        existing = self.store.get_ref(kind="se", id=slug)
        meta = {"description": description}
        with self.store.tx() as conn:
            if existing is None:
                ref = self.store.insert_ref(
                    kind="se", slug=slug, title=ttl, meta=meta, conn=conn
                )
                created = True
            else:
                ref = existing
                conn.execute(
                    "UPDATE refs SET title = %s, meta = %s WHERE ref_id = %s",
                    (ttl, Jsonb(meta), ref.id),
                )
                created = False
            persist.save_tree(
                self.store,
                ref_id=ref.id,
                tree=tree,
                card_text=_card_text(ttl, description, tree),
                conn=conn,
            )
            # Every block now carries the uid it was saved under — the
            # shared design-core tables' write only becomes possible here.
            _materialize_states(self.store, ref.id, tree, conn=conn, set_by="se.put")
            if scenario_id is not None:
                # Same transaction as the tree: a design and the production
                # context that decides which physics runs on it are one
                # fact. An ABSENT key leaves the existing choice standing —
                # put replaces the block tree, and the scenario is not part
                # of it (design-state-core.md item 3: a design references
                # one scenario).
                design_scenarios.set_design_scenario(
                    self.store, ref.id, scenario_id, set_by="se.put", conn=conn
                )
        # After the tx, not inside it: the projection is derived, and a
        # link-sync hiccup must not roll back a saved design (cad's sync
        # sits outside its write for the same reason).
        persist.sync_realized_by(self.store, ref.id, tree)
        verb = "created" if created else "replaced"
        body = f"# se design '{slug}' {verb}\n\n" + _render_tree(tree, ttl, description)
        if echo:
            body += f"\n\n{echo}"
        return Response(body=body)

    # ── edit ─────────────────────────────────────────────────────────
    def edit(
        self,
        *,
        id: str | int | None = None,
        ops: list[dict[str, Any]] | None = None,
        text: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("edit(kind='se') requires id= (the design slug)")
        ref = self.store.get_ref(kind="se", id=str(id).strip())
        if ref is None:
            raise NotFound(f"se design {id!r} not found")
        op_list = ops
        if op_list is None:
            payload = _payload(text, args)
            op_list = payload.get("ops", payload if isinstance(payload, list) else [])
        if not op_list:
            raise BadInput(
                "edit(kind='se') requires ops=",
                next="edit(kind='se', id="
                f"{str(ref.slug)!r}, "
                "ops=[{'op':'add_block','name':'hub','parent':'fork'}])",
            )
        tree = persist.load_tree(self.store, ref.id)
        tree.own_slug = str(ref.slug)
        tree.foreign = self._foreign_resolver()
        echo = self._apply(tree, op_list, slug=str(ref.slug))
        description = str((ref.meta or {}).get("description") or "").strip()
        ttl = ref.title or str(ref.slug)
        with self.store.tx() as conn:
            persist.save_tree(
                self.store,
                ref_id=ref.id,
                tree=tree,
                card_text=_card_text(ttl, description, tree),
                conn=conn,
            )
            # Same reasoning as put: block_uid only exists once save_tree
            # has run, so the shared design-core write lands in the same
            # transaction right after it, never before.
            _materialize_states(self.store, ref.id, tree, conn=conn, set_by="se.edit")
        persist.sync_realized_by(self.store, ref.id, tree)
        body = f"# se design '{ref.slug}' edited\n\n" + _render_tree(
            tree, ttl, description
        )
        if echo:
            body += f"\n\n{echo}"
        return Response(body=body)

    # ── get ──────────────────────────────────────────────────────────
    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        ref = self.store.get_ref(kind="se", id=str(id).strip())
        if ref is None:
            raise NotFound(f"se design {id!r} not found")
        tree = persist.load_tree(self.store, ref.id)
        tree.own_slug = str(ref.slug)
        tree.foreign = self._foreign_resolver()
        v = (view or "").strip().lower()
        _vet_view_args(v, args)
        state_map = _state_arg_map(self.store, ref.id, tree, args)
        if state_map:
            _apply_state_arg(tree, state_map)
        if v in ("", "tree"):
            description = str((ref.meta or {}).get("description") or "").strip()
            return Response(
                body=_render_tree(tree, ref.title or str(ref.slug), description)
            )
        if v == "block":
            name = (args or {}).get("name")
            if not name or not str(name).strip():
                raise BadInput(
                    "get(kind='se', view='block') requires args={'name': ...}"
                )
            block_name = str(name).strip()
            # A label OR a uid ('#41') — :mod:`precis_se.identity`, the
            # agent-facing half of the uid cutover.
            try:
                node = resolve_block(tree, block_name)
            except AmbiguousLabel as exc:
                raise BadInput(str(exc)) from exc
            if node is None:
                raise NotFound(_block_not_found(tree, block_name))
            return Response(body=_render_block(tree, node, self.store, ref.id))
        if v == "ports":
            return Response(body=_render_ports(tree))
        if v == "topology":
            return Response(body=_render_topology(tree))
        if v == "measures":
            return Response(body=_render_measures(tree))
        if v == "datums":
            return Response(body=_render_datums(tree))
        if v == "validate":
            return Response(body=self._render_validate(tree, ref.id))
        if v == "mechanics":
            return Response(body=se_atomic_render.render_mechanics(self.store, tree))
        if v == "literature":
            block_arg = (args or {}).get("block")
            lit_block: str | None = str(block_arg).strip() if block_arg else None
            if lit_block and lit_block not in tree.blocks:
                raise NotFound(_block_not_found(tree, lit_block))
            return se_atomic_render.render_literature(
                self.hub, self.store, tree, ref, block_name=lit_block
            )
        if v == "clearance":
            return Response(body=_render_clearance(tree, args))
        if v == "sweep":
            return Response(body=_render_sweep(self.store, ref.id, tree))
        if v == "drc":
            body = _render_drc(tree, _scenario_line(self.store, ref.id))
            try:
                body += self._fdm_drc_pointer(tree)
            except se_printing.PrintUnsupported as exc:
                raise Unsupported(
                    str(exc), next="pip install --force-reinstall 'precis-mcp'"
                ) from exc
            return Response(body=body)
        if v == "bom":
            return Response(body=self._render_bom(tree))
        if v == "fasten":
            return Response(body=_render_fasten(tree))
        if v == "interview":
            return Response(body=_render_interview(tree))
        if v == "freedom":
            return Response(body=_render_freedom(tree))
        if v == "stability":
            return Response(body=_render_stability(tree))
        if v == "fret":
            return Response(body=_render_fret(tree))
        if v == "print":
            return self._render_print(tree, ref, args or {})
        if v == "fab":
            return Response(body=self._render_fab(tree))
        if v == "links":
            from precis.handlers._links_render import render_links_view

            return render_links_view(self.store, ref, sense="se")
        raise BadInput(
            f"unknown se view {view!r}",
            next="view='tree' (default, nested TOC) | view='block' "
            "(args={'name':...}) | view='ports' | view='topology' "
            "(atomic mode: threading + declared dof) | view='measures' "
            "(+ stack-up) | view='datums' (datum ranking + which "
            "measures hang off each) | view='validate' | view='clearance' "
            "(args={'a':...,'b':...}, or omit args for an all-pairs "
            "CONNECTS digest) | view='sweep' (does anything collide in ANY "
            "declared state? — the cross product of every state-carrying "
            "block's declared states, budget-bounded) | view='drc' "
            "(graph tier + DOF "
            "probe) | view='bom' (bought items, multiplied through the "
            "arrays, with cost/mass) | view='fasten' (screw joints: grip "
            "stack-up, the holes it stamps — clearance, countersink or "
            "counterbore, and whatever the far end's thread_strategy "
            "names — thread lead, and which driver can reach it) "
            "| view='interview' "
            "(the question/answer/decision ledger, open questions first) "
            "| view='freedom' (what is still undecided, and by whom) "
            "| view='stability' (Maxwell/Calladine rigid / mechanism / "
            "prestress-stabilized over the axial members) "
            "| view='mechanics' (atomic mode: advisory L4 ceilings — "
            "buckling, strain energy, min-cut tensile) | view='literature' "
            "(atomic mode: a deterministic paper query, optional "
            "args={'block':...}) "
            "| view='fret' (optical domain: per-donor FRET budget solved "
            "against every other chromophore block at once, declared-link "
            "PASS/FAIL, orientation/Dexter/negligible/crosstalk findings) "
            "| view='print' (fdm process DRC + STL/3MF export, "
            "args={'block':...,'fmt':...,'path':...}) | view='fab' (the "
            "whole fabrication plan, one row per implementation-bearing "
            "block) | view='links' (the design's link graph, both "
            "directions)",
        )

    def _render_validate(self, tree: SeTree, ref_id: int) -> str:
        """``view='validate'`` — :mod:`precis_se.validate`'s findings plus
        :func:`precis_se.atomic.validate.validate_atomic`'s, under the
        filled-fraction honesty header, on BOTH the clean and the findings
        branch (a fresh scaffold trivially has no findings, and a bare
        check-mark would misread as done).

        Store-aware for the atomic half only: the chemistry checks need
        every bound ``structure`` design hydrated
        (:func:`precis_se.atomic.render.hydrate_bound_scenes` — the
        "assemble in the view path, keep the checker pure" split), and a
        design with no ``structure`` binding anywhere hydrates nothing, so
        a plain se design pays one set-comprehension for the atomic tier.

        The header also records **which scenario governed** these checks
        (:func:`_scenario_line`) — a verdict whose production context isn't
        stated can't be re-read later."""
        findings = list(se_validate.validate(tree))
        bound_scenes, bound_full_scenes = se_atomic_render.hydrate_bound_scenes(
            self.store, tree
        )
        findings.extend(
            se_atomic_validate.validate_atomic(
                tree,
                bound_scenes=bound_scenes,
                bound_full_scenes=bound_full_scenes,
            )
        )
        header_lines = [_fill_fraction_line(tree), _scenario_line(self.store, ref_id)]
        atomic_line = se_atomic_render.atomic_fill_line(tree)
        if atomic_line:
            header_lines.append(atomic_line)
        try:
            fdm_status = self._fdm_print_status(tree)
        except se_printing.PrintUnsupported as exc:
            raise Unsupported(
                str(exc), next="pip install --force-reinstall 'precis-mcp'"
            ) from exc
        fdm_checked = sum(1 for _, r in fdm_status if r.printed is not None)
        header_lines.append(
            f"{fdm_checked}/{len(fdm_status)} fdm block(s) print-checked"
        )
        fill_block = "\n".join(header_lines)
        if not findings:
            return f"✓ no validator findings\n{fill_block}"
        n_error = sum(1 for f in findings if f.severity == "error")
        n_warn = sum(1 for f in findings if f.severity == "warn")
        n_info = sum(1 for f in findings if f.severity == "info")
        rows = [
            {
                "severity": f.severity,
                "rule": f.rule,
                "subject": f.subject,
                "detail": f.detail,
            }
            for f in findings
        ]
        info_suffix = f", {n_info} info" if n_info else ""
        return (
            f"# {n_error} error(s), {n_warn} warning(s){info_suffix}\n"
            f"{fill_block}\n\n"
            + render_agent_table(rows, schema=["severity", "rule", "subject", "detail"])
        )

    def _render_bom(self, tree: SeTree) -> str:
        """The bought-item rollup: one row per ``component``/``part``,
        quantities multiplied through the tree's array multiplicities
        (:mod:`precis_se.bom`), with unit cost and mass resolved from the
        ``component`` kind's own sourced spec values.

        The one place in se that touches another kind's store surface, and
        deliberately read-only: a BOM is a *view over* the component store,
        never a second copy of it. An item that isn't in the store yet
        renders `—` and is counted as uncovered, exactly as an unpriced
        component does in `component`'s own BOM."""
        totals = se_bom.rollup(tree)
        lines = ["# BOM — bought items (quantities include array multiplicity)"]
        if not totals:
            lines.append("")
            lines.append(
                "(nothing bought yet — add_bom hangs a component/part off a "
                "block or a connect)"
            )
            return "\n".join(lines)

        rows: list[dict[str, Any]] = []
        total_cost = 0.0
        cost_covered = 0
        total_mass = 0.0
        mass_covered = 0
        unknown: list[str] = []
        unresolved: list[str] = []

        for total in totals:
            resolved = self._resolve_item(total.item_kind, total.item)
            qty_display = "?" if total.total is None else f"{total.total:g}"
            if total.mixed_uom:
                # Summing metres and each is not a quantity — say so
                # instead of picking one and printing a number.
                qty_display += f" ⚠ mixed uom: {', '.join(sorted(total.uoms))}"
            elif total.uom:
                qty_display += f" {total.uom}"
            cost_display = "—"
            mass_display = "—"
            item_display = f"{total.item_kind}:{total.item}"
            if resolved is None:
                unknown.append(item_display)
            else:
                ref_id, label = resolved
                item_display = f"{label} ({item_display})"
                if total.item_kind == "component":
                    cost_num = self._spec_number(ref_id, "unit_cost")
                    if cost_num is not None:
                        cost_display = f"{cost_num:g}"
                        if total.total is not None:
                            total_cost += total.total * cost_num
                            cost_covered += 1
                    mass_num = self._spec_number(ref_id, "mass")
                    if mass_num is not None:
                        mass_display = f"{mass_num:g}"
                        if total.total is not None:
                            total_mass += total.total * mass_num
                            mass_covered += 1
            if total.unresolved:
                unresolved.extend(
                    f"{target} ({total.item_kind}:{total.item})"
                    for target, _qty, occ in total.contributions
                    if occ is None
                )
            rows.append(
                {
                    "item": item_display,
                    "qty": qty_display,
                    "unit_cost": cost_display,
                    "unit_mass": mass_display,
                    "for": ", ".join(
                        f"{target} {qty:g}×{'?' if occ is None else occ}"
                        for target, qty, occ in total.contributions
                    ),
                }
            )

        n = len(rows)
        lines.append(
            render_agent_table(
                rows, schema=["item", "qty", "unit_cost", "unit_mass", "for"]
            )
        )
        lines.append("")
        lines.append(
            f"unit_cost total: {total_cost:g} — priced: {cost_covered} of {n} item(s)"
        )
        lines.append(
            f"mass total: {total_mass:g} — massed: {mass_covered} of {n} item(s)"
        )
        if unknown:
            lines.append("")
            lines.append(
                f"⚠ not in the store: {', '.join(sorted(set(unknown)))} — "
                "totals exclude them (put the component first, or fix the slug)"
            )
        if unresolved:
            lines.append(
                f"⚠ target not in the tree: {', '.join(sorted(set(unresolved)))} "
                "— quantity unknown, excluded from totals"
            )
        return "\n".join(lines)

    def _spec_number(self, ref_id: int, spec_id: str) -> float | None:
        """One component's current numeric value for ``spec_id``, through
        the component store's own single "current value" authority
        (``component_current_spec_value``) — so an se BOM and a
        ``component`` BOM can never disagree about a price."""
        row = self.store.component_current_spec_value(ref_id, spec_id)
        if row is None:
            return None
        value = row.get("value_num")
        return None if value is None else float(value)

    def _resolve_item(self, item_kind: str, item: str) -> tuple[int, str] | None:
        """``(ref_id, label)`` for a BOM line's item, or ``None`` when the
        store has no such row. Never raises — an unresolvable item is a
        *report*, not a failed read."""
        try:
            ref = self.store.get_ref(kind=item_kind, id=item)
        except Exception:  # pragma: no cover — defensive: a kind may be dark
            return None
        if ref is None:
            return None
        return ref.id, str(ref.title or ref.slug or ref.id)

    def _bom_cost_mass_footer(self, tree: SeTree) -> str | None:
        """The same unit_cost/mass total numbers ``view='bom'`` computes
        (:meth:`_render_bom`) — reused by ``view='fab'``'s footer when any
        purchase row exists, se-print-implementer.md's "the view='bom'
        cost/mass line": one BOM total, never two truths. ``None`` when
        nothing is bought at all (``view='bom'``'s own empty case)."""
        totals = se_bom.rollup(tree)
        if not totals:
            return None
        total_cost = 0.0
        cost_covered = 0
        total_mass = 0.0
        mass_covered = 0
        for total in totals:
            if total.item_kind != "component" or total.total is None:
                continue
            resolved = self._resolve_item(total.item_kind, total.item)
            if resolved is None:
                continue
            ref_id, _label = resolved
            cost_num = self._spec_number(ref_id, "unit_cost")
            if cost_num is not None:
                total_cost += total.total * cost_num
                cost_covered += 1
            mass_num = self._spec_number(ref_id, "mass")
            if mass_num is not None:
                total_mass += total.total * mass_num
                mass_covered += 1
        n = len(totals)
        return (
            f"unit_cost total: {total_cost:g} — priced: {cost_covered} of {n} item(s)\n"
            f"mass total: {total_mass:g} — massed: {mass_covered} of {n} item(s)"
        )

    def _fdm_print_status(
        self, tree: SeTree
    ) -> list[tuple[str, se_printing.BlockPrintReport]]:
        """Every fdm-family (ordinary, template-owning) block's
        :class:`~precis_se.printing.BlockPrintReport`, name-sorted — the
        one store-aware pass ``view='validate'``'s header line and
        ``view='drc'``'s pointer line both read, so the two counts can
        never drift apart."""
        out: list[tuple[str, se_printing.BlockPrintReport]] = []
        for name in sorted(tree.blocks):
            node = tree.blocks[name]
            if node.template is not None:
                continue
            family = se_modes.family_of(node.mode)
            if family is None or family.key != "fdm":
                continue
            report = se_printing.report_for(tree, name, cad_store_reader=self.store)
            assert report is not None  # already gated on family.key == 'fdm'
            out.append((name, report))
        return out

    def _fdm_drc_pointer(self, tree: SeTree) -> str:
        """``view='drc'``'s appended pointer (se-print-implementer.md): one
        line per fdm block naming its process-finding count and
        ``view='print'`` — the design-DRC view stays design-tier, but a red
        print is never missed."""
        status = self._fdm_print_status(tree)
        if not status:
            return ""
        lines = ["", "## fdm print-check (view='print' for the full report)"]
        for name, report in status:
            n = len(report.findings)
            lines.append(
                f"- {name}: {n} process finding(s) — "
                f"view='print' args={{'block': {name!r}}}"
            )
        return "\n" + "\n".join(lines)

    def _render_print(self, tree: SeTree, ref: Any, args: dict[str, Any]) -> Response:
        """``view='print'`` — Engine 3's report
        (:mod:`precis_se.printing`): no args renders one section per
        fdm-family block; ``block=`` alone renders one block's full
        orientation-candidate table; ``block=``+``fmt=`` writes the file
        (``manifold3d`` required — the existing ``Unsupported`` + install
        hint on a broken venv, mirroring ``handlers/cad.py``)."""
        block_arg = args.get("block")
        block = str(block_arg).strip() if block_arg else ""
        fmt_arg = args.get("fmt")
        if fmt_arg is not None and not block:
            raise BadInput("view='print': fmt= needs block= (which block to export)")
        if not block:
            return Response(body=self._render_print_all(tree))
        node = tree.blocks.get(block)
        if node is None:
            raise NotFound(_block_not_found(tree, block))
        if node.template is not None:
            raise BadInput(
                f"block {block!r} is an instance (of {node.template!r}) — print "
                f"status lives on the template; view='print' "
                f"args={{'block': {node.template!r}}} instead"
            )
        try:
            report = se_printing.report_for(tree, block, cad_store_reader=self.store)
        except se_printing.PrintUnsupported as exc:
            raise Unsupported(
                str(exc), next="pip install --force-reinstall 'precis-mcp'"
            ) from exc
        if report is None:
            raise BadInput(
                f"block {block!r}'s resolved mode family isn't fdm — "
                "view='print' only covers fdm blocks; view='fab' covers "
                "every source"
            )
        if fmt_arg is None:
            return Response(body=_render_print_block(report))
        fmt = str(fmt_arg).strip().lower()
        if fmt not in ("stl", "3mf"):
            raise BadInput(
                f"view='print': fmt= must be 'stl' or '3mf', got {fmt_arg!r}"
            )
        if report.printed is None or report.chosen_down is None:
            raise BadInput(
                f"block {block!r} has nothing to export "
                f"({'unrealized' if report.printed is None else 'net-empty solid'})"
            )
        raw_path = args.get("path")
        out = (
            Path(str(raw_path)).expanduser()
            if raw_path
            else Path(tempfile.gettempdir()) / f"{ref.slug}-{block}.{fmt}"
        )
        try:
            path = se_printing.write_mesh(report.printed, report.chosen_down, fmt, out)
        except se_printing.PrintUnsupported as exc:
            raise Unsupported(
                str(exc), next="pip install --force-reinstall 'precis-mcp'"
            ) from exc
        except ExportError as exc:
            raise BadInput(str(exc)) from exc
        size = path.stat().st_size
        error_findings = [f for f in report.findings if f.severity == "error"]
        lines = [
            f"# exported {ref.slug}:{block} → {fmt.upper()} (manifold3d mesh)",
            f"{path}  ({size:,} bytes)",
            f"build frame: down={se_printing.format_down(report.chosen_down)} "
            f"({'pinned' if report.pinned else 'proposed'})",
        ]
        if error_findings:
            lines.append("")
            lines.append(
                "⚠ exported WITH error-severity finding(s) — a file "
                "never leaves without its warnings:"
            )
            lines.append(_findings_table(error_findings))
        return Response(body="\n".join(lines))

    def _render_print_all(self, tree: SeTree) -> str:
        """``view='print'`` with no args — one section per fdm-family
        block, then a pointer to ``view='fab'`` for everything else."""
        names = sorted(
            name
            for name, node in tree.blocks.items()
            if node.template is None
            and (fam := se_modes.family_of(node.mode)) is not None
            and fam.key == "fdm"
        )
        if not names:
            return (
                "# view='print' — no fdm-mode block in this design\n"
                "(set_mode block=... mode='fdm/<material>' first, or "
                "view='fab' for the whole fabrication plan)"
            )
        sections = []
        for name in names:
            try:
                report = se_printing.report_for(tree, name, cad_store_reader=self.store)
            except se_printing.PrintUnsupported as exc:
                raise Unsupported(
                    str(exc), next="pip install --force-reinstall 'precis-mcp'"
                ) from exc
            assert report is not None
            sections.append(_render_print_summary(report))
        return (
            "\n\n".join(sections)
            + "\n\nNext: view='fab' for the whole fabrication plan."
        )

    def _render_fab(self, tree: SeTree) -> str:
        """``view='fab'`` — the design's whole fabrication plan
        (se-print-implementer.md): one row per implementation-bearing
        block, any source, pointing at each row's own handle. Never
        exports itself — every row's handle is the export route."""
        occ = se_bom.design_occurrences(tree)
        rows: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for name in sorted(tree.blocks):
            node = tree.blocks[name]
            if node.template is not None:
                continue
            qty = occ.get(name, 0)
            mode = node.mode
            family = se_modes.family_of(mode)
            if mode is None:
                key, source, status, handle = "unassigned", "—", "—", "set_mode"
            elif family is None:
                key, source = "unknown", mode
                status, handle = "⚠ unknown family — set_mode to repair", "set_mode"
            elif family.key == "purchase":
                key, source = "purchase", mode
                if node.bound_kind in ("component", "part") and node.bound:
                    status = f"bound: {node.bound_kind}:{node.bound}"
                    handle = f"{node.bound} (view='bom')"
                else:
                    status, handle = "no item", "set_binding"
            elif family.key == "atomic":
                key, source = "atomic", mode
                if node.bound_kind == "structure" and node.bound:
                    status = f"bound: {node.bound}"
                    handle = f"view='block' args={{'name': {name!r}}}"
                else:
                    status, handle = "unbound", "bind_structure / generate"
            elif family.key == "fdm":
                key, source = "fdm", mode
                try:
                    report = se_printing.report_for(
                        tree, name, cad_store_reader=self.store
                    )
                except se_printing.PrintUnsupported as exc:
                    raise Unsupported(
                        str(exc), next="pip install --force-reinstall 'precis-mcp'"
                    ) from exc
                assert report is not None
                status, handle = _fab_fdm_cell(name, mode, report)
            elif not family.implemented:
                key, source = family.key, mode
                status, handle = (
                    "planned, not checked",
                    f"{family.key} — no implementer yet",
                )
            else:  # pragma: no cover - every implemented family is handled above
                key, source, status, handle = family.key, mode, "—", "—"
            counts[key] = counts.get(key, 0) + 1
            rows.append(
                {
                    "block": name,
                    "qty": f"{qty:g}",
                    "source": source,
                    "status": status,
                    "handle": handle,
                }
            )
        if not rows:
            return "# view='fab' — no blocks in this design"
        lines = [
            "# view='fab' — the fabrication plan (one row per "
            "implementation-bearing block)",
            render_agent_table(
                rows, schema=["block", "qty", "source", "status", "handle"]
            ),
            "",
            "totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
        ]
        footer = self._bom_cost_mass_footer(tree)
        if footer:
            lines.append(footer)
        return "\n".join(lines)

    # ── delete ───────────────────────────────────────────────────────
    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='se') requires id= (the design slug)")
        ref = self.store.get_ref(kind="se", id=str(id).strip())
        if ref is None:
            raise NotFound(f"se design {id!r} not found")
        n = persist.retire_design(self.store, ref.id)
        return Response(body=f"retired se design '{ref.slug}' ({n} block(s))")

    # ── link ─────────────────────────────────────────────────────────
    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove a typed relation from this design to another ref —
        the quest/todo it serves (``rel='serves'``), a sibling variant
        (``rel='related-to'``), a motivating paper (``rel='cites'``); any
        registered relation works (gr332020: designs must not live outside
        the ref graph). The reserved virtual ``rel='parent'`` is folder
        placement — a ``refs.parent_id`` write, never a stored link.
        ``view='links'`` renders the graph both directions."""
        from precis.handlers._link_tag_ops import (
            apply_link_ops,
            format_link_tag_ack,
            require_link_target,
            validate_link_mode,
        )
        from precis.handlers._placement import RESERVED_PARENT_REL, place_ref
        from precis.handlers._slug_ref_shared import resolve_live_slug_ref

        if rel == RESERVED_PARENT_REL:
            ref = resolve_live_slug_ref(self.store, kind="se", id=str(id).strip())
            return place_ref(self.store, kind="se", ref=ref, target=target, mode=mode)
        target = require_link_target("se", target)
        validate_link_mode(mode)
        ref = resolve_live_slug_ref(self.store, kind="se", id=str(id).strip())
        n_added, n_removed = apply_link_ops(
            self.store,
            ref.id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.spec.kind,
                ref_label=str(ref.slug),
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # ── search ───────────────────────────────────────────────────────
    def search(
        self,
        *,
        q: str | None = None,
        mode: str | None = None,
        page_size: int = 20,
        wants: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        # Ranked library search (blocktree-library-build-plan.md §Slice 4):
        # `wants=` makes `q=` optional — with one, the card search narrows
        # the CANDIDATE designs; a narrow to zero falls back to the whole
        # library and says so, rather than reading as "no matches" (this
        # surface is never a strict filter). Checked before the q-only
        # BadInput below, on purpose — that guard is unchanged for the
        # plain-search caller.
        if wants is not None:
            narrowed_slugs: set[str] | None = None
            narrow_note = ""
            if q is not None and str(q).strip():
                triples = self._card_search(
                    str(q), query_vec=None, mode=mode, page_size=500
                )
                slugs = {ref.slug for _b, ref, _s in triples}
                if slugs:
                    narrowed_slugs = slugs
                    narrow_note = f"(q={q!r} narrowed to {len(slugs)} design(s))"
                else:
                    narrow_note = (
                        f"(q={q!r} matched no designs — showing the whole library)"
                    )
            return Response(
                body=se_library.render_search(
                    self.store,
                    wants=wants,
                    q=q,
                    narrowed_slugs=narrowed_slugs,
                    narrow_note=narrow_note,
                    page_size=page_size,
                )
            )
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='se') requires q=",
                next="search(kind='se', q='caster fork wheel')",
            )
        triples = self._card_search(
            str(q), query_vec=None, mode=mode, page_size=page_size
        )
        if not triples:
            return Response(
                body=f"no se designs match {q!r}\n\n"
                "Next: widen with mode='semantic', or add a 'description' "
                "to a design so it's findable by purpose."
            )
        lines = [f"# {len(triples)} se design(s) for {q!r}"]
        for _block, ref, _score in triples:
            desc = (ref.meta or {}).get("description") or ""
            lines.append(f"- {ref.slug}  {ref.title}  {desc}".rstrip())
        return Response(body="\n".join(lines))

    def _card_search(
        self,
        q: str,
        *,
        query_vec: list[float] | None,
        mode: str | None,
        page_size: int,
    ) -> list[Any]:
        if not (q and q.strip()):
            return []
        if (mode or "").strip().lower() == "lexical":
            query_vec = None
        elif query_vec is None:
            query_vec = embed_query(self.embedder, q)
        return self.store.chunks.search_chunks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="se",
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            card_kinds=("card_combined",),
        )

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        page_size: int = 10,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> list[SearchHit]:
        triples = self._card_search(
            q, query_vec=query_vec, mode=mode, page_size=page_size
        )
        self.store.chunks.bump_salience([b.id for b, _r, _s in triples])
        out: list[SearchHit] = []
        for block, ref, score in triples:
            text = (getattr(block, "text", "") or "").strip()
            preview = text if len(text) <= 200 else text[:199].rstrip() + "…"
            out.append(
                SearchHit(
                    score=float(score),
                    kind="se",
                    title=ref.title or ref.slug or "",
                    preview=preview,
                    slug=ref.slug,
                    ref_id=ref.id,
                    dedupe_key=f"se:{ref.slug or ref.id}",
                )
            )
        return out

    # ── helpers ──────────────────────────────────────────────────────
    def _render_list(self) -> Response:
        refs = self.store.list_refs(kind="se", order_by="id_desc", limit=50)
        if not refs:
            return Response(
                body="no se designs yet\n\nNext: put(kind='se', id='caster1', "
                'text=\'{"ops":[{"op":"add_block","name":"fork"}]}\')'
            )
        lines = [f"# {len(refs)} se design(s)"]
        for r in refs:
            desc = (r.meta or {}).get("description") or ""
            lines.append(f"- {r.slug}  {desc}".rstrip())
        return Response(body="\n".join(lines))


# ── payload / rendering (module-level, no store access) ────────────────


def _payload(text: str | None, args: dict[str, Any] | None) -> dict[str, Any]:
    if args:
        return dict(args)
    if text and text.strip():
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BadInput(f"se payload must be JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise BadInput("se payload must be a JSON object {description?, ops}")
        return obj
    return {}


#: The only top-level keys ``put`` consumes.
_PUT_PAYLOAD_KEYS = frozenset({"description", "ops", "scenario"})


def _vet_scenario(store: Any, raw: Any) -> str | None:
    """``put``'s ``scenario`` key → a scenario id that exists, or ``None``
    when the caller didn't name one.

    se's first rental of the shared design core
    (:mod:`precis.design.scenarios`, docs/backlog/design-state-core.md —
    rented exactly as the cad kernel is, no new kind and no verb of its
    own). Checked here, before anything is written, because an unknown
    scenario id would otherwise land as a silent no-governance design: the
    whole point of the scenario is that validate can say which production
    context its verdict assumed."""
    if raw is None:
        return None
    scenario_id = str(raw).strip()
    if not scenario_id:
        return None
    if design_scenarios.get_scenario(store, scenario_id) is None:
        known = " | ".join(
            s.scenario_id for s in design_scenarios.list_scenarios(store)
        )
        raise BadInput(
            f"unknown scenario {scenario_id!r} — known: {known}",
            next="put(kind='se', id='caster1', text='{\"scenario\": "
            '"prototype", "ops": [...]}\')',
        )
    return scenario_id


def _scenario_line(store: Any, ref_id: int) -> str:
    """The one-line "which production context governed this verdict"
    header carried by ``view='validate'`` and ``view='drc'``.

    No scenario is a real answer, not a gap to paper over (design-state-
    core.md item 3): a design sketched before anyone decided how many to
    build has none, and the honest line says so rather than implying a
    default governed the run."""
    scenario = design_scenarios.design_scenario(store, ref_id)
    if scenario is None:
        return (
            "scenario: none chosen — no production context governed these "
            "checks (set one with put's scenario= key)"
        )
    bits = [f"scenario: {scenario.scenario_id}"]
    if scenario.quantity is not None:
        bits.append(f"{scenario.quantity} off")
    env = scenario.service_environment
    if env is not None:
        bits.append(
            f"lifetime checks {'ON' if scenario.lifetime_checks else 'off'} "
            f"({env.env_id})"
        )
    if scenario.objective_weights:
        weights = ", ".join(
            f"{k} {v:g}" for k, v in sorted(scenario.objective_weights.items())
        )
        bits.append(f"weights: {weights}")
    return " · ".join(bits)


def _materialize_states(
    store: Any, ref_id: int, tree: SeTree, *, conn: Any, set_by: str
) -> None:
    """Write this call's ``declare_states``/``declare_transitions``/
    ``set_current_state`` ops (:mod:`precis_se.ops`) into the shared
    design-core tables (:mod:`precis.design.states`) — blocktree slice 2's
    second rental of the shared design core, the same posture as
    :func:`_vet_scenario`.

    Deliberately run *after* ``persist.save_tree`` (both ``put`` and
    ``edit`` call this once the save has returned, in the same
    transaction) because the shared tables key on ``block_uid``, which
    only exists once that save has minted or adopted one for every block —
    ``node.pending_states``/``pending_transitions``/
    ``pending_current_state`` (set by the three ops, ``None`` when none of
    them ran for that block) is exactly the payload this was waiting to
    write. States land before transitions before current-state, block by
    block: a call that declares states and transitions (or poses into a
    just-declared state) for the same block in one shot must see its own
    new states already written before its transitions' endpoints are
    checked against the shared FK, or before ``set_current_state`` can
    validate against them."""
    for node in tree.blocks.values():
        if (
            node.pending_states is None
            and node.pending_transitions is None
            and node.pending_current_state is None
        ):
            continue
        assert node.uid is not None, (
            "save_tree mints a uid for every block before this runs"
        )
        uid: int = node.uid
        if node.pending_states is not None:
            states = [
                design_states.BlockState(
                    block_uid=uid,
                    name=s["name"],
                    envelope=s["envelope"],
                    port_pose_overrides=s["port_pose_overrides"],
                    descr=s["descr"],
                )
                for s in node.pending_states
            ]
            try:
                design_states.set_states(store, ref_id, uid, states, conn=conn)
            except design_states.StateError as exc:
                raise BadInput(f"declare_states: {exc}") from exc
        if node.pending_transitions is not None:
            transitions = [
                design_states.Transition(
                    block_uid=uid,
                    from_state=t["from_state"],
                    to_state=t["to_state"],
                    driver_kind=t["driver_kind"],
                    driver_ref=t["driver_ref"],
                    params=t["params"],
                )
                for t in node.pending_transitions
            ]
            try:
                design_states.set_transitions(
                    store, ref_id, uid, transitions, conn=conn
                )
            except design_states.StateError as exc:
                raise BadInput(f"declare_transitions: {exc}") from exc
        if node.pending_current_state is not None:
            # No StateError to catch here — set_current_state itself never
            # raises one (design/states.py); an unknown state name would
            # otherwise surface as a raw FK violation, so the known names
            # are checked here instead, se's own rejection-message style
            # (name what IS declared).
            declared = {
                s.name for s in design_states.states_for(store, ref_id, uid, conn=conn)
            }
            if node.pending_current_state not in declared:
                known = ", ".join(sorted(declared)) or "(none — declare_states first)"
                raise BadInput(
                    f"set_current_state: block {node.name!r} has no state "
                    f"{node.pending_current_state!r} — declared: {known}"
                )
            design_states.set_current_state(
                store,
                ref_id,
                uid,
                node.pending_current_state,
                set_by=set_by,
                conn=conn,
            )


def _vet_put_payload(payload: dict[str, Any]) -> None:
    """Reject an unrecognised top-level ``put`` payload shape loudly,
    before any op is applied or anything is written — ``put`` is a full
    replace, so a payload that isn't the ``{description?, ops}`` shape
    (e.g. a caller's own ``{blocks, connects}`` sketch, mistaken for se's
    op-list vocabulary) must never be quietly discarded as a no-op replace
    that empties a real design (gripe 334778)."""
    strays = sorted(set(payload) - _PUT_PAYLOAD_KEYS)
    if strays:
        known = " | ".join(sorted(_PUT_PAYLOAD_KEYS))
        raise BadInput(
            f"put(kind='se') payload has unrecognised key(s) "
            f"{', '.join(strays)} — valid top-level keys: {known}. put is "
            "a full replace, so an unrecognised shape is rejected rather "
            "than silently emptying the design"
        )


def _card_text(title: str, description: str, tree: SeTree) -> str:
    """The one embeddable summary per design — title + block names + every
    block's desc/use text + the design's own description, so
    ``search(kind='se')`` lands on intent."""
    names = ", ".join(sorted(tree.blocks)) or "(no blocks yet)"
    bits = [b.descr for b in tree.blocks.values() if b.descr]
    bits += [b.use for b in tree.blocks.values() if b.use]
    port_bits = [
        f"{port.name}({', '.join(port.roles)})" if port.roles else port.name
        for block in tree.blocks.values()
        for port in block.ports.values()
    ]
    intent = f" {description}" if description else ""
    body = f" {' '.join(bits)}" if bits else ""
    ports = f" Ports: {', '.join(port_bits)}." if port_bits else ""
    return (
        f"{title} (structural envelope design).{intent} Blocks: {names}.{body}{ports}"
    )


def _fmt3(v: list[float]) -> str:
    return ", ".join(f"{x:g}" for x in v)


def _fmt_rot3(v: list[float]) -> str:
    """A 3-vector of SI-radian angles, rendered in degrees — the angle
    sibling of :func:`_fmt3` (never a bare-radian tuple, which would
    misread as degrees, the units-policy-cutover angle ruling)."""
    return ", ".join(format_quantity(float(x), "angle") for x in v)


def _array_label(spec: dict[str, Any]) -> str:
    """The compact array marker on a tree/block line — e.g.
    ``×6 polar r=0.04 axis=[0, 0, 1]`` / ``×10 linear pitch=0.005
    axis=[0, 0, 1]`` (metres)."""
    axis = spec.get("axis") or [0.0, 0.0, 1.0]
    if spec.get("kind") == "linear":
        return (
            f"×{spec.get('count')} linear pitch={spec.get('pitch'):g} "
            f"axis=[{_fmt3(axis)}]"
        )
    return f"×{spec.get('count')} polar r={spec.get('radius'):g} axis=[{_fmt3(axis)}]"


#: Atomic-mode dof kinds, abbreviated for the one-line tree marker.
_DOF_ABBR = {"rotational": "rot", "translational": "trans"}


def _dof_marker(dof: dict[str, Any] | None) -> str:
    if not dof:
        return ""
    kind = str(dof.get("kind") or "?")
    return f"[{_DOF_ABBR.get(kind, kind)}]"


def _fmt_expected(element: str | None, hybridization: str | None) -> str:
    """The ``expected`` cell of a ports table — the chemistry an atomic
    port demands of the atom it will attach to."""
    bits = [b for b in (element, hybridization) if b]
    return " ".join(bits) if bits else "—"


def _fmt_bound(port: PortSpec) -> str:
    """The port→atom map cell (:class:`~precis_se.ops.PortSpec`'s
    ``bound_design``/``bound_atom`` — the "one fact, two projections"
    port's atom-side half, set by ``bind_structure``)."""
    if port.bound_design and port.bound_atom:
        return f"{port.bound_design}:{port.bound_atom}"
    return "—"


#: The atomic mode's own port columns, appended to a ports table only
#: when :func:`_shows_atomic_ports` says something fills them.
_ATOMIC_PORT_SCHEMA = ("expected", "bound")

#: The port-pose column, appended on the same conditional rule as
#: :data:`_ATOMIC_PORT_SCHEMA`: a design whose ports carry no pose (the
#: common case — the slot is nullable by design) renders exactly as it did
#: before this column existed, rather than growing a column of dashes.
_POSE_PORT_SCHEMA = ("pose",)


def _shows_atomic_ports(ports: Iterable[PortSpec]) -> bool:
    """Whether a ports table should carry the two atomic columns —
    mode-scoped help (nm-se-merge.md): a frame of bolted extrusions must
    not grow ``expected``/``bound`` columns of dashes, and an atomic
    design must not hide its chemistry."""
    return any(
        p.expected_element or p.expected_hybridization or p.bound_design for p in ports
    )


def _shows_port_poses(ports: Iterable[PortSpec]) -> bool:
    """Whether a ports table should carry the ``pose`` column — the same
    mode-scoped rule the atomic two get: the slot is nullable on purpose
    (:class:`~precis.blocktree.types.Port`), so a design that never filled
    it must not grow a column of dashes."""
    return any(p.pose is not None for p in ports)


def _fmt_port_pose(port: PortSpec) -> str:
    """The ``pose`` cell — the port's OWN origin (and frame, when it has
    one) in the block's local frame, with its provenance, e.g.
    ``[0, 0, 0.004] rot [0, 1.5708, 0] · declared``. ``rot`` is omitted
    rather than printed as zeros when the port carries none: unrotated and
    "no rotation stated" mean the same thing, unlike ``pose``, whose
    absence is exactly what the dash is there to report."""
    if port.pose is None:
        return "—"
    rot = f" rot [{_fmt3(port.rot)}]" if port.rot is not None else ""
    return f"[{_fmt3(port.pose)}]{rot} · {port.pose_source or '?'}"


def _port_cells(port: PortSpec, *, atomic: bool, posed: bool = False) -> dict[str, str]:
    """One ports-table row's cells for ``port``. The atomic two and the
    pose one ride along only when the table declares them — a row key
    outside the schema would still reach the JSON backend, which ignores
    ``schema``."""
    cells = {
        "port": port.name,
        "roles": ", ".join(port.roles) or "—",
        "direction": f"[{_fmt3(port.direction)}]" if port.direction else "—",
        "annotations": json.dumps(port.annotations) if port.annotations else "—",
    }
    if posed:
        cells["pose"] = _fmt_port_pose(port)
    if atomic:
        cells["expected"] = _fmt_expected(
            port.expected_element, port.expected_hybridization
        )
        cells["bound"] = _fmt_bound(port)
    return cells


def _block_line(tree: SeTree, node: SeBlock) -> str:
    parts = [node.name]
    if node.template and node.array:
        parts.append(f"(array of {node.template} {_array_label(node.array)})")
    elif node.template:
        parts.append(f"(instance of {node.template})")
    env = effective_envelope(tree, node)
    if env:
        marker = f" (from {node.template})" if node.template else ""
        parts.append(f"env={env}{marker}")
    parts.append(f"pose=[{_fmt3(node.pose)}]")
    if any(node.rot):
        parts.append(f"rot=[{_fmt_rot3(node.rot)}]")
    dof = effective_dof(tree, node)
    if dof:
        marker = f" (from {node.template})" if node.template else ""
        parts.append(f"{_dof_marker(dof)}{marker}")
    n_ports = len(effective_ports(tree, node))
    if n_ports:
        parts.append(f"[{n_ports} port{'s' if n_ports != 1 else ''}]")
    if node.descr:
        parts.append(f"— {node.descr}")
    return "  ".join(parts)


def _render_tree(tree: SeTree, title: str, description: str) -> str:
    lines = [f"# se design '{title}'  (units: metres)"]
    if description:
        lines.append(description)
    if not tree.blocks:
        lines.append("\n(no blocks yet — unfilled)")
        return "\n".join(lines)
    children: dict[str | None, list[str]] = {}
    for name, node in tree.blocks.items():
        children.setdefault(node.parent, []).append(name)
    for kids in children.values():
        kids.sort()

    def _walk(name: str, depth: int, path: tuple[str, ...]) -> None:
        node = tree.blocks[name]
        lines.append(f"{'  ' * depth}- {_block_line(tree, node)}")
        # An instance's (or array's) subtree is the template's, resolved
        # here at read time — never copied onto the instance row. ``path``
        # carries every expansion source already on the walk — defense in
        # depth against a cyclic instance chain that reached storage
        # despite ops._find_instance_cycle (hand-corrupted data): render
        # must never infinite-recurse regardless of what's stored.
        source = node.template or name
        if source in path:
            lines.append(
                f"{'  ' * (depth + 1)}⚠ instance cycle: "
                f"{' → '.join((*path, source))} — not expanding further"
            )
            return
        for child in children.get(source, []):
            _walk(child, depth + 1, (*path, source))

    lines.append("")
    for root in sorted(children.get(None, [])):
        _walk(root, 0, ())
    return "\n".join(lines)


def _render_block(tree: SeTree, node: SeBlock, store: Any, ref_id: int) -> str:
    # The uid is shown because it is the ADDRESS that survives a relabel —
    # args={'name': '#41'} reaches this block whatever it is called
    # (precis_se.identity). A block added but not yet saved has none.
    uid = f"  (uid #{node.uid})" if node.uid is not None else ""
    lines = [f"# block '{node.name}'{uid}"]
    if node.template and node.array:
        lines.append(f"array of: {node.template}  ({_array_label(node.array)})")
    elif node.template:
        lines.append(f"instance of: {node.template}")
    lines.append(f"parent: {node.parent or '(root)'}")
    lines.append(f"pose: [{_fmt3(node.pose)}] m")
    lines.append(f"rot: [{_fmt_rot3(node.rot)}]")
    if node.template:
        # desc/use stay raw (an instance genuinely has none — those keys
        # are rejected at mint time); envelope resolves via the template,
        # marked as inherited.
        env = effective_envelope(tree, node)
        marker = f" (from {node.template})" if env else ""
        lines.append(f"envelope: {env or '—'}{marker}")
    else:
        lines.append(f"envelope: {node.envelope or '— (unfilled)'}")
    lines.append(f"desc: {node.descr or '—'}")
    lines.append(f"use: {node.use or '—'}")
    if node.objectives:
        lines.append(f"loads: {json.dumps(node.objectives)}")
    dof = effective_dof(tree, node)
    if dof:
        # Atomic mode only, and shown only when declared — a bolted
        # bracket's record must not carry a 'dof: —' line it can never
        # fill (mode-scoped help, nm-se-merge.md).
        via = f" (from {node.template})" if node.template else ""
        lines.append(f"dof: {json.dumps(dof)}{via}")
    lines.append(_mode_line(tree, node))
    lines.append(_binding_line(tree, node))
    occurrences = se_bom.design_occurrences(tree).get(node.name, 0)
    if occurrences != 1:
        lines.append(f"realized: ×{occurrences} (arrays/instances included)")

    measures_owner = node.template or node.name
    own_measures = [m for m in tree.measures if m.block == measures_owner]
    if own_measures:
        via = f" (via template {node.template!r})" if node.template else ""
        lines.append("")
        lines.append(f"## measures{via}")
        lines.append(
            render_agent_table(
                [_measure_row(m) for m in own_measures],
                schema=["measure", "value", "relation", "strength", "reason"],
            )
        )

    # Blocktree slice 2 — declared states + transitions
    # (docs/backlog/blocktree-library-build-plan.md §Slice 2). A LOCAL
    # instance/array shows its TEMPLATE's — the same rule as mode/dof/
    # envelope, since it's the same design's ref_id and the shared tables
    # (:mod:`precis.design.states`) key on (ref_id, block_uid). A
    # CROSS-design template is deliberately skipped here (not resolved):
    # its states live under the FOREIGN design's ref_id, which this design
    # does not have, so guessing would read the wrong row rather than
    # degrade to "none" — states/transitions stay a same-design-only read
    # until cross-design resolution earns its own round. A block that
    # never declared any (the common case today) gets NO section at all —
    # the whole point of "a block with no declared states has exactly one
    # implicit state" is that its rendered shape doesn't change either.
    states_owner: Any = node
    if node.template is not None:
        design_slug, _ = parse_template_ref(node.template)
        states_owner = (
            resolve_template(tree, node.template) if design_slug is None else None
        )
    states_uid: int | None = getattr(states_owner, "uid", None)
    states = (
        design_states.states_for(store, ref_id, states_uid)
        if states_uid is not None
        else []
    )
    if states_uid is not None and states:
        via = f" (from template {node.template!r})" if node.template else ""
        lines.append("")
        lines.append(f"## states{via}")
        lines.append(
            render_agent_table(
                [
                    {
                        "name": s.name,
                        "envelope": s.envelope or "—",
                        "port_pose_overrides": (
                            json.dumps(s.port_pose_overrides)
                            if s.port_pose_overrides
                            else "—"
                        ),
                        "descr": s.descr or "—",
                    }
                    for s in states
                ],
                schema=["name", "envelope", "port_pose_overrides", "descr"],
            )
        )
        transitions = design_states.transitions_for(store, ref_id, states_uid)
        if transitions:
            lines.append("")
            lines.append(f"## transitions{via}")
            lines.append(
                render_agent_table(
                    [
                        {
                            "from": t.from_state,
                            "to": t.to_state,
                            "driver_kind": t.driver_kind,
                            "driver_ref": t.driver_ref or "—",
                            "params": json.dumps(t.params) if t.params else "—",
                        }
                        for t in transitions
                    ],
                    schema=["from", "to", "driver_kind", "driver_ref", "params"],
                )
            )

    ports = effective_ports(tree, node)
    lines.append("")
    if ports:
        via = f" (resolved via template {node.template!r})" if node.template else ""
        lines.append(f"## ports{via}")
        atomic = _shows_atomic_ports(ports.values())
        posed = _shows_port_poses(ports.values())
        extra = (
            *(_POSE_PORT_SCHEMA if posed else ()),
            *(_ATOMIC_PORT_SCHEMA if atomic else ()),
        )
        lines.append(
            render_agent_table(
                [_port_cells(p, atomic=atomic, posed=posed) for p in ports.values()],
                schema=["port", "roles", "direction", "annotations", *extra],
            )
        )
    else:
        lines.append("## ports\n(none)")

    touching = [c for c in tree.connects if node.name in (c.a_block, c.b_block)]
    lines.append("")
    if touching:
        lines.append("## connects")
        # The atomic ``kind`` column appears only when an edge carries one
        # — the same mode-scoped rule as the ports table's two.
        kind_schema = ["kind"] if any(c.kind for c in touching) else []
        rows = [
            {
                "a": f"{c.a_block}.{c.a_port}",
                "b": f"{c.b_block}.{c.b_port}",
                "joint": json.dumps(c.joint) if c.joint else "—",
                "objectives": json.dumps(c.objectives) if c.objectives else "—",
                **({"kind": c.kind or "—"} if kind_schema else {}),
            }
            for c in touching
        ]
        lines.append(
            render_agent_table(
                rows, schema=["a", "b", "joint", *kind_schema, "objectives"]
            )
        )
    else:
        lines.append("## connects\n(none)")

    threaded = [t for t in tree.threading if node.name in (t.a, t.b)]
    if threaded:
        lines.append("")
        lines.append("## threading")
        lines.append(
            render_agent_table(
                [{"relation": f"{t.a} threaded through {t.b}"} for t in threaded],
                schema=["relation"],
            )
        )

    bought = [
        line
        for line in tree.bom
        if line.block == node.name
        or (line.is_connect and node.name in (line.a_block, line.b_block))
    ]
    if bought:
        lines.append("")
        lines.append("## bought (per occurrence)")
        lines.append(
            render_agent_table(
                [
                    {
                        "item": f"{line.item_kind}:{line.item}",
                        "qty": f"{line.qty:g}" + (f" {line.uom}" if line.uom else ""),
                        "for": line.target,
                        "reason": line.reason or "—",
                    }
                    for line in bought
                ],
                schema=["item", "qty", "for", "reason"],
            )
        )
    return "\n".join(lines)


def _mode_line(tree: SeTree, node: SeBlock) -> str:
    """The block's manufacturing mode, with the honesty marker: a family
    whose implementer hasn't shipped reads as *intent*, never as a checked
    plan (:mod:`precis_se.modes`). An instance shows its template's — LOCAL
    or cross-design (:func:`~precis_se.ops.resolve_template`)."""
    owner = resolve_template(tree, node.template) if node.template else node
    mode = getattr(owner, "mode", None)
    if not mode:
        return "mode: — (unassigned)"
    via = f" (from {node.template})" if node.template else ""
    family = se_modes.family_of(mode)
    if family is None:
        return f"mode: {mode}{via}  ⚠ unknown family — set_mode to repair"
    if not family.implemented:
        return f"mode: {mode}{via}  (recorded intent — no implementer yet)"
    return f"mode: {mode}{via}  — {family.summary}"


def _binding_line(tree: SeTree, node: SeBlock) -> str:
    """The block's L3 realization binding — what its solid actually *is*
    (a cad/nm design, or a bought component/part). An instance shows its
    template's — LOCAL or cross-design
    (:func:`~precis_se.ops.resolve_template`)."""
    owner = resolve_template(tree, node.template) if node.template else node
    bound_kind = getattr(owner, "bound_kind", None)
    bound = getattr(owner, "bound", None)
    if owner is None or not bound_kind or not bound:
        return "realization: — (envelope only)"
    via = f" (from {node.template})" if node.template else ""
    return f"realization: {bound_kind}:{bound}{via}"


def _render_ports(tree: SeTree) -> str:
    """``view='ports'`` — every block's live ports; an instance's/array's
    row resolves from its template (:func:`effective_ports`), marked."""
    by_block = {
        name: effective_ports(tree, tree.blocks[name]) for name in sorted(tree.blocks)
    }
    # One design-wide decision, not one per block: a table whose rows had
    # different column sets would be unreadable (and the JSON backend
    # ignores ``schema``, so a stray key would simply appear).
    atomic = _shows_atomic_ports(
        p for ports in by_block.values() for p in ports.values()
    )
    posed = _shows_port_poses(p for ports in by_block.values() for p in ports.values())
    extra = (
        *(_POSE_PORT_SCHEMA if posed else ()),
        *(_ATOMIC_PORT_SCHEMA if atomic else ()),
    )
    rows = []
    for name, ports in by_block.items():
        node = tree.blocks[name]
        block_label = f"{name} (via {node.template})" if node.template else name
        for p in ports.values():
            rows.append(
                {"block": block_label, **_port_cells(p, atomic=atomic, posed=posed)}
            )
    if not rows:
        return "# se ports\n\n(no ports declared yet)"
    return f"# {len(rows)} port(s)\n" + render_agent_table(
        rows,
        schema=["block", "port", "roles", "direction", "annotations", *extra],
    )


def _render_topology(tree: SeTree) -> str:
    """``view='topology'`` — the atomic mode's L2 statements in one table:
    every live threading pair, plus every block's declared dof. Pure over
    ``tree`` (no store access — unlike ``validate``/``clearance``, a
    topology fact never depends on hydrated structure/cad data)."""
    lines = ["# se topology (atomic mode: L2 threading + declared dof)", ""]
    lines.append("## threading")
    if tree.threading:
        lines.append(
            render_agent_table(
                [
                    {"a": t.a, "b": t.b, "relation": f"{t.a} threaded through {t.b}"}
                    for t in tree.threading
                ],
                schema=["a", "b", "relation"],
            )
        )
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## dof")
    dof_rows = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        dof = effective_dof(tree, node)
        if not dof:
            continue
        via = f" (via {node.template})" if node.template else ""
        dof_rows.append(
            {
                "block": f"{name}{via}",
                "kind": dof.get("kind", "—"),
                "axis_ports": ", ".join(dof.get("axis_ports") or []),
            }
        )
    if dof_rows:
        lines.append(
            render_agent_table(dof_rows, schema=["block", "kind", "axis_ports"])
        )
    else:
        lines.append("(none)")
    return "\n".join(lines)


def _fmt_num(v: Any) -> str:
    """``:g`` when the value is a number; ``repr`` otherwise — a
    hand-corrupted stored relation must render legibly (and be flagged by
    DRC), never crash the read path."""
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return repr(v)


def _fmt_len(v: float) -> str:
    """A metre value through the shared neat formatter — ``15 mm``, ``2.3
    nm`` — instead of a bare metre float, so an agent thinking in any
    non-metre unit sees its magnitude in a scale-appropriate unit rather
    than counting zeros."""
    return format_quantity(v, "length")


def _fmt_in_unit(v: float | None, unit: str) -> str:
    """``_fmt_len``'s neat-formatter gloss for metre measures; a bare
    ``:g`` + unit word for the rest of the closed registry."""
    if v is None:
        return "—"
    if unit == "m":
        return _fmt_len(v)
    return f"{v:g} {unit}"


def _fmt_band(m: Any) -> str:
    """The declared acceptable band, one-sided ends included — the
    declarative set a point ``value`` (if any) was chosen from."""
    if m.min_value is None and m.max_value is None:
        return "—"
    if m.max_value is None:
        return f"≥ {_fmt_in_unit(m.min_value, m.unit)}"
    if m.min_value is None:
        return f"≤ {_fmt_in_unit(m.max_value, m.unit)}"
    return f"[{_fmt_in_unit(m.min_value, m.unit)}, {_fmt_in_unit(m.max_value, m.unit)}]"


def _measure_row(m: Any, tree: Any = None) -> dict[str, str]:
    rel = "—"
    if m.relation is not None:
        if m.relation.get("source"):
            scale = m.relation.get("scale", 1)
            scale_part = "" if scale == 1 else f"{_fmt_num(scale)} × "
            rel = (
                f"= {scale_part}{m.relation['source']} "
                f"+ {_fmt_num(m.relation.get('offset', 0))} "
                f"± {_fmt_num(m.relation.get('tol', 0))}"
            )
        if m.relation.get("feature"):
            rel = (
                f"feature {m.relation['feature']}"
                if rel == "—"
                else rel + f" · feature {m.relation['feature']}"
            )
    row = {
        "measure": f"{m.block}.{m.name}",
        "value": _fmt_in_unit(m.value, m.unit),
        "band": _fmt_band(m),
        "relation": rel,
        "strength": m.strength,
        "origin": m.origin,
        "reason": m.reason or "—",
        "datum": m.datum or "frame",
        "derived": "—",
    }
    if tree is not None:
        # The geometric number beside the declared one — evaluation is
        # cheap (envelope ray exits, no field solve) and stateless.
        try:
            mv = se_datums.evaluate_measure(tree, m)
        except (se_datums.MeasureError, cad_dsl.DslError):
            mv = None
        if mv is not None:
            if mv.value is not None:
                row["derived"] = _fmt_in_unit(mv.value, m.unit)
                if mv.datum_resolved:
                    row["datum"] = f"{m.datum or 'frame'} → {mv.datum_resolved}"
            if mv.notes:
                row["reason"] = "; ".join(
                    part for part in [m.reason or "", *mv.notes] if part
                )
    return row


def _stackup_rows(results: list[Any]) -> list[dict[str, str]]:
    rows = []
    for r in results:
        if r.derived is not None:
            derived = (
                f"{_fmt_in_unit(r.derived, r.unit)} "
                f"± {_fmt_in_unit(r.tol_accum, r.unit)}"
            )
        elif r.derived_min is not None:
            derived = (
                f"[{_fmt_in_unit(r.derived_min, r.unit)}, "
                f"{_fmt_in_unit(r.derived_max, r.unit)}] "
                f"± {_fmt_in_unit(r.tol_accum, r.unit)}"
            )
        else:
            derived = "—"
        rows.append(
            {
                "measure": r.measure,
                "declared": _fmt_in_unit(r.declared, r.unit),
                "derived": derived,
                "chain": " → ".join(r.chain),
                "status": r.problem or "ok",
            }
        )
    return rows


def _render_measures(tree: SeTree) -> str:
    """``view='measures'`` — every measure (its own unit; metres default)
    with its declared band and tolerance relation, then the stack-up
    evaluation (:func:`precis_se.measures.stackup`): each related
    measure's derived value (or band, from an interval anchor) ±
    accumulated worst-case tolerance, with unresolved/cyclic/mismatching
    chains named in the status column (DRC turns those into findings)."""
    if not tree.measures:
        return (
            "# se measures  (units: metres unless a measure declares "
            "count | ratio | deg)\n\n(no measures declared yet)\n\n"
            "Next: edit(kind='se', id=..., ops=[{'op':'add_measure',"
            "'block':'wheel','name':'bore_d','relation':{'source':"
            "'hub.od_d','offset':2e-4,'tol':5e-5},'strength':'hard'}])"
        )
    lines = [f"# {len(tree.measures)} measure(s)  (units: metres unless noted)"]
    lines.append(
        render_agent_table(
            [_measure_row(m, tree) for m in tree.measures],
            schema=[
                "measure",
                "value",
                "band",
                "relation",
                "datum",
                "derived",
                "strength",
                "origin",
                "reason",
            ],
        )
    )
    results = se_stackup(tree.measures)
    lines.append("")
    if results:
        lines.append("## stack-up (worst-case linear: Σ|tol| along the chain)")
        lines.append(
            render_agent_table(
                _stackup_rows(results),
                schema=["measure", "declared", "derived", "chain", "status"],
            )
        )
    else:
        lines.append("## stack-up\n(no relations declared — nothing to evaluate)")
    return "\n".join(lines)


def _render_datums(tree: SeTree) -> str:
    """``view='datums'`` — per block: the deterministic datum ranking
    (:func:`precis_se.datums.rank_datums`) with reasons, and which
    measures hang off each datum (declared selector, or ``frame`` when
    the column is NULL — the pose-frame default)."""
    lines = [
        "# se datums  (a datum is a feature of the block, never an "
        "optimiser DOF; NULL datum = pose frame)"
    ]
    if not tree.blocks:
        lines.append("\n(no blocks)")
        return "\n".join(lines)
    for name, node in tree.blocks.items():
        ranked = se_datums.rank_datums(tree, node)
        lines.append(f"\n## {name}")
        # A measure hangs off the datum its selector RESOLVES to — a
        # predicate (`face:largest`, `face:normal=+z`) never string-equals
        # a ranked row's concrete identity, so match on the resolution.
        # Declared text matches directly; else the resolution, in the
        # ranked rows' identity form (`face:<instance>.<tag>`).
        # Unresolvable declared selectors still hang by text (a port
        # without a direction is ranked but not measurable).
        hangs_on: dict[str, set[str]] = {}
        for m in tree.measures:
            if m.block != name:
                continue
            declared = m.datum or "frame"
            ids = {declared}
            try:
                res = se_datums.resolve(tree, node, declared)
            except se_datums.MeasureError:
                res = None
            if res is not None and res.resolved is not None:
                ids.add(f"face:{res.resolved}" if res.kind == "face" else res.resolved)
            hangs_on[m.name] = ids
        rows = []
        for i, r in enumerate(ranked, 1):
            hanging = sorted(
                f"{m.name} (default)"
                if not m.datum
                else f"{m.name} (declared)"
                if m.datum == r.datum
                else f"{m.name} (declared {m.datum})"
                for m in tree.measures
                if m.block == name and r.datum in hangs_on.get(m.name, ())
            )
            rows.append(
                {
                    "rank": str(i),
                    "datum": r.datum,
                    "score": "—" if math.isinf(r.score) else f"{r.score:.4g}",
                    "reason": r.reason,
                    "measures": ", ".join(hanging) if hanging else "—",
                }
            )
        lines.append(
            render_agent_table(
                rows, schema=["rank", "datum", "score", "reason", "measures"]
            )
        )
    return "\n".join(lines)


def _anchor_resolves(tree: SeTree, anchor: str) -> bool:
    """An ``about`` anchor resolves as a block name, or as a
    ``'block.measure'`` pair (split on the last dot, the relation-source
    rule)."""
    if anchor in tree.blocks:
        return True
    blk, sep, msr = anchor.rpartition(".")
    if not sep:
        return False
    return any(m.block == blk and m.name == msr for m in tree.measures)


def _note_line(tree: SeTree, n: Any) -> str:
    when = n.created_at.strftime("%Y-%m-%d") if n.created_at else "(unsaved)"
    parts = [f"- **{n.name}** ({n.kind}, {n.origin}, {when}): {n.body}"]
    if n.re is not None:
        target = any(x.name == n.re for x in tree.notes)
        parts.append(
            f"  re: {n.re}" + ("" if target else " (orphaned — that note was removed)")
        )
    if n.about:
        rendered = [
            a if _anchor_resolves(tree, a) else f"{a} (dangling)" for a in n.about
        ]
        parts.append(f"  about: {', '.join(rendered)}")
    return "\n".join(parts)


def _render_interview(tree: SeTree) -> str:
    """``view='interview'`` — the interrogation ledger
    (:mod:`precis_se.notes`): open questions first (what a propose job —
    or a human — should answer next), then the full timeline in created
    order. A question is *open* until a live answer/decision names it in
    ``re`` (derived, never stored)."""
    if not tree.notes:
        return (
            "# se interview\n\n(no notes yet — the ledger is empty)\n\n"
            "Next: edit(kind='se', id=..., ops=[{'op':'add_note',"
            "'name':'q-bore','kind':'question','text':'what bearing "
            "bore?','about':['wheel.bore_d']}]) — answers/decisions link "
            "back via 're'"
        )
    open_qs = se_notes.open_questions(tree.notes)
    lines = [
        f"# se interview — {len(tree.notes)} note(s), {len(open_qs)} open question(s)"
    ]
    if open_qs:
        lines.append("\n## open questions (unanswered — answer or decide)")
        for n in open_qs:
            lines.append(_note_line(tree, n))
    lines.append("\n## timeline")
    for n in tree.notes:
        lines.append(_note_line(tree, n))
    return "\n".join(lines)


def _render_freedom(tree: SeTree) -> str:
    """``view='freedom'`` — :mod:`precis_se.freedom`: what is still
    undecided, and by whom. DRC's honest counterpart — this view lists
    liberties, not defects."""
    report = se_freedom.freedom(tree)
    lines = ["# se freedom — what is still undecided, and by whom"]
    empty = not (
        report.motions
        or report.unconnected
        or report.undecided
        or report.unenveloped
        or report.soft_measures
        or report.loads_recorded
        or report.proposed_facets
    )
    if empty:
        lines.append(
            f"\n(nothing undecided that this view can see — "
            f"{len(tree.blocks)} block(s), {len(tree.measures)} "
            "measure(s). An EMPTY design also reads as fully decided "
            "here: pair with view='validate' for filled-fraction honesty)"
        )
        return "\n".join(lines)
    if report.motions:
        lines.append("\n## kinematic freedom (declared moving joints)")
        for mo in report.motions:
            axis = _fmt3(mo.axis) if mo.axis else "no axis"
            lines.append(f"- {mo.subject}: {mo.klass} ({axis})")
    if report.unconnected:
        lines.append("\n## unconnected blocks (free in all 6 DOF)")
        lines.append("- " + ", ".join(report.unconnected))
    if report.undecided:
        lines.append("\n## undecided measures")
        lines.append(
            render_agent_table(
                [
                    {
                        "measure": u.measure,
                        "state": u.state,
                        "band": (
                            f"[{_fmt_in_unit(u.band[0], u.unit)}, "
                            f"{_fmt_in_unit(u.band[1], u.unit)}]"
                            if u.band
                            else "—"
                        ),
                        "unit": u.unit,
                        "origin": u.origin,
                    }
                    for u in report.undecided
                ],
                schema=["measure", "state", "band", "unit", "origin"],
            )
        )
    if report.unenveloped:
        lines.append("\n## blocks without an envelope (space unclaimed)")
        lines.append("- " + ", ".join(report.unenveloped))
    if report.soft_measures or report.loads_recorded:
        lines.append("\n## declared but unevaluated (no engaged consumer yet)")
        if report.soft_measures:
            lines.append(
                f"- soft measure(s): {', '.join(report.soft_measures)} — "
                "objectives, evaluated by nothing until compliance "
                "advisories ship (ship-order step 6)"
            )
        if report.loads_recorded:
            lines.append(
                f"- {report.loads_recorded} block(s)/connect(s) carry "
                "loads — recorded intent; the deformation/compression "
                "evaluators are ship-order step 6"
            )
    by_whom = ", ".join(f"{k}: {v}" for k, v in sorted(report.measure_origins.items()))
    if by_whom:
        lines.append(f"\nmeasure origins — {by_whom}")
    if report.proposed_facets:
        lines.append(
            "proposed (revisable) facets: " + ", ".join(report.proposed_facets)
        )
    return "\n".join(lines)


def _render_drc(tree: SeTree, scenario_line: str = "") -> str:
    """``view='drc'`` — the graph-tier report (:mod:`precis_se.drc`):
    findings under the filled-fraction header (same honesty rule as
    validate — a clean empty design is unfilled, not done) and the
    governing-scenario line (``scenario_line``, the design core rental —
    the handler resolves it; this stays store-free), then the DOF probe
    outcomes (including honest skips) and any stack-up problems' full
    rows."""
    report = se_drc.drc(tree)
    fill_line = _fill_fraction_line(tree)
    if scenario_line:
        fill_line = f"{fill_line}\n{scenario_line}"
    lines: list[str] = []
    if not report.findings:
        lines.append(f"✓ no DRC findings\n{fill_line}")
    else:
        n_error = sum(1 for f in report.findings if f.severity == "error")
        n_warn = sum(1 for f in report.findings if f.severity == "warn")
        lines.append(f"# {n_error} error(s), {n_warn} warning(s)\n{fill_line}\n")
        lines.append(
            render_agent_table(
                [
                    {
                        "severity": f.severity,
                        "rule": f.rule,
                        "subject": f.subject,
                        "detail": f.detail,
                    }
                    for f in report.findings
                ],
                schema=["severity", "rule", "subject", "detail"],
            )
        )
    if report.dof_probes:
        lines.append("")
        lines.append("## declared-vs-derived DOF (axis-travel probe, advisory)")
        lines.append(
            render_agent_table(
                [
                    {"connect": p.subject, "class": p.klass, "outcome": p.outcome}
                    for p in report.dof_probes
                ],
                schema=["connect", "class", "outcome"],
            )
        )
    problems = [r for r in report.stackup if r.problem is not None]
    if problems:
        lines.append("")
        lines.append("## stack-up problems (full rows in view='measures')")
        lines.append(
            render_agent_table(
                _stackup_rows(problems),
                schema=["measure", "declared", "derived", "chain", "status"],
            )
        )
    return "\n".join(lines)


def _findings_table(findings: list[se_validate.ValidationIssue]) -> str:
    """The six-column ``view='print'`` findings table — the one render
    that shows ``measured``/``expected``/``suggested_fix`` (module
    docstring's rule: existing renderers stay 4-column, unchanged)."""
    return render_agent_table(
        [
            {
                "severity": f.severity,
                "rule": f.rule,
                "subject": f.subject,
                "measured": f.measured or "",
                "expected": f.expected or "",
                "detail": f.detail,
                "suggested_fix": f.suggested_fix or "",
            }
            for f in findings
        ],
        schema=[
            "severity",
            "rule",
            "subject",
            "measured",
            "expected",
            "detail",
            "suggested_fix",
        ],
    )


def _render_print_summary(report: se_printing.BlockPrintReport) -> str:
    """One ``## block`` section of ``view='print'``'s no-args summary —
    mode, realized/unrealized, the proposed/pinned frame with its score,
    then findings."""
    lines = [f"## {report.block} — mode {report.mode}"]
    if report.printed is None:
        lines.append("unrealized — no bound cad design yet")
    else:
        lines.append(
            f"realized: {report.printed.volume_after_m3 * 1e9:.4g} mm³ "
            f"({len(report.printed.features)} stamped feature(s))"
        )
        if report.chosen_down is not None:
            origin = "pinned" if report.pinned else "proposed"
            score_bit = (
                f", score {report.chosen_score.total:.4g}"
                if report.chosen_score is not None
                else ""
            )
            lines.append(
                f"build frame ({origin}): "
                f"down={se_printing.format_down(report.chosen_down)}{score_bit}"
            )
            if report.best_other:
                lines.append(f"vs best: {report.best_other}")
    if report.findings:
        lines.append(_findings_table(report.findings))
    else:
        lines.append("no findings")
    return "\n".join(lines)


def _render_print_block(report: se_printing.BlockPrintReport) -> str:
    """``view='print' args={'block': ...}`` — one block's full
    orientation-candidate table plus findings."""
    lines = [f"# view='print' — {report.block} (mode {report.mode})"]
    if report.printed is None:
        lines.append("")
        lines.append("unrealized — no bound cad design yet")
        lines.append("")
        lines.append(
            _findings_table(report.findings) if report.findings else "no findings"
        )
        return "\n".join(lines)
    lines.append(
        f"volume: {report.printed.volume_before_m3 * 1e9:.4g} mm³ before, "
        f"{report.printed.volume_after_m3 * 1e9:.4g} mm³ after "
        f"{len(report.printed.features)} stamped feature(s)"
    )
    if report.chosen_down is not None:
        origin = "pinned" if report.pinned else "proposed"
        lines.append("")
        lines.append(
            f"build frame ({origin}): down={se_printing.format_down(report.chosen_down)}"
        )
        if report.best_other:
            lines.append(f"vs best: {report.best_other}")
        if report.candidates:
            top = report.candidates[: se_printing.CANDIDATE_TABLE_N]
            term_keys = sorted(top[0].terms.keys())
            lines.append("")
            lines.append(
                render_agent_table(
                    [
                        {
                            "down": se_printing.format_down(c.down),
                            "score": f"{c.score:.4g}",
                            **{k: f"{c.terms[k]:.4g}" for k in term_keys},
                        }
                        for c in top
                    ],
                    schema=["down", "score", *term_keys],
                )
            )
    lines.append("")
    lines.append(_findings_table(report.findings) if report.findings else "no findings")
    return "\n".join(lines)


def _fab_fdm_cell(
    name: str, mode: str, report: se_printing.BlockPrintReport
) -> tuple[str, str]:
    """``view='fab'``'s ``(status, handle)`` pair for one fdm-family row —
    se-print-implementer.md's status priority: unrealized, else abstract
    joints, else the process-finding count, else the frame's pinned/
    proposed origin."""
    if report.printed is None:
        return "unrealized", f"realize(block={name!r}, mode={mode!r})"
    bits = ["realized"]
    n_abs = sum(1 for f in report.findings if f.rule == "abstract_joint")
    if n_abs:
        bits.append(f"abstract joints: {n_abs}")
    other = [
        f
        for f in report.findings
        if f.rule != "abstract_joint" and f.severity != "info"
    ]
    if other:
        bits.append(f"print-checked: {len(other)} finding(s)")
    elif not n_abs:
        bits.append("pinned" if report.pinned else "proposed")
    handle = f"view='print' args={{'block': {name!r}, 'fmt': 'stl'}}"
    return ", ".join(bits), handle


#: Share of a donor's excitation an UNDECLARED pair has to reach before
#: it is worth a finding — a comm-system crosstalk threshold, not a
#: numerical tolerance (hence no ``*_EPS``/``*_TOL`` name): below it, two
#: chromophores merely being in the same design is not actionable; above
#: it, an agent that thought it was building one channel has quietly
#: built two.
_CROSSTALK_NOTABLE_FRACTION = 0.05


def _fret_length(value_m: float) -> str:
    """One FRET-scale length for a human reader — same neat SI-prefixed
    formatter the rest of se's views use (:func:`_mm`'s sibling), so a
    5 nm separation reads as ``5 nm`` and not ``5e-09 m``."""
    return format_quantity(value_m, "length")


def _isolation_str(db: float) -> str:
    if math.isinf(db):
        return "+∞ dB" if db > 0 else "−∞ dB"
    return f"{db:+.1f} dB"


def _fret_chromophores(
    tree: SeTree,
) -> tuple[
    dict[str, tuple[fret.Chromophore, NDArray[np.float64], NDArray[np.float64]]],
    list[str],
]:
    """Every block with an effective chromophore, resolved to its physics
    card plus its world-space position/dipole — the collection step
    shared by every section of ``view='fret'``.

    World placement matches ``view='clearance'``'s own v1 convention
    (:func:`_pair_clearance`/``precis_se.validate._posed_component``): a
    block's own ``pose``/``rot`` ARE its world placement, never composed
    through a parent chain — nested-frame inheritance is a later
    increment, tracked in the same place clearance's is, and this view
    stays consistent with the rest of se rather than quietly disagreeing
    about what "world space" means.

    A card that fails to build (a stored dipole that norms to zero, a
    donor emission spectrum that integrates to zero) is excluded rather
    than raised through — se designs are suggestive by contract, and a
    half-built optical card is a finding for the header, never a crash
    for the whole view."""
    chromo: dict[
        str, tuple[fret.Chromophore, NDArray[np.float64], NDArray[np.float64]]
    ] = {}
    bad: list[str] = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        raw = effective_chromophore(tree, node)
        if raw is None:
            continue
        try:
            card = fret.chromophore_from_spec(raw)
            position = np.asarray([float(v) for v in node.pose], dtype=np.float64)
            r_world = cad_rotation(*(float(v) for v in node.rot)).R
            world_dip = fret.world_dipole(card.dipole, r_world)
        except (fret.FretError, TypeError, ValueError, IndexError) as exc:
            bad.append(f"{name}: {exc}")
            continue
        chromo[name] = (card, position, world_dip)
    return chromo, bad


def _render_fret(tree: SeTree) -> str:
    """``view='fret'`` — the L4 optical-link (FRET) budget: every
    chromophore-bearing block's realised transfer channels, solved
    together per donor (:func:`precis_se.fret.solve_donor` — the
    competition the physics enforces, never a per-pair number in
    isolation), checked against any connect's declared ``optical``
    requirement, and flagged for the geometry pitfalls the r⁻⁶ formula
    itself cannot see (an orientation null, a too-close Dexter pair,
    unintended crosstalk to a block nobody declared a link to).

    Never stored: a design's poses move underneath it on every edit, so
    this is recomputed from the realised geometry on every read — the
    same rule ``view='clearance'``/``view='drc'`` already follow.
    """
    optics = tree.optics or {}
    medium_declared = tree.optics is not None
    medium_index = float(optics.get("medium_index", fret.DEFAULT_MEDIUM_INDEX))
    excitation_nm = optics.get("excitation_nm")

    chromo, bad = _fret_chromophores(tree)
    lines: list[str] = [f"# fret — {len(chromo)} chromophore block(s)"]
    if medium_declared:
        lines.append(f"medium index: {medium_index:g} (declared)")
    else:
        lines.append(
            f"medium index: {medium_index:g} (ASSUMED — set_optics not "
            "declared; this is precis_se.fret.DEFAULT_MEDIUM_INDEX, not a "
            "measured value)"
        )
    if excitation_nm is not None:
        lines.append(f"pump: {float(excitation_nm):g} nm")
    else:
        lines.append(
            "pump: not declared (set_optics excitation_nm=) — "
            "spectral crosstalk NOT evaluated"
        )
    for msg in bad:
        lines.append(f"⚠ {msg} — excluded from the budget")
    # An array node is ONE row in `tree.blocks`; its members are derived at
    # read time and have no poses here. Counting it once at the array
    # node's own pose is an undercount of N-1 emitters, and an undercount
    # that looks like an answer is worse than no answer — so say it.
    arrayed = sorted(
        name
        for name, node in tree.blocks.items()
        if node.array is not None and effective_chromophore(tree, node) is not None
    )
    for name in arrayed:
        lines.append(
            f"⚠ '{name}' is an array of chromophore blocks — counted ONCE at "
            "the array node's own pose. Members are derived at read time and "
            "are not expanded here, so this budget undercounts both the "
            "emitters and the crosstalk between them"
        )

    if not chromo:
        lines.append("")
        lines.append(
            "no optical domain in this design — no block carries a "
            "chromophore card (set_chromophore to add one)"
        )
        return "\n".join(lines)

    ids = {name: i for i, name in enumerate(sorted(chromo))}
    id_names = {i: name for name, i in ids.items()}
    budgets: dict[str, fret.DonorBudget] = {}
    for donor_name, (donor_card, donor_pos, donor_dip) in chromo.items():
        acceptors = [
            fret.PairGeometry(
                block_uid=ids[acc_name],
                chromophore=acc_card,
                separation=acc_pos - donor_pos,
                world_dipole=acc_dip,
            )
            for acc_name, (acc_card, acc_pos, acc_dip) in chromo.items()
            if acc_name != donor_name
        ]
        budgets[donor_name] = fret.solve_donor(
            donor_uid=ids[donor_name],
            donor=donor_card,
            donor_world_dipole=donor_dip,
            acceptors=acceptors,
            refractive_index=medium_index,
        )

    if len(chromo) == 1:
        (only,) = chromo
        lines.append("")
        lines.append(
            f"only one chromophore block ('{only}') — a donor with no "
            "acceptor in range is not an error"
        )
        return "\n".join(lines)

    lines.append("")
    lines.append("## per-donor budget (channels strongest-first)")
    for name in sorted(chromo):
        card = chromo[name][0]
        budget = budgets[name]
        lines.append("")
        lines.append(f"### {name} — donor, τ={card.lifetime_s:g} s")
        if not budget.channels:
            lines.append("(no other chromophore block)")
        else:
            lines.append(
                render_agent_table(
                    [
                        {
                            "acceptor": id_names[ch.block_uid],
                            "separation": _fret_length(ch.separation_m),
                            "kappa_sq": f"{ch.kappa_sq:.3f}",
                            "R0": _fret_length(ch.forster_radius_m),
                            "efficiency": f"{ch.efficiency * 100:.2f}%",
                            "regime": ch.regime.value,
                        }
                        for ch in budget.channels
                    ],
                    schema=[
                        "acceptor",
                        "separation",
                        "kappa_sq",
                        "R0",
                        "efficiency",
                        "regime",
                    ],
                )
            )
        lines.append(f"residual (own decay): {budget.residual_efficiency * 100:.2f}%")

    declared_pairs: set[frozenset[str]] = set()
    verdict_rows: list[dict[str, Any]] = []
    findings: list[str] = []
    # Narrowed once here rather than re-``float()``-ed at the call site:
    # ``excitation_nm`` comes off a jsonb payload as ``Any | None``, and a
    # bare ``float(...)`` guarded by a separate bool is something mypy
    # cannot follow.
    pump_nm = float(excitation_nm) if excitation_nm is not None else None
    show_crosstalk = pump_nm is not None
    for c in tree.connects:
        if c.optical is None:
            continue
        a, b = c.a_block, c.b_block
        declared_pairs.add(frozenset((a, b)))
        min_eff = float(c.optical["min_efficiency"])
        row: dict[str, Any] = {
            "link (a→b, a=donor)": f"{a} → {b}",
            "min_efficiency": f"{min_eff * 100:.1f}%",
        }
        if a not in chromo or b not in chromo:
            missing = ", ".join(x for x in (a, b) if x not in chromo)
            row |= {
                "efficiency": "—",
                "verdict": "N/A",
                "isolation": "—",
                "sensitivity": "—",
            }
            if show_crosstalk:
                row["crosstalk"] = "—"
            verdict_rows.append(row)
            findings.append(
                f"declared link {a}→{b}: {missing} has no chromophore card "
                "— set_chromophore, or clear the link, to resolve"
            )
            continue
        budget = budgets[a]
        channel = budget.channel_for(ids[b])
        assert (
            channel is not None
        )  # every other chromophore block is a candidate acceptor
        verdict = "PASS" if channel.efficiency >= min_eff else "FAIL"
        row |= {
            "efficiency": f"{channel.efficiency * 100:.2f}%",
            "verdict": verdict,
            "isolation": _isolation_str(budget.isolation_db(ids[b])),
            # Sensitivity is meaningful only where the r⁻⁶ law is what
            # governs. Quoting ×6.00 for a coincident or nulled pair would
            # be the limit of a formula for a link that does not exist —
            # exactly the plausible-but-empty number the rest of this leg
            # refuses to print.
            "sensitivity": (
                f"×{fret.distance_sensitivity(channel.efficiency):.2f} "
                "efficiency error per unit distance error"
                if channel.regime is fret.Regime.FORSTER
                else "—"
            ),
        }
        if pump_nm is not None:
            donor_card, acc_card = chromo[a][0], chromo[b][0]
            xtalk = fret.spectral_crosstalk(donor_card, acc_card, pump_nm)
            row["crosstalk"] = (
                "∞ (pump misses donor)" if math.isinf(xtalk) else f"{xtalk:.3g}"
            )
        verdict_rows.append(row)

        if verdict == "FAIL":
            findings.append(
                f"declared link {a}→{b} FAILS its {min_eff * 100:.1f}% "
                f"requirement: realised {channel.efficiency * 100:.2f}%"
            )
        if channel.regime is fret.Regime.ORIENTATION_NULL:
            headroom = fret.orientation_headroom(channel.kappa_sq)
            findings.append(
                f"declared link {a}→{b} is orientation-nulled (κ²="
                f"{channel.kappa_sq:.4f}, {headroom * 100:.1f}% of "
                "isotropic) — the fix is rotating one of the two blocks, "
                "not moving them"
            )
        elif channel.regime is fret.Regime.DEXTER:
            findings.append(
                f"declared link {a}→{b} is inside the Dexter crossover "
                f"({_fret_length(channel.separation_m)} apart) — no "
                "Förster number is quoted: exchange transfer competes and "
                "the point-dipole model is unsafe this close"
            )
        elif channel.regime is fret.Regime.NEGLIGIBLE:
            findings.append(
                f"declared link {a}→{b} is negligible at this separation "
                f"({_fret_length(channel.separation_m)}, R0="
                f"{_fret_length(channel.forster_radius_m)}) — not a "
                "working link"
            )
        elif channel.regime is fret.Regime.COINCIDENT:
            findings.append(
                f"declared link {a}→{b} has both blocks at the same pose — "
                "no separation vector, so no efficiency can be computed. "
                "set_pose on one of them; this is what an unplaced design "
                "looks like, not a broken one"
            )

    if verdict_rows:
        lines.append("")
        lines.append("## declared links")
        crosstalk_schema = ["crosstalk"] if show_crosstalk else []
        lines.append(
            render_agent_table(
                verdict_rows,
                schema=[
                    "link (a→b, a=donor)",
                    "min_efficiency",
                    "efficiency",
                    "verdict",
                    "isolation",
                    "sensitivity",
                    *crosstalk_schema,
                ],
            )
        )

    for donor_name in sorted(chromo):
        budget = budgets[donor_name]
        for ch in budget.channels:
            if ch.regime is not fret.Regime.FORSTER:
                continue
            if ch.efficiency < _CROSSTALK_NOTABLE_FRACTION:
                continue
            acc_name = id_names[ch.block_uid]
            if frozenset((donor_name, acc_name)) in declared_pairs:
                continue
            findings.append(
                f"undeclared crosstalk: {donor_name} transfers "
                f"{ch.efficiency * 100:.1f}% to {acc_name} though no "
                "optical link declares this pair — set_optical_link if "
                "intended, or move/rotate the blocks apart if not"
            )

    lines.append("")
    if findings:
        lines.append(f"## findings ({len(findings)})")
        for finding in findings:
            lines.append(f"- {finding}")
    else:
        lines.append("✓ no fret findings")
    return "\n".join(lines)


def _render_stability(tree: SeTree) -> str:
    """``view='stability'`` — the Maxwell/Calladine report
    (:mod:`precis_se.stability`): counts, verdict, and the per-member
    self-stress state, under the model-assumption header (pin nodes at
    block poses, axial subgraph only — the honesty the tripwire contract
    demands)."""
    report = se_stability.classify(tree)
    lines = [
        "# stability — axial subgraph (Maxwell/Calladine)",
        "model: one pin node per block at its pose; pin-ended axial "
        "members only — non-axial connects and envelope contact are NOT "
        "modelled",
        "",
        f"j={report.j} node(s)  b={report.b} member(s)  c={report.c} "
        f"grounded translation(s)  rank={report.rank}",
        f"m={report.m} mechanism(s) (rb={report.rb_dim} rigid-body, "
        f"internal={report.m_internal})  s={report.s} self-stress state(s)",
        "",
        f"verdict: {report.verdict}",
    ]
    for note in report.notes:
        lines.append(f"note: {note}")
    if report.members:
        lines.append("")
        lines.append(
            render_agent_table(
                [
                    {
                        "member": row.subject,
                        "role": row.role,
                        "length": "—" if row.length is None else f"{row.length:g} m",
                        "self_stress": "—"
                        if row.self_stress is None
                        else f"{row.self_stress:+.3f}",
                        "note": row.skipped or "",
                    }
                    for row in report.members
                ],
                schema=["member", "role", "length", "self_stress", "note"],
            )
        )
        if any(row.self_stress is not None for row in report.members):
            lines.append(
                "self_stress: tension-positive coefficients of the "
                "reported state, normalized to the largest magnitude — "
                "a ray, so only ratios and signs mean anything"
            )
    prestress = se_stability.prestress_report(tree)
    if prestress is not None:
        lines.append("")
        lines.append("## prestress — declared preloads vs the self-stress space")
        if prestress.nothing_to_verify:
            lines.append("no preload declared — nothing to verify")
        elif prestress.compatible is None:
            lines.append("not checkable: no analysable axial system")
        elif prestress.compatible:
            lines.append(
                "declared preload(s) ARE a self-stress state — "
                f"out-of-balance {prestress.residual:g} N ≤ tolerance "
                f"{prestress.tolerance:g} N"
            )
        else:
            lines.append(
                "declared preload(s) are NOT a self-stress state — "
                f"out-of-balance {prestress.residual:g} N at node "
                f"'{prestress.worst_node}' (tolerance "
                f"{prestress.tolerance:g} N)"
            )
        lines.append(
            render_agent_table(
                [
                    {
                        "member": row.subject,
                        "role": row.role,
                        "declared": "—"
                        if row.declared is None
                        else f"{row.declared:g} N",
                        "implied": "—" if row.implied is None else f"{row.implied:g} N",
                        "note": row.skipped or row.flag or "",
                    }
                    for row in prestress.rows
                ],
                schema=["member", "role", "declared", "implied", "note"],
            )
        )
        lines.append(
            "implied: the completed self-stress member force (tension-positive N)"
        )
        for note in prestress.notes:
            lines.append(f"note: {note}")
        for subject, detail in prestress.findings:
            lines.append(f"finding: {subject} — {detail}")
    return "\n".join(lines)


def _mm(value: float | None) -> str:
    """One length for a human reader, through the shared neat formatter
    (``15 mm``, ``2.3 m``) — se stores metres; every number in this view
    is converted once, here, and the string carries its own unit, so a
    view that mixed units would be the bug this subsystem keeps having,
    printed."""
    return "—" if value is None else format_quantity(value, "length")


def _render_fasten(tree: SeTree) -> str:
    """``view='fasten'`` — what each screw joint does to the parts it
    joins (:mod:`precis_se.fasten`): the stack its axis walks through, the
    grip and length check, the thread's lead and travel limits, which
    driver can reach the head (rung 3b), and every feature it stamps —
    the clearance holes, the head's countersink or counterbore, and the
    far end, which on a printed member is whatever
    ``params.thread_strategy`` named and nothing at all when it named
    nothing (rung 3c).

    The holes are **derived**: regenerated from the connect on every read,
    stored nowhere, so this view is the feature list — there is no other
    copy of it to drift."""
    results = se_fasten.fasten(tree)
    if not results:
        return (
            "✓ no screw joints\n"
            "This view covers connects whose joint declares the `screw` "
            "mechanism (threaded fastening) or the `screw` kinematic class "
            "(a helical pair). Nothing here declares either yet."
        )
    lines: list[str] = [f"# {len(results)} screw joint(s)"]
    for res in results:
        who = res.component or res.fastener or "no fastener"
        head = f"\n## {res.subject} — {who}"
        if res.thread_size:
            head += f" ({res.thread_size})"
        lines.append(head)
        if res.why_not:
            lines.append(f"⚠ {res.why_not}")
        if res.members:
            lines.append(
                render_agent_table(
                    [
                        {
                            "member": m.block,
                            "from": _mm(m.t_in),
                            "to": _mm(m.t_out),
                            "thickness": _mm(m.thickness_m),
                            "note": (
                                f"bought {m.form}"
                                if m.bought and m.form
                                else "bought"
                                if m.bought
                                else "designed"
                            ),
                            "made by": m.mode or "—",
                        }
                        for m in res.members
                    ],
                    schema=["member", "from", "to", "thickness", "note", "made by"],
                )
            )
            lines.append(
                f"grip {_mm(res.grip_m)} · stack {_mm(res.stack_m)}"
                # Only the nut is worth saying here: 'tapped' is the
                # internal "no nut in the stack" classification, and
                # printing it beside a far end the pass refused to decide
                # was two lines of one view contradicting each other.
                + (" · terminated by a nut" if res.termination == "nut" else "")
                + f" · screw is {_mm(res.length_m)} under the head, needs "
                f"{_mm(res.required_length_m)}"
            )
            if res.tool:
                lines.append(f"driven with: {res.tool}")
            lines.append(
                "far end: "
                + (
                    f"{res.thread_strategy} into "
                    f"{res.material_class or 'an undeclared material'}"
                    if res.thread_strategy
                    else "UNDECIDED — see the finding below"
                )
            )
        if res.thread is not None:
            t = res.thread
            txt = (
                f"thread: {_mm(t.lead_m)} of travel per turn "
                f"(pitch {_mm(t.pitch_m)} × {t.starts} start)"
            )
            if t.engagement_m is not None and t.turns is not None:
                txt += (
                    f" — {_mm(t.engagement_m)} engaged, "
                    f"{t.turns:.1f} turns from first thread to seated"
                )
            if res.declared_lead_m is not None:
                txt += f" · joint declares {_mm(res.declared_lead_m)}/turn"
            lines.append(txt)
        if res.holes:
            lines.append(
                f"holes stamped by this joint (derived from the connect, "
                f"regenerated on every read) — fit {res.fit.fit_class!r}"
                if res.fit
                else "holes stamped by this joint"
            )
            lines.append(
                render_agent_table(
                    [
                        {
                            "feature": h.name,
                            "member": h.block,
                            "kind": h.kind,
                            "diameter": _mm(h.diameter_m),
                            "depth": _mm(h.depth_m),
                            # A blind tapped/core hole is drilled past its
                            # full-form thread; both numbers matter to the
                            # person cutting it.
                            "full thread": (
                                _mm(h.thread_depth_m) if h.thread_depth_m else "—"
                            ),
                            "across flats": (
                                _mm(h.across_flats_m) if h.across_flats_m else "—"
                            ),
                        }
                        for h in res.holes
                    ],
                    schema=[
                        "feature",
                        "member",
                        "kind",
                        "diameter",
                        "depth",
                        "full thread",
                        "across flats",
                    ],
                )
            )
            # Where each non-clearance number came from, once per kind. A
            # shop rule and a published table must not read alike, and the
            # table has no room to say which is which.
            seen: set[str] = set()
            for h in res.holes:
                if h.kind == "clearance" or not h.source or h.kind in seen:
                    continue
                seen.add(h.kind)
                lines.append(f"  {h.kind}: {h.source}")
        for f in res.findings:
            mark = "ℹ" if getattr(f, "severity", "warn") == "info" else "⚠"
            lines.append(f"{mark} {f.rule}: {f.detail}")
    return "\n".join(lines)


def _fill_fraction_line(tree: SeTree) -> str:
    """``view='validate'``'s filled-fraction honesty header (the maze.py
    lesson, nm's ``_fill_fraction_line`` transferred): at this round a
    block is "filled" when it declares an envelope (L1) — the L3 binding
    notion arrives with realization. Ordinary blocks only: an instance/
    array fills exactly when its template does."""
    ordinary = [n for n in tree.blocks.values() if n.template is None]
    if not ordinary:
        return "0/0 block(s) filled — no blocks declared yet (unfilled)"
    filled = [n for n in ordinary if n.envelope]
    line = f"{len(filled)}/{len(ordinary)} block(s) have envelopes (L1 filled)"
    if not filled:
        line += (
            " — UNFILLED scaffold: zero findings below means nothing is "
            "wrong YET, not that this design is done"
        )
    return line


#: Views that accept a TRANSIENT ``args={'state': {block: state_name}}``
#: pose override (blocktree slice 2's get-time posing rung,
#: :func:`_state_arg_map`/:func:`_apply_state_arg`) — se's discrete-domain
#: analogue of cad's ``CadHandler._state_arg`` continuous joint state.
#: ``tree``/``block`` so a posed block's own summary/record reads
#: correctly; ``clearance`` so a posed pair can be probed for clash the
#: same way an unposed one already is (the plan's "posed in either,
#: probed for clash in each"). Deliberately NOT ``drc`` — wiring per-state
#: clash into its own findings is a later round (docs/backlog/
#: blocktree-library-build-plan.md §Slice 2, "leave drc.py alone" this
#: round) — nor any other view; se has no ``view='sweep'`` at all yet.
_STATE_VIEWS = frozenset({"", "tree", "block", "clearance"})

#: Every ``get(kind='se')`` view's accepted ``args=`` keys — the single
#: source :func:`_vet_view_args` checks a caller's ``args`` dict against
#: (nm's ``_VIEW_ARGS`` transferred by the merge, extended to se's views).
#: Only a view listed here gets checked at all; an unrecognized ``view=``
#: falls through to the plain "unknown se view" error unchanged, from
#: :meth:`SeHandler.get`.
_VIEW_ARGS: dict[str, frozenset[str]] = {
    "": frozenset({"state"}),
    "tree": frozenset({"state"}),
    "block": frozenset({"name", "state"}),
    "ports": frozenset(),
    "topology": frozenset(),
    "measures": frozenset(),
    "datums": frozenset(),
    "validate": frozenset(),
    "clearance": frozenset({"a", "b", "state"}),
    "sweep": frozenset(),
    "drc": frozenset(),
    "bom": frozenset(),
    "fasten": frozenset(),
    "interview": frozenset(),
    "freedom": frozenset(),
    "stability": frozenset(),
    "mechanics": frozenset(),
    "literature": frozenset({"block"}),
    "fret": frozenset(),
    "links": frozenset(),
    "print": frozenset({"block", "fmt", "path"}),
    "fab": frozenset(),
}
assert {v for v, keys in _VIEW_ARGS.items() if "state" in keys} == _STATE_VIEWS


def _vet_view_args(view: str, args: dict[str, Any] | None) -> None:
    """Reject any ``args=`` key a view doesn't accept — loudly, rather
    than silently ignoring it (gripe 334766: ``args={'state': ...}`` used
    to be accepted and dropped on every view, returning a confident answer
    over the wrong (or just the default) geometry with no error at all).
    ``state`` poses declared block states (blocktree slice 2) on the views
    listed in :data:`_STATE_VIEWS`; elsewhere it still gets its own
    pointed message rather than a generic "unknown key", since a caller
    reaching for it on, say, ``view='drc'`` is reaching for a real
    capability that just isn't wired there yet. Checked against
    :data:`_VIEW_ARGS` — the same table both this function and every
    ``view=`` branch above implicitly agree on, so an accepted key can
    never silently drift out of sync with what a view actually reads."""
    if not args:
        return
    allowed = _VIEW_ARGS.get(view)
    if allowed is None:
        return  # unrecognized view — the dispatch above raises its own error
    unknown = sorted(set(args) - allowed)
    if not unknown:
        return
    accepted = ", ".join(sorted(allowed)) if allowed else "(none)"
    if "state" in unknown:
        supported = ", ".join(sorted(f"{v or 'tree'!r}" for v in _STATE_VIEWS))
        raise BadInput(
            f"state is not supported on view={view or 'tree'!r} — declared "
            f"block states pose on view={supported} only",
            next=f"accepted args for view={view or 'tree'!r}: {accepted}",
        )
    raise BadInput(
        f"unknown args key(s) {unknown} for view={view or 'tree'!r}; "
        f"accepted: {accepted}"
    )


def _state_arg_map(
    store: Any, ref_id: int, tree: SeTree, args: dict[str, Any] | None
) -> dict[str, design_states.BlockState]:
    """``args.state`` → ``{block label: BlockState}`` — se's discrete-domain
    analogue of cad's ``CadHandler._state_arg`` (blocktree slice 2's
    get-time posing rung, "copy cad's posing surface, do not invent one").
    Resolved and vetted fully up front, before any view renders: an
    unknown block name or an undeclared state name is rejected loudly,
    naming what IS available — se's existing rejection-message style —
    never silently ignored or partially applied. Only called once
    :func:`_vet_view_args` has already confirmed ``view`` accepts
    ``state`` at all (:data:`_STATE_VIEWS`).

    Ordinary blocks only — the same rule ``declare_states`` itself
    follows: a block's declared states live on it directly, and an
    instance has no states of its own to be posed into (instance-side
    state resolution is a later round, same as the states/transitions
    render section already notes)."""
    if not args or args.get("state") is None:
        return {}
    raw = args["state"]
    if not isinstance(raw, dict):
        raise BadInput(
            "args.state must be a JSON object of {block: state_name}",
            next="get(kind='se', id='<slug>', view='block', "
            "args={'name': '<block>', 'state': {'<block>': '<state name>'}})",
        )
    resolved: dict[str, design_states.BlockState] = {}
    for block_token, state_name in raw.items():
        try:
            node = resolve_block(tree, block_token)
        except AmbiguousLabel as exc:
            raise BadInput(str(exc)) from exc
        if node is None:
            raise NotFound(_block_not_found(tree, str(block_token)))
        if node.template is not None:
            raise BadInput(
                f"args.state: block {node.name!r} is an instance (of "
                f"{node.template!r}) — declared states live on the "
                "template, and instance-side posing isn't supported yet; "
                f"pose {node.template!r} instead"
            )
        assert node.uid is not None, "a loaded block always carries its uid"
        by_name = {s.name: s for s in design_states.states_for(store, ref_id, node.uid)}
        if not by_name:
            raise BadInput(
                f"args.state: block {node.name!r} has no declared states "
                "(declare_states first)"
            )
        want = str(state_name).strip()
        if want not in by_name:
            raise BadInput(
                f"args.state: block {node.name!r} has no state "
                f"{state_name!r} — declared: {', '.join(sorted(by_name))}"
            )
        resolved[node.name] = by_name[want]
    return resolved


def _apply_state_arg(
    tree: SeTree, resolved: dict[str, design_states.BlockState]
) -> None:
    """Mutate ``tree`` in place so every downstream view reads the posed
    geometry — TRANSIENT, for this one ``get`` only: the tree is a fresh
    load discarded at the end of the call (nothing here ever reaches
    ``save_tree``; the persistent counterpart is the ``set_current_state``
    op, materialized by :func:`_materialize_states`).

    Envelope: the state's own (``None`` = unchanged, the block keeps its
    default). Ports: ``port_pose_overrides``, keyed by port name
    (vetted at ``declare_states`` time,
    :func:`~precis_se.ops._vet_port_pose_overrides`) —

    * ``direction`` replaces the port's direction outright (already
      unit-normalized at write time);
    * ``pose``/``rot`` are a rigid DELTA in the block frame: the
      translation adds to the port's own origin, the rotation composes on
      top of the port's own frame.

    A delta on a port whose own :attr:`~precis.blocktree.types.Port.pose`
    is ``None`` is **not** applied — the pose stays null and the
    consumers keep their envelope approximation, because there is no
    origin to displace and inventing one (implicitly ``[0,0,0]``) would
    manufacture a position the design never declared. The state row still
    shows the raw override JSON, so the intent is visible either way.

    An override naming a port the block doesn't currently have is skipped
    rather than raised — the same read-time honesty a dangling reference
    gets elsewhere in se; wiring a checker for it is drc.py's job, out of
    scope this round."""
    for name, state in resolved.items():
        node = tree.blocks[name]
        if state.envelope is not None:
            node.envelope = state.envelope
        for port_name, override in (state.port_pose_overrides or {}).items():
            port = node.ports.get(port_name)
            if port is None or not isinstance(override, dict):
                continue
            direction = override.get("direction")
            if direction is not None:
                port.direction = list(direction)
            _apply_port_delta(port, override)


def _apply_port_delta(port: PortSpec, override: dict[str, Any]) -> None:
    """Apply one state's rigid ``pose``/``rot`` delta to ``port``, in the
    block's local frame. No-op for a pose-less port (see
    :func:`_apply_state_arg`). Rotation composes through the cad kernel's
    own transforms rather than a second Euler implementation here — a null
    port ``rot`` reads as zeros, and the composed matrix goes back to
    Euler radians via :func:`~precis.cad.vec.euler_rad_from_matrix`.

    Frame: the delta rotation is expressed in the BLOCK frame — the same
    frame the ``pose`` delta is added in — so the result is
    ``R_delta @ R_port`` (a hinge swinging the port about a block axis),
    NOT the port-local ``R_port @ R_delta`` that a scene mate's spin uses.
    Off-axis base + delta pairs do not commute, so the order is pinned by
    a multi-axis test, not just the single-axis ones."""
    delta_pose = override.get("pose")
    delta_rot = override.get("rot")
    if (delta_pose is None and delta_rot is None) or port.pose is None:
        return
    if delta_pose is not None:
        port.pose = [p + float(d) for p, d in zip(port.pose, delta_pose, strict=True)]
    if delta_rot is not None:
        base = port.rot or [0.0, 0.0, 0.0]
        composed = cad_rotation(*(float(x) for x in delta_rot)).compose(
            cad_rotation(*(float(x) for x in base))
        )
        # ``-0.0`` is what the arcsin/arctan2 round trip hands back for an
        # untouched axis; it compares equal to zero but RENDERS as "-0",
        # which reads like a real (tiny, negative) angle. Normalize once,
        # here, rather than teaching every renderer about it.
        port.rot = [0.0 if x == 0.0 else x for x in cad_euler_rad(composed.R)]


def _clearance_verdict(gap: float, resolution: float) -> str:
    """Verdict for a signed gap, honest about the query's own resolution.

    ``resolution`` is :attr:`~precis.cad.relate.ClearanceResult.resolution`
    — a fraction of the smaller block's size, so the band means the same
    thing whatever the design is drawn in. A gap inside it is *not* clear
    and *not* interference: the query cannot tell (gr334763 — printing an
    unqualified "clear" for a sub-resolution gap is how a real
    interpenetration read as clearance)."""
    if gap < -resolution:
        return "interference"
    if abs(gap) <= resolution:
        return "≈ touching (within resolution)"
    return "clear"


#: Cap on the number of unique CONNECTS pairs the no-args clearance digest
#: (gr338444) will actually query — a survey, not an unbounded fan-out of
#: SDF solves. Pairs beyond this many are simply not computed; the digest
#: says so ("showing first N of M") rather than silently truncating.
_CLEARANCE_DIGEST_BUDGET = 64


@dataclass
class _PairClearance:
    """One pairwise clearance query's result, already reduced to display
    units (metres) — the shared payload both the targeted ``view='clearance'``
    render and the no-args all-pairs digest build their output from."""

    a_name: str
    b_name: str
    gap_m: float
    resolution_m: float
    verdict: str
    witness_m: list[float]


class _ClearanceUnavailable(Exception):
    """A clearance query between two named blocks could not run — no
    effective envelope on one side, incompatible scales, or a corrupt
    stored envelope. Raised by :func:`_pair_clearance` only; the targeted
    ``view='clearance'`` (explicit ``a=``/``b=``) re-raises this as
    ``BadInput`` since the caller asked about that one pair specifically,
    while the no-args all-pairs digest catches it and renders a one-line
    skip note instead — a survey shouldn't abort on one bad pair."""


def _pair_clearance(tree: SeTree, a_name: str, b_name: str) -> _PairClearance:
    """Signed minimum envelope gap between two named blocks
    (:func:`precis.cad.relate.clearance`, the exact-sign CSG SDF at
    metres — nm's ``_render_clearance`` transferred; its shaft-in-bored-hub
    case is literally se's hub-through-wheel interface). **Nested blocks
    v1**: a block's envelope is its own only — a child's envelope is never
    unioned into its parent's; array members are not expanded (the array
    node is posed once, at its own pose).

    Raises ``NotFound`` for an unknown block name, and
    :class:`_ClearanceUnavailable` for any condition that keeps the query
    from running at all (see that class's docstring)."""
    a_node = tree.blocks.get(a_name)
    if a_node is None:
        raise NotFound(_block_not_found(tree, a_name))
    b_node = tree.blocks.get(b_name)
    if b_node is None:
        raise NotFound(_block_not_found(tree, b_name))

    envelopes: dict[str, str] = {}
    for name, node in ((a_name, a_node), (b_name, b_node)):
        env = effective_envelope(tree, node)
        if not env:
            raise _ClearanceUnavailable(
                f"block {name!r} has no effective envelope — set one "
                "(set_envelope, or instance a block that has one) before "
                "requesting clearance"
            )
        envelopes[name] = env

    design = CadDesign()
    # Out-of-band designs (nanoscale, planetary) are normalized into kernel
    # units at this seam — the kernel's tolerances are absolute in the
    # numbers it is handed (see validate.kernel_scale); results divide back
    # to metres below.
    scale = se_validate.kernel_scale(
        (envelopes[a_name], a_node), (envelopes[b_name], b_node)
    )
    if scale is None:
        raise _ClearanceUnavailable(
            f"blocks {a_name!r} and {b_name!r} differ too much in size to "
            "share one clearance query (the SDF grid cannot resolve both "
            "bodies at once) — cross-scale seating is a v1 limit; check "
            "each block against a similar-sized neighbour instead"
        )
    for name, node in ((a_name, a_node), (b_name, b_node)):
        if (
            se_validate._posed_component(design, name, envelopes[name], node, scale)
            is None
        ):
            # A stored-but-now-invalid envelope (hand-corrupted data) must
            # surface as a legible message, not a raw traceback — the
            # write path validates via the same parser, but this is a
            # read-time re-check over whatever is actually stored. Re-run
            # the parse/build here purely to name the cause (the seam's
            # shared helper swallowed it into its None).
            try:
                cad_dsl.build_config(envelopes[name])
                cause = "parsed, but its solid is degenerate at this scale"
            except (cad_dsl.DslError, ValueError) as exc:
                cause = str(exc)
            raise _ClearanceUnavailable(
                f"block {name!r} has an invalid envelope {envelopes[name]!r}: {cause}"
            )
    result = cad_relate.clearance(design, a_name, b_name)
    # Verdict thresholds live in kernel space — judge the RAW gap against
    # the RAW resolution; dividing first would re-break nanoscale. Display
    # converts both back to metres below.
    verdict = _clearance_verdict(result.gap, result.resolution)
    return _PairClearance(
        a_name=a_name,
        b_name=b_name,
        gap_m=result.gap / scale,
        resolution_m=result.resolution / scale,
        verdict=verdict,
        witness_m=[float(x) / scale for x in result.point],
    )


def _joint_class_for(tree: SeTree, a_name: str, b_name: str) -> str | None:
    """The declared ``joint`` class (``rigid``/``revolute``/...) of the
    CONNECTS edge between ``a_name`` and ``b_name``, order-independent, or
    ``None`` when no connect names that pair or it declares no joint."""
    pair = frozenset((a_name, b_name))
    for conn in tree.connects:
        if frozenset((conn.a_block, conn.b_block)) != pair:
            continue
        joint = getattr(conn, "joint", None)
        if joint and joint.get("class"):
            return str(joint["class"])
    return None


def _render_clearance(tree: SeTree, args: dict[str, Any] | None) -> str:
    """``view='clearance'`` — with ``args={'a': ..., 'b': ...}``, one
    targeted signed envelope gap between two named blocks. With no args
    (or an empty dict), an all-pairs digest instead: every unique block
    pair named by the design's CONNECTS, worst gap first — a design-wide
    interference/contact survey rather than a single probe (gr338444)."""
    a_name = (args or {}).get("a")
    b_name = (args or {}).get("b")
    if not a_name and not b_name:
        return _render_clearance_digest(tree)
    if not a_name or not b_name:
        raise BadInput(
            "get(kind='se', view='clearance') requires "
            "args={'a': <block>, 'b': <block>}"
        )
    a_name, b_name = str(a_name).strip(), str(b_name).strip()
    if a_name == b_name:
        raise BadInput("get(kind='se', view='clearance'): 'a' and 'b' must differ")
    try:
        pc = _pair_clearance(tree, a_name, b_name)
    except _ClearanceUnavailable as exc:
        raise BadInput(str(exc)) from None
    return _render_pair_clearance(tree, pc)


def _render_pair_clearance(tree: SeTree, pc: _PairClearance) -> str:
    lines = [f"# clearance: {pc.a_name!r} vs {pc.b_name!r}"]
    lines.append(f"gap: {format_quantity(pc.gap_m, 'length')}  ({pc.verdict})")
    lines.append(
        f"resolution: ±{format_quantity(pc.resolution_m, 'length')} (scale-relative)"
    )
    lines.append(f"witness point: [{_fmt3(pc.witness_m)}] m")
    for name in (pc.a_name, pc.b_name):
        kids_with_env = [
            c.name
            for c in tree.blocks.values()
            if c.parent == name and effective_envelope(tree, c)
        ]
        if kids_with_env:
            lines.append(
                f"note: block {name!r} has children with their own "
                f"envelope(s) ({', '.join(sorted(kids_with_env))}) — v1 "
                f"clearance uses only {name!r}'s own envelope, not a "
                "subtree union (a later increment)"
            )
    return "\n".join(lines)


def _render_clearance_digest(tree: SeTree) -> str:
    """All-pairs clearance survey: every unique block pair named by a
    CONNECTS edge (self-connects excluded), worst gap first. A pair that
    can't be queried (missing/invalid envelope, incompatible scales) is
    skipped with a one-line note rather than aborting the whole survey."""
    seen: set[frozenset[str]] = set()
    pairs: list[tuple[str, str]] = []
    for conn in tree.connects:
        if conn.a_block == conn.b_block:
            continue
        key = frozenset((conn.a_block, conn.b_block))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((conn.a_block, conn.b_block))

    if not pairs:
        return (
            "# clearance digest\n"
            "no CONNECTS in this design yet — nothing to survey. connect "
            "two block.port endpoints, or query a pair directly: "
            "view='clearance', args={'a': ..., 'b': ...}"
        )

    total_pairs = len(pairs)
    truncated = total_pairs > _CLEARANCE_DIGEST_BUDGET
    if truncated:
        pairs = pairs[:_CLEARANCE_DIGEST_BUDGET]

    computed: list[_PairClearance] = []
    notes: list[str] = []
    for a_name, b_name in pairs:
        try:
            computed.append(_pair_clearance(tree, a_name, b_name))
        except (_ClearanceUnavailable, NotFound) as exc:
            notes.append(f"- {a_name!r} vs {b_name!r}: skipped — {exc}")

    # Worst (most negative = deepest interference) gap first.
    computed.sort(key=lambda pc: pc.gap_m)

    rows = [
        {
            "a": pc.a_name,
            "b": pc.b_name,
            "joint": _joint_class_for(tree, pc.a_name, pc.b_name) or "—",
            "gap": format_quantity(pc.gap_m, "length"),
            "tag": pc.verdict,
        }
        for pc in computed
    ]
    header = f"# clearance digest — {len(computed)} pair(s) from CONNECTS"
    if truncated:
        header += f" (showing first {_CLEARANCE_DIGEST_BUDGET} of {total_pairs})"
    table = render_agent_table(rows, schema=["a", "b", "joint", "gap", "tag"])
    body = f"{header}\n\n{table}"
    if notes:
        body += "\n\n" + "\n".join(notes)
    return body


# ── sweep ────────────────────────────────────────────────────────────────

#: Cap on the number of discrete-state COMBINATIONS ``view='sweep'`` will
#: actually check. N state-carrying blocks with k states each is k^N
#: combinations — same shape of problem as the clearance digest's pair
#: count (:data:`_CLEARANCE_DIGEST_BUDGET`), same fix: a hard count cap,
#: and combinations beyond it are reported UNCHECKED, never silently
#: dropped (blocktree-library-build-plan.md §Slice 2's "swept across all
#: states" — an honest partial sweep, not a truncated one that reads as
#: complete).
_SWEEP_COMBO_BUDGET = 64

#: Overall wall-clock budget for the WHOLE sweep, seconds — NOT one
#: allowance per combination. Each combination's ``envelope_overlaps``
#: call used to get its own fresh ``validate._OVERLAP_BUDGET_S`` (30s)
#: allowance, so a full ``_SWEEP_COMBO_BUDGET``-combination sweep could
#: run up to combo_budget × 30s ≈ 32 minutes while every other se view
#: caps near 30s (pre-ship review, blocktree slice 2). One deadline for
#: the whole sweep, shared across every combination's call (each call is
#: passed whatever time is actually left), keeps the ceiling the same
#: order of magnitude as a single ``envelope_overlaps`` call regardless of
#: how many combinations are in the domain. Same value as
#: ``validate._OVERLAP_BUDGET_S`` by design — duplicated rather than
#: imported since that name is private to its own module.
_SWEEP_WALL_BUDGET_S = 30.0


def _sweep_domain(
    store: Any, ref_id: int, tree: SeTree
) -> list[tuple[str, list[design_states.BlockState]]]:
    """Every STATE-CARRYING ordinary block in this design (more than one
    declared state — :func:`precis.design.states.state_carrying_uids`, the
    A9 rule's own gate on what may enter a product at all), paired with
    its declared states, in block-name order for a deterministic combo
    enumeration. A block that never called ``declare_states``, or declared
    exactly one, has exactly one implicit state and contributes NOTHING to
    the product — including it would multiply the combination count for a
    block that can never actually differ, which is exactly the mistake
    this helper exists to avoid.

    Instance/array blocks are skipped — the same "declared states live on
    the template" rule :func:`_state_arg_map` already enforces; an
    instance's uid never carries its own ``design_states`` rows."""
    carrying = design_states.state_carrying_uids(store, ref_id)
    domain: list[tuple[str, list[design_states.BlockState]]] = []
    if not carrying:
        return domain
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        if node.template is not None or node.uid is None or node.uid not in carrying:
            continue
        domain.append((name, design_states.states_for(store, ref_id, node.uid)))
    return domain


def _combo_label(
    domain_names: list[str], combo: tuple[design_states.BlockState, ...]
) -> str:
    return ", ".join(
        f"{name}={s.name}" for name, s in zip(domain_names, combo, strict=True)
    )


#: One block's un-posed sweep baseline: its envelope, plus each port's
#: ``(direction, pose, rot)`` — every field :func:`_apply_state_arg` can
#: touch. A ``pose``/``rot`` override is a *delta* (it accumulates), so
#: missing one here would silently compound across combinations rather
#: than merely leaking one.
_PortBaseline = dict[
    str, tuple[list[float] | None, list[float] | None, list[float] | None]
]
_SweepBaseline = dict[str, tuple[str | None, _PortBaseline]]


def _snapshot_sweep_domain(tree: SeTree, domain_names: list[str]) -> _SweepBaseline:
    """Each state-carrying block's UN-posed envelope + per-port
    direction/pose/rot, before the sweep touches anything — the base every
    combination resets to (:func:`_pose_sweep_combo`) so combo *i+1* never
    inherits combo *i*'s overrides, and the tree is restored to exactly
    this once the sweep is done (the loaded tree is discarded at the end of
    ``get`` regardless, but a mid-call reader — e.g. a future finding that
    runs after this one in the same call — must not see a stale posed
    state)."""
    return {
        name: (
            tree.blocks[name].envelope,
            {
                p: (
                    list(port.direction) if port.direction is not None else None,
                    list(port.pose) if port.pose is not None else None,
                    list(port.rot) if port.rot is not None else None,
                )
                for p, port in tree.blocks[name].ports.items()
            },
        )
        for name in domain_names
    }


def _restore_sweep_domain(tree: SeTree, originals: _SweepBaseline) -> None:
    for name, (env0, ports0) in originals.items():
        node = tree.blocks[name]
        node.envelope = env0
        for port_name, (direction, pose, rot) in ports0.items():
            port = node.ports.get(port_name)
            if port is None:
                continue
            port.direction = None if direction is None else list(direction)
            port.pose = None if pose is None else list(pose)
            port.rot = None if rot is None else list(rot)


def _pose_sweep_combo(
    tree: SeTree,
    originals: _SweepBaseline,
    domain_names: list[str],
    combo: tuple[design_states.BlockState, ...],
) -> None:
    """Reset every state-carrying block to its snapshot, then pose this ONE
    combination via :func:`_apply_state_arg` — the same transient posing
    surface ``args={'state': ...}`` already uses, reused rather than
    reinvented."""
    _restore_sweep_domain(tree, originals)
    state_map = dict(zip(domain_names, combo, strict=True))
    _apply_state_arg(tree, state_map)


def _render_sweep(store: Any, ref_id: int, tree: SeTree) -> str:
    """``view='sweep'`` — "does anything collide in ANY declared state?"
    (blocktree-library-build-plan.md §Slice 2's "Done when": "swept across
    all states"). cad's ``view='sweep'`` samples a joint across its
    continuous ``limits:``; se's discrete-domain mirror enumerates the
    cross product of every state-carrying block's declared states instead
    — same question, discrete domain, same report shape (per-combination
    hits, an honest budget line rather than a silent truncation).

    A loop over posed evaluations reusing what already exists —
    :func:`_apply_state_arg` for the pose,
    :func:`precis_se.validate.envelope_overlaps` for the clash — never a
    second geometry engine. No caching/memoization here at all (the A9
    hysteresis rule's warning is moot for a sweep: every combination is
    computed fresh, never looked up by a configuration-only key).

    A design with no state-carrying blocks is a sensible, non-error
    result — se's absence-is-not-failure posture, not an empty-design
    error.

    Bounded two ways, complementary and separately reported:
    :data:`_SWEEP_COMBO_BUDGET` caps how many combinations are even
    attempted; :data:`_SWEEP_WALL_BUDGET_S` caps the whole loop's
    wall-clock, shared across every attempted combination's
    ``envelope_overlaps`` call rather than handed out fresh per call — a
    combination cannot silently borrow another combination's time budget
    and blow the total past what every other se view is bounded by."""
    domain = _sweep_domain(store, ref_id, tree)
    if not domain:
        return (
            "# sweep\n\nno state-carrying blocks in this design (a block "
            "needs 2+ declared states to enter the sweep) — nothing to "
            "check\n\nNext: edit(kind='se', id=..., ops=[{'op':"
            "'declare_states','block':'<name>','states':[{'name':'a'},"
            "{'name':'b'}]}])"
        )
    domain_names = [name for name, _ in domain]
    state_lists = [states for _, states in domain]
    total_combos = 1
    for states in state_lists:
        total_combos *= len(states)
    truncated = total_combos > _SWEEP_COMBO_BUDGET
    combos = list(
        itertools.islice(itertools.product(*state_lists), _SWEEP_COMBO_BUDGET)
    )

    originals = _snapshot_sweep_domain(tree, domain_names)
    hit_rows: list[dict[str, str]] = []
    cross_scale_seen: set[tuple[str, str]] = set()
    unchecked_geometry: set[tuple[str, str]] = set()
    checked = 0
    # One deadline for the whole sweep (:data:`_SWEEP_WALL_BUDGET_S`), not
    # one fresh allowance per combination — each ``envelope_overlaps`` call
    # below is passed whatever's actually left of it, so a wide domain
    # degrades to reporting the tail as unchecked rather than running
    # unbounded (pre-ship review, blocktree slice 2).
    deadline = time.monotonic() + _SWEEP_WALL_BUDGET_S
    time_budget_exceeded = False
    try:
        for combo in combos:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                time_budget_exceeded = True
                break
            _pose_sweep_combo(tree, originals, domain_names, combo)
            overlaps, cross_scale, unchecked_budget = se_validate.envelope_overlaps(
                tree, budget_s=remaining
            )
            checked += 1
            label = _combo_label(domain_names, combo)
            for a_name, b_name, gap in overlaps:
                hit_rows.append(
                    {
                        "states": label,
                        "pair": f"{a_name} ↔ {b_name}",
                        "gap": format_quantity(gap, "length"),
                    }
                )
            cross_scale_seen.update(cross_scale)
            unchecked_geometry.update(unchecked_budget)
    finally:
        # Leave the tree exactly as loaded — the sweep's own poses are
        # every bit as transient as a single args={'state': ...} read's
        # (never written back; use set_current_state to persist one).
        _restore_sweep_domain(tree, originals)

    n_colliding_combos = len({r["states"] for r in hit_rows})
    verdict = (
        "no interference in any checked state ✓"
        if not hit_rows
        else f"⚠ {n_colliding_combos} colliding combination(s)"
    )
    lines = [
        f"# sweep — {len(domain)} state-carrying block(s), "
        f"{checked}/{total_combos} combination(s) checked: {verdict}",
        "states swept: "
        + "; ".join(
            f"{name} ({', '.join(s.name for s in states)})" for name, states in domain
        ),
    ]
    if truncated:
        lines.append(
            f"⚠ {total_combos - len(combos)} combination(s) UNCHECKED — the "
            f"sweep's combination budget ({_SWEEP_COMBO_BUDGET}) ran out "
            "before reaching them; they are unchecked, not clear (narrow "
            "the state-carrying set to sweep it in full)"
        )
    if time_budget_exceeded:
        lines.append(
            f"⚠ {len(combos) - checked} combination(s) UNCHECKED — the "
            f"sweep's overall wall-clock budget ({_SWEEP_WALL_BUDGET_S:g}s) "
            "ran out before reaching them; they are unchecked, not clear. "
            "This is a separate cap from the combination-budget count "
            "(time, not a combination count) — narrow the state-carrying "
            "set or the design's geometry to sweep it in full"
        )
    body = "\n".join(lines) + "\n"
    if hit_rows:
        body += "\n" + render_agent_table(hit_rows, schema=["states", "pair", "gap"])
    if cross_scale_seen:
        pairs_sorted = sorted(cross_scale_seen)
        shown = ", ".join(f"{a}—{b}" for a, b in pairs_sorted[:5])
        more = f" (+{len(pairs_sorted) - 5} more)" if len(pairs_sorted) > 5 else ""
        body += (
            f"\n\n⚠ cross-scale unverifiable in {len(pairs_sorted)} pair(s) "
            f"(at least one checked state): {shown}{more} — block sizes "
            "differ too much to share one SDF query"
        )
    if unchecked_geometry:
        pairs_sorted = sorted(unchecked_geometry)
        shown = ", ".join(f"{a}—{b}" for a, b in pairs_sorted[:5])
        more = f" (+{len(pairs_sorted) - 5} more)" if len(pairs_sorted) > 5 else ""
        body += (
            f"\n\n⚠ {len(pairs_sorted)} pair(s) UNCHECKED by the per-state "
            f"geometry budget (at least one checked state): {shown}{more} "
            "— not clear, not reached"
        )
    return body


def _block_not_found(tree: SeTree, name: str) -> str:
    base = f"no such block: {name!r}"
    if not tree.blocks:
        return f"{base} — the design has no blocks yet"
    roster = ", ".join(sorted(tree.blocks)[:8])
    more = "" if len(tree.blocks) <= 8 else f", … ({len(tree.blocks)} blocks total)"
    return f"{base}. Available blocks: {roster}{more}"
