"""NmHandler — the ``nm`` (nanomachine) kind.

A ``nm`` design is a slug-addressed ref whose content is a **block tree**
(:mod:`precis_nm.ops`/:mod:`precis_nm.persist`) — nested building blocks
with spatial envelopes, poses, declared DOF, per-block **ports**
(capability-gated attachment points), port↔port **connects**, L2
**threading** invariants, and an L5 **binding** to a real ``structure``
design. Maps onto all seven verbs as of this round (slice 3 round 3,
docs/backlog/nm-kind.md "Slice 3 design"):

- ``put``    — create/replace a design from a JSON payload
  ``{description?, ops: [...]}`` (``id=`` the design slug). A re-put
  soft-retires the prior blocks/ports/connects/threading and reinserts the
  new tree, the ``structure``/``cad`` re-put shape. ``bind_structure``/
  ``unbind_structure`` are intercepted here (store-aware, the
  ``import_fragment`` precedent — :meth:`NmHandler._apply_ops_with_bindings`)
  before every other op runs through the pure :func:`~precis_nm.ops.apply_ops`.
- ``edit``   — apply more ops (``ops=`` or ``text=`` JSON) to an existing
  design's live tree.
- ``get``    — list designs (no ``id``), or a design's nested tree TOC
  (``id=slug``, the default view), one block's full record
  (``view='block'``, ``args={'name': ...}``), every block's ports
  (``view='ports'``), L0-L2 feasibility findings (``view='validate'``),
  the signed envelope gap between two blocks (``view='clearance'``,
  ``args={'a': ..., 'b': ...}``, the cad kernel at Å — see
  ``docs/backlog/nm-kind.md`` "Slice 3 design"), or every threading pair +
  declared dof in one table (``view='topology'``).
- ``delete`` — soft-retire a whole design (the ref + every live block/port/
  connect/threading row).
- ``search`` — find designs by intent over each design's one
  ``card_combined`` chunk (title + description + block names/desc/use +
  port names/roles); ``search_hits`` opts into the cross-kind fan-out
  (``kind='*'``).

Ships **dark** behind the ``nm.enabled`` setting (``KindSpec.
requires_setting``; DB row → ``PRECIS_NM_ENABLED`` env → unset/off), the
``chem.enabled`` plumbing verbatim — the kind is hidden from the catalogue
and the dispatcher until enabled. Direct construction (as in tests) is
unaffected by the flag; it only gates the registry.

See ``docs/backlog/nm-kind.md`` (unshipped — no ``nm`` skill exists yet;
write one only once the slice ships, per that doc's closing note).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from psycopg.types.json import Jsonb

from precis import settings as _settings
from precis.cad import dsl as cad_dsl
from precis.cad import relate as cad_relate
from precis.cad.graph import Design as CadDesign
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import SearchHit
from precis_nm import persist
from precis_nm import validate as nm_validate
from precis_nm.ops import (
    BlockNode,
    BlockTree,
    OpError,
    apply_ops,
    effective_dof,
    effective_envelope,
    effective_ports,
)

#: Registered at import time, before ``NmHandler.spec`` is consumed by the
#: kind gate — mirrors ``precis_chem.route``'s ``chem.enabled`` registration.
#: ``default=None`` ⇒ unset means unavailable (dark by default).
_settings.register(
    _settings.SettingSpec(
        key="nm.enabled",
        type="bool",
        env_var="PRECIS_NM_ENABLED",
        default=None,
        doc="Dark-ship flag for the `nm` kind (precis_nm plugin).",
    )
)


class NmHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="nm",
        title="Nanomachine",
        description=(
            "A hierarchical molecular-machine design (precis-nm plugin): "
            "nested blocks with envelopes, poses, DOF, capability-gated "
            "ports, port-to-port connects, L2 threading, and L5 bindings "
            "to structure designs. put/edit take typed ops (add_block/"
            "instance_block/set_pose/remove_block/add_port/remove_port/"
            "connect/disconnect/declare_dof/clear_dof/declare_threading/"
            "remove_threading/bind_structure/unbind_structure); get lists "
            "designs or renders one (view='tree'|'block'|'ports'|"
            "'validate'|'clearance'|'topology'; block/ports take "
            "args={'name':...}, clearance takes args={'a':...,'b':...}); "
            "delete soft-retires; search finds by intent. Envelopes reuse "
            "the cad mini-DSL (e.g. 'cyl:r5h2') at Angstrom scale — "
            "view='clearance' runs the cad kernel's signed-distance gap "
            "between two blocks' envelopes. A bond connect needs both "
            "ports to share a role (default 'covalent'). bind_structure "
            "maps ports to atoms in a real structure design, gated by each "
            "port's expected_element. The LLM traverses a block tree, "
            "never atoms directly."
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
        views=("tree", "block", "ports", "validate", "clearance", "topology"),
        # Dark-ship: hidden until the `nm.enabled` setting resolves.
        requires_setting=("nm.enabled",),
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("nm: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # ── bindings (store-aware ops, intercepted before apply_ops) ───────
    def _apply_ops_with_bindings(
        self, tree: BlockTree, ops: list[dict[str, Any]]
    ) -> str | None:
        """Walk ``ops`` in order, applying ``bind_structure``/
        ``unbind_structure`` here (store-aware; ``ops.py`` never sees them —
        the ``import_fragment`` precedent,
        ``handlers/structure.py::_apply_ops_with_imports``) and everything
        else through the ordinary :func:`~precis_nm.ops.apply_ops`, one op
        at a time, so a ``bind_structure`` sharing a call with an earlier
        ``add_block``/``add_port`` sees exactly what that op already
        placed. Returns a compact echo of every binding op (for the
        caller's response), or ``None`` when there were none."""
        echoes: list[str] = []
        for op in ops:
            if not isinstance(op, dict) or "op" not in op:
                raise BadInput(f"op missing 'op' key: {op!r}")
            name = op["op"]
            if name == "bind_structure":
                echoes.append(self._bind_structure(tree, op))
                continue
            if name == "unbind_structure":
                echoes.append(self._unbind_structure(tree, op))
                continue
            try:
                apply_ops(tree, [op])
            except OpError as exc:
                raise BadInput(str(exc)) from exc
        return "\n".join(echoes) if echoes else None

    def _bind_structure(self, tree: BlockTree, op: dict[str, Any]) -> str:
        """``{"op": "bind_structure", "block": <name>, "design": <structure
        slug>, "ports": {<port name>: <atom label>, ...} (optional)}`` — the
        L5 binding (``nm_blocks.bound_design`` + per-port
        ``nm_ports.bound_atom``/``bound_design``, migration
        ``0003_nm_bindings.sql``). Loads the source structure design via
        the same ``get_ref``/``structure_load`` path
        ``handlers/structure.py::_import_fragment`` uses, then checks every
        mapped port exists on the block, every mapped atom label exists in
        the structure, and (the capability-gate philosophy: a loud failure
        at bind time, not a silent drift) each port's declared
        ``expected_element`` — when set — matches the bound atom's actual
        element. Nothing is written until every mapped port passes.

        **Rebind semantics**: binding to a *different* design than the
        block's current ``bound_design`` first clears every existing port
        binding on the block (a full re-target — the old design's atom
        labels mean nothing in the new design's scene, so leaving them
        would strand stale ``bound_atom`` values that ``validate`` would
        then look up in the wrong scene: a false ``dangling_binding``, or
        worse, a silently wrong element check on a label that happens to
        collide). Binding again to the *same* design is incremental — an
        earlier call's port map survives a later call that only maps
        additional (or different) ports, so a design can be filled in
        across several ``bind_structure`` calls."""
        block = op.get("block")
        if not block or not str(block).strip():
            raise BadInput("bind_structure needs 'block'")
        block_name = str(block).strip()
        node = tree.blocks.get(block_name)
        if node is None:
            raise NotFound(_block_not_found(tree, block_name))
        if node.template is not None:
            raise BadInput(
                f"block {block_name!r} is an instance (of {node.template!r}) "
                "— an instance binds via its template; bind_structure on "
                f"{node.template!r} instead"
            )
        design = op.get("design")
        if not design or not str(design).strip():
            raise BadInput("bind_structure needs 'design' (the structure slug)")
        design_slug = str(design).strip()
        src_ref = self.store.get_ref(kind="structure", id=design_slug)
        if src_ref is None:
            roster = ", ".join(
                r.slug
                for r in self.store.list_refs(
                    kind="structure", order_by="id_desc", limit=8
                )
                if r.slug
            )
            raise NotFound(
                f"no structure design {design_slug!r}",
                next=(
                    f"known designs: {roster}"
                    if roster
                    else "put(kind='structure', id=..., ...) to create one first"
                ),
            )
        scene, _handles = self.store.structure_load(src_ref.id)
        ports_raw = op.get("ports")
        if ports_raw is None:
            ports_raw = {}
        if not isinstance(ports_raw, dict):
            raise BadInput(
                "bind_structure 'ports' must be a JSON object {port name: atom label}"
            )
        resolved: dict[str, str] = {}
        for port_name_raw, atom_label_raw in ports_raw.items():
            port_name = str(port_name_raw).strip()
            if port_name not in node.ports:
                roster = ", ".join(sorted(node.ports)) if node.ports else "(none)"
                raise NotFound(
                    f"no such port on block {block_name!r}: {port_name!r}. "
                    f"Available ports: {roster}"
                )
            atom_label = str(atom_label_raw).strip()
            atom = scene.atoms.get(atom_label)
            if atom is None:
                labels = sorted(scene.atoms)
                roster = ", ".join(labels[:8]) if labels else "(none)"
                more = "" if len(labels) <= 8 else f", … ({len(labels)} atoms total)"
                raise NotFound(
                    f"no such atom in structure {design_slug!r}: "
                    f"{atom_label!r}. Available atoms: {roster}{more}"
                )
            port = node.ports[port_name]
            if port.expected_element and port.expected_element != atom.element:
                raise BadInput(
                    f"bind_structure: port {block_name}.{port_name} expects "
                    f"element {port.expected_element!r}, but atom "
                    f"{atom_label!r} in {design_slug!r} is {atom.element!r}"
                )
            resolved[port_name] = atom_label
        if node.bound_design is not None and node.bound_design != design_slug:
            # Full re-target (docstring above): the old design's atom
            # labels are meaningless against the new scene, so every
            # existing port binding on this block is cleared before this
            # call's map is applied — never left stranded against a scene
            # they were never bound to.
            for p in node.ports.values():
                p.bound_design = None
                p.bound_atom = None
        node.bound_design = design_slug
        for port_name, atom_label in resolved.items():
            p = node.ports[port_name]
            p.bound_design = design_slug
            p.bound_atom = atom_label
        mapped = ", ".join(f"{p}→{a}" for p, a in resolved.items())
        return f"bound block {block_name!r} to structure {design_slug!r}" + (
            f" (ports: {mapped})" if mapped else ""
        )

    def _unbind_structure(self, tree: BlockTree, op: dict[str, Any]) -> str:
        """``{"op": "unbind_structure", "block": <name>}`` — clears the
        block's ``bound_design`` and every one of its ports'
        ``bound_design``/``bound_atom``."""
        block = op.get("block")
        if not block or not str(block).strip():
            raise BadInput("unbind_structure needs 'block'")
        block_name = str(block).strip()
        node = tree.blocks.get(block_name)
        if node is None:
            raise NotFound(_block_not_found(tree, block_name))
        node.bound_design = None
        cleared = 0
        for p in node.ports.values():
            if p.bound_design is not None or p.bound_atom is not None:
                p.bound_design = None
                p.bound_atom = None
                cleared += 1
        return f"unbound block {block_name!r} ({cleared} port binding(s) cleared)"

    def _render_validate(self, tree: BlockTree) -> str:
        """``view='validate'`` — L0-L2 feasibility findings
        (:mod:`precis_nm.validate`'s module docstring). Hydrates every
        structure design any block/port references via ``bound_design``
        once, up front, into the ``bound_scenes`` mapping the pure
        validator needs for ``dangling_binding``/``binding_element_mismatch``
        (validate.py stays store-free — this is the "assemble in the view
        path" half of that split)."""
        slugs = {n.bound_design for n in tree.blocks.values() if n.bound_design}
        slugs |= {
            p.bound_design
            for n in tree.blocks.values()
            for p in n.ports.values()
            if p.bound_design
        }
        bound_scenes: dict[str, dict[str, str] | None] = {}
        for slug in slugs:
            ref = self.store.get_ref(kind="structure", id=slug)
            if ref is None:
                bound_scenes[slug] = None
                continue
            scene, _handles = self.store.structure_load(ref.id)
            bound_scenes[slug] = {
                label: atom.element for label, atom in scene.atoms.items()
            }
        findings = nm_validate.validate(tree, bound_scenes=bound_scenes)
        if not findings:
            return "✓ no validator findings"
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
        return f"# {n_error} error(s), {n_warn} warning(s)\n" + render_agent_table(
            rows, schema=["severity", "rule", "subject", "detail"]
        )

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
                "put(kind='nm') requires id= (the design slug)",
                next="put(kind='nm', id='rotaxane1', "
                'text=\'{"ops":[{"op":"add_block","name":"axle",'
                '"envelope":"cyl:r2h20"}]}\')',
            )
        slug = str(id).strip()
        payload = _payload(text, args)
        ops = payload.get("ops") or []
        if not isinstance(ops, list):
            raise BadInput("put(kind='nm') 'ops' must be a list of typed ops")
        description = str(payload.get("description") or "").strip()
        tree = BlockTree()
        echo = self._apply_ops_with_bindings(tree, ops)
        ttl = (title or slug).strip() or slug
        existing = self.store.get_ref(kind="nm", id=slug)
        meta = {"description": description}
        with self.store.tx() as conn:
            if existing is None:
                ref = self.store.insert_ref(
                    kind="nm", slug=slug, title=ttl, meta=meta, conn=conn
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
        verb = "created" if created else "replaced"
        body = f"# nm design '{slug}' {verb}\n\n" + _render_tree(tree, ttl, description)
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
            raise BadInput("edit(kind='nm') requires id= (the design slug)")
        ref = self.store.get_ref(kind="nm", id=str(id).strip())
        if ref is None:
            raise NotFound(f"nm design {id!r} not found")
        op_list = ops
        if op_list is None:
            payload = _payload(text, args)
            op_list = payload.get("ops", payload if isinstance(payload, list) else [])
        if not op_list:
            raise BadInput(
                "edit(kind='nm') requires ops=",
                next="edit(kind='nm', id="
                f"{str(ref.slug)!r}, "
                "ops=[{'op':'add_block','name':'fork','parent':'axle'}])",
            )
        tree = persist.load_tree(self.store, ref.id)
        echo = self._apply_ops_with_bindings(tree, op_list)
        description = str((ref.meta or {}).get("description") or "").strip()
        ttl = ref.title or str(ref.slug)
        persist.save_tree(
            self.store,
            ref_id=ref.id,
            tree=tree,
            card_text=_card_text(ttl, description, tree),
        )
        body = f"# nm design '{ref.slug}' edited\n\n" + _render_tree(
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
        ref = self.store.get_ref(kind="nm", id=str(id).strip())
        if ref is None:
            raise NotFound(f"nm design {id!r} not found")
        tree = persist.load_tree(self.store, ref.id)
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
                    "get(kind='nm', view='block') requires args={'name': ...}"
                )
            block_name = str(name).strip()
            node = tree.blocks.get(block_name)
            if node is None:
                raise NotFound(_block_not_found(tree, block_name))
            return Response(body=_render_block(tree, node))
        if v == "ports":
            return Response(body=_render_ports(tree))
        if v == "validate":
            return Response(body=self._render_validate(tree))
        if v == "clearance":
            return Response(body=_render_clearance(tree, args))
        if v == "topology":
            return Response(body=_render_topology(tree))
        raise BadInput(
            f"unknown nm view {view!r}",
            next="view='tree' (default, nested TOC) | view='block' "
            "(args={'name':...}) | view='ports' | view='validate' | "
            "view='clearance' (args={'a':...,'b':...}) | view='topology'",
        )

    # ── delete ───────────────────────────────────────────────────────
    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='nm') requires id= (the design slug)")
        ref = self.store.get_ref(kind="nm", id=str(id).strip())
        if ref is None:
            raise NotFound(f"nm design {id!r} not found")
        n = persist.retire_design(self.store, ref.id)
        return Response(body=f"retired nm design '{ref.slug}' ({n} block(s))")

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
                "search(kind='nm') requires q=",
                next="search(kind='nm', q='rotaxane axle')",
            )
        triples = self._card_search(
            str(q), query_vec=None, mode=mode, page_size=page_size
        )
        if not triples:
            return Response(
                body=f"no nm designs match {q!r}\n\n"
                "Next: widen with mode='semantic', or add a 'description' "
                "to a design so it's findable by purpose."
            )
        lines = [f"# {len(triples)} nm design(s) for {q!r}"]
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
            kind="nm",
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
                    kind="nm",
                    title=ref.title or ref.slug or "",
                    preview=preview,
                    slug=ref.slug,
                    ref_id=ref.id,
                    dedupe_key=f"nm:{ref.slug or ref.id}",
                )
            )
        return out

    # ── helpers ──────────────────────────────────────────────────────
    def _render_list(self) -> Response:
        refs = self.store.list_refs(kind="nm", order_by="id_desc", limit=50)
        if not refs:
            return Response(
                body="no nm designs yet\n\nNext: put(kind='nm', id='rotaxane1', "
                'text=\'{"ops":[{"op":"add_block","name":"axle"}]}\')'
            )
        lines = [f"# {len(refs)} nm design(s)"]
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
            raise BadInput(f"nm payload must be JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise BadInput("nm payload must be a JSON object {description?, ops}")
        return obj
    return {}


def _card_text(title: str, description: str, tree: BlockTree) -> str:
    """The one embeddable summary per design — title + block names + every
    block's desc/use text + port names/roles + the design's own
    description, so ``search(kind='nm')`` lands on intent (e.g. a design
    described only by its ports' 'covalent'/'coordination' roles is still
    findable by that vocabulary)."""
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
    return f"{title} (nanomachine design).{intent} Blocks: {names}.{body}{ports}"


def _fmt3(v: list[float]) -> str:
    return ", ".join(f"{x:g}" for x in v)


#: The ``[rot]``/``[trans]`` tree/block-line dof marker per declared kind —
#: any other (future) kind falls back to itself, so a marker never disappears.
_DOF_ABBR = {"rotational": "rot", "translational": "trans"}


def _dof_marker(dof: dict[str, Any] | None) -> str:
    if not dof:
        return ""
    kind = str(dof.get("kind") or "?")
    return f"[{_DOF_ABBR.get(kind, kind)}]"


def _block_line(tree: BlockTree, node: BlockNode) -> str:
    parts = [node.name]
    if node.template:
        parts.append(f"(instance of {node.template})")
    # An instance's own ``envelope``/``dof`` fields are always None (see
    # _op_instance_block's rejection of those keys) — resolve them the same
    # way ports already do (effective_ports), marked as inherited so they
    # don't read as declared directly on the instance.
    env = effective_envelope(tree, node)
    if env:
        marker = f" (from {node.template})" if node.template else ""
        parts.append(f"env={env}{marker}")
    parts.append(f"pose=[{_fmt3(node.pose)}]")
    if any(node.rot):
        parts.append(f"rot=[{_fmt3(node.rot)}]")
    dof = effective_dof(tree, node)
    if dof:
        marker = f" (from {node.template})" if node.template else ""
        parts.append(f"{_dof_marker(dof)}{marker}")
    if node.bound_design:
        parts.append(f"⇒ st:{node.bound_design}")
    n_ports = len(effective_ports(tree, node))
    if n_ports:
        parts.append(f"[{n_ports} port{'s' if n_ports != 1 else ''}]")
    if node.descr:
        parts.append(f"— {node.descr}")
    return "  ".join(parts)


def _render_tree(tree: BlockTree, title: str, description: str) -> str:
    lines = [f"# nm design '{title}'"]
    if description:
        lines.append(description)
    if not tree.blocks:
        lines.append("\n(no blocks yet)")
        return "\n".join(lines)
    children: dict[str | None, list[str]] = {}
    for name, node in tree.blocks.items():
        children.setdefault(node.parent, []).append(name)
    for kids in children.values():
        kids.sort()

    def _walk(name: str, depth: int, path: tuple[str, ...]) -> None:
        node = tree.blocks[name]
        lines.append(f"{'  ' * depth}- {_block_line(tree, node)}")
        # An instance's subtree is the template's, resolved here at read
        # time — never copied onto the instance row (module docstring).
        # ``path`` is every "expansion source" (template, or the plain
        # block name when there's none) already on the current walk —
        # defense in depth against a cyclic instance chain (A hosts an
        # instance of B, B hosts an instance of A) that reached this row
        # despite ``ops._find_instance_cycle`` rejecting it at write time
        # (hand-corrupted data, a future bug elsewhere): render must never
        # be able to infinite-recurse regardless of what's stored, so a
        # repeated source stops the walk with a visible marker instead of
        # descending again.
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


def _fmt_expected(element: str | None, hybridization: str | None) -> str:
    bits = [b for b in (element, hybridization) if b]
    return " ".join(bits) if bits else "—"


def _fmt_direction(direction: list[float] | None) -> str:
    return f"[{_fmt3(direction)}]" if direction is not None else "—"


def _fmt_bound(port: Any) -> str:
    """The port-atom-map cell for a ports table row (:mod:`precis_nm.ops`'s
    ``PortSpec.bound_design``/``bound_atom``, the "one fact, two
    projections" port's atom-side half, set by ``bind_structure``)."""
    if port.bound_design and port.bound_atom:
        return f"{port.bound_design}:{port.bound_atom}"
    return "—"


def _render_block(tree: BlockTree, node: BlockNode) -> str:
    lines = [f"# block '{node.name}'"]
    if node.template:
        lines.append(f"instance of: {node.template}")
    lines.append(f"parent: {node.parent or '(root)'}")
    lines.append(f"pose: [{_fmt3(node.pose)}] Å")
    lines.append(f"rot: [{_fmt3(node.rot)}] deg")
    if node.template:
        # desc/use stay raw (an instance genuinely has none — those keys
        # are rejected at instance_block time); envelope/dof resolve via
        # the template like ports do, marked as inherited.
        env = effective_envelope(tree, node)
        marker = f" (from {node.template})" if env else ""
        lines.append(f"envelope: {env or '—'}{marker}")
    else:
        lines.append(f"envelope: {node.envelope or '—'}")
    lines.append(f"desc: {node.descr or '—'}")
    lines.append(f"use: {node.use or '—'}")
    dof = effective_dof(tree, node)
    dof_marker = f" (from {node.template})" if node.template and dof else ""
    lines.append(f"dof: {json.dumps(dof) if dof else '—'}{dof_marker}")
    lines.append(f"bound_design: {node.bound_design or '—'}")

    ports = effective_ports(tree, node)
    lines.append("")
    if ports:
        via = f" (resolved via template {node.template!r})" if node.template else ""
        lines.append(f"## ports{via}")
        rows = [
            {
                "port": p.name,
                "roles": ", ".join(p.roles) or "—",
                "direction": _fmt_direction(p.direction),
                "expected": _fmt_expected(p.expected_element, p.expected_hybridization),
                "bound": _fmt_bound(p),
            }
            for p in ports.values()
        ]
        lines.append(
            render_agent_table(
                rows, schema=["port", "roles", "direction", "expected", "bound"]
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
                "kind": c.kind,
                "objectives": json.dumps(c.objectives) if c.objectives else "—",
            }
            for c in touching
        ]
        lines.append(render_agent_table(rows, schema=["a", "b", "kind", "objectives"]))
    else:
        lines.append("## connects\n(none)")

    touching_threading = [t for t in tree.threading if node.name in (t.a, t.b)]
    lines.append("")
    if touching_threading:
        lines.append("## threading")
        rows = [
            {"relation": f"{t.a} threaded through {t.b}"} for t in touching_threading
        ]
        lines.append(render_agent_table(rows, schema=["relation"]))
    else:
        lines.append("## threading\n(none)")
    return "\n".join(lines)


def _render_ports(tree: BlockTree) -> str:
    """``view='ports'`` — every block's live ports; an instance's row
    resolves from its template (:func:`effective_ports`) and is marked."""
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
                    "direction": _fmt_direction(p.direction),
                    "expected": _fmt_expected(
                        p.expected_element, p.expected_hybridization
                    ),
                    "bound": _fmt_bound(p),
                }
            )
    if not rows:
        return "# nm ports\n\n(no ports declared yet)"
    return f"# {len(rows)} port(s)\n" + render_agent_table(
        rows, schema=["block", "port", "roles", "direction", "expected", "bound"]
    )


def _render_topology(tree: BlockTree) -> str:
    """``view='topology'`` — every live threading pair, plus every block's
    declared dof, in one table (pure over ``tree``; no store access
    needed — unlike ``view='validate'``/``view='clearance'``, topology
    findings never depend on hydrated structure/cad data)."""
    lines = ["# nm topology"]
    lines.append("")
    lines.append("## threading")
    if tree.threading:
        rows = [
            {"a": t.a, "b": t.b, "relation": f"{t.a} threaded through {t.b}"}
            for t in tree.threading
        ]
        lines.append(render_agent_table(rows, schema=["a", "b", "relation"]))
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


def _clearance_verdict(gap: float) -> str:
    if gap < -cad_relate.CONTACT_TOL_MM:
        return "interference"
    if abs(gap) <= cad_relate.CONTACT_TOL_MM:
        return "touching (≈0)"
    return "clear"


def _render_clearance(tree: BlockTree, args: dict[str, Any] | None) -> str:
    """``view='clearance'`` — the signed minimum envelope gap between two
    blocks (:func:`precis.cad.relate.clearance`, the exact-sign CSG SDF at
    Å — see ``docs/backlog/nm-kind.md`` "Slice 3 design" and
    ``precis.cad.relate``'s module docstring on the shaft-in-bored-hub
    false-collision trap this construction avoids). Builds a fresh
    ``cad`` :class:`~precis.cad.graph.Design` in memory with each block's
    effective envelope (:func:`effective_envelope` — an instance uses its
    template's) placed at the block's own pose+rot
    (:func:`precis.cad.vec.pose`). **Nested blocks v1**: a block's envelope
    is its own only — a child's envelope is never unioned into its
    parent's for this check (a later increment) — so this notes it when
    either queried block has children that themselves declare an envelope."""
    a_name = (args or {}).get("a")
    b_name = (args or {}).get("b")
    if not a_name or not b_name:
        raise BadInput(
            "get(kind='nm', view='clearance') requires "
            "args={'a': <block>, 'b': <block>}"
        )
    a_name, b_name = str(a_name).strip(), str(b_name).strip()
    if a_name == b_name:
        raise BadInput("get(kind='nm', view='clearance'): 'a' and 'b' must differ")
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
                "(add_block(envelope=...), or instance a block that has "
                "one) before requesting clearance"
            )
        envelopes[name] = env

    design = CadDesign()
    for name, node in ((a_name, a_node), (b_name, b_node)):
        try:
            prim = cad_dsl.build_config(envelopes[name])
        except (cad_dsl.DslError, ValueError) as exc:
            # A stored-but-now-invalid envelope (hand-corrupted data, or a
            # future bug elsewhere — add_block/instance_block validate at
            # write time via the same parser, ops.py's _validate_envelope,
            # but this is a read-time re-check over whatever is actually
            # stored) must surface as a legible BadInput, not a raw
            # traceback — mirrors _validate_envelope's own wrapping.
            raise BadInput(
                f"block {name!r} has an invalid envelope {envelopes[name]!r}: {exc}"
            ) from exc
        xform = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
        design.add_component(name, design.prim(name, prim, xform))
    result = cad_relate.clearance(design, a_name, b_name)
    verdict = _clearance_verdict(result.gap)

    lines = [f"# clearance: {a_name!r} vs {b_name!r}"]
    lines.append(f"gap: {result.gap:g} Å  ({verdict})")
    lines.append(f"witness point: [{_fmt3([float(x) for x in result.point])}] Å")
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


def _block_not_found(tree: BlockTree, name: str) -> str:
    base = f"no such block: {name!r}"
    if not tree.blocks:
        return f"{base} — the design has no blocks yet"
    roster = ", ".join(sorted(tree.blocks)[:8])
    more = "" if len(tree.blocks) <= 8 else f", … ({len(tree.blocks)} blocks total)"
    return f"{base}. Available blocks: {roster}{more}"
