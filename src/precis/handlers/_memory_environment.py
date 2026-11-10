"""Memory graph-environment reads for shape review (gr570722).

Two read-only renders over the same gathered neighbourhood:

- ``get(kind='memory', id='meN', view='environment', args={'hops': 3})``
  walks every live memory-memory edge up to ``hops`` (1–4, default 2)
  from the focus and lists each node's inventory row, the inner edges,
  the computed shape smells, co-read pairs and — on
  ``args={'mermaid': true}`` — a mermaid sketch.
- ``search(kind='memory', view='shape', args={'under': 'meHUB'})`` rolls
  the same smells up over an ``under=`` scope (all live memories when
  omitted) with their evidence handles.

Per-node rows join the ref, its live body size, lifetime ``ref_reads``
counters, 3d/14d read windows and **distinct sessions** from
``tool_calls`` (each ``get`` row's ``arg_sample->>'id'``; retention is
:data:`precis.tool_ledger.DEFAULT_RETENTION_DAYS`, so the windows cap
there — a read the ledger pruned is invisible, and a ``get`` the
response never saw, e.g. an error, is excluded by ``outcome='ok'``).
Edit history reads ``revisions`` ``event='edited'`` rows; the row's
``edited`` column falls back to ``refs.updated_at`` — the same clock
the thread sweep's staleness rule reads — when no revision exists. An
edge's origin is ``mention`` when the row carries
``meta.auto='mention'`` (the write-path sync), else ``explicit``; its
age is ``created_at``.

The smell pass is the reviewer's side of the garden detectors
(``docs/backlog/memory-gardener.md``): computed over the gathered set,
each finding naming its evidence handles. Nothing is proposed, tagged
or written — these are observations, never a gate. Thresholds follow
the 2026-11-05 ``SPACE:repo-dev`` export analysis on the gripe.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.format import render_agent_table
from precis.response import Response
from precis.store import Tag
from precis.store._tag_filter import split_tag_polarity
from precis.utils import handle_registry
from precis.utils.match_all import fold_match_all_q
from precis.utils.memory_hubs import hubs_of, parents_of
from precis.utils.memory_threads import (
    INDEX_TAG,
    STALE_DAYS,
    first_left,
    left_is_done,
    newest_body_date,
)
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.types import Ref

# ── tunables (thresholds the gripe's export analysis used) ────────────

_HOPS_DEFAULT = 2
_HOPS_MAX = 4
#: Read-window days: 3d ("hot") and 14d (tool_calls GC keeps 30d).
_READS_HOT_DAYS = 3
_READS_WINDOW_DAYS = 14
#: A "hot dead end" needs this many distinct reading sessions …
_DEAD_END_MIN_SESSIONS = 4
#: … while carrying at most this many mention edges.
_DEAD_END_MAX_MENTIONS = 2
#: ``related-to`` edges on a childless node before it is a mention fan.
_MENTION_FAN_MIN = 12
#: Revisions on a non-hub node before the churn is a smell.
_EDIT_CHURN_MIN = 10
#: Body size floor for the grab-bag detector (chars).
_GRAB_BAG_MIN_CHARS = 2000
#: Distinct dates in a body before it reads as accumulated history.
_HISTORY_MIN_DATES = 5
#: Shared sessions before two nodes fetched together count as a
#: missing-link candidate …
_CO_READ_MIN_SESSIONS = 3
#: … when the neighbourhood puts them more than this many hops apart.
_CO_READ_MIN_DISTANCE = 2

# ── render caps ─────────────────────────────────────────────────────

#: Node rows on one environment page; the truncated tail is counted.
_MAX_NODE_ROWS = 60
_MAX_EDGE_ROWS = 80
_MAX_SMELL_HANDLES = 12
_MAX_CO_READ = 15
#: Mermaid stays legible only in a small sketch — cap nodes, then
#: per-node mention edges (the rest collapse into a "+N more" stub).
_MERMAID_MAX_NODES = 40
_MERMAID_MAX_MENTIONS = 4

_NEXT_ENV = "get(kind='memory', id='me<N>', view='environment', args={'hops': 2})"
_NEXT_SHAPE = "search(kind='memory', view='shape', args={'under': 'me<hub>'})"

#: Relations that make an edge *structural* or a *mention* for the
#: per-node counts — every other relation is a "typed" edge.
_STRUCTURAL_RELS = frozenset({"part-of", "contains"})
_MENTION_REL = "related-to"
#: ``qualifies`` pairs may be stored either direction (the read side
#: shows the inverse ``qualified-by``); a node at either end of one is
#: "qualified".
_QUALIFIES_RELS = frozenset({"qualifies", "qualified-by"})

_SLUG_TITLE_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)+$")
_PATH_TITLE_RE = re.compile(r"\S[/\\]\S|\S+\.(?:py|md|txt|ya?ml|json|sql|sh)\b", re.I)
_DATE_TITLE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_BODY_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

_HUB_SECTION = "index"
_SECTION_ORDER = ("index", "threads", "gotchas", "runbooks", "workflow", "reference")


@dataclass
class _Node:
    """One live memory in the gathered scope."""

    ref_id: int
    title: str
    tags: frozenset[str]  # rendered ('section:gotchas', 'SPACE:repo-dev', …)
    depth: int  # hops from focus (environment) or part-of depth (shape)
    body: str
    body_chars: int
    reads: int
    reads_3d: int
    reads_14d: int
    sessions: int
    created_at: datetime | None
    updated_at: datetime | None
    last_edit_at: datetime | None
    edits: int
    editors: int
    hub_id: int | None
    children: int
    parents: tuple[int, ...]
    typed: int
    mentions: int
    cross_kind: int

    @property
    def handle(self) -> str:
        return handle_registry.format_handle("memory", self.ref_id)

    @property
    def section(self) -> str:
        """The node's ``section:<type>`` value ('' when untyped, first
        when several — a multi-section node is itself odd-shaped)."""
        values = sorted(t[8:] for t in self.tags if t.startswith("section:"))
        return values[0] if values else ""


@dataclass(frozen=True)
class _Edge:
    """One link row touching the scope (either direction)."""

    src: int
    dst: int
    relation: str
    origin: str  # 'mention' when meta.auto='mention', else 'explicit'
    set_by: str
    created_at: datetime | None


@dataclass
class _Smell:
    """One computed shape finding: code, one-line gloss, flagged nodes."""

    code: str
    gloss: str
    nodes: list[int] = field(default_factory=list)
    detail: dict[int, str] = field(default_factory=dict)


@dataclass
class _Scope:
    """The gathered neighbourhood both renders share."""

    nodes: dict[int, _Node]
    inner: list[_Edge]  # edges whose endpoints are both in scope
    edges: list[_Edge]  # every edge touching a scoped node
    calls: list[tuple[int, str | None, datetime]]  # (ref_id, session, ts)
    #: ``ref_tags_bulk`` over scope ∪ live part-of parents — the gotcha
    #: detail node's inherit check reads its summary's tags too.
    tag_map: dict[int, list[tuple[str, str]]] = field(default_factory=dict)


def _handle(ref_id: int) -> str:
    return handle_registry.format_handle("memory", ref_id)


def _day(at: datetime | None) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%d") if at is not None else "-"


def _days_since(at: datetime | None, now: datetime) -> int | None:
    return (now - at).days if at is not None else None


def _tag_strings(
    tag_map: dict[int, list[tuple[str, str]]], ref_id: int
) -> frozenset[str]:
    """The view's tag spellings — the shape ``_scoped_proposals`` uses:
    ``OPEN``/``FLAG`` values bare, closed axes ``NS:value``."""
    return frozenset(
        value if ns in ("OPEN", "FLAG") else f"{ns}:{value}"
        for ns, value in tag_map.get(ref_id, [])
    )


# ── gather ────────────────────────────────────────────────────────────

_REFS_SQL = """
SELECT ref_id, title, created_at, updated_at FROM refs
WHERE ref_id = ANY(%s) AND kind = 'memory' AND retired_at IS NULL
"""

_BODY_SQL = """
SELECT ref_id, string_agg(text, E'\\n' ORDER BY ord) AS body,
       sum(char_length(text)) AS body_chars
FROM chunks
WHERE ref_id = ANY(%s) AND chunk_kind = 'memory_body'
  AND ord >= 0 AND retired_at IS NULL
GROUP BY ref_id
"""

_READS_SQL = """
SELECT ref_id, n_agent, n_human FROM ref_reads WHERE ref_id = ANY(%s)
"""

#: Windowed reads: one row per successful ``get`` whose ``id=`` names a
#: scoped ref — scalar ``'meN'``/``N`` spellings and batch ``id=[…]``
#: arrays alike (``jsonb_array_elements_text`` flattens them; the
#: ``'^(me)?[0-9]+$'`` guard drops path ids like ``'/recent'``).
_CALLS_SQL = """
SELECT regexp_replace(idtxt, '^me', '')::bigint AS rid, session_id, ts
FROM (
    SELECT tc.session_id, tc.ts, tc.arg_sample->>'id' AS idtxt
      FROM tool_calls tc
     WHERE tc.verb = 'get' AND tc.kind = 'memory' AND tc.outcome = 'ok'
       AND tc.ts >= %s AND tc.arg_sample ? 'id'
       AND jsonb_typeof(tc.arg_sample->'id') <> 'array'
    UNION ALL
    SELECT tc.session_id, tc.ts, e.v
      FROM tool_calls tc,
           jsonb_array_elements_text(tc.arg_sample->'id') AS e(v)
     WHERE tc.verb = 'get' AND tc.kind = 'memory' AND tc.outcome = 'ok'
       AND tc.ts >= %s
       AND jsonb_typeof(tc.arg_sample->'id') = 'array'
) s
WHERE idtxt ~ '^(me)?[0-9]+$'
  AND regexp_replace(idtxt, '^me', '')::bigint = ANY(%s::bigint[])
"""

_REVISIONS_SQL = """
SELECT target_id,
       count(*) FILTER (WHERE event = 'edited') AS edits,
       max(at) FILTER (WHERE event = 'edited') AS last_edit,
       count(DISTINCT actor) FILTER (WHERE event = 'edited') AS editors
FROM revisions
WHERE target_kind = 'ref' AND target_id = ANY(%s)
GROUP BY target_id
"""

_EDGES_SQL = """
SELECT src_ref_id, dst_ref_id, relation, set_by, created_at, meta->>'auto'
FROM links WHERE src_ref_id = ANY(%s) OR dst_ref_id = ANY(%s)
"""

_ENDPOINTS_SQL = """
SELECT ref_id, kind FROM refs WHERE ref_id = ANY(%s) AND retired_at IS NULL
"""


def neighbourhood(store: Store, root_id: int, hops: int) -> dict[int, int]:
    """``{live memory ref_id: min hops from root}`` over every relation."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE nbr(ref_id, depth) AS (
                SELECT %s::bigint, 0
                UNION
                SELECT CASE WHEN l.src_ref_id = w.ref_id
                            THEN l.dst_ref_id ELSE l.src_ref_id END,
                       w.depth + 1
                FROM nbr w
                JOIN links l
                  ON l.src_ref_id = w.ref_id OR l.dst_ref_id = w.ref_id
                JOIN refs other
                  ON other.ref_id = CASE WHEN l.src_ref_id = w.ref_id
                                         THEN l.dst_ref_id
                                         ELSE l.src_ref_id END
                 AND other.kind = 'memory' AND other.retired_at IS NULL
                WHERE w.depth < %s
            )
            SELECT ref_id, MIN(depth) FROM nbr GROUP BY ref_id
            """,
            (root_id, hops),
        ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


def _gather(store: Store, ids: list[int], now: datetime) -> _Scope:
    """One bulk read of everything the environment and shape views need.

    Six bounded queries keyed on the id set (refs / bodies / tags /
    reads / revisions / links), one tool_calls window scan (it rides the
    ``(verb, kind, ts)`` index), one endpoint-kind lookup. A scoped
    node's structural counts use *all* its edges, not just the inner
    ones — a mention fan reads the same inside and outside the cut.
    """
    empty = _Scope({}, [], [], [])
    if not ids:
        return empty
    with store.pool.connection() as conn:
        refs = conn.execute(_REFS_SQL, (ids,)).fetchall()
        live_ids = [int(r[0]) for r in refs]
        if not live_ids:
            return empty
        bodies = {
            int(r[0]): (str(r[1] or ""), int(r[2] or 0))
            for r in conn.execute(_BODY_SQL, (live_ids,)).fetchall()
        }
        reads = {
            int(r[0]): (int(r[1] or 0) + int(r[2] or 0))
            for r in conn.execute(_READS_SQL, (live_ids,)).fetchall()
        }
        cutoff = now - timedelta(days=_READS_WINDOW_DAYS)
        calls = [
            (int(r[0]), str(r[1]) if r[1] is not None else None, r[2])
            for r in conn.execute(_CALLS_SQL, (cutoff, cutoff, live_ids)).fetchall()
        ]
        revisions = {
            int(r[0]): (int(r[1]), r[2], int(r[3]))
            for r in conn.execute(_REVISIONS_SQL, (live_ids,)).fetchall()
        }
        edge_rows = conn.execute(_EDGES_SQL, (live_ids, live_ids)).fetchall()
    edges = [
        _Edge(
            src=int(r[0]),
            dst=int(r[1]),
            relation=str(r[2]),
            origin="mention" if r[5] == "mention" else "explicit",
            set_by=str(r[3] or ""),
            created_at=r[4],
        )
        for r in edge_rows
    ]
    endpoints = sorted({e.src for e in edges} | {e.dst for e in edges})
    kinds: dict[int, str] = {}
    if endpoints:
        with store.pool.connection() as conn:
            kinds = {
                int(r[0]): str(r[1])
                for r in conn.execute(_ENDPOINTS_SQL, (endpoints,)).fetchall()
            }
    live_set = set(live_ids)
    inner = [e for e in edges if e.src in live_set and e.dst in live_set]

    # Parent/child and the gotcha-inherits check read live parents
    # outside the scope too — a gotcha detail node inherits its
    # summary's qualification wherever the summary sits.
    parents_map = parents_of(store, live_ids)
    tag_map = store.ref_tags_bulk(
        sorted(live_set | {p for ps in parents_map.values() for p in ps})
    )
    hubs = hubs_of(store, live_ids)

    hot_cut = now - timedelta(days=_READS_HOT_DAYS)
    per_node_calls: dict[int, list[tuple[str | None, datetime]]] = defaultdict(list)
    for rid, session, ts in calls:
        per_node_calls[rid].append((session, ts))
    children_of: dict[int, int] = defaultdict(int)
    for member, parents in parents_map.items():
        for parent in parents:
            children_of[parent] += 1

    nodes: dict[int, _Node] = {}
    for ref_id, title, created_at, updated_at in refs:
        ref_id = int(ref_id)
        typed = mentions = cross_kind = 0
        for e in edges:
            if ref_id not in (e.src, e.dst):
                continue
            other = e.dst if e.src == ref_id else e.src
            if other == ref_id or other not in kinds:
                continue  # self-loop or retired endpoint
            if kinds[other] != "memory":
                cross_kind += 1
            if e.relation == _MENTION_REL:
                mentions += 1
            elif e.relation not in _STRUCTURAL_RELS:
                typed += 1
        ncalls = per_node_calls.get(ref_id, [])
        edits, last_edit, editors = revisions.get(ref_id, (0, None, 0))
        body, body_chars = bodies.get(ref_id, ("", 0))
        hub_list = hubs.get(ref_id, [])
        nodes[ref_id] = _Node(
            ref_id=ref_id,
            title=str(title or ""),
            tags=_tag_strings(tag_map, ref_id),
            depth=-1,  # the caller's walk stamps it
            body=body,
            body_chars=body_chars,
            reads=reads.get(ref_id, 0),
            reads_3d=sum(1 for _s, ts in ncalls if ts >= hot_cut),
            reads_14d=len(ncalls),
            sessions=len({s for s, _ts in ncalls if s}),
            created_at=created_at,
            updated_at=updated_at,
            last_edit_at=last_edit,
            edits=edits,
            editors=editors,
            hub_id=hub_list[0] if hub_list else None,
            children=children_of.get(ref_id, 0),
            parents=tuple(parents_map.get(ref_id, [])),
            typed=typed,
            mentions=mentions,
            cross_kind=cross_kind,
        )
    return _Scope(nodes, inner, edges, calls, tag_map)


# ── smells ────────────────────────────────────────────────────────────


def _smell(smells: list[_Smell], code: str, gloss: str, node: int, detail: str) -> None:
    """Append ``node`` to ``code``'s finding, creating it on first hit."""
    for s in smells:
        if s.code == code:
            s.nodes.append(node)
            s.detail[node] = detail
            return
    smells.append(_Smell(code, gloss, [node], {node: detail}))


def _child_nodes(scope: _Scope, node: _Node) -> list[_Node]:
    """The node's live part-of children inside the scope."""
    out: list[_Node] = []
    for e in scope.edges:
        child_id: int | None = None
        if e.relation == "part-of" and e.dst == node.ref_id:
            child_id = e.src
        elif e.relation == "contains" and e.src == node.ref_id:
            child_id = e.dst
        if child_id is not None and child_id in scope.nodes:
            out.append(scope.nodes[child_id])
    return out


def _sweep_eligible(
    child: _Node,
    superseded: set[int],
    stale_cut: datetime,
    suspect_cut: date,
) -> bool:
    """Whether the thread sweep's rules a–c would retire this node.

    Mirrors :func:`precis.workers.memory_thread_sweep.plan_thread_sweep`:
    ``section:threads`` (never ``section:index``), not scratch-owned, and
    'Left: done' / superseded / untouched ≥ STALE_DAYS without a fresh
    dated section (a fresh date makes it a currency suspect — listed by
    the sweep but never retired, so not this smell's evidence either).
    """
    if child.section != "threads" or INDEX_TAG in child.tags:
        return False
    if {t for t in child.tags if t.startswith("SPACE:")} == {"SPACE:scratch"}:
        return False
    left = first_left(child.body)
    if left is not None and left_is_done(left):
        return True
    if child.ref_id in superseded:
        return True
    if child.updated_at is not None and child.updated_at < stale_cut:
        fresh = newest_body_date(child.body)
        if fresh is None or fresh < suspect_cut:
            return True
    return False


def compute_smells(scope: _Scope, now: datetime) -> list[_Smell]:
    """The review-side shape detectors over the gathered nodes.

    Each detector is a small deterministic predicate; the finding lists
    every flagged handle plus a one-line evidence string. Thresholds are
    the module constants — deliberately the same numbers the gripe's
    export analysis validated, so the view reproduces those counts.
    """
    from precis.handlers._memory_shape import _atomic_titles, _prose_lines

    smells: list[_Smell] = []
    stale_cut = now - timedelta(days=STALE_DAYS)
    suspect_cut = (now - timedelta(days=STALE_DAYS)).date()
    # A parent outside the scope still governs the inherit check — its
    # section values ride the widened tag_map.
    parent_sections = {
        rid: {v for ns, v in pairs if ns == "OPEN" and v.startswith("section:")}
        for rid, pairs in scope.tag_map.items()
    }
    qualified = {
        r for e in scope.edges if e.relation in _QUALIFIES_RELS for r in (e.src, e.dst)
    }
    superseded = {e.dst for e in scope.edges if e.relation == "supersedes"}
    for n in sorted(scope.nodes.values(), key=lambda x: x.ref_id):
        title = n.title.strip()
        if _SLUG_TITLE_RE.match(title) or _PATH_TITLE_RE.search(title):
            _smell(
                smells,
                "slug-title",
                "title is a slug, path or filename — the claim belongs in words",
                n.ref_id,
                title[:60],
            )
        if _DATE_TITLE_RE.search(title):
            _smell(
                smells,
                "dated-title",
                "title carries a date — a snapshot label, not a standing claim",
                n.ref_id,
                title[:60],
            )
        if not n.section:
            _smell(
                smells,
                "no-section",
                "no section:<type> tag — the graph can't place it",
                n.ref_id,
                "tags: " + (", ".join(sorted(n.tags)) or "none"),
            )
        # A detail node under a gotcha summary inherits the summary's
        # qualification — the missing edge is the parent's, not its.
        if (
            n.section == "gotchas"
            and n.ref_id not in qualified
            and not any(
                "section:gotchas" in parent_sections.get(p, set()) for p in n.parents
            )
        ):
            _smell(
                smells,
                "unqualified-gotcha",
                "gotcha qualifies nothing — link it rel='qualifies' to a thread",
                n.ref_id,
                title[:60],
            )
        if n.section == "threads" and n.ref_id not in qualified:
            _smell(
                smells,
                "unqualified-thread",
                "no gotcha qualifies this thread — write or link one",
                n.ref_id,
                title[:60],
            )
        if n.edits >= _EDIT_CHURN_MIN and n.section != _HUB_SECTION:
            _smell(
                smells,
                "edit-churn",
                f"{_EDIT_CHURN_MIN}+ edits on a non-hub — claims keep shifting",
                n.ref_id,
                f"{n.edits} edits by {n.editors} writers",
            )
        if (
            n.sessions >= _DEAD_END_MIN_SESSIONS
            and n.typed == 0
            and n.mentions <= _DEAD_END_MAX_MENTIONS
        ):
            _smell(
                smells,
                "hot-dead-end",
                "sessions keep landing on it, yet no typed edge and "
                f"at most {_DEAD_END_MAX_MENTIONS} mentions point in",
                n.ref_id,
                f"{n.sessions} sessions, {n.mentions} mentions, 0 typed edges",
            )
        if n.body_chars >= _GRAB_BAG_MIN_CHARS:
            groups = _atomic_titles(n.title, _prose_lines(n.body))
            if groups:
                _smell(
                    smells,
                    "grab-bag",
                    "one body, several claim groups — split into one-claim memories",
                    n.ref_id,
                    f"{n.body_chars} chars; proposed: {'; '.join(groups[:3])}",
                )
        dates = set(_BODY_DATE_RE.findall(n.body))
        if len(dates) >= _HISTORY_MIN_DATES:
            _smell(
                smells,
                "history-dense",
                f"{_HISTORY_MIN_DATES}+ distinct dates — reads as a changelog, "
                "not a claim",
                n.ref_id,
                f"{len(dates)} dates, newest {max(dates)}",
            )
        if n.reads_3d > 0 and n.updated_at is not None and n.updated_at < stale_cut:
            _smell(
                smells,
                "stale-hot",
                f"read in the last {_READS_HOT_DAYS}d but untouched "
                f"{STALE_DAYS}d+ — fresh traffic on possibly stale prose",
                n.ref_id,
                f"{n.reads_3d} reads/3d, last touched {_day(n.updated_at)}",
            )
        if len(n.parents) > 1:
            _smell(
                smells,
                "multi-parent",
                "more than one live part-of parent — one node, one hub",
                n.ref_id,
                "parents: " + ", ".join(_handle(p) for p in n.parents),
            )
        if n.mentions >= _MENTION_FAN_MIN and n.children == 0:
            _smell(
                smells,
                "mention-fan-leaf",
                f"{_MENTION_FAN_MIN}+ mentions on a leaf — the citing nodes "
                "should probably be children or a summary",
                n.ref_id,
                f"{n.mentions} mention edges",
            )
        sweepable = [
            c
            for c in _child_nodes(scope, n)
            if _sweep_eligible(c, superseded, stale_cut, suspect_cut)
        ]
        if sweepable:
            _smell(
                smells,
                "sweep-ready-children",
                "part-of children the 14-day thread sweep would retire — "
                "their resume state rides on this node",
                n.ref_id,
                f"{len(sweepable)}: " + ", ".join(c.handle for c in sweepable[:6]),
            )
    return smells


# ── co-read pairs ─────────────────────────────────────────────────────


def _adjacency(scope: _Scope) -> dict[int, set[int]]:
    adj: dict[int, set[int]] = {rid: set() for rid in scope.nodes}
    for e in scope.inner:
        adj[e.src].add(e.dst)
        adj[e.dst].add(e.src)
    return adj


def _distance(adj: dict[int, set[int]], a: int, b: int) -> int | None:
    """Undirected hops a→b inside the scope; ``None`` when disconnected."""
    if a == b:
        return 0
    seen = {a}
    frontier = deque([(a, 0)])
    while frontier:
        node, d = frontier.popleft()
        for nxt in adj.get(node, ()):
            if nxt == b:
                return d + 1
            if nxt not in seen:
                seen.add(nxt)
                frontier.append((nxt, d + 1))
    return None


def co_read_pairs(scope: _Scope) -> list[tuple[int, int, int, int | None]]:
    """``[(a, b, sessions, distance)]`` — node pairs read in the same
    session in ``_CO_READ_MIN_SESSIONS``+ distinct sessions while more
    than ``_CO_READ_MIN_DISTANCE`` hops apart (or disconnected). Sorted
    by session count desc. Missing-link candidates: readers keep
    fetching them together, but the graph doesn't know they're related.
    """
    sessions: dict[str, set[int]] = defaultdict(set)
    for rid, session, _ts in scope.calls:
        if session and rid in scope.nodes:
            sessions[session].add(rid)
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for rids in sessions.values():
        ordered = sorted(rids)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                counts[(a, b)] += 1
    adj: dict[int, set[int]] | None = None
    out: list[tuple[int, int, int, int | None]] = []
    for (a, b), n in counts.items():
        if n < _CO_READ_MIN_SESSIONS:
            continue
        if adj is None:
            adj = _adjacency(scope)
        d = _distance(adj, a, b)
        if d is None or d > _CO_READ_MIN_DISTANCE:
            out.append((a, b, n, d))
    out.sort(key=lambda t: (-t[2], t[0], t[1]))
    return out


# ── mermaid ───────────────────────────────────────────────────────────


def render_mermaid(scope: _Scope, now: datetime) -> str:
    """A bounded mermaid sketch of the neighbourhood.

    Node label = handle, title, lifetime reads, body chars, days since
    edit; ``:::class`` by ``section:<type>``. part-of solid, qualifies
    thick, mentions dotted — a node's mentions past
    ``_MERMAID_MAX_MENTIONS`` collapse into one ``+N more`` stub.
    """
    ids = sorted(scope.nodes)[:_MERMAID_MAX_NODES]
    shown = set(ids)
    lines = ["graph LR"]
    for section in _SECTION_ORDER:
        lines.append(f"  classDef {section} fill:#eef,stroke:#889")
    for rid in ids:
        n = scope.nodes[rid]
        days = _days_since(n.last_edit_at or n.updated_at, now)
        age = "?" if days is None else f"{days}d"
        title = " ".join(n.title.split())[:28].replace('"', "'")
        cls = f":::{n.section}" if n.section in _SECTION_ORDER else ""
        lines.append(
            f'  m{rid}["{n.handle} {title}\\n{n.reads} reads · '
            f'{n.body_chars}ch · {age}"]{cls}'
        )
    mentions_rendered: dict[int, int] = defaultdict(int)
    for e in scope.inner:
        if e.src not in shown or e.dst not in shown:
            continue
        if e.relation in _STRUCTURAL_RELS:
            child, parent = (
                (e.src, e.dst) if e.relation == "part-of" else (e.dst, e.src)
            )
            lines.append(f"  m{child} --> m{parent}")
        elif e.relation in _QUALIFIES_RELS:
            src, dst = (e.src, e.dst) if e.relation == "qualifies" else (e.dst, e.src)
            lines.append(f"  m{src} ==> m{dst}")
        elif e.relation == _MENTION_REL:
            if mentions_rendered[e.src] < _MERMAID_MAX_MENTIONS:
                lines.append(f"  m{e.src} -.-> m{e.dst}")
            mentions_rendered[e.src] += 1
        else:
            lines.append(f"  m{e.src} -->|{e.relation}| m{e.dst}")
    for rid in ids:
        extra = mentions_rendered.get(rid, 0) - _MERMAID_MAX_MENTIONS
        if extra > 0:
            lines.append(f'  m{rid}m["+{extra} more mentions"] -.-> m{rid}')
    cut = len(scope.nodes) - len(ids)
    if cut > 0:
        lines.append(f'  m_cut["… {cut} more nodes not drawn"]')
    return "```mermaid\n" + "\n".join(lines) + "\n```"


# ── renders ───────────────────────────────────────────────────────────

_NODE_SCHEMA = [
    "handle",
    "title",
    "section",
    "depth",
    "hub",
    "chars",
    "reads",
    "r3d",
    "r14d",
    "sess",
    "created",
    "edited",
    "edits",
    "edtrs",
    "kids",
    "typed",
    "ment",
    "xkind",
]

_EDGE_SCHEMA = ["edge", "rel", "origin", "set_by", "age_d"]


def _node_rows(nodes: list[_Node], now: datetime) -> list[dict[str, Any]]:
    return [
        {
            "handle": n.handle,
            "title": " ".join(n.title.split())[:40],
            "section": n.section or "-",
            "depth": n.depth,
            "hub": _handle(n.hub_id) if n.hub_id else "-",
            "chars": n.body_chars,
            "reads": n.reads,
            "r3d": n.reads_3d,
            "r14d": n.reads_14d,
            "sess": n.sessions,
            "created": _day(n.created_at),
            "edited": _day(n.last_edit_at or n.updated_at),
            "edits": n.edits,
            "edtrs": n.editors,
            "kids": n.children,
            "typed": n.typed,
            "ment": n.mentions,
            "xkind": n.cross_kind,
        }
        for n in nodes
    ]


def _edge_rows(scope: _Scope, now: datetime) -> list[dict[str, Any]]:
    return [
        {
            "edge": f"{_handle(e.src)} → {_handle(e.dst)}",
            "rel": e.relation,
            "origin": e.origin,
            "set_by": e.set_by or "-",
            "age_d": _days_since(e.created_at, now) or 0,
        }
        for e in sorted(
            scope.inner,
            key=lambda e: (e.relation not in _STRUCTURAL_RELS, e.src, e.dst),
        )
    ]


def _smell_block(smells: list[_Smell]) -> str:
    if not smells:
        return "## smells\nnone — nothing in this scope trips a detector."
    lines = [
        f"## smells — {sum(len(s.nodes) for s in smells)} findings "
        f"over {len({r for s in smells for r in s.nodes})} nodes"
    ]
    for s in smells:
        shown = ", ".join(_handle(r) for r in s.nodes[:_MAX_SMELL_HANDLES])
        more = len(s.nodes) - _MAX_SMELL_HANDLES
        lines.append(
            f"- {s.code} ({len(s.nodes)}): {s.gloss}\n"
            f"  {shown}{f' … +{more}' if more > 0 else ''}"
        )
    return "\n".join(lines)


def _co_read_block(pairs: list[tuple[int, int, int, int | None]]) -> str:
    if not pairs:
        return ""
    lines = [
        "## co-read pairs — fetched together in "
        f"{_CO_READ_MIN_SESSIONS}+ sessions, >{_CO_READ_MIN_DISTANCE} hops "
        "apart (missing-link candidates)"
    ]
    for a, b, n, d in pairs[:_MAX_CO_READ]:
        dist = "disconnected" if d is None else f"{d} hops"
        lines.append(f"- {_handle(a)} ↔ {_handle(b)}: {n} sessions, {dist}")
    return "\n".join(lines)


def render_environment(store: Store, ref: Ref, *, hops: int, mermaid: bool) -> Response:
    """``get(kind='memory', id=N, view='environment')``.

    The focus's live memory neighbourhood, its inner edges, the shape
    smells over them and the session co-read pairs — one read for the
    review loop the gripe's three SQL exports replaced.
    """
    now = datetime.now(UTC)
    depths = neighbourhood(store, ref.id, hops)
    scope = _gather(store, sorted(depths), now)
    for rid, node in scope.nodes.items():
        node.depth = depths.get(rid, hops)
    ordered = sorted(scope.nodes.values(), key=lambda n: (n.depth, n.ref_id))
    smells = compute_smells(scope, now)
    pairs = co_read_pairs(scope)

    head = (
        f"environment: {_handle(ref.id)} — hops={hops}, "
        f"{len(scope.nodes)} nodes, {len(scope.inner)} memory-memory edges; "
        f"reads window {_READS_WINDOW_DAYS}d (tool_calls retention), "
        "sess = distinct sessions\n"
        "reads = lifetime counter · r3d/r14d = get calls in window · "
        "ment = related-to edges · typed = non-part-of non-related-to · "
        "xkind = live non-memory neighbours"
    )
    parts = [head]
    shown = ordered[:_MAX_NODE_ROWS]
    parts.append(
        render_agent_table(_node_rows(shown, now), schema=_NODE_SCHEMA)
        if shown
        else "(no nodes)"
    )
    if len(ordered) > len(shown):
        parts.append(
            f"… {len(ordered) - len(shown)} more nodes — "
            "page with a smaller hops= or re-focus on a listed neighbour"
        )
    edge_rows = _edge_rows(scope, now)
    if edge_rows:
        parts.append(
            "## edges\n"
            + render_agent_table(edge_rows[:_MAX_EDGE_ROWS], schema=_EDGE_SCHEMA)
        )
        if len(edge_rows) > _MAX_EDGE_ROWS:
            parts.append(f"… {len(edge_rows) - _MAX_EDGE_ROWS} more edges")
    parts.append(_smell_block(smells))
    co = _co_read_block(pairs)
    if co:
        parts.append(co)
    if mermaid:
        parts.append("## mermaid\n" + render_mermaid(scope, now))
    parts.append(
        render_next_section(
            [
                (
                    "search(kind='memory', view='shape', "
                    f"args={{'under': '{_handle(ref.id)}'}})",
                    "roll these smells up over a hub scope",
                ),
                (
                    f"get(kind='memory', id={ref.id}, view='fisheye+2hop')",
                    "read the prose, not the stats",
                ),
            ]
        )
    )
    return Response(body="\n\n".join(parts), result_count=len(ordered))


def _scope_nodes(
    store: Store,
    include_ref_ids: list[int] | None,
    exclude_ref_ids: list[int] | None,
    tags: list[str] | None,
) -> _Scope:
    """The shape view's gathered scope: live memories under the resolved
    ``include_ref_ids`` (or every live memory), tag-filtered like the
    smells view's ``_scoped_proposals`` polarity split."""
    if include_ref_ids is None:
        with store.pool.connection() as conn:
            ids = [
                int(r[0])
                for r in conn.execute(
                    "SELECT ref_id FROM refs WHERE kind = 'memory' "
                    "AND retired_at IS NULL ORDER BY ref_id"
                ).fetchall()
            ]
    else:
        ids = sorted({int(i) for i in include_ref_ids})
    if exclude_ref_ids:
        drop = {int(i) for i in exclude_ref_ids}
        ids = [i for i in ids if i not in drop]
    wanted: list[str] = Tag.normalize_filter(tags, kind="memory") or []
    if wanted:
        pos, neg = split_tag_polarity(wanted)
        tag_map = store.ref_tags_bulk(ids)
        ids = [
            rid
            for rid in ids
            if all(t in _tag_strings(tag_map, rid) for t in pos)
            and not any(t in _tag_strings(tag_map, rid) for t in neg)
        ]
    return _gather(store, ids, datetime.now(UTC))


def _fill_part_of_depth(scope: _Scope, root_id: int) -> None:
    """Stamp ``depth`` as part-of distance from ``root_id`` (0 on the
    root when it is in scope; -1 stays on nodes the tree can't reach)."""
    adj: dict[int, list[int]] = defaultdict(list)
    for e in scope.edges:
        if e.relation == "part-of":
            adj[e.dst].append(e.src)
        elif e.relation == "contains":
            adj[e.src].append(e.dst)
    seen = {root_id}
    frontier = deque([(root_id, 0)])
    while frontier:
        node, d = frontier.popleft()
        if node in scope.nodes:
            scope.nodes[node].depth = d
        for child in adj.get(node, []):
            if child not in seen:
                seen.add(child)
                frontier.append((child, d + 1))


def render_shape(
    store: Store,
    *,
    include_ref_ids: list[int] | None,
    exclude_ref_ids: list[int] | None,
    tags: list[str] | None,
    under: str | int | None,
    q: str | None,
    page: int,
    mode: str | None,
    scope: str | None,
    status: str | None,
    sort: str | None = None,
) -> Response:
    """``search(kind='memory', view='shape')`` — the hub rollup.

    Aggregates the whole scoped corpus like ``render_smell_rollup``
    (never a search page): every flagged node counts, with its evidence
    handles, whatever page a search would have put it on.
    """
    if fold_match_all_q(q) or mode or scope or status is not None or page != 1:
        raise BadInput(
            "view='shape' does not apply q/mode/scope/status/page filters",
            next=_NEXT_SHAPE,
        )
    if sort is not None:
        raise BadInput("sort= is not applied by view='shape'", next=_NEXT_SHAPE)
    if under is not None and include_ref_ids is None:
        raise BadInput(
            "under= reached the handler without resolved members",
            next=_NEXT_SHAPE,
        )
    now = datetime.now(UTC)
    gathered = _scope_nodes(store, include_ref_ids, exclude_ref_ids, tags)
    if under is not None and gathered.nodes:
        root = store.resolve_handle(str(under))
        if root is not None:
            _fill_part_of_depth(gathered, int(root.ref_id))
    smells = compute_smells(gathered, now)
    pairs = co_read_pairs(gathered)
    heading = (
        f"shape: {under or 'all live memories'} — "
        f"{len(gathered.nodes)} live memories, "
        f"{len(gathered.inner)} memory-memory edges; "
        f"reads window {_READS_WINDOW_DAYS}d"
    )
    parts = [heading, _smell_block(smells)]
    co = _co_read_block(pairs)
    if co:
        parts.append(co)
    parts.append(
        render_next_section(
            [
                (
                    "get(kind='memory', id='me<N>', view='environment', "
                    "args={'hops': 3})",
                    "walk one flagged node's neighbourhood",
                ),
                (
                    "search(kind='memory', view='smells', args={'under': 'me<hub>'})",
                    "the garden's proposal side of the same smells",
                ),
            ]
        )
    )
    flagged = {r for s in smells for r in s.nodes}
    return Response(body="\n\n".join(parts), result_count=len(flagged))


def count_shape_nodes(
    store: Store,
    *,
    include_ref_ids: list[int] | None = None,
    exclude_ref_ids: list[int] | None = None,
    tags: list[str] | None = None,
) -> int:
    """The shape view's corpus size under one scope — the domain
    footer's before/after counter. The view renders flagged nodes but
    *evaluates* the whole corpus, so the corpus is the count that
    matters for "entries the domain door hid"."""
    return len(_scope_nodes(store, include_ref_ids, exclude_ref_ids, tags).nodes)
