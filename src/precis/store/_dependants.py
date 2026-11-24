"""The dependants walk — ``Store.dependants`` (gr626866).

Mutation review needs the half of the link graph the reading views hide:
the refs that *lean on* this one — "uses-by-others" — plus the transitive
evidence dependants behind them. ``neighbourhood``/``view='links'`` show
edges as stored; this walk normalises each relation to its *rely* side and
follows only the edges where the walked node is the relied-upon end:

- Most relations read ``src relies on dst`` — the src's own record points
  at and needs the dst (``cites``, ``part-of``, ``derived-from``,
  ``specialises``, ``has-prerequisite``, ``has-requirement``, every
  ``*-of``, ``supersedes``, ``corrects``, ``refines``, ``considered``,
  ``see-also``, ``quantifies``, ``made-by``, ``authored-by``, …).
- The exception families (:data:`_DEPENDANT_AT_DST`) have the *dst*
  relying on the src: the evidence/service actives (``corroborates``:
  the claim leans on the paper; ``serves``: the quest leans on the
  finding; ``blocks``: the blocked leans on the blocker; ``establishes``,
  ``supports``, ``entails`` the same), the producer→artifact actives
  (``authored``, ``derived-into``, ``generalises``, ``contains``,
  ``makes``, ``realizes``: the artifact leans on its origin), the
  dossier disposition edges (``cited-in``, ``superseded-in``,
  ``off-topic-for``: the dossier's record of the paper leans on it),
  ownership edges such as ref-level ``has-figure`` (the figure leans on
  its project), and the passive
  spellings of src-depends actives — ``cited-by``, ``prerequisite-of``,
  ``requirement-of``, ``used-in``, ``depicted-in``, ``fixed-by``,
  ``contradicted-by``, ``quantified-by``, every ``*-ed-by``/``*-in``
  inverse of a src-depends active. ``authored-by`` is the deliberate
  odd one out: ``authored`` itself is dst-depends, so its passive
  spelling is src-depends like the default.
- For a draft figure anchor, chunk-level ``has-figure`` points to the
  canvas the draft relies on. Its companion ``figure-in`` points from the
  canvas to that anchor and places the dependant at dst.
- Symmetric relations (:data:`_DEPENDANT_BOTH`) make each endpoint the
  other's dependant (``related-to``, ``same-family-as``, …).

Like :data:`precis.utils.refeye.RING_GROUPS` the tables are a decision,
not a consequence: a new relation joins the walk on the src-depends
default (safe — most relations are), and moving one is a human call.

Paging is offset-based over a re-walk: each call re-runs the bounded BFS
and slices ``rows[cursor:cursor+limit]`` — deterministic because the walk
orders by ``(depth, ref_id)``. ``complete`` is True only when the frontier
was fully expanded inside ``depth`` *and* every seen row was returned: a
capped-empty page still reports ``INCOMPLETE``, never "no dependants".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from psycopg import Connection

from precis.errors import BadInput, NotFound

if TYPE_CHECKING:
    from precis.store import Store

#: Depth bound for :meth:`DependantsMixin.dependants` — dependant chains
#: worth a mutation review are shallow (evidence → hub → draft is 2);
#: the cap keeps a pre-existing cycle cheap.
DEPENDANTS_MAX_DEPTH = 8

#: Default expansion budget: how many *nodes* (the target included) the
#: walk may scan the edges of. A discovered-but-unexpanded node is
#: ``uninspected`` — its own dependants may exist but were never looked
#: at. Deliberately larger than ``limit``: the page slices *seen* rows,
#: the budget bounds the *walk*.
DEPENDANTS_NODE_BUDGET = 256

#: Max rows one page returns.
DEPENDANTS_MAX_LIMIT = 200

#: Per-level edge-fetch cap. One level's touching-edge set past this is
#: truncated in ``link_id`` order; the unexamined edges count into
#: ``uninspected`` (a lower bound — they may hide novel dependants).
_DEPENDANTS_EDGE_CAP = 10_000

#: Cursor spelling: ``dep:<offset>`` — the number of rows already
#: returned. Opaque to callers; ``dep:`` pins the format so a guessed
#: cursor is a BadInput, not a silent wrong page.
_CURSOR_PREFIX = "dep:"

#: ``dependants(viewer=)`` default: resolve the acting identity — the
#: same sentinel convention :meth:`LinksMixin.neighbourhood` uses.
_ACTING_VIEWER = object()

#: Relations whose *dst* is the relying party — see the module docstring.
#: A stored ``A -rel-> T`` edge here means T relies on A, so A's
#: dependants list gains T. Every other named relation falls to the
#: ``src`` default (``A -rel-> B`` reads as A's record pointing at and
#: needing B).
_DEPENDANT_AT_DST: frozenset[str] = frozenset(
    {
        # evidence/service: the dst's record leans on the src
        "corroborates",
        "establishes",
        "supports",
        "serves",
        "blocks",
        "entails",
        # producer→artifact actives: the artifact leans on its origin
        "authored",
        "derived-into",
        "generalises",
        "contains",
        "makes",
        "realizes",
        # dossier disposition edges (migration 0085 family, alongside
        # corroborates): src=paper, dst=dossier — the dossier's record
        # leans on the paper
        "cited-in",
        "superseded-in",
        "off-topic-for",
        # passive spellings of src-depends actives — the rely party is
        # the stored dst (``B -fixed-by-> A`` ≡ ``A -fixes-> B``: A
        # relies on B, so A is B's dependant)
        "cited-by",
        "superseded-by",
        "retracted-by",
        "corrected-by",
        "concern-raised-by",
        "misattributed-by",
        "contradicted-by",
        "fixed-by",
        "depicted-in",
        "used-in",
        "quantified-by",
        "has-draft",
        "has-plan",
        "has-dossier",
        "has-paper",
        "has-datasheet",
        "has-figure",
        "figure-in",
        "has-mermaid",
        "has-snapshot",
        "has-fold-structure",
        "has-copy",
        "has-instance",
        "prerequisite-of",
        "requirement-of",
        "plotted-by",
        "analyzed-by",
        "qualified-by",
        "represented-by",
        "requested-by",
    }
)

#: Symmetric relations — each endpoint is the other's dependant.
_DEPENDANT_BOTH: frozenset[str] = frozenset(
    {
        "related-to",
        "analogy-of",
        "contrasts-with",
        "kept-distinct",
        "same-family-as",
        "touched",
    }
)


@dataclass(frozen=True)
class DependantRow:
    """One discovered dependant.

    ``relation`` is the edge *as the relied-upon node sees it* (the same
    rewrite ``links_for``/``neighbourhood`` apply: the stored slug when
    that node is the src, the relation's inverse when it is the dst);
    ``via_ref_id``/``via_kind`` are the depth-``depth-1`` node the edge
    hangs on — the anchor for a depth-2+ row, the target for depth 1.
    """

    depth: int
    ref_id: int
    kind: str
    relation: str
    via_ref_id: int
    via_kind: str


@dataclass(frozen=True)
class DependantsPage:
    """One page of the dependants walk.

    ``rows`` is this page's slice; ``shown`` its length. ``total_seen``
    counts every dependant the walk *identified* (all pages, all depths
    inside the budget). ``uninspected`` counts discovered nodes the node
    budget left unexpanded plus edges the per-level scan cap truncated —
    dependants that may exist but were never looked at. ``complete`` is
    True only with the frontier exhausted inside ``depth`` and every seen
    row returned — a capped empty page keeps it False.
    """

    rows: tuple[DependantRow, ...]
    shown: int
    total_seen: int
    uninspected: int
    next_cursor: str | None
    complete: bool


class DependantsMixin:
    """``Store.dependants`` — bounded, paged BFS over rely-side edges.

    Mixin assumes the concrete Store provides ``self.pool``.
    """

    pool: Any

    def _inverse_map(self) -> dict[str, str | None]:
        """``relations.slug -> inverse_slug``, cached like
        :meth:`precis.store._links_ops.LinksMixin.inverse_relation`
        (same ``__dict__`` pattern — the vocabulary is static once
        migrations have run)."""
        cached = getattr(self, "_dependants_inverse_cache", None)
        if cached is None:
            with self.pool.connection() as c:
                rows = c.execute("SELECT slug, inverse_slug FROM relations").fetchall()
            cached = {
                str(r[0]): (str(r[1]) if r[1] is not None else None) for r in rows
            }
            self._dependants_inverse_cache = cached
        return cached

    @staticmethod
    def _dependant_side(relation: str, *, from_chunk: bool = False) -> str:
        """Which stored endpoint relies on the other: ``'dst'``, ``'src'``
        (the default — ``A -rel-> B`` means A's record points at and needs
        B), or ``'both'`` for a symmetric relation."""
        if relation in _DEPENDANT_BOTH:
            return "both"
        # A draft anchor embedding a canvas relies on that canvas. The
        # ref-level project → figure spelling describes project ownership.
        if relation == "has-figure" and from_chunk:
            return "src"
        if relation in _DEPENDANT_AT_DST:
            return "dst"
        return "src"

    def _dependant_edges(
        self,
        conn: Connection,
        frontier: list[int],
    ) -> tuple[list[tuple[int, int, int, str, bool]], int]:
        """Every link touching ``frontier``, plus the count of
        matching edges the scan cap left unfetched.

        Returns ``(rows, truncated)`` — rows as ``(link_id, src_ref_id,
        dst_ref_id, relation, from_chunk)`` in ``link_id`` order, ``truncated`` the
        number of edges never examined (0 when the cap didn't bite).
        """
        rows = conn.execute(
            """
            SELECT l.link_id, l.src_ref_id, l.dst_ref_id, l.relation,
                   l.src_chunk_id IS NOT NULL AS from_chunk,
                   count(*) OVER () AS total
              FROM links l
             WHERE (l.src_ref_id = ANY(%(f)s::bigint[])
                OR  l.dst_ref_id = ANY(%(f)s::bigint[]))
               AND l.src_ref_id <> l.dst_ref_id
             ORDER BY l.link_id
             LIMIT %(cap)s::int
            """,
            {"f": list(frontier), "cap": _DEPENDANTS_EDGE_CAP},
        ).fetchall()
        edges = [(int(r[0]), int(r[1]), int(r[2]), str(r[3]), bool(r[4])) for r in rows]
        total = int(rows[0][5]) if rows else 0
        return edges, max(total - len(edges), 0)

    def _live_dependant_kinds(
        self,
        conn: Connection,
        candidate_ids: list[int],
        *,
        viewer_login: str | None,
    ) -> dict[int, str]:
        """``{ref_id: kind}`` for the candidates that are live and
        visible — retired refs are never dependants, and another owner's
        ``profile`` row is a missing dependant the way
        :meth:`LinksMixin.neighbourhood` makes it a missing node
        (gr490266)."""
        if not candidate_ids:
            return {}
        rows = conn.execute(
            """
            SELECT r.ref_id, r.kind
              FROM refs r
             WHERE r.ref_id = ANY(%(ids)s::bigint[])
               AND r.retired_at IS NULL
               AND (%(viewer)s::text IS NULL OR r.kind <> 'profile'
                    OR r.owner_login IS NULL
                    OR r.owner_login = %(viewer)s::text)
            """,
            {"ids": candidate_ids, "viewer": viewer_login},
        ).fetchall()
        return {int(r[0]): str(r[1]) for r in rows}

    def dependants(
        self,
        ref_id: int,
        *,
        depth: int = 2,
        limit: int = 25,
        cursor: str | None = None,
        node_budget: int = DEPENDANTS_NODE_BUDGET,
        viewer: str | object | None = _ACTING_VIEWER,
    ) -> DependantsPage:
        """Bounded BFS over the refs that rely on ``ref_id``.

        ``depth`` (1..:data:`DEPENDANTS_MAX_DEPTH`) bounds the chain —
        1 is "uses-by-others", deeper adds transitive evidence
        dependants. ``limit`` (1..:data:`DEPENDANTS_MAX_LIMIT`) is the
        page size; ``cursor`` is a previous page's ``next_cursor``
        (``dep:<offset>``), anything else a ``BadInput``. ``node_budget``
        bounds how many nodes the walk expands — discovered nodes left
        unexpanded land in ``uninspected`` so a truncated walk is always
        stated, never read as "no dependants".
        """
        if not isinstance(depth, int) or not 1 <= depth <= DEPENDANTS_MAX_DEPTH:
            raise BadInput(
                f"depth must be 1..{DEPENDANTS_MAX_DEPTH}, got {depth!r}",
                next="call again with depth=2",
            )
        if not isinstance(limit, int) or not 1 <= limit <= DEPENDANTS_MAX_LIMIT:
            raise BadInput(
                f"limit must be 1..{DEPENDANTS_MAX_LIMIT}, got {limit!r}",
                next="call again with limit=25",
            )
        if not isinstance(node_budget, int) or node_budget < 1:
            raise BadInput(
                f"node_budget must be >= 1, got {node_budget!r}",
            )
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.startswith(_CURSOR_PREFIX):
                raise BadInput(
                    f"unknown dependants cursor {cursor!r}",
                    next="omit cursor= for the first page",
                )
            try:
                offset = int(cursor[len(_CURSOR_PREFIX) :])
            except ValueError:
                raise BadInput(
                    f"unknown dependants cursor {cursor!r}",
                    next="omit cursor= for the first page",
                ) from None
            if offset < 0:
                raise BadInput(
                    f"unknown dependants cursor {cursor!r}",
                    next="omit cursor= for the first page",
                )

        inv_map = self._inverse_map()
        if viewer is _ACTING_VIEWER:
            from precis import profiles

            viewer_login = profiles.resolve_viewer(cast("Store", self), viewer)
        else:
            viewer_login = viewer if isinstance(viewer, str) else None

        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT kind FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
            if row is None:
                raise NotFound(
                    f"ref_id={ref_id} does not exist",
                    next="search(kind=<kind>, q='...') for a live ref",
                )
            seen: dict[int, str] = {ref_id: str(row[0])}
            rows_by_id: dict[int, DependantRow] = {}
            frontier: list[int] = [ref_id]
            uninspected = 0
            budget = node_budget

            for d in range(1, depth + 1):
                if not frontier:
                    break
                if budget <= 0:
                    uninspected += len(frontier)
                    break
                expandable = frontier[:budget]
                skipped = len(frontier) - len(expandable)
                uninspected += skipped
                budget -= len(expandable)
                exp_set = set(expandable)

                edges, truncated = self._dependant_edges(conn, expandable)
                uninspected += truncated

                # dep_id -> (relation-as-seen, via_ref_id); first edge in
                # link_id order wins so the walk is deterministic.
                candidates: dict[int, tuple[str, int]] = {}
                for _lid, src, dst, rel, from_chunk in edges:
                    side = self._dependant_side(rel, from_chunk=from_chunk)
                    for anchor in (src, dst):
                        if anchor not in exp_set:
                            continue
                        if side == "both":
                            dep = dst if anchor == src else src
                            rel_seen = (
                                rel if anchor == src else (inv_map.get(rel) or rel)
                            )
                        elif side == "dst":
                            if anchor != src:
                                continue
                            dep, rel_seen = dst, rel
                        else:
                            if anchor != dst:
                                continue
                            dep, rel_seen = src, (inv_map.get(rel) or rel)
                        if dep not in candidates:
                            candidates[dep] = (rel_seen, anchor)

                new_ids = [i for i in candidates if i not in seen]
                live = self._live_dependant_kinds(
                    conn, new_ids, viewer_login=viewer_login
                )
                nxt: list[int] = []
                for dep_id in sorted(new_ids):
                    kind = live.get(dep_id)
                    if kind is None:
                        continue
                    rel_seen, via = candidates[dep_id]
                    seen[dep_id] = kind
                    rows_by_id[dep_id] = DependantRow(
                        depth=d,
                        ref_id=dep_id,
                        kind=kind,
                        relation=rel_seen,
                        via_ref_id=via,
                        via_kind=seen[via],
                    )
                    nxt.append(dep_id)
                frontier = nxt

        ordered = sorted(rows_by_id.values(), key=lambda r: (r.depth, r.ref_id))
        total_seen = len(ordered)
        if cursor is not None and offset >= total_seen:
            # A cursor only ever mints below total_seen; anything at or
            # past it is a guessed/replayed offset, and answering it with
            # an empty "complete" page would read as "no dependants".
            raise BadInput(
                f"unknown dependants cursor {cursor!r} — past the "
                f"{total_seen} seen dependant(s)",
                next="omit cursor= for the first page",
            )
        page = tuple(ordered[offset : offset + limit])
        next_cursor = (
            f"{_CURSOR_PREFIX}{offset + limit}" if offset + limit < total_seen else None
        )
        complete = uninspected == 0 and next_cursor is None
        return DependantsPage(
            rows=page,
            shown=len(page),
            total_seen=total_seen,
            uninspected=uninspected,
            next_cursor=next_cursor,
            complete=complete,
        )
