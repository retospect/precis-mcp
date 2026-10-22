"""Coverage metrics for the kg-mesh loop — ``precis mesh loop stats``.

Per active quest (spec §Design, "Stamp + coverage"): hubs, stamped hubs,
stance edges per hub, writes proposed/accepted/reverted, strong-tier
tokens per accepted write, proposer–writer agreement — plus the global
"clusters left" count the selector would admit now.

Ledger conventions (the shape slices 2–4 write; see the package
docstring):

- a proposal is a ``reviews`` row ``actor='kg-mesh-propose'``,
  ``verdict='proposed'``, ``note`` JSON carrying ``{"kg_mesh":
  {"v": 1, "quest": <quest ref_id>, "key": <dedup key>, "run_id":
  <tick id>, ...}}``;
- a resolution is a later row echoing the same ``key`` (any actor — the
  strong tier or a human), folded newest-first like the garden's
  :func:`~precis.graph.garden.proposals.pending_proposals`: newest row
  ``approved`` counts *accepted*, ``rejected`` counts *reverted* (the
  spec's failed-write outcome — the edge is retired, never deleted);
- a hub is **stamped** when a terminal kg-mesh row (``actor LIKE
  'kg-mesh%%'``, ``verdict IN ('approved','rejected')``) sits on
  ``('ref', hub_id)`` at its current ``content_sha`` — the stamp says
  "the strong tier considered this hub at this state". This is
  kg-mesh's own coverage axis, distinct from the trust-coverage view
  the review-stamps work builds;
- strong-tier tokens read ``llm_call_log`` rows ``source =
  'kg_mesh:write'`` attributed to a quest through ``features->>'run_id'``
  recorded on that quest's kg-mesh ledger rows. ``tokens_per_accepted``
  is spend over accepted writes — the pilot's ~6k tokens-per-useful-write
  comparator.

Read-only throughout — plain SELECTs, safe on ``agent_ro``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from precis.graph.mesh_loop.clusters import (
    DEFAULT_MAX_STANCE_PER_HUB,
    DEFAULT_MIN_HUBS,
    Cluster,
    collect_clusters,
    hub_membership,
)

#: reviews.actor for cheap-tier proposal rows (spec §Design).
PROPOSE_ACTOR = "kg-mesh-propose"
#: llm_call_log.source for strong-tier writes (spec §Design names the op
#: ``llm.op.kg_mesh:write``; the route-log source drops the ``llm.op.``
#: prefix, matching ``graph_garden:propose``).
WRITE_SOURCE = "kg_mesh:write"
#: The note-payload namespace every kg-mesh ledger row carries.
NOTE_KEY = "kg_mesh"


@dataclass(frozen=True, slots=True)
class QuestStats:
    """One active quest's coverage row."""

    cluster: Cluster
    stamped_hubs: int
    proposed: int
    accepted: int
    reverted: int
    writer_tokens: int
    tokens_per_accepted: float | None
    agreement: float | None  # accepted / (accepted + reverted)


@dataclass(frozen=True, slots=True)
class LoopStats:
    """The full report: one row per active quest plus the global count."""

    rows: list[QuestStats]
    clusters_left: int
    active_quests: int


def _kg_payload(note: str | None) -> dict[str, Any] | None:
    """The ``note.kg_mesh`` payload of a review row, or None."""
    if not note:
        return None
    try:
        body = json.loads(note)
    except (TypeError, ValueError):
        return None
    g = body.get(NOTE_KEY)
    return g if isinstance(g, dict) else None


def _quest_of(g: dict[str, Any]) -> int | None:
    q = g.get("quest")
    return int(q) if isinstance(q, int) else None


_STAMPED_SQL = """
    SELECT target_id
      FROM reviews
     WHERE target_kind = 'ref'
       AND actor LIKE 'kg-mesh%%'
       AND verdict IN ('approved', 'rejected')
       AND content_sha = precis_target_sha('ref', target_id)
"""

_LEDGER_SQL = """
    SELECT actor, verdict, note
      FROM reviews
     WHERE note LIKE '%%"kg_mesh"%%'
     ORDER BY review_id DESC
"""


def _tokens_sql(run_ids: list[str]) -> tuple[str, dict[str, Any]]:
    return (
        "SELECT features->>'run_id' AS run_id, "
        "       sum(coalesce(input_tokens,0) + coalesce(output_tokens,0)) "
        "  FROM llm_call_log "
        " WHERE source = %(src)s AND features->>'run_id' = ANY(%(ids)s) "
        " GROUP BY 1",
        {"src": WRITE_SOURCE, "ids": run_ids},
    )


def collect_stats(
    conn: Any,
    *,
    min_hubs: int = DEFAULT_MIN_HUBS,
    max_stance_per_hub: float = DEFAULT_MAX_STANCE_PER_HUB,
) -> LoopStats:
    """Per-quest coverage metrics over one read-only connection."""
    clusters = collect_clusters(conn)
    hubs_by_quest: dict[int, set[int]] = {}
    for q, h in hub_membership(conn):
        hubs_by_quest.setdefault(q, set()).add(h)

    stamped_ids = {int(r[0]) for r in conn.execute(_STAMPED_SQL).fetchall()}

    # Ledger fold, newest row per (quest, key) — garden's two-rows rule.
    proposed: dict[int, int] = {}
    newest_by_key: dict[tuple[int, str], str] = {}
    run_quest: dict[str, int] = {}
    for actor, verdict, note in conn.execute(_LEDGER_SQL).fetchall():
        g = _kg_payload(note)
        if g is None:
            continue
        qid = _quest_of(g)
        if qid is None:
            continue
        run_id = g.get("run_id")
        if isinstance(run_id, str) and run_id:
            run_quest.setdefault(run_id, qid)
        if str(actor) == PROPOSE_ACTOR and str(verdict) == "proposed":
            proposed[qid] = proposed.get(qid, 0) + 1
        key = g.get("key")
        if isinstance(key, str) and key and (qid, key) not in newest_by_key:
            newest_by_key[(qid, key)] = str(verdict)

    tokens: dict[int, int] = {}
    if run_quest:
        sql, params = _tokens_sql(list(run_quest))
        for run_id, total in conn.execute(sql, params).fetchall():
            qid = run_quest.get(str(run_id))
            if qid is not None:
                tokens[qid] = tokens.get(qid, 0) + int(total)

    rows: list[QuestStats] = []
    for c in clusters:
        hubset = hubs_by_quest.get(c.quest_id, set())
        resolved = [v for (qid, _k), v in newest_by_key.items() if qid == c.quest_id]
        accepted = sum(1 for v in resolved if v == "approved")
        reverted = sum(1 for v in resolved if v == "rejected")
        tok = tokens.get(c.quest_id, 0)
        rows.append(
            QuestStats(
                cluster=c,
                stamped_hubs=len(hubset & stamped_ids),
                proposed=proposed.get(c.quest_id, 0),
                accepted=accepted,
                reverted=reverted,
                writer_tokens=tok,
                tokens_per_accepted=(tok / accepted) if accepted else None,
                agreement=(
                    accepted / (accepted + reverted) if (accepted + reverted) else None
                ),
            )
        )
    qualifying = sum(
        1
        for c in clusters
        if c.hubs >= min_hubs and c.stance_density < max_stance_per_hub
    )
    return LoopStats(
        rows=rows,
        clusters_left=qualifying,
        active_quests=len(clusters),
    )


def _fmt_rate(x: float | None) -> str:
    return f"{x:.2f}" if x is not None else "-"


def format_stats(rep: LoopStats) -> str:
    """Plain-text table for ``precis mesh loop stats``."""
    lines = [
        "kg-mesh coverage (per active quest):",
        f"  {'quest':<10} {'hubs':>5} {'stamped':>7} {'st/hub':>7} "
        f"{'prop':>5} {'acc':>4} {'rev':>4} {'tok/acc':>8} {'agree':>6}  title",
    ]
    for r in rep.rows:
        c = r.cluster
        tpa = f"{r.tokens_per_accepted:.0f}" if r.tokens_per_accepted else "-"
        lines.append(
            f"  qu{c.quest_id:<8} {c.hubs:>5} {r.stamped_hubs:>7} "
            f"{c.stance_density:>7.3f} {r.proposed:>5} {r.accepted:>4} "
            f"{r.reverted:>4} {tpa:>8} {_fmt_rate(r.agreement):>6}  "
            f"{' '.join(c.title.split())[:48]}"
        )
    lines.append(
        f"clusters left: {rep.clusters_left} qualifying "
        f"of {rep.active_quests} active quests"
    )
    return "\n".join(lines)


def stats_json(rep: LoopStats) -> dict[str, Any]:
    """The ``--json`` shape."""
    return {
        "active_quests": rep.active_quests,
        "clusters_left": rep.clusters_left,
        "quests": [
            {
                "quest_id": r.cluster.quest_id,
                "quest": f"qu{r.cluster.quest_id}",
                "title": r.cluster.title,
                "prio": r.cluster.prio,
                "hubs": r.cluster.hubs,
                "stamped_hubs": r.stamped_hubs,
                "stance_edges": r.cluster.stance_edges,
                "stance_per_hub": r.cluster.stance_density,
                "newest_paper": (
                    r.cluster.newest_paper.isoformat()
                    if r.cluster.newest_paper is not None
                    else None
                ),
                "proposed": r.proposed,
                "accepted": r.accepted,
                "reverted": r.reverted,
                "writer_tokens": r.writer_tokens,
                "tokens_per_accepted": r.tokens_per_accepted,
                "agreement": r.agreement,
            }
            for r in rep.rows
        ],
    }


__all__ = [
    "NOTE_KEY",
    "PROPOSE_ACTOR",
    "WRITE_SOURCE",
    "LoopStats",
    "QuestStats",
    "collect_stats",
    "format_stats",
    "stats_json",
]
