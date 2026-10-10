"""TaxonHandler — nodes in the term taxonomy (migrations 0173 kind, 0174 seed).

The ``concept`` numeric-ref pattern: ``refs.title`` = the print label, text is
``"<name> — <definition>"``, the definition lands in ``meta.definition``, and
the only chunk is the reused ``card_combined`` at ord -1 (name + definition +
aliases) so a node is a vector in the corpus manifold. There is no ord 0 body.

Node keys arrive through the verb-level ``put(meta=...)`` and are checked
against the fixed key set in :mod:`precis.taxonomy.nodes`. ``status`` is
earned, not put: a new node is ``proposed`` and a caller asking for anything
else is refused.

Stage B adds the hierarchy: ``link(rel='specialises', target='taxon:N',
meta={'axis': ...})`` (child -> parent), ``get(view='path')`` and
``get(id='/unrooted')``. Stage D (the seed, migration 0174) adds
``get(id='/unmapped')``: seeded nodes whose legacy dimension label had no
mapping and so carry no ``dimension_kind``. The link rules (taxon at both ends, no cycle, the
inherited start-node contract) live in
:func:`precis.handlers._link_tag_ops.guard_taxon_hierarchy`, called from every
link door; traversal is :mod:`precis.store._taxon_ops`.

Stage C adds search and dedup:

* ``search(q=...)`` is lexical on name AND definition (the ``card_combined``
  chunk is scanned via ``search_card_kinds``) fused with the embedding leg,
  and works with the embedder down.
* ``search(under=<node>, axis=, depth=)`` returns exactly the descendants of
  ``under`` (never ``under`` itself), ``axis=`` restricting every hop to edges
  whose ``meta.axis`` matches, ``depth=N`` meaning <= N hops. With ``q=`` the
  hits are ranked and intersected with that set; without it the set is listed
  by depth then name.
* ``put`` refuses a likely duplicate (name/alias/slug match, then embedding
  nearest-neighbour under ``DEDUP_MAX_DISTANCE`` when an embedder answers;
  lexical-only when it does not). A candidate is **suppressed only on an
  explicit dimension mismatch**: both sides carry ``dimension_kind`` and the
  kinds differ, or both are ``si`` with different ``si_vector``. A side with
  no ``dimension_kind`` stays compatible. ``dedup=False`` mints anyway.
* A node is addressed by numeric id, ``tn<id>`` handle, ``taxon:<id>``, or a
  slash path of names/slugs/aliases (``measurand/temperature``) — resolved on
  ``get``, ``under=`` and ``link(target='taxon:...')``. A path never starts
  with ``/`` (that is a list view); an ambiguous or unmatched path is refused
  with candidates.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar

from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._numeric_ref import _BASE_VIEWS, NumericRefHandler
from precis.protocol import KindSpec
from precis.reading.concepts import normalize_name, split_name_def
from precis.response import Response
from precis.taxonomy.nodes import (
    BOUNDARY_KEYS,
    STATUS_PROPOSED,
    initial_taxon_meta,
    slugify,
    taxon_card_text,
    validate_taxon_meta,
)
from precis.utils import handle_registry
from precis.utils.embed_query import query_vec_for

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")

#: Cosine-distance cutoff for the embedding leg of put-time dedup (0 =
#: identical). Tight on purpose: a near-miss that is really a different term
#: must mint, and ``dedup=False`` is one kwarg away. The general search floor
#: is 0.65; this is a "same term, reworded" bar.
DEDUP_MAX_DISTANCE = 0.25

#: Dedup candidates named in a refusal (the rest are counted).
_DEDUP_SHOW = 5
_GLOSS_CHARS = 80  # definition prefix on a candidate line


def _dimension_clash(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True only on an *explicit* dimension mismatch: both nodes carry a
    ``dimension_kind`` and the kinds differ, or both are ``si`` and their
    ``si_vector`` differs. A node with no ``dimension_kind`` never clashes."""
    ka, kb = a.get("dimension_kind"), b.get("dimension_kind")
    if not ka or not kb:
        return False
    if ka != kb:
        return True
    return ka == "si" and a.get("si_vector") != b.get("si_vector")


def _matches_term(meta: dict[str, Any], seg: str) -> bool:
    """Does a node's slug / norm_name / any alias equal ``seg``?"""
    norm = normalize_name(seg)
    if meta.get("slug") and meta.get("slug") == slugify(seg):
        return True
    if meta.get("norm_name") == norm:
        return True
    return any(normalize_name(str(a)) == norm for a in (meta.get("aliases") or []))


def _lede(definition: str) -> str:
    """First sentence of a definition (the node's stand-alone summary)."""
    d = (definition or "").strip()
    return _SENTENCE_END.split(d, maxsplit=1)[0] if d else ""


class TaxonHandler(NumericRefHandler):
    owns_under = True

    spec: ClassVar[KindSpec] = KindSpec(
        kind="taxon",
        title="Taxon",
        description=(
            "A node in the term taxonomy: a named term with an embeddable "
            "definition, an earned status (proposed / systematic), an optional "
            "dimension (dimension_kind + si_vector) and, on start nodes, a "
            "required-key contract. put text='<name> — <definition>' (node keys "
            "in meta=; a likely duplicate is refused unless dedup=False). Nodes "
            "form a DAG via link(rel='specialises', meta={'axis': …}); "
            "get(view='path') shows the chains up to the start nodes; "
            "search(q=) matches name and definition, search(under=, axis=, "
            "depth=) lists descendants. Ids: 42, tn42, or a path "
            "'measurand/temperature'. See docs/backlog/term-taxonomy.md."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "taxon"
    sense: ClassVar[str] = "taxon"

    #: Embeddable `card_combined` (name + definition + aliases), as concept.
    emits_card: ClassVar[bool] = True
    accepts_put_meta: ClassVar[bool] = True
    #: A taxon has no body chunk: name + definition live only in the ord -1
    #: card, so the lexical/hybrid search scans it (definition-only words hit).
    search_card_kinds: ClassVar[tuple[str, ...] | None] = ("card_combined",)

    def _initial_meta(self, text: str, tags: list[str]) -> dict[str, Any]:
        name, definition = split_name_def(text)
        return initial_taxon_meta(name, definition)

    def _merge_put_meta(
        self, text: str, meta: dict[str, Any], put_meta: dict[str, Any]
    ) -> dict[str, Any]:
        status = put_meta.get("status")
        if status is not None and status != STATUS_PROPOSED:
            raise BadInput(
                f"status={status!r} cannot be set on put: status is earned, "
                "a new taxon is always 'proposed'",
                next="omit status; promotion to 'systematic' is a usage test, not a put",
            )
        checked = validate_taxon_meta(put_meta)
        out = dict(meta)
        for key, val in checked.items():
            # name/norm_name/slug are derived from the text; the text's
            # definition wins over a meta one.
            if key in ("name", "norm_name", "slug"):
                continue
            if key == "definition" and out.get("definition"):
                continue
            out[key] = val
        # Re-run on the merged node so cross-key rules see the final state.
        return validate_taxon_meta(out)

    def _card_text_for(self, text: str, meta: dict[str, Any]) -> str:
        return taxon_card_text(
            meta.get("name", ""), meta.get("definition", ""), meta.get("aliases")
        )

    def _ref_title(self, text: str) -> str:
        return split_name_def(text)[0] or text

    # ── id resolution: numeric, handle, taxon:<id>, path form ───────

    def _resolve_spec(self, spec: str | int) -> int:
        """A node reference to its ref id: ``42``, ``'42'``, ``tn42``,
        ``taxon:42``, or a slash path of names/slugs/aliases
        (``measurand/temperature``; a single segment is a plain term lookup).
        Path/term forms raise on none or several matches."""
        if isinstance(spec, int):
            return spec
        s = str(spec).strip()
        if s.startswith("taxon:"):
            s = s[len("taxon:") :].strip()
        if not s:
            raise BadInput("empty taxon reference", next="get(kind='taxon', id=42)")
        if s.isdigit():
            return int(s)
        parsed = handle_registry.parse(s)
        if parsed is not None and parsed[0] == self.kind and not parsed[1]:
            return parsed[2]
        return self._resolve_path(s)

    def resolve_node(self, spec: str | int) -> int:
        """Public form of the node reference grammar (``42``, ``tn42``,
        ``taxon:42``, ``measurand/temperature``) for sibling kinds, which
        resolve a ``property=`` the same way."""
        return self._resolve_spec(spec)

    def node_path(self, ref_id: int) -> str:
        """``top/…/node`` slug path of a node, for display."""
        return self._path_label(ref_id)

    def _term_matches(self, term: str) -> list[int]:
        return self.store.taxon_find_by_term(
            slug=slugify(term), norm=normalize_name(term)
        )

    def _slug_of(self, ref_id: int) -> str:
        ref = self.store.get_ref(kind=self.kind, id=ref_id)
        if ref is None:
            return "?"
        return str((ref.meta or {}).get("slug") or slugify(ref.title))

    def _path_label(self, ref_id: int) -> str:
        """``top/…/node`` slug path along each node's first parent."""
        chain = [self._slug_of(ref_id)]
        seen = {ref_id}
        cur = ref_id
        while True:
            parents = self.store.taxon_parents(cur)
            if not parents or parents[0][0] in seen:
                break
            cur = parents[0][0]
            seen.add(cur)
            chain.append(self._slug_of(cur))
        return "/".join(reversed(chain))

    def _candidate_lines(self, ids: list[int]) -> str:
        out = []
        for rid in ids[:_DEDUP_SHOW]:
            handle, name = self._node_label(rid)
            line = f"  {handle} {name} ({self._path_label(rid)})"
            ref = self.store.get_ref(kind=self.kind, id=rid)
            if gloss := ((ref.meta or {}).get("definition") if ref else None):
                line += f" — {gloss[:_GLOSS_CHARS]}"
            out.append(line)
        if len(ids) > _DEDUP_SHOW:
            out.append(f"  … {len(ids) - _DEDUP_SHOW} more")
        return "\n".join(out)

    def _chain_matches(self, node: int, segs: list[str], seen: frozenset[int]) -> bool:
        """Does ``node``'s parent chain read ``segs`` (nearest parent last)?"""
        if not segs:
            return True
        for pid, _axis in self.store.taxon_parents(node):
            if pid in seen:
                continue
            ref = self.store.get_ref(kind=self.kind, id=pid)
            if ref is None or not _matches_term(ref.meta or {}, segs[-1]):
                continue
            if self._chain_matches(pid, segs[:-1], seen | {node}):
                return True
        return False

    def _resolve_path(self, path: str) -> int:
        segs = [p.strip() for p in path.split("/")]
        if any(not p for p in segs):
            raise BadInput(
                f"empty segment in taxon path {path!r}",
                next="path form is 'measurand/temperature' (no leading or doubled '/')",
            )
        cands = self._term_matches(segs[-1])
        hits = (
            [c for c in cands if self._chain_matches(c, segs[:-1], frozenset())]
            if len(segs) > 1
            else cands
        )
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise BadInput(
                f"taxon path {path!r} is ambiguous ({len(hits)} nodes):\n"
                + self._candidate_lines(hits),
                next=(
                    "use a longer path or a handle: get(kind='taxon', id='tn<id>')"
                    if len({self._path_label(h) for h in hits}) > 1
                    else "same path: pick by definition and use its handle: "
                    "get(kind='taxon', id='tn<id>')"
                ),
            )
        near = cands or [
            r.id
            for r, _rank in self.store.search_refs_lexical(
                q=segs[-1], kind=self.kind, limit=5
            )
        ]
        msg = f"no taxon matches {path!r}"
        if near:
            msg += "; near candidates:\n" + self._candidate_lines(near)
        raise NotFound(
            msg,
            next="search(kind='taxon', q='...') to find the node, then use its handle",
        )

    # ── put: dedup ──────────────────────────────────────────────────

    def put(
        self,
        *,
        text: str | None = None,
        dedup: bool | None = None,
        auto_refresh_days: int | None = None,
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a node. ``meta=`` is declared explicitly (not read from
        ``**_kw``) so the dispatch strictness gate, which only inspects
        explicit parameters, lets it through. Unless ``dedup=False``, a likely duplicate (same
        name/alias/slug, or an embedding neighbour within
        ``DEDUP_MAX_DISTANCE`` when an embedder answers) is refused naming the
        existing node; candidates whose dimension explicitly differs are never
        offered (see :func:`_dimension_clash`)."""
        if dedup is not False and _kw.get("id") is None and text and text.strip():
            self._dedup_check(text, meta)
        return super().put(
            text=text, auto_refresh_days=auto_refresh_days, meta=meta, **_kw
        )

    def _dedup_check(self, text: str, put_meta: dict[str, Any] | None) -> None:
        meta = self._initial_meta(text, [])
        if put_meta:
            meta = self._merge_put_meta(text, meta, put_meta)
        reasons: dict[int, str] = {}
        for term in [meta.get("name", ""), *(meta.get("aliases") or [])]:
            if str(term).strip():
                for rid in self._term_matches(str(term)):
                    reasons.setdefault(rid, f"same name/alias/slug as {term!r}")
        # Embedding leg: degrades to lexical-only on a missing or failing
        # embedder (query_vec_for) or a failing vector query.
        card = self._card_text_for(text, meta)
        vec = query_vec_for(getattr(self.hub, "embedder", None), card, None)
        if vec is not None:
            try:
                rows = self.store.chunks.search_chunks_semantic(
                    query_vec=vec,
                    kind=self.kind,
                    limit=_DEDUP_SHOW * 2,
                    max_distance=DEDUP_MAX_DISTANCE,
                    card_kinds=("card_combined",),
                )
            except Exception:
                log.warning("taxon dedup: vector leg failed", exc_info=True)
                rows = []
            for _blk, hit, dist in rows:
                reasons.setdefault(
                    hit.id, f"embedding neighbour, cosine distance {dist:.2f}"
                )
        keep: list[int] = []
        for rid in reasons:
            ref = self.store.get_ref(kind=self.kind, id=rid)
            if ref is not None and not _dimension_clash(meta, ref.meta or {}):
                keep.append(rid)
        if not keep:
            return
        lines = [f"{self._candidate_lines([r])}  <- {reasons[r]}" for r in keep]
        raise BadInput(
            f"taxon {meta.get('name')!r} looks like an existing node:\n"
            + "\n".join(lines[:_DEDUP_SHOW]),
            next=(
                "use the existing node, or repeat the put with dedup=False to "
                "mint a distinct one anyway (a different dimension_kind/"
                "si_vector also avoids this check)"
            ),
        )

    # ── search: under= / axis= / depth= facets ──────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        under: str | int | None = None,
        axis: str | None = None,
        depth: int | None = None,
        page_size: int = 10,
        page: int = 1,
        **_kw: Any,
    ) -> Response:
        """Hybrid search; ``under=`` restricts to the descendants of a node
        (excluding it), ``axis=`` to edges with that ``meta.axis``,
        ``depth=N`` to <= N hops. With ``q=`` the ranked hits are intersected
        with that set; without it the set is listed by depth then name."""
        if under is None:
            if axis is not None or depth is not None:
                raise BadInput(
                    "axis=/depth= only apply with under=",
                    next="search(kind='taxon', under='taxon:42', axis='method', depth=2)",
                )
            resp = super().search(q=q, page_size=page_size, page=page, **_kw)
            return self._exact_first(resp, q, page, None)
        if axis is not None and (not isinstance(axis, str) or not axis.strip()):
            raise BadInput(
                f"axis must be a non-empty string, got {axis!r}",
                next="axis='composition'",
            )
        if depth is not None and (
            isinstance(depth, bool) or not isinstance(depth, int) or depth < 1
        ):
            raise BadInput(
                f"depth must be a positive integer, got {depth!r}",
                next="depth=2 (at most two hops below under=)",
            )
        root = self._resolve_live_ref(self._resolve_spec(under))
        best: dict[int, int] = {}
        for rid, d, _ax in self.store.taxon_descendants(
            root.id, axis=axis, max_depth=depth
        ):
            best[rid] = min(d, best.get(rid, d))
        ids = list(best)
        inc = _kw.pop("include_ref_ids", None)
        if inc is not None:
            ids = [i for i in ids if i in set(inc)]
        handle, name = self._node_label(root.id)
        scope = f"under {handle} {name}" + (f" axis={axis}" if axis else "")
        scope += f" depth<={depth}" if depth else ""
        if not ids:
            return Response(body=f"no taxa {scope}")
        if q is not None and q.strip():
            resp = super().search(
                q=q,
                include_ref_ids=ids,
                page_size=page_size,
                page=page,
                **_kw,
            )
            resp = self._exact_first(resp, q, page, set(ids))
            return Response(body=f"# {scope}\n{resp.body}", cost=resp.cost)
        rows = []
        for rid in ids:
            ref = self.store.get_ref(kind=self.kind, id=rid)
            nm = str((ref.meta or {}).get("name") or ref.title) if ref else ""
            rows.append((best[rid], nm.lower(), rid))
        rows.sort()
        start = max(0, (int(page) - 1) * int(page_size))
        window = rows[start : start + int(page_size)]
        lines = [f"# {len(rows)} taxa {scope}"]
        if len(rows) > len(window) or page > 1:
            lines[0] += f" (rows {start + 1}-{start + len(window)})"
        lines += [f"{self._hop_line(rid)}  [depth {d}]" for d, _n, rid in window]
        return Response(body="\n".join(lines))

    def _exact_first(
        self, resp: Response, q: str | None, page: int, scope: set[int] | None
    ) -> Response:
        """Page 1 leads with nodes whose name/slug/alias equals ``q`` — hybrid
        ranking can put 'Tensile yield strength' above 'Yield' (gr460338)."""
        if not q or not q.strip() or int(page) != 1:
            return resp
        hits = [h for h in self._term_matches(q) if scope is None or h in scope]
        if not hits:
            return resp
        lead = "exact: " + "; ".join(
            "{} {} ({})".format(*self._node_label(h), self._path_label(h)) for h in hits
        )
        return Response(body=f"{lead}\n{resp.body}", cost=resp.cost)

    # ── link: meta= (edge axis) ─────────────────────────────────────

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove a link; ``meta=`` (add only) lands on the edge, with
        ``axis`` (when present) a non-empty string and other keys free."""
        if meta is not None:
            if mode != "add":
                raise BadInput(
                    "meta= only applies when adding a link",
                    next="drop meta=, or use mode='add'",
                )
            if not isinstance(meta, dict):
                raise BadInput(
                    "meta= must be an object", next="meta={'axis': 'composition'}"
                )
            axis = meta.get("axis")
            if "axis" in meta and (not isinstance(axis, str) or not axis.strip()):
                raise BadInput(
                    f"meta.axis must be a non-empty string, got {axis!r}",
                    next="meta={'axis': 'composition'}",
                )
        if isinstance(target, str) and target.startswith("taxon:"):
            # path-form target ('taxon:measurand/temperature') -> numeric id
            target = f"taxon:{self._resolve_spec(target)}"
        return self._link(id=id, target=target, mode=mode, rel=rel, meta=meta)

    # ── get: view='path', id='/unrooted' ────────────────────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        if isinstance(id, str) and not id.startswith("/"):
            id = self._resolve_spec(id)
        concrete = id is not None and not (isinstance(id, str) and id.startswith("/"))
        if concrete and view == "path":
            return self._render_path_view(self._resolve_live_ref(self._coerce_id(id)))
        if concrete and view is not None and view not in _BASE_VIEWS:
            raise Unsupported(
                f"unknown view {view!r} for kind='taxon'",
                options=["path", *_BASE_VIEWS],
                next=(
                    "view='path' (chains up to the start nodes) "
                    "· links, log, raw (generic)"
                ),
            )
        return super().get(id=id, view=view, q=q, **_kw)

    def _supported_list_views(self) -> tuple[str, ...]:
        return ("recent", "unrooted", "unmapped")

    def _list_view(self, view: str) -> Response | None:
        if view == "unrooted":
            ids = self.store.taxon_unrooted()
            if not ids:
                return Response(body="every taxon reaches a start node")
            lines = [f"# {len(ids)} taxon(s) reaching no start node"]
            lines += [self._hop_line(rid) for rid in ids]
            return Response(body="\n".join(lines))
        if view == "unmapped":
            ids = self.store.taxon_unmapped()
            if not ids:
                return Response(body="every legacy-seeded taxon has a dimension")
            lines = [f"# {len(ids)} legacy-seeded taxon(s) with no dimension_kind"]
            lines += [self._hop_line(rid) for rid in ids]
            return Response(body="\n".join(lines))
        return super()._list_view(view)

    def _node_label(self, ref_id: int) -> tuple[str, str]:
        """``(handle, name)`` for a node id."""
        from precis.utils import handle_registry

        ref = self.store.get_ref(kind=self.kind, id=ref_id)
        handle = handle_registry.format_handle(self.kind, ref_id)
        if ref is None:
            return handle, "?"
        return handle, str((ref.meta or {}).get("name") or ref.title)

    def _hop_line(self, ref_id: int) -> str:
        """``<handle> <name> — <lede>`` for one node."""
        handle, name = self._node_label(ref_id)
        ref = self.store.get_ref(kind=self.kind, id=ref_id)
        lede = _lede((ref.meta or {}).get("definition", "")) if ref else ""
        return f"{handle} {name} — {lede}" if lede else f"{handle} {name}"

    def _render_path_view(self, ref: Any, *, limit: int = 20) -> Response:
        chains, total, partial = self.store.taxon_paths_report(ref.id, limit=limit)
        handle, name = self._node_label(ref.id)
        out = [f"# {handle} {name} — path"]
        shown = chains or partial
        if not chains:
            out += ["", f"{handle} reaches no start node."]
            if not partial:
                return Response(body="\n".join(out))
            out.append("partial chains (each ends at a node with no parent):")
        for n, chain in enumerate(shown, 1):
            out += ["", f"chain {n}"]
            # chain is node -> ... -> top; render the top first.
            for i in range(len(chain) - 1, -1, -1):
                out.append(self._hop_line(chain[i][0]))
                if i > 0 and chain[i - 1][1]:
                    out.append(f"  ↳ [{chain[i - 1][1]}]")
        if chains and total > len(chains):
            out += ["", f"{total - len(chains)} more chain(s) cut (limit {limit})"]
        return Response(body="\n".join(out))

    def _render_one(self, ref: Any, tags: Any) -> str:
        meta = ref.meta or {}
        name = meta.get("name") or ref.title
        out = [f"# taxon {ref.id}: {name}"]
        if meta.get("definition"):
            out += ["", meta["definition"]]
        if meta.get("aliases"):
            out += ["", "aka: " + ", ".join(meta["aliases"])]
        for key in BOUNDARY_KEYS:
            if meta.get(key):
                out += ["", f"{key}:"] + [f"- {e}" for e in meta[key]]
        facts = [f"status: {meta.get('status', '?')}"]
        for key in ("dimension_kind", "si_vector", "canonical_unit"):
            if meta.get(key):
                facts.append(f"{key}: {meta[key]}")
        out += ["", "  ".join(facts)]
        if meta.get("start"):
            req = (meta.get("contract") or {}).get("required_keys") or []
            line = "start node"
            if req:
                line += " (requires: " + ", ".join(req) + ")"
            out += ["", line]
        parents = self.store.taxon_parents(ref.id)
        if parents:
            shown = []
            for pid, axis in parents:
                p_handle, p_name = self._node_label(pid)
                shown.append(f"{p_handle} {p_name}" + (f" [{axis}]" if axis else ""))
            out += ["", "specialises: " + ", ".join(shown)]
        n_children = self.store.taxon_child_count(ref.id)
        if n_children:
            out += ["", f"children: {n_children}"]
        if tags:
            out += ["", "tags: " + " ".join(str(t) for t in tags)]
        return "\n".join(out)


__all__ = ["TaxonHandler"]
