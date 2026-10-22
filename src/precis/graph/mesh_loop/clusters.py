"""Cluster selector for the kg-mesh loop — ``precis mesh loop clusters``.

SQL only, no LLM calls, no writes. The cluster definition and ranking
are the spec's (``docs/backlog/kg-mesh-loop.md`` §Design, "Cluster
selection"): a quest's hub set, a ≥8-hub / <1-stance-edge-per-hub
qualifier, ordered ``prio`` → stance density → most recent paper.

Because the ``finding serves quest`` direction fix has not landed, hub
membership reads through the three doors documented in the package
docstring: direct quest↔finding links, claim edges to serving papers,
and one hop of hub↔hub extension. On prod (2026-10-22) that recovers
all ten Pd-hydride pilot hubs fi504759–fi504782 for qu164903 — nine via
serving papers (``pa38520``/``pa4906``/``pa4725``/``pa4682`` carry
``corroborates``/``disputes`` edges to them), the tenth through
``fi504778 qualifies fi504780``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from precis.errors import BadInput

#: Stance relations — the edges the loop exists to write, and the ones
#: counted for the <1-per-hub density bar (spec §Design, verbatim).
STANCE_RELS: tuple[str, ...] = (
    "supports",
    "disputes",
    "qualifies",
    "refines",
    "corroborates",
    "explains",
)

#: Relations that mark a finding as belonging to the cluster when it
#: links to a serving paper or a member hub — the stance rels plus the
#: evidence and provenance edges that ground a claim in a paper
#: (``establishes``/``contradicts`` are the remaining taproot evidence
#: rels; ``motivated-by``/``derived-from``/``awaits-evidence``/``cites``
#: are how harvester and hand hubs attach to their source). Deliberately
#: excludes the weak ``related-to``/``see-also`` edges.
MEMBERSHIP_RELS: tuple[str, ...] = (
    *STANCE_RELS,
    "establishes",
    "contradicts",
    "motivated-by",
    "derived-from",
    "awaits-evidence",
    "cites",
)

#: Spec defaults (open question 6): ship as params with these values.
DEFAULT_MIN_HUBS = 8
DEFAULT_MAX_STANCE_PER_HUB = 1.0

#: Membership pairs ``(quest_id, hub_id)`` for every active quest, via
#: the three doors. ``aq`` is the active-quest set (live quest +
#: ``STATUS:active`` tag — the same definition ``quest.reweight`` uses);
#: ``sp`` its live serving papers.
_MEMBERSHIP_SQL = """
    WITH aq AS (
        SELECT r.ref_id
          FROM refs r
          JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE r.kind = 'quest' AND r.retired_at IS NULL
           AND t.namespace = 'STATUS' AND t.value = 'active'
    ),
    sp AS (
        SELECT l.dst_ref_id AS quest_id, l.src_ref_id AS paper_id
          FROM links l
          JOIN refs p ON p.ref_id = l.src_ref_id
         WHERE l.relation = 'serves' AND p.kind = 'paper'
           AND p.retired_at IS NULL
           AND l.dst_ref_id IN (SELECT ref_id FROM aq)
    ),
    base AS (
        -- door 1: a direct quest<->finding link, either direction, any rel
        SELECT DISTINCT q.ref_id AS quest_id, f.ref_id AS hub_id
          FROM aq q
          JOIN links l ON l.src_ref_id = q.ref_id OR l.dst_ref_id = q.ref_id
          JOIN refs f ON f.ref_id = CASE WHEN l.src_ref_id = q.ref_id
                                         THEN l.dst_ref_id
                                         ELSE l.src_ref_id END
         WHERE f.kind = 'finding' AND f.retired_at IS NULL
        UNION
        -- door 2: a claim edge to a serving paper, either direction
        SELECT DISTINCT sp.quest_id, f.ref_id
          FROM sp
          JOIN links l ON l.src_ref_id = sp.paper_id
                       OR l.dst_ref_id = sp.paper_id
          JOIN refs f ON f.ref_id = CASE WHEN l.src_ref_id = sp.paper_id
                                         THEN l.dst_ref_id
                                         ELSE l.src_ref_id END
         WHERE f.kind = 'finding' AND f.retired_at IS NULL
           AND l.relation = ANY(%(rels)s)
    )
    SELECT quest_id, hub_id FROM base
    UNION
    -- door 3: one hop of hub<->hub extension over the same claim rels
    SELECT DISTINCT b.quest_id, f.ref_id
      FROM base b
      JOIN links l ON l.src_ref_id = b.hub_id OR l.dst_ref_id = b.hub_id
      JOIN refs f ON f.ref_id = CASE WHEN l.src_ref_id = b.hub_id
                                     THEN l.dst_ref_id
                                     ELSE l.src_ref_id END
     WHERE f.kind = 'finding' AND f.retired_at IS NULL
       AND f.ref_id <> b.hub_id
       AND l.relation = ANY(%(rels)s)
"""


@dataclass(frozen=True, slots=True)
class Cluster:
    """One quest's cluster metrics, as the selector reports them."""

    quest_id: int
    title: str
    prio: int  # effective ordering prio (COALESCE(prio,5)); lower = more urgent
    hubs: int
    stance_edges: int
    stance_density: float
    newest_paper: datetime | None


def hub_membership(conn: Any) -> list[tuple[int, int]]:
    """``(quest_id, finding ref_id)`` pairs for every active quest."""
    rows = conn.execute(_MEMBERSHIP_SQL, {"rels": list(MEMBERSHIP_RELS)}).fetchall()
    return [(int(q), int(h)) for q, h in rows]


def collect_clusters(conn: Any) -> list[Cluster]:
    """Per-active-quest cluster metrics, unordered (the stats base)."""
    sql = f"""
        WITH membership AS ({_MEMBERSHIP_SQL}),
        aq AS (
            SELECT r.ref_id, r.title, coalesce(r.prio, 5) AS prio
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'quest' AND r.retired_at IS NULL
               AND t.namespace = 'STATUS' AND t.value = 'active'
        ),
        hubs AS (
            SELECT quest_id, count(*) AS n FROM membership GROUP BY quest_id
        ),
        stance AS (
            SELECT h1.quest_id, count(*) AS n
              FROM membership h1
              JOIN links l ON l.src_ref_id = h1.hub_id
              JOIN membership h2 ON h2.quest_id = h1.quest_id
                                AND h2.hub_id = l.dst_ref_id
             WHERE l.relation = ANY(%(stance)s)
               AND l.src_ref_id <> l.dst_ref_id
               AND NOT coalesce(l.meta ? 'reverted', false)
             GROUP BY h1.quest_id
        ),
        newest AS (
            SELECT l.dst_ref_id AS quest_id, max(p.created_at) AS ts
              FROM links l
              JOIN refs p ON p.ref_id = l.src_ref_id
             WHERE l.relation = 'serves' AND p.kind = 'paper'
               AND p.retired_at IS NULL
               AND l.dst_ref_id IN (SELECT ref_id FROM aq)
             GROUP BY l.dst_ref_id
        )
        SELECT q.ref_id, q.title, q.prio,
               coalesce(h.n, 0) AS hubs,
               coalesce(s.n, 0) AS stance_edges,
               n.ts AS newest_paper
          FROM aq q
          LEFT JOIN hubs h ON h.quest_id = q.ref_id
          LEFT JOIN stance s ON s.quest_id = q.ref_id
          LEFT JOIN newest n ON n.quest_id = q.ref_id
    """
    rows = conn.execute(
        sql, {"rels": list(MEMBERSHIP_RELS), "stance": list(STANCE_RELS)}
    ).fetchall()
    return [
        Cluster(
            quest_id=int(qid),
            title=str(title),
            prio=int(prio),
            hubs=int(hubs),
            stance_edges=int(edges),
            stance_density=(int(edges) / int(hubs)) if hubs else 0.0,
            newest_paper=ts,
        )
        for qid, title, prio, hubs, edges, ts in rows
    ]


def select_clusters(
    conn: Any,
    *,
    min_hubs: int = DEFAULT_MIN_HUBS,
    max_stance_per_hub: float = DEFAULT_MAX_STANCE_PER_HUB,
) -> list[Cluster]:
    """Qualifying clusters, in work order.

    Qualifier: ``hubs >= min_hubs`` and ``stance_edges / hubs <
    max_stance_per_hub``. Order: effective prio ASC (most urgent first),
    stance density ASC, most recent serving paper DESC, quest id for a
    stable tail.
    """
    qualifying = [
        c
        for c in collect_clusters(conn)
        if c.hubs >= min_hubs and c.stance_density < max_stance_per_hub
    ]
    qualifying.sort(
        key=lambda c: (
            c.prio,
            c.stance_density,
            -(c.newest_paper.timestamp() if c.newest_paper else 0.0),
            c.quest_id,
        )
    )
    return qualifying


def cluster_for_quest(
    conn: Any,
    quest_id: int,
    *,
    min_hubs: int = DEFAULT_MIN_HUBS,
    max_stance_per_hub: float = DEFAULT_MAX_STANCE_PER_HUB,
) -> Cluster:
    """The pinned cluster for one quest — the ``--quest`` door.

    Selects the quest's cluster regardless of where it ranks (Reto
    2026-10-22: rank order stands, the pilot pins qu164903), and refuses
    with a ``next=`` when the quest is gone, inactive or fails the
    qualifier.
    """
    live = conn.execute(
        "SELECT 1 FROM refs WHERE ref_id = %s AND kind = 'quest' "
        "AND retired_at IS NULL",
        (quest_id,),
    ).fetchone()
    if live is None:
        raise BadInput(
            f"no live quest qu{quest_id}",
            next="pin a live quest id — `precis mesh loop stats` lists the active set",
        )
    mine = next((c for c in collect_clusters(conn) if c.quest_id == quest_id), None)
    if mine is None:
        raise BadInput(
            f"qu{quest_id} is live but carries no STATUS:active tag",
            next="tag it STATUS:active, or pin a quest in the active set "
            "(`precis mesh loop stats`)",
        )
    failed = []
    if mine.hubs < min_hubs:
        failed.append(f"{mine.hubs} hubs < {min_hubs}")
    if mine.stance_density >= max_stance_per_hub:
        failed.append(f"stance/hub {mine.stance_density:.3f} >= {max_stance_per_hub:g}")
    if failed:
        raise BadInput(
            f"qu{quest_id} does not qualify as a cluster: " + ", ".join(failed),
            next="relax the bars with --min-hubs/--max-stance-per-hub, or "
            "pick a qualifying cluster from `precis mesh loop clusters`",
        )
    return mine


def _short_title(title: str, width: int = 56) -> str:
    t = " ".join(title.split())
    return t if len(t) <= width else t[: width - 1] + "…"


def format_clusters(
    rows: list[Cluster],
    *,
    min_hubs: int = DEFAULT_MIN_HUBS,
    max_stance_per_hub: float = DEFAULT_MAX_STANCE_PER_HUB,
) -> str:
    """Plain-text table for ``precis mesh loop clusters``."""
    lines = [
        f"kg-mesh clusters (qualifying: hubs>={min_hubs}, "
        f"stance/hub<{max_stance_per_hub:g}):"
    ]
    if not rows:
        lines.append("  (none)")
        return "\n".join(lines)
    lines.append(
        f"  {'#':>2}  {'quest':<10} {'prio':>4} {'hubs':>5} "
        f"{'stance/hub':>10}  {'newest paper':<10}  title"
    )
    for i, c in enumerate(rows, 1):
        ts = c.newest_paper.strftime("%Y-%m-%d") if c.newest_paper is not None else "-"
        lines.append(
            f"  {i:>2}  qu{c.quest_id:<8} {c.prio:>4} {c.hubs:>5} "
            f"{c.stance_density:>10.3f}  {ts:<10}  {_short_title(c.title)}"
        )
    return "\n".join(lines)


def clusters_json(rows: list[Cluster]) -> list[dict[str, Any]]:
    """The ``--json`` shape: one object per qualifying cluster."""
    return [
        {
            "quest_id": c.quest_id,
            "quest": f"qu{c.quest_id}",
            "title": c.title,
            "prio": c.prio,
            "hubs": c.hubs,
            "stance_edges": c.stance_edges,
            "stance_density": c.stance_density,
            "newest_paper": (
                c.newest_paper.isoformat() if c.newest_paper is not None else None
            ),
        }
        for c in rows
    ]


__all__ = [
    "DEFAULT_MAX_STANCE_PER_HUB",
    "DEFAULT_MIN_HUBS",
    "MEMBERSHIP_RELS",
    "STANCE_RELS",
    "Cluster",
    "cluster_for_quest",
    "clusters_json",
    "collect_clusters",
    "format_clusters",
    "hub_membership",
    "select_clusters",
]
