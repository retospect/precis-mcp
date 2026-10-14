"""Mesh usefulness — the standing "is the graph navigable" number.

The metric spec (``local-mesh-upkeep.md`` §"mesh-usefulness", added in
slice 0) is five read-only quantities, each a baseline a gardener pass or
an upkeep lane should move:

- **orphan rate** — per kind, share of live refs reachable by no edge
  either direction. An orphan is invisible to every walk- and
  fisheye-shaped reader; this is the relink lane's target.
- **duplicate candidates** — per kind, groups of live refs sharing a
  normalized title (casefolded, whitespace-collapsed). Deterministic,
  cheap, and the merge lane's worklist; the embedding near-neighbour
  distance variant is a heavier follow-up (``hub-duplicate-reconcile.md``
  calibrates it).
- **stale nodes** — per kind, live refs whose ``updated_at`` is older
  than the staleness window (default 90d), and of those how many are
  also orphans. Orphaned *and* untouched is the strongest "nobody reads
  this" signal the schema supports without access logs.
- **next-hop follow-through** — from the ``tool_calls`` ledger
  (verb-level only; it records kwarg *names*, never values, so a per-
  target hop is not recoverable): within one ``agentlog_id`` session,
  the share of reads (``get``/``search``/``more``) followed by another
  read (drilled further) or by a write (``put``/``edit``/``link``/
  ``tag``/``delete`` — the fetch fed a mutation). A low read→nothing rate
  is "the fisheye answered nothing the agent used"; a healthy mesh shows
  reads leading somewhere.
- **review freshness** — share of reviews whose ``content_sha`` still
  equals the target's live ``precis_target_sha`` (the 0185 registry),
  i.e. how much of the reviewed surface a later edit has made stale.

Every function takes a DB-API connection (psycopg), runs plain SELECTs,
and never writes — safe on ``agent_ro``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Verbs that mutate the graph (the "the read fed a write" side of the
#: next-hop transition matrix).
WRITE_VERBS: tuple[str, ...] = ("put", "edit", "delete", "tag", "link")
READ_VERBS: tuple[str, ...] = ("get", "search", "more")


@dataclass(frozen=True, slots=True)
class UsefulnessReport:
    """One snapshot of the mesh-usefulness metrics."""

    orphans: list[tuple[str, int, int]]  # kind, orphan_count, live_count
    duplicate_groups: list[tuple[str, int, int]]  # kind, groups, refs involved
    #: kind, stale_count, stale_and_orphaned, live_count
    stale: list[tuple[str, int, int, int]]
    transitions: list[tuple[str, str, int]]  # verb -> next verb -> count
    review_currency: list[tuple[str, int, int]]  # target_kind, stale, total
    extras: dict[str, Any] = field(default_factory=dict)


_ORPHAN_SQL = """
    SELECT r.kind,
           count(*) FILTER (WHERE NOT EXISTS (
               SELECT 1 FROM links l
               WHERE l.src_ref_id = r.ref_id OR l.dst_ref_id = r.ref_id
           )) AS orphans,
           count(*) AS live
      FROM refs r
     WHERE r.retired_at IS NULL
     GROUP BY r.kind
     ORDER BY orphans DESC, live DESC
"""

_DUP_SQL = """
    SELECT kind, count(*) AS groups, sum(n) AS refs_involved
      FROM (
        SELECT r.kind, lower(regexp_replace(btrim(r.title), '\\s+', ' ', 'g'))
               AS norm_title, count(*) AS n
          FROM refs r
         WHERE r.retired_at IS NULL
         GROUP BY r.kind, norm_title
        HAVING count(*) > 1
      ) d
     GROUP BY kind
     ORDER BY refs_involved DESC
"""

_STALE_SQL = """
    SELECT r.kind,
           count(*) FILTER (
               WHERE r.updated_at < now() - make_interval(days => %(days)s)
           ) AS stale,
           count(*) FILTER (
               WHERE r.updated_at < now() - make_interval(days => %(days)s)
                 AND NOT EXISTS (
                   SELECT 1 FROM links l
                   WHERE l.src_ref_id = r.ref_id OR l.dst_ref_id = r.ref_id)
           ) AS stale_orphans,
           count(*) AS live
      FROM refs r
     WHERE r.retired_at IS NULL
     GROUP BY r.kind
     ORDER BY stale_orphans DESC
"""

#: Verb→next-verb transitions inside one agentlog session. ``call_id``
#: order is arrival order; sessions are per-agent ticks, so "what came
#: next" is the nearest thing the ledger can say to "was the fetch used".
_NEXT_HOP_SQL = """
    WITH seq AS (
        SELECT agentlog_id, verb,
               lead(verb) OVER (PARTITION BY agentlog_id ORDER BY call_id)
                   AS next_verb
          FROM tool_calls
         WHERE ts > now() - make_interval(days => %(days)s)
           AND agentlog_id IS NOT NULL
    )
    SELECT verb, coalesce(next_verb, '(end)') AS next_verb, count(*)
      FROM seq
     WHERE verb IN ('get', 'search', 'more')
     GROUP BY 1, 2
     ORDER BY 3 DESC
"""

#: Reviews gone stale = content_sha no longer the target's live sha.
#: precis_target_sha(kind, id) is the 0185 registry; a NULL sha target
#: (deleted link) counts stale.
_REVIEW_CURRENCY_SQL = """
    SELECT r.target_kind,
           count(*) FILTER (
               WHERE r.content_sha IS DISTINCT FROM
                     precis_target_sha(r.target_kind, r.target_id)
           ) AS stale,
           count(*) AS total
      FROM reviews r
     GROUP BY r.target_kind
     ORDER BY total DESC
"""


def collect_usefulness(
    conn: Any, *, stale_days: int = 90, hop_days: int = 30
) -> UsefulnessReport:
    """Run all five metric queries over one read-only connection."""
    orphans = [
        (str(k), int(o), int(t)) for k, o, t in conn.execute(_ORPHAN_SQL).fetchall()
    ]
    dups = [(str(k), int(g), int(n)) for k, g, n in conn.execute(_DUP_SQL).fetchall()]
    stale = [
        (str(k), int(s), int(so), int(t))
        for k, s, so, t in conn.execute(_STALE_SQL, {"days": stale_days}).fetchall()
    ]
    transitions = [
        (str(v), str(nv), int(c))
        for v, nv, c in conn.execute(_NEXT_HOP_SQL, {"days": hop_days}).fetchall()
    ]
    try:
        review_currency = [
            (str(k), int(s), int(t))
            for k, s, t in conn.execute(_REVIEW_CURRENCY_SQL).fetchall()
        ]
    except Exception:
        # The 0185 sha registry may not exist on an older schema — report
        # the metric as unavailable rather than failing the whole report.
        review_currency = []
    return UsefulnessReport(
        orphans=orphans,
        duplicate_groups=dups,
        stale=stale,
        transitions=transitions,
        review_currency=review_currency,
        extras={"stale_days": stale_days, "hop_days": hop_days},
    )


def format_usefulness(rep: UsefulnessReport) -> str:
    """Render the report as a compact text block for the CLI."""

    def _pct(a: int, b: int) -> str:
        return f"{100.0 * a / b:.1f}%" if b else "-"

    lines = ["mesh usefulness:"]
    lines.append("  orphans (live refs with no edge either way):")
    for kind, o, t in rep.orphans[:15]:
        lines.append(f"    {kind:<16} {o:>7} / {t:<7} ({_pct(o, t)})")
    lines.append("  duplicate candidates (same kind + normalized title):")
    for kind, g, n in rep.duplicate_groups[:15]:
        lines.append(f"    {kind:<16} {g:>5} groups, {n:>6} refs")
    days = rep.extras.get("stale_days", 90)
    lines.append(f"  stale (untouched >{days}d; stale+orphaned / live):")
    for kind, s, so, t in rep.stale[:15]:
        lines.append(
            f"    {kind:<16} stale {s:>7} ({_pct(s, t)}), "
            f"stale+orphan {so:>7} ({_pct(so, t)})"
        )
    lines.append(
        f"  next-hop transitions after a read ({rep.extras.get('hop_days', 30)}d):"
    )
    for verb, nxt, c in rep.transitions[:20]:
        lines.append(f"    {verb:<7} -> {nxt:<10} {c:>8}")
    lines.append("  review currency (stale content_sha / total reviews):")
    for kind, s, t in rep.review_currency:
        lines.append(f"    {kind:<8} {s:>6} / {t:<6} ({_pct(s, t)} stale)")
    return "\n".join(lines)


__all__ = [
    "READ_VERBS",
    "WRITE_VERBS",
    "UsefulnessReport",
    "collect_usefulness",
    "format_usefulness",
]
