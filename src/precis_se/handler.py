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
  ``nm``/``structure`` re-put shape).
- ``edit``   — apply more ops (``ops=`` or ``text=`` JSON) to an existing
  design's live tree.
- ``get``    — list designs (no ``id``), a design's nested tree TOC
  (``id=slug``, the default view), one block's full record
  (``view='block'``, ``args={'name': ...}``), every block's ports
  (``view='ports'``), measures + stack-up (``view='measures'`` —
  :mod:`precis_se.measures`), feasibility findings with the
  filled-fraction honesty header (``view='validate'`` —
  :mod:`precis_se.validate`), the signed envelope gap between two blocks
  (``view='clearance'``, ``args={'a': ..., 'b': ...}``, the cad kernel
  at metres — the nm clearance view's design, transferred), or the
  graph-tier DRC report (``view='drc'`` — :mod:`precis_se.drc`: joint
  contradictions, mechanism-implied demands, unresolvable relations,
  the declared-vs-derived axis-travel probe), the bought-item rollup
  (``view='bom'`` — :mod:`precis_se.bom`: quantities multiplied through
  the tree's array multiplicities, priced and massed from the
  ``component`` kind's own spec values), the interrogation ledger with
  open questions first (``view='interview'`` — :mod:`precis_se.notes`),
  or the what-is-still-undecided report (``view='freedom'`` —
  :mod:`precis_se.freedom`, DRC's honest counterpart).
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

import json
from collections.abc import Callable
from typing import Any, ClassVar

from psycopg.types.json import Jsonb

from precis.cad import dsl as cad_dsl
from precis.cad import relate as cad_relate
from precis.cad.graph import Design as CadDesign
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import SearchHit
from precis.utils.units import format_quantity
from precis_se import bom as se_bom
from precis_se import drc as se_drc
from precis_se import fasten as se_fasten
from precis_se import freedom as se_freedom
from precis_se import modes as se_modes
from precis_se import notes as se_notes
from precis_se import persist
from precis_se import stability as se_stability
from precis_se import validate as se_validate
from precis_se.measures import stackup as se_stackup
from precis_se.ops import (
    OpError,
    SeBlock,
    SeTree,
    apply_ops,
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
            "plugin, sibling of nm at macro scale): nested blocks with "
            "cad-DSL envelopes in METRES, poses, read-time template "
            "instancing, and first-class arrays. put/edit take typed ops "
            "(add_block/instance_block/array_block/set_pose/set_envelope/"
            "remove_block/add_port/remove_port/connect/disconnect/"
            "set_joint/set_load/add_measure/set_measure/remove_measure/"
            "set_mode/set_binding/add_bom/remove_bom/add_note/"
            "remove_note/formfind); "
            "get lists designs or renders one (view='tree'|'block'|"
            "'ports'|'measures'|'validate'|'clearance'|'drc'|'bom'|"
            "'interview'|'freedom'|'stability'; block takes "
            "args={'name':...}, clearance takes args={'a':...,'b':...} "
            "and runs the cad kernel's signed-distance gap between two "
            "blocks' posed envelopes); delete soft-retires; search finds "
            "by intent. connect wires two 'block.port' endpoints; a "
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
            "set_mode assigns how a block gets made ('purchase' today; "
            "fdm/laser/stock-cut/cnc-2.5ax/sla/atomic are recordable "
            "intent until their implementers ship); set_binding points a "
            "block's realization at a cad|nm|component|part row. "
            "array_block patterns a template block N times "
            "(linear={'count','pitch','axis'} in metres, or "
            "polar={'count','radius','axis'}, axis default +z) — the "
            "block tree stays canonical, members are derived. The "
            "se_propose job, realization and manufacturing modes land in "
            "later slices. The LLM traverses a block tree, never raw "
            "geometry."
        ),
        supports_get=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_search=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=False,
        placement="artifact",
        corpus_role="none",
        can_own_jobs=False,
        views=(
            "tree",
            "block",
            "ports",
            "measures",
            "validate",
            "clearance",
            "drc",
            "bom",
            "fasten",
            "interview",
            "freedom",
            "stability",
        ),
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("se: store required")
        self.hub = hub
        self.store = hub.store
        self.embedder = hub.embedder

    def _apply(self, tree: SeTree, ops: list[dict[str, Any]]) -> None:
        try:
            apply_ops(tree, ops)
        except OpError as exc:
            raise BadInput(str(exc)) from exc

    def _foreign_resolver(self) -> Callable[[str], SeTree | None]:
        """Builds the cross-design ``template`` resolver
        (:attr:`~precis.blocktree.types.Tree.foreign`, docs/backlog/
        blocktree-library-build-plan.md slice 1) — store-aware, so it lives
        here rather than in ``ops.py``/``precis.blocktree`` (both stay
        store-free by design; ``precis_nm.handler``'s
        ``_foreign_resolver`` is the same shape, one per plugin since each
        resolves its OWN kind's designs). Memoized in a plain dict scoped
        to ONE put/edit/get call: resolving the same foreign design's
        template from many ports/blocks/cycle-check hops within that one
        call costs one ``get_ref``+``load_tree``, never one per hop. A slug
        that doesn't resolve to a live ``se`` design (never existed, or
        soft-retired) caches as ``None`` too — never re-queried either."""
        cache: dict[str, SeTree | None] = {}

        def resolve(slug: str) -> SeTree | None:
            if slug not in cache:
                ref = self.store.get_ref(kind="se", id=slug)
                cache[slug] = (
                    persist.load_tree(self.store, ref.id) if ref is not None else None
                )
            return cache[slug]

        return resolve

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
        tree = SeTree()
        # own_slug is known from id= before the ref row even exists — needed
        # for a foreign design's template to recognise a hop back into THIS
        # design (cross-design cycle detection, ops._find_instance_cycle).
        tree.own_slug = slug
        tree.foreign = self._foreign_resolver()
        self._apply(tree, ops)
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
        # After the tx, not inside it: the projection is derived, and a
        # link-sync hiccup must not roll back a saved design (cad's sync
        # sits outside its write for the same reason).
        persist.sync_realized_by(self.store, ref.id, tree)
        verb = "created" if created else "replaced"
        body = f"# se design '{slug}' {verb}\n\n" + _render_tree(tree, ttl, description)
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
        self._apply(tree, op_list)
        description = str((ref.meta or {}).get("description") or "").strip()
        ttl = ref.title or str(ref.slug)
        persist.save_tree(
            self.store,
            ref_id=ref.id,
            tree=tree,
            card_text=_card_text(ttl, description, tree),
        )
        persist.sync_realized_by(self.store, ref.id, tree)
        body = f"# se design '{ref.slug}' edited\n\n" + _render_tree(
            tree, ttl, description
        )
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
            node = tree.blocks.get(block_name)
            if node is None:
                raise NotFound(_block_not_found(tree, block_name))
            return Response(body=_render_block(tree, node))
        if v == "ports":
            return Response(body=_render_ports(tree))
        if v == "measures":
            return Response(body=_render_measures(tree))
        if v == "validate":
            return Response(body=_render_validate(tree))
        if v == "clearance":
            return Response(body=_render_clearance(tree, args))
        if v == "drc":
            return Response(body=_render_drc(tree))
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
        raise BadInput(
            f"unknown se view {view!r}",
            next="view='tree' (default, nested TOC) | view='block' "
            "(args={'name':...}) | view='ports' | view='measures' "
            "(+ stack-up) | view='validate' | view='clearance' "
            "(args={'a':...,'b':...}) | view='drc' (graph tier + DOF "
            "probe) | view='bom' (bought items, multiplied through the "
            "arrays, with cost/mass) | view='fasten' (screw joints: grip "
            "stack-up, clearance holes, thread lead) | view='interview' "
            "(the question/answer/decision ledger, open questions first) "
            "| view='freedom' (what is still undecided, and by whom) "
            "| view='stability' (Maxwell/Calladine rigid / mechanism / "
            "prestress-stabilized over the axial members)",
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

    # ── delete ───────────────────────────────────────────────────────
    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='se') requires id= (the design slug)")
        ref = self.store.get_ref(kind="se", id=str(id).strip())
        if ref is None:
            raise NotFound(f"se design {id!r} not found")
        n = persist.retire_design(self.store, ref.id)
        return Response(body=f"retired se design '{ref.slug}' ({n} block(s))")

    # ── search ───────────────────────────────────────────────────────
    def search(
        self,
        *,
        q: str | None = None,
        mode: str | None = None,
        page_size: int = 20,
        **_kw: Any,
    ) -> Response:
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
_PUT_PAYLOAD_KEYS = frozenset({"description", "ops"})


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


def _render_block(tree: SeTree, node: SeBlock) -> str:
    lines = [f"# block '{node.name}'"]
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

    ports = effective_ports(tree, node)
    lines.append("")
    if ports:
        via = f" (resolved via template {node.template!r})" if node.template else ""
        lines.append(f"## ports{via}")
        rows = [
            {
                "port": p.name,
                "roles": ", ".join(p.roles) or "—",
                "direction": f"[{_fmt3(p.direction)}]" if p.direction else "—",
                "annotations": json.dumps(p.annotations) if p.annotations else "—",
            }
            for p in ports.values()
        ]
        lines.append(
            render_agent_table(
                rows, schema=["port", "roles", "direction", "annotations"]
            )
        )
    else:
        lines.append("## ports\n(none)")

    touching = [c for c in tree.connects if node.name in (c.a_block, c.b_block)]
    lines.append("")
    if touching:
        lines.append("## connects")
        rows = [
            {
                "a": f"{c.a_block}.{c.a_port}",
                "b": f"{c.b_block}.{c.b_port}",
                "joint": json.dumps(c.joint) if c.joint else "—",
                "objectives": json.dumps(c.objectives) if c.objectives else "—",
            }
            for c in touching
        ]
        lines.append(render_agent_table(rows, schema=["a", "b", "joint", "objectives"]))
    else:
        lines.append("## connects\n(none)")

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
    rows = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        ports = effective_ports(tree, node)
        block_label = f"{name} (via {node.template})" if node.template else name
        for p in ports.values():
            rows.append(
                {
                    "block": block_label,
                    "port": p.name,
                    "roles": ", ".join(p.roles) or "—",
                    "direction": f"[{_fmt3(p.direction)}]" if p.direction else "—",
                    "annotations": json.dumps(p.annotations) if p.annotations else "—",
                }
            )
    if not rows:
        return "# se ports\n\n(no ports declared yet)"
    return f"# {len(rows)} port(s)\n" + render_agent_table(
        rows, schema=["block", "port", "roles", "direction", "annotations"]
    )


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


def _measure_row(m: Any) -> dict[str, str]:
    rel = "—"
    if m.relation is not None:
        scale = m.relation.get("scale", 1)
        scale_part = "" if scale == 1 else f"{_fmt_num(scale)} × "
        rel = (
            f"= {scale_part}{m.relation.get('source')} "
            f"+ {_fmt_num(m.relation.get('offset', 0))} "
            f"± {_fmt_num(m.relation.get('tol', 0))}"
        )
    return {
        "measure": f"{m.block}.{m.name}",
        "value": _fmt_in_unit(m.value, m.unit),
        "band": _fmt_band(m),
        "relation": rel,
        "strength": m.strength,
        "origin": m.origin,
        "reason": m.reason or "—",
    }


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
            [_measure_row(m) for m in tree.measures],
            schema=[
                "measure",
                "value",
                "band",
                "relation",
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


def _render_drc(tree: SeTree) -> str:
    """``view='drc'`` — the graph-tier report (:mod:`precis_se.drc`):
    findings under the filled-fraction header (same honesty rule as
    validate — a clean empty design is unfilled, not done), then the DOF
    probe outcomes (including honest skips) and any stack-up problems'
    full rows."""
    report = se_drc.drc(tree)
    fill_line = _fill_fraction_line(tree)
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
                        "note": row.skipped or "",
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
    grip and length check, the thread's lead and travel limits, and the
    clearance/tapped holes it stamps.

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
                        }
                        for m in res.members
                    ],
                    schema=["member", "from", "to", "thickness", "note"],
                )
            )
            lines.append(
                f"grip {_mm(res.grip_m)} · stack {_mm(res.stack_m)} · "
                f"terminated by a {res.termination} · screw is "
                f"{_mm(res.length_m)} under the head, needs "
                f"{_mm(res.required_length_m)}"
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
                        }
                        for h in res.holes
                    ],
                    schema=["feature", "member", "kind", "diameter", "depth"],
                )
            )
        for f in res.findings:
            lines.append(f"⚠ {f.rule}: {f.detail}")
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


def _render_validate(tree: SeTree) -> str:
    """``view='validate'`` — :mod:`precis_se.validate`'s findings under
    the filled-fraction honesty header, on BOTH the clean and the findings
    branch (nm's ``_render_validate`` rule: a fresh scaffold trivially has
    no findings, and a bare check-mark would misread as done)."""
    findings = se_validate.validate(tree)
    fill_line = _fill_fraction_line(tree)
    if not findings:
        return f"✓ no validator findings\n{fill_line}"
    n_error = sum(1 for f in findings if f.severity == "error")
    n_warn = sum(1 for f in findings if f.severity == "warn")
    rows = [
        {
            "severity": f.severity,
            "rule": f.rule,
            "subject": f.subject,
            "detail": f.detail,
        }
        for f in findings
    ]
    return (
        f"# {n_error} error(s), {n_warn} warning(s)\n{fill_line}\n\n"
        + render_agent_table(rows, schema=["severity", "rule", "subject", "detail"])
    )


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


def _render_clearance(tree: SeTree, args: dict[str, Any] | None) -> str:
    """``view='clearance'`` — the signed minimum envelope gap between two
    blocks (:func:`precis.cad.relate.clearance`, the exact-sign CSG SDF at
    metres — nm's ``_render_clearance`` transferred; its shaft-in-bored-hub
    case is literally se's hub-through-wheel interface). **Nested blocks
    v1**: a block's envelope is its own only — a child's envelope is never
    unioned into its parent's (noted when a queried block has enveloped
    children); array members are not expanded (the array node is posed
    once, at its own pose)."""
    a_name = (args or {}).get("a")
    b_name = (args or {}).get("b")
    if not a_name or not b_name:
        raise BadInput(
            "get(kind='se', view='clearance') requires "
            "args={'a': <block>, 'b': <block>}"
        )
    a_name, b_name = str(a_name).strip(), str(b_name).strip()
    if a_name == b_name:
        raise BadInput("get(kind='se', view='clearance'): 'a' and 'b' must differ")
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
            raise BadInput(
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
        raise BadInput(
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
            # surface as a legible BadInput, not a raw traceback — the
            # write path validates via the same parser, but this is a
            # read-time re-check over whatever is actually stored. Re-run
            # the parse/build here purely to name the cause (the seam's
            # shared helper swallowed it into its None).
            try:
                cad_dsl.build_config(envelopes[name])
                cause = "parsed, but its solid is degenerate at this scale"
            except (cad_dsl.DslError, ValueError) as exc:
                cause = str(exc)
            raise BadInput(
                f"block {name!r} has an invalid envelope {envelopes[name]!r}: {cause}"
            )
    result = cad_relate.clearance(design, a_name, b_name)
    # Verdict thresholds live in kernel space — judge the RAW gap against
    # the RAW resolution; dividing first would re-break nanoscale. Display
    # converts both back to metres below.
    verdict = _clearance_verdict(result.gap, result.resolution)

    lines = [f"# clearance: {a_name!r} vs {b_name!r}"]
    lines.append(f"gap: {format_quantity(result.gap / scale, 'length')}  ({verdict})")
    lines.append(
        f"resolution: ±{format_quantity(result.resolution / scale, 'length')} "
        "(scale-relative)"
    )
    lines.append(
        f"witness point: [{_fmt3([float(x) / scale for x in result.point])}] m"
    )
    for name in (a_name, b_name):
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


def _block_not_found(tree: SeTree, name: str) -> str:
    base = f"no such block: {name!r}"
    if not tree.blocks:
        return f"{base} — the design has no blocks yet"
    roster = ", ".join(sorted(tree.blocks)[:8])
    more = "" if len(tree.blocks) <= 8 else f", … ({len(tree.blocks)} blocks total)"
    return f"{base}. Available blocks: {roster}{more}"
