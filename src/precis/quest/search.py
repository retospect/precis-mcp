"""Quest lit-search — a tick can go *ground* itself in the literature.

The missing half of the research loop. A reasoning-only quest with no paper
servers (the ``no-literature`` / ``thin-support`` gaps) had only one lever: mint
another hypothesis — so it recycled the same open question forever (the spin the
allocator then rewarded as "activity"). This gives a tick a real grounding
action: it emits ``searches`` (queries), and each becomes a corpus lookup whose
top hits are linked ``serves``→quest. The next tick sees those papers as
servers + context, and — crucially — acquiring a paper is *external progress*
(cascade resets the stall clock), so grounding earns compute where re-reasoning
does not.

**Local first.** :func:`make_acquiring_search`'s local leg is the hybrid
cross-kind search over papers, findings, drafts, concepts and memories
(:func:`_local_graph_search`); the external S2 + acquire leg runs only when
that returns fewer than :data:`LOCAL_ENOUGH` hits above the floor, and an S2
failure keeps the local hits. Only :data:`LINKABLE_KINDS` are linked ``serves``.
Each query's :class:`QueryReport` feeds the logbook's local-vs-acquired line.

The search is an **injectable seam** (``search_fn``) exactly like
``dispatch_relax`` in :mod:`precis.quest.compute`: the default
(``_default_paper_search``) is a safe, embedder-free lexical lookup over held
papers (no network, no acquisition). Acquisition is now **built**:
:func:`make_acquiring_search` layers a Semantic Scholar free-text search on top
— any hit with a DOI is queued via ``PaperHandler.acquire`` (idempotent stub
mint + ``fetch_oa`` pickup), so a query that misses the held corpus doesn't just
log an "acquisition needed" observation, it actually requests the paper. Tests
and other callers may still pass a narrower search (e.g. lexical-only, or a
semantic reranker) through the same ``search_fn`` seam.

**HyDE for the corpus leg (dossier-hygiene design).** A tick's ``searches``
entry may carry an optional ``hypothetical`` alongside its keyword ``query``
(:class:`SearchQuery`, :func:`_parse_search_entry`) — a one-two sentence
passage phrased the way it might appear verbatim in the abstract of the paper
the model wishes existed. The model's question-phrased ``query`` alone kept
missing the held corpus (7 misses across 3 prod ticks: retrieval matches
DOCUMENTS, not questions). When present, :func:`_hyde_corpus_hits` routes the
corpus leg through :mod:`precis.handlers._paper_search`'s broad-retrieval
fusion (the same ``queries=``/``answers=`` facility ``search(kind='paper',
…)`` exposes) instead of :func:`_default_paper_search`'s plain lexical
lookup — run by :func:`run_search_step` itself, independent of ``search_fn``,
so the ``search_fn`` seam (the Semantic Scholar + acquire leg, still driven by
the plain keyword ``query``) keeps its exact 3-argument shape unchanged.
Degrades to a fused-LEXICAL-only result when no embedder is wired (never
raises) — same contract as the broad ``search()`` verb with no embedder
configured.

**Relevance floor (quest-tick-incident-fix.md).** Every ``search_fn`` now
returns ``(ref_id, score)`` pairs instead of bare ids — the score each leg
already computed and used to discard (``ts_rank_cd``, the fused block
score) is now what :func:`run_search_step` floors on before linking (see
:func:`relevance_floor`; default is log-only, ``0.0``). The one leg with no
native score — :func:`make_acquiring_search`'s Semantic Scholar candidates
— carries ``score=None`` and is bounded the other way: it no longer links
``related-to``→quest itself (that used to happen unconditionally, inside
``PaperHandler.acquire``, *before* this step's own slice/floor ever ran —
the unbounded path qu401863's stray ``related-to`` papers came from); the
only link either leg can produce is this step's ``serves``, gated by the
same floor+:data:`MAX_LINK_PER_QUERY` cut as everything else.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.quest.gaps import _handle, _live_servers
from precis.quest.logbook import append_entry
from precis.quest.tagging import quest_tag_value
from precis.store.types import Tag
from precis.utils.env import env_float, env_int

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


#: Cap the queries honoured per tick (a weak proposer can't flood acquisition).
#: A day at the library beats weeks in the lab — lean hard into lit-search.
MAX_QUERIES = env_int("PRECIS_QUEST_MAX_QUERIES", 10, lo=1, hi=100)
#: How many top hits to link per query.
MAX_LINK_PER_QUERY = 3
#: Local-first: the external (Semantic Scholar + acquire) leg runs only when
#: the graph leg returns fewer than this many hits above the relevance floor.
LOCAL_ENOUGH = 3
#: Kinds the local leg searches — the same hybrid cross-kind primitive
#: (``Store.search_chunks_across_kinds``) the ``search()`` verb's cross-kind
#: fan-out runs on.
LOCAL_KINDS = ("paper", "finding", "draft", "concept", "memory")
#: Kinds a local hit may ``serves``-link to the quest. ``draft`` is excluded
#: on purpose: a draft that ``serves`` a quest is that quest's OWNED draft
#: (``draft_refresh._owning_quest_id``), so linking an arbitrary held draft
#: would hijack ownership. ``memory`` is a private note, not a source. Both
#: still count toward :data:`LOCAL_ENOUGH` (they answer the query) — they just
#: are not linked.
LINKABLE_KINDS = frozenset({"paper", "finding", "concept"})


def _acquire_per_query() -> int:
    """How many S2 results per query the acquiring search will try to acquire
    (default 4, clamped 1..10) — a knob on acquisition volume without a
    redeploy."""
    return env_int("PRECIS_QUEST_ACQUIRE_PER_QUERY", 4, lo=1, hi=10)


def relevance_floor() -> float:
    """Minimum score a hit must clear to be linked in :func:`run_search_step`
    (quest-tick-incident-fix.md item 4 — the "score computed and discarded"
    fix; default **0.0**, i.e. log every score but filter nothing).

    ``0.0`` is a deliberate no-op default, not a placeholder: every scored
    leg here (``ts_rank_cd`` off :func:`_default_paper_search`, the fused
    block score off :func:`_hyde_corpus_hits`) is non-negative by
    construction, so a hit is never dropped until an operator raises this.
    The backlog's own decision log is explicit that the floor "wants a
    number from real score distributions, not a guess" — ship
    log-and-don't-filter for a while, then set it from what got logged.

    Scores are **not comparable across legs** — lexical ``ts_rank_cd`` and
    the fused semantic+lexical block score live on different scales, and
    the S2 acquire leg (:func:`make_acquiring_search`) has no score at all
    (its candidates carry ``score=None`` and always pass the floor — see
    that function). A single global floor is a known simplification, fine
    at the ``0.0`` default; raising it applies unevenly per leg until each
    leg's distribution is characterised separately.
    """
    return env_float("PRECIS_QUEST_SEARCH_FLOOR", 0.0, lo=0.0)


#: (store, query, exclude_ref_ids) -> ranked ``(ref_id, score)`` pairs (best
#: first). ``score`` is ``None`` when the leg has no relevance signal to
#: offer (e.g. the S2 acquire leg's un-ranked candidates) — :func:`
#: run_search_step`'s floor never drops a ``None``-scored hit, only logs it.
SearchFn = Callable[["Store", str, list[int]], list[tuple[int, float | None]]]

#: Parses ``id=N`` out of the ``PaperHandler.acquire`` ack (mirrors
#: ``_good_search._ID_IN_ACK``).
_ID_IN_ACK = re.compile(r"\bid=(\d+)\b")


@dataclass
class QueryReport:
    """What one query's search did — the logbook's local-vs-acquired line.

    ``kinds`` maps each returned ref id to its kind so
    :func:`run_search_step` can apply :data:`LINKABLE_KINDS` without a second
    store round-trip."""

    local: int = 0
    acquired: int = 0
    external_ran: bool = False
    #: The external leg ran because the search was escalated
    #: (``AcquiringSearch.force_external``), not because the graph was thin.
    forced: bool = False
    external_error: str | None = None
    lexical_only: bool = False
    kinds: dict[int, str] = field(default_factory=dict)


class AcquiringSearch:
    """The ``search_fn`` :func:`make_acquiring_search` builds: callable with the
    plain 3-argument :data:`SearchFn` shape, plus a per-query
    :class:`QueryReport` side channel (``report_for``) that
    :func:`run_search_step` reads for its logbook line.

    ``force_external`` (default False) makes the external leg run whatever the
    local count — the roadmap supply role's escalation after a dry tick (see
    ``roadmap_tick._run_supply``). Local hits are still returned first, same
    ordering and dedup; only the "graph answered, skip outside" shortcut is
    off. It is a plain attribute so a caller can flip it for one step."""

    def __init__(
        self,
        quest_id: int,
        hub: Any,
        embedder: Any | None = None,
        *,
        force_external: bool = False,
    ) -> None:
        self.quest_id = quest_id
        self.hub = hub
        self.force_external = force_external
        self.embedder = (
            embedder if embedder is not None else getattr(hub, "embedder", None)
        )
        self._reports: dict[str, QueryReport] = {}
        self._warned = False

    def report_for(self, query: str) -> QueryReport | None:
        return self._reports.get(query)

    def __call__(
        self, store: Store, query: str, exclude_ref_ids: list[int]
    ) -> list[tuple[int, float | None]]:
        report = QueryReport(lexical_only=self.embedder is None)
        self._reports[query] = report
        if self.embedder is None and not self._warned:
            self._warned = True
            log.warning(
                "quest %s: no embedder — local graph leg is lexical-only "
                "(all query words must match), so S2 runs more than it should",
                self.quest_id,
            )
        ex = set(exclude_ref_ids)

        local = _local_graph_search(store, self.quest_id, query, self.embedder)
        floor = relevance_floor()
        local_ok = [(rid, kind, sc) for rid, kind, sc in local if sc >= floor]
        report.local = len(local_ok)
        for rid, kind, _sc in local_ok:
            report.kinds[rid] = kind

        acquired: list[int] = []
        if len(local_ok) < LOCAL_ENOUGH or self.force_external:
            report.external_ran = True
            report.forced = len(local_ok) >= LOCAL_ENOUGH
            acquired, report.external_error = self._acquire(query)
            report.acquired = len(acquired)
            for rid in acquired:
                report.kinds.setdefault(rid, "paper")

        ordered: list[tuple[int, float | None]] = [
            *((rid, sc) for rid, _kind, sc in local_ok),
            *((rid, None) for rid in acquired),
        ]
        seen: set[int] = set()
        out: list[tuple[int, float | None]] = []
        for rid, score in ordered:
            if rid in ex or rid in seen:
                continue
            seen.add(rid)
            out.append((rid, score))
        return out

    def _acquire(self, query: str) -> tuple[list[int], str | None]:
        """The external leg: S2 free-text search, then queue each DOI-bearing
        candidate via ``PaperHandler.acquire``. Returns ``(acquired ids,
        error)`` — an S2 failure is recorded, never raised (the caller keeps
        its local hits); a per-candidate acquire failure is swallowed. The S2
        client is the ``[paper]`` extra, so its import sits inside the same
        guard: a venv without it records an error instead of failing the tick."""
        from precis.handlers.paper import PaperHandler

        error: str | None = None
        try:
            from precis.ingest.semantic_scholar import search_s2_papers

            candidates = search_s2_papers(query, limit=_acquire_per_query())
        except Exception as exc:
            log.debug("quest %s: S2 search failed for %r", self.quest_id, query[:80])
            candidates = []
            error = f"{type(exc).__name__}: {str(exc)[:120]}"

        acquired: list[int] = []
        handler = PaperHandler(hub=self.hub)
        for paper in candidates:
            doi = paper.get("doi")
            if not doi:
                continue
            try:
                resp = handler.acquire(
                    identifier=f"doi:{doi}",
                    reason=f"quest lit-search: {query[:120]}",
                    verify=True,
                )
            except Exception:
                log.debug(
                    "quest %s: acquire failed for doi=%s (query=%r)",
                    self.quest_id,
                    doi,
                    query[:80],
                )
                continue
            m = _ID_IN_ACK.search(resp.body or "")
            if m is not None:
                acquired.append(int(m.group(1)))
        return acquired, error


def _local_graph_search(
    store: Store, quest_id: int, query: str, embedder: Any | None
) -> list[tuple[int, str, float]]:
    """The local-first leg: hybrid (RRF-fused lexical + semantic) search over
    :data:`LOCAL_KINDS` in ONE query via ``Store.search_chunks_across_kinds``
    — the primitive behind ``search()``'s cross-kind source search, not a new
    ranker. One best chunk per ref; ``(ref_id, kind, score)`` best first.

    The quest itself and its own dossier / paper drafts are excluded (they
    would "answer" every query about the quest). Degrades to lexical-only
    without an embedder; any failure returns ``[]`` (a miss — the external
    leg then runs, as before this leg existed)."""
    from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR

    try:
        exclude = {quest_id}
        for rel in ("dossier-of", "paper-of"):
            exclude.update(
                ln.src_ref_id
                for ln in store.links_for(quest_id, direction="in", relation=rel)
            )
        query_vec: list[float] | None = None
        if embedder is not None:
            try:
                query_vec = embedder.embed_one(query)
            except Exception:
                log.warning(
                    "quest %s: local-leg embed failed; lexical-only",
                    quest_id,
                    exc_info=True,
                )
        rows = store.chunks.search_chunks_across_kinds(
            kinds=list(LOCAL_KINDS),
            q=query,
            query_vec=query_vec,
            limit=10,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            exclude_ref_ids=sorted(exclude),
        )
    except Exception:
        log.warning("quest %s: local graph search failed", quest_id, exc_info=True)
        return []
    return [(ref.id, ref.kind, float(score)) for _blk, ref, score in rows]


@dataclass(frozen=True)
class SearchStep:
    queries_run: int
    papers_linked: int
    notes: list[str] = field(default_factory=list)


def _default_paper_search(
    store: Store, query: str, exclude_ref_ids: list[int]
) -> list[tuple[int, float]]:
    """Safe corpus-only default: lexical paper-title lookup, no network.

    Returns ``(ref_id, rank)`` pairs, best first — ``rank`` is
    ``search_refs_lexical``'s own ``ts_rank_cd`` score, previously computed
    and discarded here; :func:`run_search_step` now floors on it (quest-
    tick-incident-fix.md item 4).
    """
    ex = set(exclude_ref_ids)
    rows = store.search_refs_lexical(q=query, kind="paper", limit=10)
    return [(r.id, rank) for (r, rank) in rows if r.id not in ex]


@dataclass(frozen=True)
class SearchQuery:
    """One parsed ``searches`` payload entry (dossier-hygiene design).

    ``query`` is the short keyword phrasing — unchanged role, still what
    drives the Semantic Scholar + acquire leg (:func:`make_acquiring_search`)
    and the logbook entry's own text. ``hypothetical`` is optional: a HyDE
    passage that, when present, routes the CORPUS leg through
    :func:`_hyde_corpus_hits` instead of :func:`_default_paper_search`'s
    plain lexical lookup — see the module docstring.
    """

    query: str
    hypothetical: str | None = None


def _parse_search_entry(raw: Any) -> SearchQuery | None:
    """Parse one ``payload["searches"]`` entry — either the legacy plain
    string, or ``{"query": "...", "hypothetical": "..."}``. ``None`` when
    the entry has no usable ``query`` (a blank string, an empty/malformed
    dict, or anything else) — the caller's cue to skip it, mirroring the
    old blank-string skip."""
    if isinstance(raw, dict):
        query = str(raw.get("query") or "").strip()
        hypothetical = str(raw.get("hypothetical") or "").strip() or None
    else:
        query = str(raw or "").strip()
        hypothetical = None
    return SearchQuery(query=query, hypothetical=hypothetical) if query else None


def _hyde_corpus_hits(
    store: Store,
    embedder: Any | None,
    quest_id: int,
    query: str,
    hypothetical: str,
    exclude_ref_ids: list[int],
    *,
    limit: int = 10,
) -> list[tuple[int, float]]:
    """The HyDE-fused corpus leg: ``query`` + ``hypothetical`` run through
    :class:`precis.handlers._paper_search.FusedBlockSearch` (``queries=
    [query], answers=[hypothetical]``) — the same broad-retrieval fusion the
    ``search(kind='paper', queries=…, answers=…)`` verb exposes — in place of
    :func:`_default_paper_search`'s plain lexical lookup. Ranked ``(ref_id,
    score)`` pairs, best first, deduped, ``exclude_ref_ids`` dropped — the
    fused score was previously computed and discarded here;
    :func:`run_search_step` now floors on it (quest-tick-incident-fix.md
    item 4).

    Degrades to ``[]`` on any failure (an embedder-less store, a store stub
    missing a method this pulls in, a flaky embed call) — one search entry's
    HyDE leg must never sink the whole lit-search step; the caller still has
    ``search_fn``'s ordinary corpus+acquire leg to fall back on.
    """
    from precis.handlers._paper_search import FusedBlockSearch

    try:
        result = FusedBlockSearch(store=store, embedder=embedder, kind="paper").run(
            q=query,
            scope=None,
            tags=None,
            page_size=limit,
            page=1,
            exclude=None,
            mode=None,
            after=None,
            before=None,
            queries=[query],
            answers=[hypothetical],
            per_paper=None,
        )
    except Exception:
        log.debug(
            "quest %s: HyDE corpus search failed for %r",
            quest_id,
            query[:80],
            exc_info=True,
        )
        return []
    ex = set(exclude_ref_ids)
    seen: set[int] = set()
    out: list[tuple[int, float]] = []
    for _block, ref, score in result.hits:
        if ref.id in ex or ref.id in seen:
            continue
        seen.add(ref.id)
        out.append((ref.id, float(score)))
    return out


def make_acquiring_search(
    quest_id: int,
    hub: Any,
    embedder: Any | None = None,
    *,
    force_external: bool = False,
) -> AcquiringSearch:
    """Build a ``search_fn`` that looks locally first, then acquires.

    **Local first.** The graph (papers, findings, drafts, concepts, memories —
    :func:`_local_graph_search`) is searched; only when it returns fewer than
    :data:`LOCAL_ENOUGH` hits above :func:`relevance_floor` does the external
    leg run: Semantic Scholar's top results for the query — anything carrying
    a DOI — are queued through ``PaperHandler.acquire`` (idempotent stub mint
    + ``fetch_oa`` pickup later, out of band). A query the graph already
    answers acquires nothing and makes no S2 call — unless ``force_external``
    is set, the roadmap supply role's escalation after a dry tick: then the
    external leg runs for every query regardless of the local count (local
    hits still come first, same dedup) and the logbook line says
    "outside searched (escalated)". Default False; no other caller sets it.

    An S2 failure (rate limit, exception) is recorded on the query's
    :class:`QueryReport` and the local hits are still returned; a bad DOI or
    a flaky acquire is swallowed per-candidate — one dud result must never
    sink the whole lit-search step.

    This does **not** pass ``context_ref_id=quest_id`` to ``acquire()`` — that
    linked ``related-to``→quest for every accepted candidate
    *unconditionally*, before :func:`run_search_step` sliced to
    :data:`MAX_LINK_PER_QUERY` or applied the floor (the unbounded path
    qu401863's stray ``related-to`` papers came from, quest-tick-incident-fix
    item 5). An S2 result carries no relevance score, so it returns
    ``score=None`` — never dropped by the floor, but bounded like every other
    candidate: ``serves``→quest is the only link this step creates.

    Returns an :class:`AcquiringSearch` (a plain 3-argument ``SearchFn`` plus
    the per-query report).
    """
    return AcquiringSearch(quest_id, hub, embedder, force_external=force_external)


def _sources_clause(report: QueryReport | None) -> str:
    """`` [local N, acquired M …]`` — the logbook's local-vs-outside split for
    one query (empty for a ``search_fn`` that reports nothing)."""
    if report is None:
        return ""
    if not report.external_ran:
        tail = "outside skipped, graph answered"
    elif report.forced and not report.external_error:
        tail = "graph answered, outside searched (escalated)"
    elif report.external_error:
        tail = f"outside search failed: {report.external_error}"
    else:
        tail = "graph thin, outside searched"
    if report.lexical_only:
        tail += "; local leg lexical-only, no embedder"
    return f" [local {report.local}, acquired {report.acquired}; {tail}]"


def run_search_step(
    store: Store,
    quest_id: int,
    searches: list[Any],
    *,
    by: str = "agent",
    search_fn: SearchFn | None = None,
    embedder: Any | None = None,
) -> SearchStep:
    """Run each search, link the top held papers as ``serves`` servers.

    ``searches`` entries are parsed by :func:`_parse_search_entry` — either
    the legacy plain query string, or ``{"query": ..., "hypothetical":
    ...}`` (HyDE, dossier-hygiene design). An entry with a ``hypothetical``
    runs its corpus leg through :func:`_hyde_corpus_hits` (fused
    ``queries=``/``answers=`` retrieval — the model's ``query`` phrasing
    alone kept missing the held corpus) IN ADDITION to ``search_fn`` (still
    driven by the plain ``query`` — the Semantic Scholar + acquire leg is
    unchanged); the two hit lists are merged (HyDE first) and deduped before
    the per-query link cap. ``embedder`` (optional) powers the HyDE leg's
    semantic reformulation — see :func:`_hyde_corpus_hits`.

    Every search lands a logbook entry: a ``result`` when papers were linked
    (external progress → the cascade stall clock resets), or an ``observation``
    when nothing held matched (the un-held / acquisition-needed case, made
    visible rather than silent). The entry's own text always quotes the
    short ``query`` (not the ``hypothetical`` passage).

    Every freshly-linked paper also picks up the ``quest:<public_id>`` OPEN
    tag (see :mod:`precis.quest.tagging`) — the same tag the Drive-scoped
    hub links point at, so a paper this step links is immediately visible
    there without waiting on a backfill.

    **Relevance floor** (quest-tick-incident-fix.md item 4): each merged hit
    carries the score its leg computed (``ts_rank_cd`` off
    :func:`_default_paper_search`, the fused score off
    :func:`_hyde_corpus_hits`; the S2 acquire leg's un-ranked candidates
    carry ``score=None``). A scored hit below :func:`relevance_floor` is
    dropped before the :data:`MAX_LINK_PER_QUERY` slice — never linked — and
    logged with its score; a ``None``-scored hit always passes (there is
    nothing to floor it against). Default floor is ``0.0`` (log-only, see
    :func:`relevance_floor`).
    """
    search = search_fn or _default_paper_search
    quest_tag = Tag.open(quest_tag_value(quest_id, store))
    existing = {
        s.id for s in _live_servers(store, quest_id) if s.kind in LINKABLE_KINDS
    }
    floor = relevance_floor()
    queries_run = 0
    linked_total = 0
    notes: list[str] = []

    for raw in searches[:MAX_QUERIES]:
        entry = _parse_search_entry(raw)
        if entry is None:
            continue
        query = entry.query
        queries_run += 1
        merged: list[tuple[int, float | None]] = []
        if entry.hypothetical:
            merged.extend(
                _hyde_corpus_hits(
                    store,
                    embedder,
                    quest_id,
                    query,
                    entry.hypothetical,
                    list(existing),
                )
            )
        seen = {rid for rid, _score in merged}
        for rid, score in search(store, query, list(existing)):
            if rid in seen:
                continue
            seen.add(rid)
            merged.append((rid, score))
        above_floor: list[int] = []
        for rid, score in merged:
            if score is not None and score < floor:
                log.info(
                    "quest %s: dropped below-floor hit ref=%s score=%.4f "
                    "floor=%.4f query=%r",
                    quest_id,
                    rid,
                    score,
                    floor,
                    query[:80],
                )
                continue
            above_floor.append(rid)
        report: QueryReport | None = getattr(search, "report_for", lambda _q: None)(
            query
        )
        kinds = report.kinds if report is not None else {}
        # Only kinds that may carry a `serves` link to a quest (the default
        # kind is paper: legacy search_fns return paper ids only).
        above_floor = [
            r for r in above_floor if kinds.get(r, "paper") in LINKABLE_KINDS
        ]
        hits = above_floor[:MAX_LINK_PER_QUERY]
        linked: list[int] = []
        for rid in hits:
            if rid in existing:
                continue
            store.add_link(
                src_ref_id=rid,
                dst_ref_id=quest_id,
                relation="serves",
                set_by="agent",
            )
            store.add_tag(rid, quest_tag, set_by="system")
            existing.add(rid)
            linked.append(rid)
        linked_total += len(linked)
        sources = _sources_clause(report)
        if linked:
            handles = ", ".join(_handle(kinds.get(rid, "paper"), rid) for rid in linked)
            append_entry(
                store,
                quest_id,
                text=(
                    f'lit-search: "{query[:80]}" → linked {len(linked)} '
                    f"source(s): {handles}{sources}"
                ),
                entry_type="result",
                by=by,
            )
            notes.append(f"{query[:40]}: +{len(linked)}")
        else:
            append_entry(
                store,
                quest_id,
                text=(
                    f'lit-search: "{query[:80]}" → nothing new linked'
                    f"{sources or ' (acquisition needed)'}"
                ),
                entry_type="observation",
                by=by,
            )
            notes.append(f"{query[:40]}: 0")

    return SearchStep(queries_run=queries_run, papers_linked=linked_total, notes=notes)


__all__ = [
    "LINKABLE_KINDS",
    "LOCAL_ENOUGH",
    "LOCAL_KINDS",
    "MAX_LINK_PER_QUERY",
    "MAX_QUERIES",
    "AcquiringSearch",
    "QueryReport",
    "SearchFn",
    "SearchQuery",
    "SearchStep",
    "make_acquiring_search",
    "relevance_floor",
    "run_search_step",
]
