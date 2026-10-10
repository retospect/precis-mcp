"""Memory hub graph: ``section:index`` hubs, ``part-of`` membership, ``qualifies``.

A *hub* is a memory tagged ``section:index``. Every other memory carries one
``part-of`` edge to its subject hub (``part-of``/``contains`` is one relation
pair, stored in either orientation) and a type tag ``section:<type>``; a
gotcha links to a thread it applies to with ``qualifies`` (the thread reads
``qualified-by``). The batch readers here feed the memory export manifest
(``precis.cli.memory``); :func:`link_hints` feeds the write-path hints of
``put`` / ``edit`` on a memory (``precis.handlers.memory``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.utils.eye_render import _HUB_TAG, _VERBATIM_CAP, _hub_ids

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from precis.store import Store

#: The type tags a memory node carries besides ``section:index``.
SECTION_TYPES = ("threads", "runbooks", "gotchas", "workflow", "reference")

_HINT_HUBS = 2
_HINT_GOTCHAS = 5
_QUERY_BODY_CHARS = 300


def parents_of(store: Store, ref_ids: list[int]) -> dict[int, list[int]]:
    """``{ref id: [live part-of parents]}`` — direct parents only, either
    stored orientation (``part-of`` member→parent, ``contains`` parent→member).
    Refs with no parent are absent."""
    if not ref_ids:
        return {}
    ids = list(ref_ids)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.src_ref_id, l.dst_ref_id, l.relation FROM links l "
            "WHERE (l.relation = 'part-of' AND l.src_ref_id = ANY(%s)) "
            "   OR (l.relation = 'contains' AND l.dst_ref_id = ANY(%s))",
            (ids, ids),
        ).fetchall()
        pairs = [
            (int(s), int(d)) if rel == "part-of" else (int(d), int(s))
            for s, d, rel in rows
        ]
        if not pairs:
            return {}
        live = {
            int(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) AND retired_at IS NULL",
                (sorted({p for _m, p in pairs}),),
            ).fetchall()
        }
    out: dict[int, list[int]] = {}
    for member, parent in pairs:
        if parent in live and parent != member and parent not in out.get(member, []):
            out.setdefault(member, []).append(parent)
    return out


_MAX_DEPTH = 8


def hubs_of(store: Store, ref_ids: list[int]) -> dict[int, list[int]]:
    """``{ref id: [nearest live section:index ancestors]}`` along the
    ``part-of`` chain: a node's parent hub, else (a detail node under a
    summary node) the hub its parent reaches. Refs whose chain never reaches
    a hub are absent. The chain is acyclic by relation constraint; a depth cap
    and a seen-set guard it anyway."""
    out: dict[int, list[int]] = {}
    frontier: dict[int, set[int]] = {r: {r} for r in ref_ids}
    seen: dict[int, set[int]] = {r: {r} for r in ref_ids}
    for _ in range(_MAX_DEPTH):
        nodes = sorted({n for ns in frontier.values() for n in ns})
        if not nodes:
            break
        parents = parents_of(store, nodes)
        hubs = _live_hubs(store, {p for ps in parents.values() for p in ps})
        for origin, ns in list(frontier.items()):
            nxt: set[int] = set()
            for n in ns:
                for p in parents.get(n, []):
                    if p in hubs:
                        if p not in out.setdefault(origin, []):
                            out[origin].append(p)
                    elif p not in seen[origin]:
                        seen[origin].add(p)
                        nxt.add(p)
            frontier[origin] = set() if origin in out else nxt
    return out


def recency_stamps(store: Store, ref_ids: list[int]) -> dict[int, Any]:
    """``{ref id: GREATEST(last_viewed_at, last_recalled_at, updated_at)}`` —
    NULLs ignored (``updated_at`` is never NULL, so every live ref is present)."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, GREATEST(last_viewed_at, last_recalled_at, updated_at) "
            "FROM refs WHERE ref_id = ANY(%s)",
            (list(ref_ids),),
        ).fetchall()
    return {int(r[0]): r[1] for r in rows}


def accessed_dates(store: Store, ref_ids: list[int]) -> dict[int, str]:
    """``{ref id: UTC date of GREATEST(last_viewed_at, last_recalled_at)}`` —
    the last human or agent access; refs never accessed are absent."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, to_char(GREATEST(last_viewed_at, last_recalled_at) "
            "AT TIME ZONE 'UTC', 'YYYY-MM-DD') FROM refs "
            "WHERE ref_id = ANY(%s) "
            "AND (last_viewed_at IS NOT NULL OR last_recalled_at IS NOT NULL)",
            (list(ref_ids),),
        ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def _live_hubs(store: Store, ids: set[int]) -> set[int]:
    if not ids:
        return set()
    hubs = _hub_ids(store, sorted(ids))
    if not hubs:
        return set()
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) AND retired_at IS NULL",
            (sorted(hubs),),
        ).fetchall()
    return {int(r[0]) for r in rows}


def qualified_counts(store: Store, ref_ids: list[int]) -> dict[int, int]:
    """``{ref id: live qualified-by edges}`` (``qualifies`` rows pointing at
    the ref, from a live ref). Refs with none are absent."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.dst_ref_id, count(DISTINCT l.src_ref_id) FROM links l "
            "JOIN refs s ON s.ref_id = l.src_ref_id AND s.retired_at IS NULL "
            "WHERE l.relation = 'qualifies' AND l.dst_ref_id = ANY(%s) "
            "GROUP BY l.dst_ref_id",
            (list(ref_ids),),
        ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


REVIEW_TAG = "memory-review"


def review_todos(store: Store, ref_ids: list[int]) -> dict[int, str]:
    """``{memory id: 'td<id>:<created UTC date>,…'}`` — the open
    ``memory-review`` todos that point at the memory (a todo -> memory link).
    Open = no terminal ``STATUS`` (done / won't-do / auto-timeout). Memories
    with none are absent."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.dst_ref_id, t.ref_id, "
            "to_char(t.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') "
            "FROM links l JOIN refs t ON t.ref_id = l.src_ref_id "
            "AND t.kind = 'todo' AND t.retired_at IS NULL "
            "WHERE l.dst_ref_id = ANY(%s) "
            "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags g "
            "ON g.tag_id = rt.tag_id WHERE rt.ref_id = t.ref_id "
            "AND g.namespace = 'OPEN' AND g.value = %s) "
            "AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags g "
            "ON g.tag_id = rt.tag_id WHERE rt.ref_id = t.ref_id "
            "AND g.namespace = 'STATUS' "
            "AND g.value IN ('done', 'won''t-do', 'auto-timeout')) "
            "ORDER BY t.ref_id",
            (list(ref_ids), REVIEW_TAG),
        ).fetchall()
    from precis.utils import handle_registry

    out: dict[int, list[str]] = {}
    for mem, todo, day in rows:
        h = handle_registry.try_format("todo", int(todo)) or f"td{todo}"
        out.setdefault(int(mem), []).append(f"{h}:{day}")
    return {k: ",".join(v) for k, v in out.items()}


def _tag_values(store: Store, ref_id: int) -> set[str]:
    """The ref's tags as strings: ``ns:value`` for a closed-namespace tag
    (``SPACE:repo-dev``), the bare value otherwise (``section:index`` is an
    open tag whose value holds the colon)."""
    return {
        f"{ns}:{v}" if ns == "SPACE" else v
        for ns, v in store.ref_tags_bulk([ref_id]).get(ref_id, [])
    }


def link_hints(handler: Any, ref_id: int) -> list[tuple[str, str]]:
    """Advisory lines for a just-written memory ``ref_id``; ``[]`` when all
    is in place, the node is a hub, or its space has no hub. Never raises."""
    try:
        return _link_hints(handler, ref_id)
    except Exception:
        log.debug("memory link hints skipped for %s", ref_id, exc_info=True)
        return []


def _space_tag(values: set[str]) -> str | None:
    return next((v for v in sorted(values) if v.startswith("SPACE:")), None)


def _link_hints(handler: Any, ref_id: int) -> list[tuple[str, str]]:
    store: Store = handler.store
    values = _tag_values(store, ref_id)
    space = _space_tag(values)
    if space is None or _HUB_TAG in values:
        return []
    # hubs are few; no practical cap (a silent 200 would drop the rest)
    hubs = store.list_refs(kind="memory", tags=[space, _HUB_TAG], limit=10_000)
    hubs = [h for h in hubs if h.id != ref_id]
    if not hubs:
        return []
    ref = store.get_ref(kind="memory", id=ref_id)
    if ref is None:
        return []
    from precis.utils import handle_registry

    def h(i: int) -> str:
        return handle_registry.try_format("memory", i) or f"me{i}"

    me = h(ref_id)
    lines: list[tuple[str, str]] = []
    mine = hubs_of(store, [ref_id]).get(ref_id, [])
    body = handler._body_text(ref)

    if not mine:
        hook = str((ref.meta or {}).get("hook") or body[:_QUERY_BODY_CHARS])
        query = f"{ref.title or ''} {hook}".strip()
        from precis.store import Tag

        hub_ids = {x.id for x in hubs}
        ranked: list[int] = []
        if query:
            hits, _total = handler._best_body_hits(
                query,
                Tag.normalize_filter([space, _HUB_TAG], kind="memory"),
                _HINT_HUBS + 1,
                mode="lexical",  # a write must not embed synchronously
                include_ref_ids=sorted(hub_ids),
            )
            ranked = [r.id for _b, r, _s in hits if r.id in hub_ids]
            if not ranked:
                ranked = _by_word_overlap(query, hubs)
        for hid in ranked[:_HINT_HUBS]:
            lines.append(
                (
                    f"link(kind='memory', id='{me}', target='{h(hid)}', rel='part-of')",
                    f"file this under hub {h(hid)}",
                )
            )
    elif "section:threads" in values:
        gotchas = _unlinked_gotchas(store, ref_id, mine)
        for gid in gotchas[:_HINT_GOTCHAS]:
            lines.append(
                (
                    f"link(kind='memory', id='{h(gid)}', target='{me}', "
                    "rel='qualifies')",
                    f"mark gotcha {h(gid)} as applying to this thread",
                )
            )

    if len(body) > _VERBATIM_CAP:
        lines.append(
            (
                f"edit(kind='memory', id='{me}', mode='replace', text='<summary>')",
                f"fisheye shows {_VERBATIM_CAP} of {len(body)} chars — split into "
                "a summary node + part-of detail nodes",
            )
        )
    if not any(v.startswith("section:") for v in values):
        types = " | ".join(f"section:{t}" for t in SECTION_TYPES)
        lines.append(
            (
                f"tag(kind='memory', id='{me}', add=['section:threads'])",
                f"type this node (one of {types})",
            )
        )
    return lines


def _by_word_overlap(query: str, hubs: list[Any]) -> list[int]:
    """Hubs sharing words with ``query`` (title + hook), best first: the
    fallback when the chunk search finds nothing (its lexical leg ANDs every
    term, and a fresh hub may have no embeddings yet)."""
    words = {w for w in query.lower().split() if len(w) > 3}
    scored = []
    for h in hubs:
        text = f"{h.title or ''} {(h.meta or {}).get('hook') or ''}".lower().split()
        n = len(words & set(text))
        if n:
            scored.append((-n, h.id))
    return [i for _n, i in sorted(scored)]


def _unlinked_gotchas(store: Store, thread_id: int, hub_ids: list[int]) -> list[int]:
    """Live ``section:gotchas`` descendants of the thread's hubs (the same
    ``part-of`` subtree ``search(args={'under': hub})`` walks, detail nodes
    included) not yet linked to it with ``qualifies``."""
    cand_set: set[int] = set()
    for hid in hub_ids:
        cand_set |= store.part_of_descendants(hid)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id FROM links "
            "WHERE relation = 'qualifies' AND dst_ref_id = %s",
            (thread_id,),
        ).fetchall()
    cand_set -= {int(r[0]) for r in rows} | {thread_id}
    cand = sorted(cand_set)
    if not cand:
        return []
    return [
        r
        for r, vals in sorted(store.ref_tags_bulk(cand).items())
        if "section:gotchas" in {v for _ns, v in vals}
    ]


#: Share of hits one hub / type tag must hold before a narrowing hint is worth a line.
_NARROW_SHARE = 0.6
_NARROW_MIN_HITS = 3


def narrowing_hints(store: Store, ref_ids: list[int], q: str) -> list[str]:
    """One-line, ready-to-run narrowing calls for a memory search: when at least
    :data:`_NARROW_SHARE` (but not all) of the hits sit under one hub, or share
    one ``section:<type>`` tag. ``[]`` when nothing would narrow; fail-soft."""
    ids = list(dict.fromkeys(ref_ids))
    if len(ids) < _NARROW_MIN_HITS:
        return []
    out: list[str] = []
    try:
        need = _NARROW_SHARE * len(ids)
        by_hub: dict[int, int] = {}
        for hubs in hubs_of(store, ids).values():
            by_hub[hubs[0]] = by_hub.get(hubs[0], 0) + 1
        if by_hub:
            hub, n = max(by_hub.items(), key=lambda kv: kv[1])
            if need <= n < len(ids):
                ref = store.get_ref(kind="memory", id=hub)
                title = ((ref.title if ref else "") or "").strip().splitlines()
                label = title[0][:60] if title else str(hub)
                out.append(
                    f"narrow to hub {label}: search(kind='memory', "
                    f"q={q!r}, args={{'under': 'me{hub}'}})"
                )
        counts: dict[str, int] = {}
        tags = store.ref_tags_bulk(ids)
        for rid in ids:
            for _ns, val in tags.get(rid, []):
                if val.startswith("section:") and val != _HUB_TAG:
                    counts[val] = counts.get(val, 0) + 1
        if counts:
            tag, n = max(counts.items(), key=lambda kv: kv[1])
            if need <= n < len(ids):
                out.append(
                    f"narrow to {tag}: search(kind='memory', q={q!r}, tags=[{tag!r}])"
                )
    except Exception:
        log.debug("memory narrowing hints skipped", exc_info=True)
        return []
    return out
