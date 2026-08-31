"""NmHandler — the ``nm`` (nanomachine) kind.

A ``nm`` design is a slug-addressed ref whose content is a **block tree**
(:mod:`precis_nm.ops`/:mod:`precis_nm.persist`) — nested building blocks
with spatial envelopes, poses, optional declared DOF, per-block **ports**
(capability-gated attachment points) and port↔port **connects**. Maps onto
five of the seven verbs this round (topology/clearance/bind_structure are
later rounds, docs/backlog/nm-kind.md "Slice 3 design"):

- ``put``    — create/replace a design from a JSON payload
  ``{description?, ops: [...]}`` (``id=`` the design slug). A re-put
  soft-retires the prior blocks/ports/connects and reinserts the new tree,
  the ``structure``/``cad`` re-put shape.
- ``edit``   — apply more ops (``ops=`` or ``text=`` JSON) to an existing
  design's live tree.
- ``get``    — list designs (no ``id``), or a design's nested tree TOC
  (``id=slug``, the default view), one block's full record
  (``view='block'``, ``args={'name': ...}``), every block's ports
  (``view='ports'``), or L0 feasibility findings (``view='validate'``).
- ``delete`` — soft-retire a whole design (the ref + every live block/port/
  connect).
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
            "ports, and port-to-port connects. put/edit take typed ops "
            "(add_block/instance_block/set_pose/remove_block/add_port/"
            "remove_port/connect/disconnect); get lists designs or renders "
            "one (view='tree'|'block'|'ports'|'validate'; block/ports take "
            "args={'name':...}); delete soft-retires; search finds by "
            "intent. Envelopes reuse the cad mini-DSL (e.g. 'cyl:r5h2') at "
            "Angstrom scale. A bond connect needs both ports to share a "
            "role (default 'covalent'). The LLM traverses a block tree, "
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
        views=("tree", "block", "ports", "validate"),
        # Dark-ship: hidden until the `nm.enabled` setting resolves.
        requires_setting=("nm.enabled",),
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("nm: store required")
        self.store = hub.store
        self.embedder = hub.embedder

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
        try:
            apply_ops(tree, ops)
        except OpError as exc:
            raise BadInput(str(exc)) from exc
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
        return Response(
            body=f"# nm design '{slug}' {verb}\n\n"
            + _render_tree(tree, ttl, description)
        )

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
        try:
            apply_ops(tree, op_list)
        except OpError as exc:
            raise BadInput(str(exc)) from exc
        description = str((ref.meta or {}).get("description") or "").strip()
        ttl = ref.title or str(ref.slug)
        persist.save_tree(
            self.store,
            ref_id=ref.id,
            tree=tree,
            card_text=_card_text(ttl, description, tree),
        )
        return Response(
            body=f"# nm design '{ref.slug}' edited\n\n"
            + _render_tree(tree, ttl, description)
        )

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
            return Response(body=_render_validate(tree))
        raise BadInput(
            f"unknown nm view {view!r}",
            next="view='tree' (default, nested TOC) | view='block' "
            "(args={'name':...}) | view='ports' | view='validate'",
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


def _block_line(tree: BlockTree, node: BlockNode) -> str:
    parts = [node.name]
    if node.template:
        parts.append(f"(instance of {node.template})")
    # An instance's own ``envelope`` field is always None (see
    # _op_instance_block's rejection of that key) — resolve it the same
    # way ports already do (effective_ports), marked as inherited so it
    # doesn't read as declared directly on the instance.
    env = effective_envelope(tree, node)
    if env:
        marker = f" (from {node.template})" if node.template else ""
        parts.append(f"env={env}{marker}")
    parts.append(f"pose=[{_fmt3(node.pose)}]")
    if any(node.rot):
        parts.append(f"rot=[{_fmt3(node.rot)}]")
    if node.dof:
        parts.append("dof")
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


def _render_block(tree: BlockTree, node: BlockNode) -> str:
    lines = [f"# block '{node.name}'"]
    if node.template:
        lines.append(f"instance of: {node.template}")
    lines.append(f"parent: {node.parent or '(root)'}")
    lines.append(f"pose: [{_fmt3(node.pose)}] Å")
    lines.append(f"rot: [{_fmt3(node.rot)}] deg")
    if node.template:
        # desc/use/dof stay raw (an instance genuinely has none — those
        # keys are rejected at instance_block time); envelope resolves via
        # the template like ports do, marked as inherited.
        env = effective_envelope(tree, node)
        marker = f" (from {node.template})" if env else ""
        lines.append(f"envelope: {env or '—'}{marker}")
    else:
        lines.append(f"envelope: {node.envelope or '—'}")
    lines.append(f"desc: {node.descr or '—'}")
    lines.append(f"use: {node.use or '—'}")
    lines.append(f"dof: {json.dumps(node.dof) if node.dof else '—'}")

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
            }
            for p in ports.values()
        ]
        lines.append(
            render_agent_table(rows, schema=["port", "roles", "direction", "expected"])
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
                }
            )
    if not rows:
        return "# nm ports\n\n(no ports declared yet)"
    return f"# {len(rows)} port(s)\n" + render_agent_table(
        rows, schema=["block", "port", "roles", "direction", "expected"]
    )


def _render_validate(tree: BlockTree) -> str:
    """``view='validate'`` — L0 feasibility findings, the
    ``structure.validate`` error/warn-tier shape (:mod:`precis_nm.validate`'s
    module docstring)."""
    findings = nm_validate.validate(tree)
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


def _block_not_found(tree: BlockTree, name: str) -> str:
    base = f"no such block: {name!r}"
    if not tree.blocks:
        return f"{base} — the design has no blocks yet"
    roster = ", ".join(sorted(tree.blocks)[:8])
    more = "" if len(tree.blocks) <= 8 else f", … ({len(tree.blocks)} blocks total)"
    return f"{base}. Available blocks: {roster}{more}"
