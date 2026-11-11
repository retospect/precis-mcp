"""Generated memory TOCs (gr631890): listings derived from structure,
never hand-maintained. Two disciplines supply everything — tagging says
what a node is, ``part-of`` says where it belongs.

``search(kind='memory', view='toc', tags=[...])`` renders a tag's
*forest*: every live node carrying the tag whose own ``part-of``
parents do not (the tips of the trees), one line each with extracted
keywords and a member count. ``tags=['spec:*']`` lists every spec root
wherever it hangs — a trailing ``*`` on an open-tag entry compiles to a
prefix ``LIKE``; over the exact-tag filter ``spec:*`` parses as the
literal value and matches nothing. ``args={'under': …}`` scopes the
forest to a subtree.

``get(kind='memory', id=N, view='toc')`` renders the node's part-of
subtree as an indented tree. Child order is ``meta.order`` when set,
else the position of the child's handle mention in the parent's body,
else title — a hand-maintained "reading order" list just becomes one
more mention source, so generated TOCs can replace them over time.
``args={'depth': k}`` collapses below depth *k* with subtree counts;
``offset`` pages under the byte budget like ``view='members'``.

Keywords are the existing extraction — the ``rake-lemma``
``chunk_summaries`` row, then ``chunks.keywords`` (KeyBERT) — with
``meta.hook`` and the first body line as first-class fallback while the
extractor queue lags.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from precis._result_budget import result_context_cap
from precis.errors import BadInput
from precis.response import Response
from precis.store import Tag
from precis.store._refs_ops import _like_escape
from precis.store._tag_filter import _parse_tag_string, split_tag_polarity
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.types import Ref

#: Bytes reserved for the header + continuation/footer lines so an
#: emitted page always fits the result cap (``memory.py``'s
#: ``_PAGED_FOOTER_BYTES`` — a module here can't import the handler).
_FOOTER_RESERVE = 400

#: A node's keyword/hook descriptor never runs past this many chars —
#: a rake-lemma row can join up to 50 keywords.
_DESCRIPTOR_CAP = 120

#: ``me<N>`` and ``memory:<N>`` mentions, for reading-order extraction
#: from a parent body.
_MENTION_RE = re.compile(r"\bme(\d+)\b|memory:(\d+)\b")

#: (namespace, op, value) — one storage-level tag condition; ``op`` is
#: ``=`` for an exact tag or ``LIKE`` for a ``*``-suffixed prefix glob.
_Cond = tuple[str, str, str]


def _conds_for(body: str) -> list[_Cond]:
    """Compile one canonical filter entry into OR'ed tag conditions.

    ``'spec:*'``-shape entries (a trailing ``*`` on an open/bare tag, or
    an unregistered-but-in-use closed axis the normaliser passed
    through) become ``LIKE '<stem>%'`` — on open tags the ``:`` lives in
    the stored value, on closed axes in the namespace, and
    :func:`_parse_tag_string` splits both correctly. Registered closed
    axes never reach here: ``normalize_filter`` already refused a
    ``*``-suffixed value they don't declare. Exact entries expand the
    bare-tag ``OPEN``/``FLAG`` pair as usual.
    """
    if body.endswith("*") and len(body) > 1:
        return [
            (ns, "LIKE", _like_escape(v) + "%")
            for ns, v in _parse_tag_string(body[:-1])
        ]
    return [(ns, "=", v) for ns, v in _parse_tag_string(body)]


def _compile_tags(
    tags: list[str] | None,
    *,
    axis_in_use: Any,
) -> tuple[list[list[_Cond]], list[list[_Cond]], list[str]]:
    """Validate + compile ``tags=`` into ``(positive, negated)``
    condition groups (each group OR'ed, groups AND'ed) plus the
    canonical spellings for echo. Mirrors the SQL tag filter's polarity
    split with the added ``*``-glob form this view needs."""
    normalized = Tag.normalize_filter(tags, kind="memory", axis_in_use=axis_in_use)
    pos_raw, neg_raw = split_tag_polarity(normalized)
    pos = [_conds_for(body) for body in pos_raw]
    neg = [_conds_for(body) for body in neg_raw]
    if not pos:
        raise BadInput(
            "view='toc' needs at least one positive tags= entry — "
            "the tag defines the forest",
            next="search(kind='memory', tags=['spec:*'], view='toc')",
        )
    return pos, neg, normalized or []


def _tag_clause(groups: list[list[_Cond]], *, negate: bool) -> tuple[str, list[Any]]:
    """One ``(NOT) EXISTS`` predicate per group, spliced into the matched
    CTE — a ref matches a group when ANY of its tag rows satisfies one
    of the group's OR'ed conditions."""
    clauses: list[str] = []
    params: list[Any] = []
    for group in groups:
        ors: list[str] = []
        for ns, op, v in group:
            escape = " ESCAPE '\\'" if op == "LIKE" else ""
            ors.append(f"(t.namespace = %s AND t.value {op} %s{escape})")
            params.extend([ns, v])
        clauses.append(
            ("NOT " if negate else "") + "EXISTS (SELECT 1 FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = r.ref_id "
            "AND (rt.expires_at IS NULL OR rt.expires_at > now()) "
            f"AND ({' OR '.join(ors)}))"
        )
    return " AND ".join(clauses), params


def _matched_cte(
    pos: list[list[_Cond]],
    neg: list[list[_Cond]],
    *,
    include_ref_ids: list[int] | None,
    exclude_ref_ids: list[int] | None,
) -> tuple[str, list[Any]]:
    """The live ``kind='memory'`` matched set as a CTE body + params,
    shared by the forest-roots select and :func:`count_tag_toc`."""
    clauses = ["r.kind = 'memory'", "r.retired_at IS NULL"]
    params: list[Any] = []
    frag, frag_params = _tag_clause(pos, negate=False)
    if frag:
        clauses.append(frag)
        params.extend(frag_params)
    frag, frag_params = _tag_clause(neg, negate=True)
    if frag:
        clauses.append(frag)
        params.extend(frag_params)
    if include_ref_ids is not None:
        clauses.append("r.ref_id = ANY(%s::bigint[])")
        params.append(include_ref_ids)
    if exclude_ref_ids:
        clauses.append("NOT (r.ref_id = ANY(%s::bigint[]))")
        params.append(exclude_ref_ids)
    return " AND ".join(clauses), params


#: The tag's forest in one statement: live roots (a matched node whose
#: live part-of parents are all unmatched), then a bounded walk counting
#: every live memory member under each root — the same subtree size
#: ``view='members'`` reports. ``UNION`` dedup makes cycles terminate.
_FOREST_SQL = """
    WITH RECURSIVE matched AS MATERIALIZED (
        SELECT r.ref_id FROM refs r WHERE {where}
    ), edges(child, parent) AS (
        SELECT l.src_ref_id, l.dst_ref_id FROM links l
         WHERE l.relation = 'part-of'
        UNION ALL
        SELECT l.dst_ref_id, l.src_ref_id FROM links l
         WHERE l.relation = 'contains'
    ), live_edges AS (
        SELECT e.child, e.parent FROM edges e
          JOIN refs c ON c.ref_id = e.child AND c.retired_at IS NULL
          JOIN refs p ON p.ref_id = e.parent AND p.retired_at IS NULL
    ), roots AS (
        SELECT m.ref_id FROM matched m
        WHERE NOT EXISTS (
            SELECT 1 FROM live_edges le
              JOIN matched p ON p.ref_id = le.parent
             WHERE le.child = m.ref_id)
    ), walk(root, node) AS (
        SELECT ref_id, ref_id FROM roots
        UNION
        SELECT w.root, le.child FROM walk w
          JOIN live_edges le ON le.parent = w.node
          JOIN refs mc ON mc.ref_id = le.child
                      AND mc.retired_at IS NULL AND mc.kind = 'memory'
    )
    SELECT w.root, COUNT(*) - 1 AS members FROM walk w GROUP BY w.root
"""


def _forest(
    store: Store,
    pos: list[list[_Cond]],
    neg: list[list[_Cond]],
    *,
    include_ref_ids: list[int] | None,
    exclude_ref_ids: list[int] | None,
) -> list[tuple[int, int]]:
    """``[(root ref_id, live member count)]`` under the compiled filter."""
    where, params = _matched_cte(
        pos, neg, include_ref_ids=include_ref_ids, exclude_ref_ids=exclude_ref_ids
    )
    with store.pool.connection() as conn:
        rows = conn.execute(_FOREST_SQL.format(where=where), params).fetchall()
    return [(int(root), int(members)) for root, members in rows]


def _cut(text: str, cap: int = _DESCRIPTOR_CAP) -> str:
    """Trim a keyword string to ``cap`` on a separator boundary."""
    text = " ".join(str(text or "").split())
    if len(text) <= cap:
        return text
    cut = text[:cap].rstrip()
    for sep in ("; ", ", ", " "):
        head, _, _tail = cut.rpartition(sep)
        if len(head) >= cap // 2:
            return head + "…"
    return cut + "…"


def _descriptors(store: Store, ref_ids: list[int]) -> dict[int, tuple[str, str, str]]:
    """``{ref_id: (rake, keybert, first_body_line)}`` for the live
    ``memory_body`` chunks — the descriptor sources, one query. The
    caller layers ``meta.hook`` between extraction and the first line
    (extraction first, hook as the pending-queue fallback)."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.ref_id, "
            "(SELECT s.text FROM chunk_summaries s "
            " WHERE s.chunk_id = c.chunk_id AND s.summarizer = 'rake-lemma' "
            "   AND s.status = 'ok' LIMIT 1), "
            "c.keywords, c.text "
            "FROM chunks c "
            "WHERE c.ref_id = ANY(%s) AND c.chunk_kind = 'memory_body' "
            "AND c.ord >= 0 AND c.retired_at IS NULL "
            "ORDER BY c.ref_id, c.ord",
            (list(ref_ids),),
        ).fetchall()
    out: dict[int, tuple[str, str, str]] = {}
    for ref_id, rake, keywords, text in rows:
        rid = int(ref_id)
        if rid in out:
            continue
        first = next(
            (ln.strip() for ln in str(text or "").splitlines() if ln.strip()), ""
        )
        out[rid] = (
            str(rake or "").strip(),
            ", ".join(str(k) for k in (keywords or [])[:8]),
            first,
        )
    return out


def _descriptor(ref: Ref, parts: dict[int, tuple[str, str, str]]) -> str:
    """rake-lemma → KeyBERT → ``meta.hook`` → first body line, cut."""
    rake, keybert, first = parts.get(ref.id, ("", "", ""))
    hook = str((ref.meta or {}).get("hook") or "")
    return _cut(rake or keybert or hook or first)


def _handle(ref_id: int) -> str:
    return handle_registry.try_format("memory", ref_id) or f"me{ref_id}"


def count_tag_toc(
    store: Store,
    *,
    tags: list[str] | None,
    include_ref_ids: list[int] | None = None,
    exclude_ref_ids: list[int] | None = None,
) -> int:
    """Forest-root count under this scope — the domain space footer's
    corpus size for ``view='toc'``."""
    pos, neg, _canon = _compile_tags(
        tags, axis_in_use=lambda p: p in store.tag_namespaces()
    )
    return len(
        _forest(
            store,
            pos,
            neg,
            include_ref_ids=include_ref_ids,
            exclude_ref_ids=exclude_ref_ids,
        )
    )


def render_tag_toc(
    store: Store,
    *,
    tags: list[str] | None,
    page: int = 1,
    page_size: int = 10,
    include_ref_ids: list[int] | None = None,
    exclude_ref_ids: list[int] | None = None,
    under: str | int | None = None,
    depth: int | str | None = None,
) -> Response:
    """``search(view='toc')``: the tag's forest roots, paged by
    ``page_size`` rows or the result byte budget, whichever binds first
    — the ``view='index'`` listing's shape, so ``args={'page': N}``
    resumes exactly where the previous page stopped."""
    pos, neg, canon = _compile_tags(
        tags, axis_in_use=lambda p: p in store.tag_namespaces()
    )
    try:
        page = max(int(page or 1), 1)
        page_size = max(int(page_size or 1), 1)
    except (TypeError, ValueError):
        raise BadInput(
            f"page=/page_size= must be integers, got page={page!r} "
            f"page_size={page_size!r}",
            next="search(kind='memory', tags=['spec:*'], view='toc')",
        ) from None
    forest = _forest(
        store,
        pos,
        neg,
        include_ref_ids=include_ref_ids,
        exclude_ref_ids=exclude_ref_ids,
    )
    if not forest:
        return Response(
            body=f"no memory entries tagged {canon} start a tree", result_count=0
        )
    found = store.fetch_refs_by_ids([root for root, _ in forest])
    parts = _descriptors(store, [root for root, _ in forest])
    counts = dict(forest)
    ordered = sorted(
        (r for r in found.values() if r.id in counts),
        key=lambda r: ((r.title or "").lower(), r.id),
    )
    budget = max(200, result_context_cap() - _FOOTER_RESERVE)
    pages: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    used = 0
    for ref in ordered:
        if len(pages) == page:
            break
        desc = _descriptor(ref, parts)
        line = f"- {ref.title} ({_handle(ref.id)})"
        if desc:
            line += f" — {desc}"
        line += f" · {counts[ref.id]} member{'s' if counts[ref.id] != 1 else ''}"
        cost = len(line.encode("utf-8")) + 1
        if cur and (len(cur) >= page_size or used + cost > budget):
            pages.append(cur)
            cur, used = [], 0
            if len(pages) == page:
                break
        cur.append((int(ref.id), line))
        used += cost
    if cur and len(pages) < page:
        pages.append(cur)
    if len(pages) < page:
        return Response(
            body=f"no memory entries on page {page} — "
            f"{len(ordered)} roots tagged {canon}",
            result_count=0,
        )
    page_rows = pages[page - 1]
    shown = sum(len(c) for c in pages)
    header = (
        f"# memory TOC — {len(ordered)} root"
        f"{'s' if len(ordered) != 1 else ''} tagged {canon}"
    )
    body = header + "\n" + "\n".join(ln for _rid, ln in page_rows)
    more = len(ordered) - shown
    if more > 0:
        args_echo = [f"'page': {page + 1}", f"'page_size': {page_size}"]
        if under is not None:
            args_echo.append(f"'under': {under!r}")
            if depth is not None:
                args_echo.append(f"'depth': {depth!r}")
        elif include_ref_ids is not None:
            args_echo.append(
                f"'include_ref_ids': {sorted({int(i) for i in include_ref_ids})}"
            )
        if exclude_ref_ids:
            args_echo.append(
                f"'exclude_ref_ids': {sorted({int(i) for i in exclude_ref_ids})}"
            )
        body += (
            f"\n… {more} more — next: search(kind='memory', tags={canon!r}, "
            f"view='toc', args={{{', '.join(args_echo)}}})"
        )
    return Response(
        body=body,
        result_count=len(page_rows),
        result_ids=[rid for rid, _ln in page_rows],
    )


def _integer(
    value: int | str | None, name: str, *, minimum: int, next_call: str
) -> int:
    try:
        out = int(str(value)) if value is not None else minimum
    except (TypeError, ValueError):
        out = -1
    if out < minimum:
        raise BadInput(
            f"{name}= must be an integer >= {minimum}, got {value!r}",
            next=next_call,
        )
    return out


def _bodies(store: Store, ref_ids: list[int]) -> dict[int, str]:
    """``{ref_id: concatenated memory_body text}`` — the mention-order
    source for reading-order ranks, one query."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, text FROM chunks WHERE ref_id = ANY(%s) "
            "AND chunk_kind = 'memory_body' AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ref_id, ord",
            (list(ref_ids),),
        ).fetchall()
    out: dict[int, list[str]] = {}
    for ref_id, text in rows:
        out.setdefault(int(ref_id), []).append(str(text or ""))
    return {rid: "\n".join(texts) for rid, texts in out.items()}


def _mention_positions(body: str) -> dict[int, int]:
    """``{ref id: first mention offset}`` of ``me<N>`` / ``memory:<N>``
    handles in a body — the implicit reading order a hand-maintained
    outline already records."""
    pos: dict[int, int] = {}
    for m in _MENTION_RE.finditer(body):
        rid = int(m.group(1) or m.group(2))
        pos.setdefault(rid, m.start())
    return pos


def _subtree(
    store: Store, ref: Ref
) -> tuple[
    dict[int, list[int]],
    dict[int, Ref],
    int,
]:
    """``(children map, memory refs, non-memory live count)`` for the
    ref's part-of subtree, restricted to live memory nodes. A node
    under a dead or non-memory intermediate re-attaches to its nearest
    live memory ancestor — the ``view='members'`` flat listing shows
    it, so the tree must too. A node with several part-of parents hangs
    under the nearest one (ties break on the lower id): the render is
    a tree, the DAG stays in the links."""
    edges = store.part_of_subtree_edges(ref.id)
    involved = {ref.id}
    for child, parent in edges:
        involved.add(child)
        involved.add(parent)
    found = store.fetch_refs_by_ids(sorted(involved), include_deleted=False)
    memories = {rid: r for rid, r in found.items() if r.kind == "memory"}
    non_memory = len(found) - len(memories)
    raw_parents: dict[int, list[int]] = {}
    for child, parent in edges:
        if child == ref.id:
            continue
        raw_parents.setdefault(child, []).append(parent)
    parent_of: dict[int, int] = {}
    for node in memories:
        if node == ref.id:
            continue
        # BFS up the raw parent chains to the nearest live memory
        # ancestor — dead intermediates and other kinds are skipped.
        best: tuple[int, int] | None = None  # (hops, parent id)
        frontier = [(p, 1) for p in raw_parents.get(node, [])]
        seen = {node}
        while frontier:
            cand, hops = frontier.pop(0)
            if cand in seen:
                continue
            seen.add(cand)
            if cand in memories:
                if best is None or (hops, cand) < best:
                    best = (hops, cand)
                continue
            if cand != ref.id:
                # A live non-memory or dead/missing intermediate: keep
                # climbing — its own parents may reach a memory node.
                frontier.extend((p, hops + 1) for p in raw_parents.get(cand, []))
        parent_of[node] = best[1] if best is not None else ref.id
    children: dict[int, list[int]] = {}
    for node, parent in parent_of.items():
        children.setdefault(parent, []).append(node)
    return children, memories, non_memory


def render_node_toc(
    store: Store,
    ref: Ref,
    *,
    depth: int | str | None = None,
    offset: int | str | None = None,
) -> Response:
    """``get(view='toc')``: the ref's part-of subtree as an indented
    tree with extracted keywords per line.

    Children order by ``meta.order`` (a reading-order rank on the
    child), else the child's first handle-mention position in the
    parent body, else title — so a body's hand-maintained "Sections in
    reading order" list supplies order until it can be dropped.
    ``args={'depth': k}`` renders through depth *k* and collapses a
    deeper subtree into a ``· +N below`` marker; ``offset`` pages DFS
    rows under the byte budget exactly like ``view='members'``.
    """
    next_call = f"get(kind='memory', id={ref.id}, view='toc')"
    off = _integer(offset, "offset", minimum=0, next_call=next_call)
    n_depth: int | None = None
    if depth is not None:
        n_depth = _integer(
            depth,
            "depth",
            minimum=1,
            next_call=(
                f"{next_call}, args={{'depth': 1}}) — depth=1 lists the "
                "node and its direct members"
            ),
        )
    children, memories, non_memory = _subtree(store, ref)
    # Bodies only where a parent orders 2+ children — the mention
    # rank's only source.
    bodies = _bodies(store, [p for p, cs in children.items() if len(cs) > 1])
    mentions = {p: _mention_positions(b) for p, b in bodies.items()}

    def rank(child_id: int, parent_id: int) -> tuple[float, str, int]:
        child = memories[child_id]
        order = (child.meta or {}).get("order")
        if isinstance(order, (int, float)) and not isinstance(order, bool):
            return (float(order), (child.title or "").lower(), child_id)
        pos = mentions.get(parent_id, {}).get(child_id)
        if pos is not None:
            return (float(pos), (child.title or "").lower(), child_id)
        return (float("inf"), (child.title or "").lower(), child_id)

    def subtree_size(node_id: int) -> int:
        total = 0
        stack = list(children.get(node_id, []))
        while stack:
            cur = stack.pop()
            total += 1
            stack.extend(children.get(cur, []))
        return total

    def ordered(node_id: int) -> list[int]:
        return sorted(
            children.get(node_id, []),
            key=lambda c: rank(c, node_id),
            reverse=True,
        )

    # Pre-order rows of the live memory tree: (ref_id, depth).
    rows: list[tuple[int, int]] = [(ref.id, 0)]
    stack: list[tuple[int, int]] = [(kid, 1) for kid in ordered(ref.id)]
    while stack:
        node_id, d = stack.pop()
        rows.append((node_id, d))
        if n_depth is None or d < n_depth:
            stack.extend((kid, d + 1) for kid in ordered(node_id))

    total = len(rows) - 1  # descendants; the root line is the header row
    if total == 0 and off >= 1:
        return Response(
            body=f"no members past offset {off} — 0 total; next: {next_call}"
        )
    descendants = rows[1:]
    page_rows = descendants[off:]
    if total and not page_rows:
        return Response(
            body=(f"no members past offset {off} — {total} total; next: {next_call}")
        )
    parts = _descriptors(store, [rid for rid, _d in page_rows] + [ref.id])
    budget = max(200, result_context_cap() - _FOOTER_RESERVE)
    lines: list[str] = []
    used = 0
    for rid, d in [(ref.id, 0), *page_rows]:
        node_ref = memories[rid]
        hidden = subtree_size(rid) if n_depth is not None and d == n_depth else 0
        desc = _descriptor(node_ref, parts)
        line = f"{'  ' * d}- {node_ref.title} ({_handle(rid)})"
        if desc:
            line += f" — {desc}"
        line += f" · +{hidden} below" if hidden else ""
        cost = len(line.encode("utf-8")) + 1
        if lines and used + cost > budget:
            break
        lines.append(line)
        used += cost
    shown_desc = len(lines) - 1  # rows beyond the root line
    end = off + max(0, shown_desc)
    depth_note = f", depth ≤ {n_depth}" if n_depth else ""
    header = f"# memory {_handle(ref.id)} — TOC · {total} in subtree{depth_note}"
    body = header + "\n" + "\n".join(lines)
    if end < total:
        args = f"'offset': {end}"
        if n_depth is not None:
            args += f", 'depth': {n_depth}"
        body += (
            f"\n… {end} of {total} shown — next: get(kind='memory', "
            f"id={ref.id}, view='toc', args={{{args}}})"
        )
    if non_memory:
        body += (
            f"\n… {non_memory} linked non-memory member"
            f"{'s' if non_memory != 1 else ''} not listed "
            "(view='links' shows them)"
        )
    return Response(
        body=body,
        result_count=len(lines),
        result_ids=[ref.id, *[rid for rid, _d in page_rows[: len(lines) - 1]]],
    )
