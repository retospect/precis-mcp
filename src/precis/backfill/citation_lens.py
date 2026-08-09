"""Citation-graph recall lens (source-backfill slice 3) — the provable-omission
lens.

A paper the section **already cites** points, through the Semantic Scholar
citation graph, at other papers: its *references* (what it cites) and its
*cited_by* (what cites it). Any such neighbour that is **already held in our
corpus** but **not yet cited** is the strongest kind of gap — you are a single
citation hop from it and left it out. Unlike the text lens (semantic proximity),
this is a *structural* argument for relevance the model can defend.

**Materialisation, corpus-internal + lazy.** S2 citation edges live nowhere in
the DB (chase fetched them transiently and discarded them). This lens fills them
on demand: when it runs on a cited paper it fetches that paper's
references/cited_by **once** (TTL-gated by a ``citation_edges`` ref_event),
resolves each neighbour against ``ref_identifiers`` (DOI / S2 id), and — only
when the neighbour resolves to a *held* ref — writes a ``cites`` edge into
``links`` (idempotent; one direction, since ``cited-by`` is the read-time
rewrite). A neighbour **not** in the corpus gets no ``links`` edge —
acquiring it is chase/watch_poll's job, not backfill's — but since the
Sources/Cited tabs landed, the *full* neighbour list (held or not) is also
persisted into ``s2_neighbors`` (migration 0106) on the same fetch, so the web
reader can render a complete Sources/Cited tab, including fetchable stubs for
the non-held majority. Once materialised the ``links``/lens-query side is pure
SQL and the fetch is skipped until the TTL lapses — see
:func:`ensure_s2_neighbors` for the single-ref on-demand entry point the web
layer calls.

**Degrades to nothing.** No ``[paper]`` extra, no network, or an S2 hiccup → the
materialise step is a caught no-op (freshness is *not* stamped, so it retries),
and the text lens still carries the whole workspace. The real S2 call is behind
:data:`fetch_citations`, a module-level seam tests monkeypatch so they never need
the extra.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.backfill.candidates import LENS_CITATION, Candidate
from precis.utils import handle_registry

log = logging.getLogger(__name__)

#: ref_events source/event stamping "citation edges materialised for this paper"
#: — the TTL gate that stops us re-hitting S2 every backfill run.
_EDGE_SOURCE = "citation_edges"
_EDGE_EVENT = "materialized"

#: Re-fetch a paper's S2 citation edges at most this often. Citations grow
#: slowly; a month-stale graph is fine for recall (env-overridable).
_DEFAULT_TTL_DAYS = 30

#: Provenance stamped on every edge this lens writes (``set_by`` is a closed
#: ``agent/user/system`` enum, so the real provenance rides in ``meta``).
_EDGE_META = {"via": "backfill:citation-graph"}


def _default_fetch(paper_id: str) -> dict[str, list[dict[str, Any]]]:
    """The real S2 fetch — imported lazily so a host without the ``[paper]``
    extra (no ``semanticscholar``) doesn't fail at import; the lens just
    no-ops."""
    from precis.ingest.citations import citations

    return citations(paper_id)


#: Module-level seam for the S2 fetch. Prod uses :func:`_default_fetch`; tests
#: monkeypatch this to a fake so they exercise the graph without the extra.
fetch_citations: Callable[[str], dict[str, list[dict[str, Any]]]] = _default_fetch


def _citation_lens_enabled() -> bool:
    """The lens is on by default; ``PRECIS_BACKFILL_CITATION_LENS=0`` disables
    it (e.g. to keep an offline/CI run purely local)."""
    return bool(int(os.environ.get("PRECIS_BACKFILL_CITATION_LENS", "1") or "0"))


def _ttl_days() -> int:
    try:
        return int(os.environ.get("PRECIS_BACKFILL_CITATION_TTL_DAYS", "") or "")
    except ValueError:
        return _DEFAULT_TTL_DAYS


# ── neighbour resolution (held-corpus only) ──────────────────────────


def _fetch_identifiers(conn: Any, ref_id: int) -> dict[str, str]:
    """The ref's external ids as ``{id_kind: id_value}`` (doi / s2 / arxiv / …).
    ``min``-free agg is fine here — one row per kind for the kinds we probe."""
    row = conn.execute(
        "SELECT COALESCE("
        "  (SELECT jsonb_object_agg(id_kind, id_value) FROM ref_identifiers "
        "     WHERE ref_id = %s), '{}'::jsonb)",
        (ref_id,),
    ).fetchone()
    return dict(row[0] or {}) if row else {}


def _s2_query_id(identifiers: dict[str, str]) -> str | None:
    """The best S2-addressable id for a held paper (mirrors ``chase``): a DOI or
    arXiv id S2 accepts prefixed, else a raw S2 paper id."""
    if identifiers.get("doi"):
        return f"doi:{identifiers['doi']}"
    if identifiers.get("arxiv"):
        return f"arxiv:{identifiers['arxiv']}"
    if identifiers.get("s2"):
        return str(identifiers["s2"])
    return None


def _held_ref_for_neighbor(conn: Any, neighbor: dict[str, Any]) -> int | None:
    """Resolve an S2 neighbour (``{doi, s2_id, …}``) to a **held** ref_id via
    ``ref_identifiers``, or ``None`` when we don't hold it. DOI is canonicalised
    to the trigger-lowercased storage form before probing (as ``chase`` does)."""
    from precis.identity import normalize_doi

    probes: list[tuple[str, str]] = []
    doi = neighbor.get("doi")
    if doi:
        nd = normalize_doi(str(doi))
        if nd:
            probes.append(("doi", nd))
    s2 = neighbor.get("s2_id")
    if s2:
        probes.append(("s2", str(s2)))
    for id_kind, id_value in probes:
        row = conn.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind = %s AND id_value = %s",
            (id_kind, id_value),
        ).fetchone()
        if row is not None:
            return int(row[0])
    return None


def _s2_neighbor_row(
    neighbor: dict[str, Any], held_ref_id: int | None
) -> dict[str, Any]:
    """Shape one S2 neighbour dict (``{title, doi, year, s2_id}`` per
    ``ingest/citations.py::citations()``) for
    :meth:`Store.replace_s2_neighbors` — carries the resolved
    ``held_ref_id`` alongside the raw S2 fields."""
    return {
        "s2_id": neighbor.get("s2_id"),
        "doi": neighbor.get("doi"),
        "title": neighbor.get("title"),
        "year": neighbor.get("year"),
        "held_ref_id": held_ref_id,
    }


# ── materialisation ──────────────────────────────────────────────────


def _is_fresh(store: Any, ref_id: int, ttl_days: int) -> bool:
    """True if this paper's citation edges were materialised within the TTL."""
    evs = store.events_for(ref_id, source=_EDGE_SOURCE, event=_EDGE_EVENT, limit=1)
    if not evs:
        return False
    ts = getattr(evs[0], "ts", None)
    if ts is None:
        return False
    return (datetime.now(UTC) - ts) < timedelta(days=ttl_days)


def materialize_citation_edges(
    store: Any, cited_ref_ids: set[int], *, ttl_days: int | None = None
) -> int:
    """Ensure the corpus-internal ``cites`` edges for every cited paper are
    materialised, fetching from S2 only for papers whose edges are missing/stale.
    Writes an edge **only** when a neighbour resolves to a held ref. Returns the
    number of edges written this call. Never raises — an S2/extra failure on one
    paper is logged and skipped (freshness un-stamped so it retries)."""
    ttl = ttl_days if ttl_days is not None else _ttl_days()
    written = 0
    for rid in sorted(cited_ref_ids):
        # Fresh stamp alone isn't enough: a pre-0106 fetch stamped the ref
        # while persisting no s2_neighbors rows — re-fetch until rows exist.
        if _is_fresh(store, rid, ttl) and store.s2_neighbors_fresh(rid):
            continue
        try:
            with store.pool.connection() as conn:
                qid = _s2_query_id(_fetch_identifiers(conn, rid))
                if qid is None:
                    continue  # no S2-addressable id → can't query; don't stamp
                result = fetch_citations(qid)
                refs = result.get("references") or []
                cby = result.get("cited_by") or []
                edges = 0

                # Resolve each neighbour's held_ref_id once and reuse it for
                # both the held↔held `cites` edge (unchanged behaviour) and
                # the full s2_neighbors write-through (source-backfill
                # slice 3 — every neighbour persisted, not just held ones).
                refs_held = [(nb, _held_ref_for_neighbor(conn, nb)) for nb in refs]
                cby_held = [(nb, _held_ref_for_neighbor(conn, nb)) for nb in cby]

                # references: this paper cites the neighbour → cites(rid → held)
                for nb, held in refs_held:
                    if held is not None and held != rid:
                        store.add_link(
                            src_ref_id=rid,
                            dst_ref_id=held,
                            relation="cites",
                            meta=_EDGE_META,
                            conn=conn,
                        )
                        edges += 1
                # cited_by: the neighbour cites this paper → cites(held → rid)
                for nb, held in cby_held:
                    if held is not None and held != rid:
                        store.add_link(
                            src_ref_id=held,
                            dst_ref_id=rid,
                            relation="cites",
                            meta=_EDGE_META,
                            conn=conn,
                        )
                        edges += 1

                # Full neighbour lists (the Sources/Cited tabs' feed): persist
                # every S2 neighbour, held or not, so the web reader can
                # render a complete Sources/Cited tab. Same delete+insert
                # refresh both directions ride — never a partial write, so
                # a paper with an empty bibliography correctly clears any
                # stale rows from a prior fetch.
                store.replace_s2_neighbors(
                    rid,
                    "cites",
                    [_s2_neighbor_row(nb, held) for nb, held in refs_held],
                    conn=conn,
                )
                store.replace_s2_neighbors(
                    rid,
                    "cited_by",
                    [_s2_neighbor_row(nb, held) for nb, held in cby_held],
                    conn=conn,
                )

                store.append_event(
                    rid,
                    source=_EDGE_SOURCE,
                    event=_EDGE_EVENT,
                    payload={
                        "refs": len(refs),
                        "cited_by": len(cby),
                        "edges_held": edges,
                    },
                    conn=conn,
                )
                conn.commit()
                written += edges
        except Exception as exc:  # pragma: no cover — defensive; lens must not
            # break the workspace. Freshness is not stamped → retried next run.
            log.debug("citation_lens: materialise failed for ref %s: %s", rid, exc)
    return written


def ensure_s2_neighbors(
    store: Any, ref_id: int, *, ttl_days: int | None = None
) -> bool:
    """The single-ref on-demand entry point (the tabs' first-view backfill):
    fetch-and-persist ``s2_neighbors`` (+ held↔held ``cites`` edges) for
    **one** ref, honouring the same ``citation_edges`` 30-day TTL
    :func:`materialize_citation_edges` gates on — a thin single-ref
    convenience wrapper, not a second fetch path. The web reader calls this
    when opening the Sources/Cited tabs so an old paper's bibliography
    backfills inline on first view, then is a no-op on every later view
    until the TTL lapses.

    Returns ``True`` if a fetch was attempted this call (fresh or not is
    then whatever :func:`materialize_citation_edges` achieved — it never
    raises, and leaves freshness un-stamped on failure so the next call
    retries), ``False`` if skipped because the edges are already fresh.
    Never raises.
    """
    ttl = ttl_days if ttl_days is not None else _ttl_days()
    # A pre-0106 ``citation_edges`` stamp can be fresh while the table has
    # no rows (the fetch predates persistence) — presence must also count,
    # else old papers show empty tabs until the TTL lapses.
    if _is_fresh(store, ref_id, ttl) and store.s2_neighbors_fresh(ref_id):
        return False
    materialize_citation_edges(store, {ref_id}, ttl_days=ttl)
    return True


# ── the lens query ───────────────────────────────────────────────────


def citation_neighbor_degrees(
    store: Any, cited_ref_ids: set[int], *, exclude: set[int]
) -> list[tuple[int, int]]:
    """Held **paper** refs one ``cites`` hop (either direction) from a cited
    paper — the citation-graph gaps. Returns ``(ref_id, degree)`` sorted by
    degree (co-citation strength) desc, excluding ``exclude`` (cited ∪ dismissed)
    and any ref with no body chunks (a bare stub is not a citable source yet)."""
    if not cited_ref_ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT e.other, count(DISTINCT e.c)::int AS degree
              FROM (
                    SELECT l.dst_ref_id AS other, l.src_ref_id AS c
                      FROM links l
                     WHERE l.relation = 'cites'
                       AND l.src_ref_id = ANY(%(cited)s)
                    UNION ALL
                    SELECT l.src_ref_id AS other, l.dst_ref_id AS c
                      FROM links l
                     WHERE l.relation = 'cites'
                       AND l.dst_ref_id = ANY(%(cited)s)
                   ) e
              JOIN refs r ON r.ref_id = e.other
             WHERE r.kind = 'paper'
               AND r.deleted_at IS NULL
               AND e.other <> ALL(%(exclude)s)
               AND EXISTS (
                     SELECT 1 FROM chunks ch
                      WHERE ch.ref_id = e.other AND ch.ord >= 0
                   )
             GROUP BY e.other
             ORDER BY degree DESC, e.other
            """,
            {"cited": list(cited_ref_ids), "exclude": list(exclude)},
        ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def _lead_chunk(conn: Any, ref_id: int) -> int | None:
    """The paper's first body chunk id (the representative to open for a
    citation-only candidate that no text query pointed at a chunk within)."""
    row = conn.execute(
        "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord >= 0 "
        "ORDER BY ord LIMIT 1",
        (ref_id,),
    ).fetchone()
    return int(row[0]) if row is not None else None


def find_citation_candidates(
    store: Any,
    cited_ref_ids: set[int],
    *,
    exclude: set[int],
    limit: int = 8,
) -> list[Candidate]:
    """Materialise the citation edges for the cited papers, then surface the
    held-but-uncited citation neighbours as :class:`Candidate` rows (lens
    ``citation``, opened at the paper's lead chunk, scored by co-citation
    degree). Best first, capped at ``limit``."""
    if not _citation_lens_enabled() or not cited_ref_ids:
        return []
    materialize_citation_edges(store, cited_ref_ids)
    degrees = citation_neighbor_degrees(store, cited_ref_ids, exclude=exclude)
    if not degrees:
        return []
    ref_ids = [rid for rid, _ in degrees[:limit]]
    refs = store.fetch_refs_by_ids(ref_ids)
    out: list[Candidate] = []
    with store.pool.connection() as conn:
        for rid, degree in degrees[:limit]:
            ref = refs.get(rid)
            if ref is None or getattr(ref, "deleted_at", None) is not None:
                continue
            lead = _lead_chunk(conn, rid)
            if lead is None:
                continue
            handle = handle_registry.format_handle("paper", lead, chunk=True)
            out.append(
                Candidate(
                    ref_id=rid,
                    ref=ref,
                    chunk_id=lead,
                    chunk_handle=handle,
                    score=float(degree),
                    lenses=(LENS_CITATION,),
                )
            )
    return out
