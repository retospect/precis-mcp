"""SeHandler — the ``se`` (structural envelope) kind.

An ``se`` design is a slug-addressed ref whose content is a **block tree**
(:mod:`precis_se.ops`/:mod:`precis_se.persist`) — nested building blocks
with cad-DSL spatial envelopes in **metres**, rough poses, read-time
template instancing, and first-class linear/polar **arrays**. Maps onto
the verbs as of this round (se slice 1/2, docs/backlog/se-kind.md "Ship
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
  (``view='ports'``), feasibility findings with the filled-fraction
  honesty header (``view='validate'`` — :mod:`precis_se.validate`), or
  the signed envelope gap between two blocks (``view='clearance'``,
  ``args={'a': ..., 'b': ...}``, the cad kernel at metres — the nm
  clearance view's design, transferred).
- ``delete`` — soft-retire a whole design.
- ``search`` — find designs by intent over each design's one
  ``card_combined`` chunk; ``search_hits`` opts into the cross-kind
  fan-out (``kind='*'``).

Ships **dark** behind the ``se.enabled`` setting (``KindSpec.
requires_setting``; DB row → ``PRECIS_SE_ENABLED`` env → unset/off), the
``nm.enabled`` plumbing verbatim — the kind is hidden from the catalogue
and the dispatcher until enabled. Direct construction (as in tests) is
unaffected by the flag; it only gates the registry.

See ``docs/backlog/se-kind.md`` for the full design. The agent-facing
skill lands last, after behavior exists (ship order step 7 — a skill
describing target state misdirects agents).
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
from precis_se import persist
from precis_se import validate as se_validate
from precis_se.ops import (
    OpError,
    SeBlock,
    SeTree,
    apply_ops,
    effective_envelope,
    effective_ports,
)

#: Registered at import time, before ``SeHandler.spec`` is consumed by the
#: kind gate — mirrors ``precis_nm.handler``'s ``nm.enabled`` registration.
#: ``default=None`` ⇒ unset means unavailable (dark by default).
_settings.register(
    _settings.SettingSpec(
        key="se.enabled",
        type="bool",
        env_var="PRECIS_SE_ENABLED",
        default=None,
        doc="Dark-ship flag for the `se` kind (precis_se plugin).",
    )
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
            "remove_block/add_port/remove_port/connect/disconnect); get "
            "lists designs or renders one "
            "(view='tree'|'block'|'ports'|'validate'|'clearance'; block "
            "takes args={'name':...}, clearance takes "
            "args={'a':...,'b':...} and runs the cad kernel's "
            "signed-distance gap between two blocks' posed envelopes); "
            "delete soft-retires; search finds by intent. connect wires "
            "two 'block.port' endpoints; its joint= dict is free-form "
            "until the kinematic-class schema lands. view='validate' "
            "leads with filled-fraction honesty (N/M blocks have "
            "envelopes) and warns on undeclared envelope "
            "interpenetration. Envelopes reuse the cad "
            "mini-DSL (e.g. 'cyl:r0.02h0.01' = a 2 cm-radius disc) — every "
            "field beyond a block's name is optional (suggestive by "
            "contract; an empty design reads as unfilled, not done). "
            "array_block patterns a template block N times "
            "(linear={'count','pitch','axis'} in metres, or "
            "polar={'count','radius','axis'}, axis default +z) — the "
            "block tree stays canonical, members are derived. Ports, "
            "joints, tolerances, loads and manufacturing modes land in "
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
        views=("tree", "block", "ports", "validate", "clearance"),
        # Dark-ship: hidden until the `se.enabled` setting resolves.
        requires_setting=("se.enabled",),
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
        ops = payload.get("ops") or []
        if not isinstance(ops, list):
            raise BadInput("put(kind='se') 'ops' must be a list of typed ops")
        description = str(payload.get("description") or "").strip()
        tree = SeTree()
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
        self._apply(tree, op_list)
        description = str((ref.meta or {}).get("description") or "").strip()
        ttl = ref.title or str(ref.slug)
        persist.save_tree(
            self.store,
            ref_id=ref.id,
            tree=tree,
            card_text=_card_text(ttl, description, tree),
        )
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
        if v == "validate":
            return Response(body=_render_validate(tree))
        if v == "clearance":
            return Response(body=_render_clearance(tree, args))
        raise BadInput(
            f"unknown se view {view!r}",
            next="view='tree' (default, nested TOC) | view='block' "
            "(args={'name':...}) | view='ports' | view='validate' | "
            "view='clearance' (args={'a':...,'b':...})",
        )

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
        parts.append(f"rot=[{_fmt3(node.rot)}]")
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
    lines.append(f"rot: [{_fmt3(node.rot)}] deg")
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
    return "\n".join(lines)


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


def _clearance_verdict(gap: float) -> str:
    if gap < -cad_relate.CONTACT_TOL_MM:
        return "interference"
    if abs(gap) <= cad_relate.CONTACT_TOL_MM:
        return "touching (≈0)"
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
    for name, node in ((a_name, a_node), (b_name, b_node)):
        try:
            prim = cad_dsl.build_config(envelopes[name])
        except (cad_dsl.DslError, ValueError) as exc:
            # A stored-but-now-invalid envelope (hand-corrupted data) must
            # surface as a legible BadInput, not a raw traceback — the
            # write path validates via the same parser, but this is a
            # read-time re-check over whatever is actually stored.
            raise BadInput(
                f"block {name!r} has an invalid envelope {envelopes[name]!r}: {exc}"
            ) from exc
        xform = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
        design.add_component(name, design.prim(name, prim, xform))
    result = cad_relate.clearance(design, a_name, b_name)
    verdict = _clearance_verdict(result.gap)

    lines = [f"# clearance: {a_name!r} vs {b_name!r}"]
    lines.append(f"gap: {result.gap:g} m  ({verdict})")
    lines.append(f"witness point: [{_fmt3([float(x) for x in result.point])}] m")
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
