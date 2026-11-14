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

from precis.quest import lit_gate
from precis.quest.gaps import _handle, _live_servers
from precis.quest.logbook import append_entry
from precis.quest.tagging import quest_tag_value
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES
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
                    explicit=False,
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
        exclude |= judged_out(store, quest_id)
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


def judged_out(store: Store, quest_id: int) -> frozenset[int]:
    """Sources the quest holds a *valid* ``not-useful`` ``considered``
    verdict on (gr662069, spec me626852) — the lit-search's "seen, not
    linked" ledger gr592951 asked for, read from the general mechanism
    instead of a private one. Every search leg drops them and the link
    loop never re-serves them, so a judged-out candidate stops costing a
    quest-link row and tick tokens on every run. A *stale* verdict (the
    quest's question or the source changed since the judgment) excludes
    nothing: it sits in ``get(kind='quest', id=…, view='considerations')``
    for reconsideration. A read failure excludes nothing either — a miss,
    never a broken tick."""
    try:
        return store.consideration_exclusions(quest_id).excluded
    except Exception:
        log.warning("quest %s: considered-verdict read failed", quest_id, exc_info=True)
        return frozenset()


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


#: Cap on the ``seen, not linked`` per-candidate verdicts the lit-search
#: logbook line names — a flooded query shows the first dozen and counts
#: the rest (gr592951 item c).
SEEN_NOT_LINKED_CAP = 12


def _seen_not_linked(rejects: list[tuple[int, str]], kinds: dict[int, str]) -> str:
    """``; seen, not linked: pa1 (no host term), pa2 (no title/abstract)``
    — the gate's per-candidate verdicts on one lit-search line, capped at
    :data:`SEEN_NOT_LINKED_CAP` shown."""
    if not rejects:
        return ""
    shown = [
        f"{_handle(kinds.get(rid, 'paper'), rid)} ({why})"
        for rid, why in rejects[:SEEN_NOT_LINKED_CAP]
    ]
    tail = (
        f", +{len(rejects) - SEEN_NOT_LINKED_CAP} more"
        if len(rejects) > SEEN_NOT_LINKED_CAP
        else ""
    )
    return "; seen, not linked: " + ", ".join(shown) + tail


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

    **Judged-out candidates** (gr662069 / gr592951): sources carrying a
    valid ``not-useful`` ``considered`` verdict on the quest
    (:func:`judged_out`) are passed to every leg as exclusions and
    skipped by the link loop, so a candidate a prior run rejected is
    never re-linked; the step's notes say how many were held out.

    **Pre-link relevance gate** (gr592951): between the floor and the
    ``serves`` edge sits :mod:`precis.quest.lit_gate` — a candidate
    with no title and no abstract is never linked, and (when the quest
    carries ``meta.lit_gate`` term lists) a paper missing a host or
    reaction term is rejected with the reason the logbook line names
    per candidate (``seen, not linked: pa<N> (<reason>)``, cap
    :data:`SEEN_NOT_LINKED_CAP`); every
    :data:`lit_gate.GATE_SAMPLE_MODULUS`-th rejection links anyway,
    flagged ``sampled past gate``. Gate failures record no
    ``considered`` row — durable rejection is the ``judgments``
    payload's job (:func:`apply_judgments`).
    """
    search = search_fn or _default_paper_search
    quest_tag = Tag.open(quest_tag_value(quest_id, store))
    existing = {
        s.id for s in _live_servers(store, quest_id) if s.kind in LINKABLE_KINDS
    }
    rejected = judged_out(store, quest_id)
    gate = lit_gate.gate_terms(store, quest_id)
    floor = relevance_floor()
    queries_run = 0
    linked_total = 0
    notes: list[str] = []
    if rejected:
        notes.append(
            f"held out {len(rejected)} considered-not-useful source(s) "
            "(view='considerations')"
        )

    for raw in searches[:MAX_QUERIES]:
        entry = _parse_search_entry(raw)
        if entry is None:
            continue
        query = entry.query
        queries_run += 1
        skip = list(existing | rejected)
        merged: list[tuple[int, float | None]] = []
        if entry.hypothetical:
            merged.extend(
                _hyde_corpus_hits(
                    store,
                    embedder,
                    quest_id,
                    query,
                    entry.hypothetical,
                    skip,
                )
            )
        seen = {rid for rid, _score in merged}
        for rid, score in search(store, query, skip):
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
        # The pre-link relevance gate (gr592951): every candidate is
        # classified before the MAX_LINK_PER_QUERY slice — a ref with no
        # title and no abstract is never linked ("no title/abstract"), a
        # paper that misses the quest's meta.lit_gate term lists is
        # rejected with its one-line reason (the gate is opt-in and only
        # papers are gated — findings/concepts are the quest's own graph),
        # and every GATE_SAMPLE_MODULUS-th rejection links anyway, flagged
        # "sampled past gate" so cheap triage isn't the only discovery
        # path. Gate failures write NO considered row — durable rejection
        # is the tick's `judgments` channel, not this cheap triage.
        passing: list[tuple[int, bool]] = []  # (ref_id, sampled-past-gate)
        rejects: list[tuple[int, str]] = []
        for rid in above_floor:
            # A search_fn that ignores its exclude list (a legacy test
            # double, a stale cache) must still not re-serve a judged-out
            # candidate.
            if rid in existing or rid in rejected:
                continue
            kind = kinds.get(rid, "paper")
            ref = lit_gate.candidate_ref(store, kind, rid)
            if not lit_gate.ref_has_metadata(store, ref):
                rejects.append((rid, "no title/abstract"))
                continue
            sampled = False
            if kind == "paper" and gate:
                ok, why = lit_gate.check_terms(
                    lit_gate.candidate_text(store, ref), gate
                )
                if not ok:
                    if rid % lit_gate.GATE_SAMPLE_MODULUS == 0:
                        sampled = True
                    else:
                        rejects.append((rid, why))
                        continue
            passing.append((rid, sampled))
        linked: list[tuple[int, bool]] = []
        for rid, sampled in passing[:MAX_LINK_PER_QUERY]:
            store.add_link(
                src_ref_id=rid,
                dst_ref_id=quest_id,
                relation="serves",
                set_by="agent",
            )
            store.add_tag(rid, quest_tag, set_by="system")
            existing.add(rid)
            linked.append((rid, sampled))
        linked_total += len(linked)
        sources = _sources_clause(report)
        seen_not = _seen_not_linked(rejects, kinds)
        if linked:
            handles = ", ".join(
                _handle(kinds.get(rid, "paper"), rid)
                + (" (sampled past gate)" if sampled else "")
                for rid, sampled in linked
            )
            append_entry(
                store,
                quest_id,
                text=(
                    f'lit-search: "{query[:80]}" → linked {len(linked)} '
                    f"source(s): {handles}{seen_not}{sources}"
                ),
                entry_type="result",
                by=by,
                extra_meta={"signal": "lit-search"},
            )
            notes.append(f"{query[:40]}: +{len(linked)}")
        else:
            append_entry(
                store,
                quest_id,
                text=(
                    f'lit-search: "{query[:80]}" → nothing new linked'
                    f"{seen_not}{sources or ' (acquisition needed)'}"
                ),
                entry_type="observation",
                by=by,
                extra_meta={"signal": "lit-search"},
            )
            notes.append(f"{query[:40]}: 0")

    return SearchStep(queries_run=queries_run, papers_linked=linked_total, notes=notes)


# ── the tick's verdict channel (gr592951 slice 5) ────────────────────
#
# The lit-search's own gate (lit_gate) is cheap triage — it never writes
# a `considered` row, because a title-match omission must not become a
# durable rejection. The DURABLE channel is the payload's `judgments`
# field: the model records a reasoned `considered` verdict on a source
# it weighed, and the general ledger (gr662069, spec me626852) holds a
# `not-useful` one out of every later lit-search leg until the striving
# or the source changes. The verdict is pair- and question-scoped and
# revisable — a `useful` re-judgment of the same pair replaces it.

#: Per-tick cap on ``judgments`` entries — the tick's channel for
#: recording ``considered`` verdicts on lit-search sources (gr592951).
MAX_JUDGMENTS_PER_TICK = 8

#: Logbook marker prefix the ``judgments`` apply writes — a summary line
#: naming each recorded verdict.
_JUDGMENTS_MARKER = "judgments:"


def _resolve_judgment_source(store: Store, ident: str) -> int | None:
    """A live source ref id for a ``judgments`` ``source`` — a universal
    handle (``pa<id>``, ``fi<id>``, ``cn<id>``, …; a chunk handle like
    ``pc<id>`` grounds at its owning ref), or a ``web:<id|slug>`` cache
    address. ``None`` when nothing live resolves."""
    ident = ident.strip()
    if ident.lower().startswith("web:"):
        entry = _resolve_cache_ref(store, "web", ident[4:])
        return int(entry[0].id) if entry is not None else None
    resolved = store.resolve_handle(ident)
    return int(resolved.ref_id) if resolved is not None else None


def apply_judgments(
    store: Store,
    quest_id: int,
    entries: Any,
    *,
    cap: int = MAX_JUDGMENTS_PER_TICK,
    by: str = "agent",
) -> int:
    """Record a tick payload's ``judgments`` — per-source ``considered``
    verdicts on this quest (gr592951).

    Each entry is ``{"source": "<handle>", "verdict":
    "useful|not-useful|uncertain", "reason": "<why>", "depth": "<how far
    read>"}``; ``depth`` defaults to ``abstract`` (the snippet depth the
    tick's served-literature list shows). A verdict goes through the
    ``rel='considered'`` write door on the quest→source pair — a
    re-judgment of the same pair replaces the older verdict. A
    **reasonless entry is dropped** (a verdict IS its reason —
    ``validate_considered_meta`` refuses the marker anyway); malformed
    entries, unresolvable handles, bad vocab and cap overflow each land
    an ``observation`` refusal — never silent, never a tick failure.
    Returns the number of verdicts recorded.
    """
    if not isinstance(entries, list):
        return 0
    from precis.store._consideration_ops import CONSIDER_DEPTHS, CONSIDER_VERDICTS

    made = 0
    reasonless = 0
    over_cap = 0
    recorded: list[str] = []
    refused: list[str] = []
    for e in entries:
        if made >= cap:
            over_cap += 1
            continue
        if not isinstance(e, dict):
            refused.append("malformed entry (not an object)")
            continue
        ident = str(e.get("source") or "").strip()
        verdict = str(e.get("verdict") or "").strip()
        reason = str(e.get("reason") or "").strip()
        depth = str(e.get("depth") or "abstract").strip() or "abstract"
        if not ident:
            refused.append("entry has no usable `source`")
            continue
        if not reason:
            reasonless += 1
            continue
        if verdict not in CONSIDER_VERDICTS:
            refused.append(
                f"{ident}: verdict must be one of {CONSIDER_VERDICTS}, got {verdict!r}"
            )
            continue
        if depth not in CONSIDER_DEPTHS:
            refused.append(
                f"{ident}: depth must be one of {CONSIDER_DEPTHS}, got {depth!r}"
            )
            continue
        src_id = _resolve_judgment_source(store, ident)
        if src_id is None:
            refused.append(f"{ident}: does not resolve to a held source")
            continue
        try:
            store.add_link(
                src_ref_id=quest_id,
                dst_ref_id=src_id,
                relation="considered",
                meta={"verdict": verdict, "reason": reason, "depth": depth},
                set_by=by,
            )
        except Exception as exc:
            refused.append(
                f"{ident}: verdict not recorded ({type(exc).__name__}: {exc!s})"[:160]
            )
            continue
        made += 1
        recorded.append(f"{ident} ({verdict})")
    if reasonless:
        refused.append(
            f"{reasonless} judgment(s) dropped: no `reason` — a verdict "
            "IS its 'not a match because …'"
        )
    if over_cap:
        refused.append(f"{over_cap} entr(ies) over the per-tick cap of {cap}")
    if recorded:
        try:
            append_entry(
                store,
                quest_id,
                text=(
                    f"{_JUDGMENTS_MARKER} recorded {len(recorded)} considered "
                    f"verdict(s): {', '.join(recorded)}"
                )[:400],
                entry_type="observation",
                by=by,
                extra_meta={"signal": "lit-search"},
            )
        except Exception:
            log.warning("apply_judgments: logbook note failed", exc_info=True)
    for note in refused:
        try:
            append_entry(
                store,
                quest_id,
                text=f"judgment refused: {note}"[:400],
                entry_type="observation",
                by=by,
            )
        except Exception:
            log.warning("apply_judgments: logbook note failed", exc_info=True)
    return made


# ── deliberate read/fetch requests (gr502680) ─────────────────────────
#
# A tick serves papers as title + abstract snippet, findings as title and
# todos as a one-line title (gr573759: a todo lead truncated in a dossier
# handoff had no door back to its full text), so the model's only escape
# from snippet-depth is to SAY what it wants
# read — the same deliberate-request pattern as the `reruns` payload field
# (gr499904, ``compute.apply_reruns``): `read` names a held finding/paper
# chunk or todo ref whose full text lands in the NEXT tick's context; `fetch` asks for
# a served abstract stub's full text via ``PaperHandler.acquire``
# (idempotent identifier-collapse — the request re-pins the stub at the
# head of the fetch_oa queue, never a second mint), or — its `{"url": …}`
# form (gr573684) — fetches a live page through the ``web`` cache and
# parks it for the same next-tick render, the tick's pricing-page /
# vendor-doc monitor.
#
# A `read` entry may carry two optional selectors (gr505186/gr507189 — a
# bare read only ever reached the front matter): ``section`` names a
# heading from the ref's ``section_path`` structure and ``page`` picks a
# later window of the body, so the model can reach results sections and
# other deep content instead of re-reading chunk 0 forever.

#: Quest meta key the ``read`` field parks its requests under — rewritten
#: wholesale each apply (the batch rendered into this tick's prompt came
#: from LAST tick's list), so a request is shown to exactly one later tick.
PENDING_READS_META_KEY = "pending_reads"

#: Logbook marker prefix a successful ``fetch`` request writes — the
#: serving context's ``full text: requested`` status scans for it.
_FETCH_MARKER = "lit-fetch:"

#: Logbook marker prefix a ``fetch`` ``{"url": ...}`` entry writes — the
#: traceable record of which live page the tick pulled, and the
#: ``web:<id>`` handle a later ``read`` can name (gr573684).
_WEB_FETCH_MARKER = "web-fetch:"


def _paper_body_held(store: Store, ref_id: int) -> bool:
    """True when the paper has a readable body — a non-heading, non-empty
    body chunk (the same "held" notion ``tick._paper_citable_handle`` uses
    for its cite-handle check; a small local copy per that convention)."""
    try:
        for b in store.chunks.list_chunks_for_ref(ref_id):
            if b.chunk_kind == "heading":
                continue
            if (b.text or "").strip():
                return True
    except Exception:
        return False
    return False


def _resolve_cache_ref(store: Store, kind: str, ident: str) -> Any | None:
    """A live cache-backed ref addressed ``<kind>:<ref_id|slug>`` — the
    cite form cache kinds carry (``web:<id>``), since provider kinds have
    no universal handle (:data:`handle_registry.CODELESS_KINDS`). Digits
    resolve as a ref id, anything else as the ref's slug; ``None`` when no
    live cache row matches."""
    ident = ident.strip()
    if not ident:
        return None
    entry = None
    if ident.isdigit():
        entry = store.get_cache_entry_by_ref_id(kind=kind, ref_id=int(ident))
    if entry is None:
        entry = store.get_cache_entry_by_slug(kind=kind, slug=ident)
    return entry[0] if entry is not None else None


def _bench_stems() -> list[str]:
    """The held results-file stems a ``bench:`` read can name — the
    address book a refused read prints so the next tick can aim again
    (gr618517). Any lookup fault is an empty list, never an apply
    failure."""
    try:
        from precis.bench import raw as bench_raw
        from precis.bench import runner as bench_runner

        return bench_raw.list_results_stems(bench_runner.default_results_dir())
    except Exception:
        return []


def _bench_stem_hits(needle: str) -> list[str]:
    """The held stems containing ``needle`` (:func:`match_stems`'s
    normalised substring) — the refusal's narrower address book when a
    ``bench:`` miss was ambiguous rather than empty (gr660023). Any
    lookup fault is an empty list, never an apply failure."""
    try:
        from precis.bench import raw as bench_raw
        from precis.bench import runner as bench_runner

        return bench_raw.match_stems(bench_runner.default_results_dir(), needle)
    except Exception:
        return []


def _resolve_read_ref(store: Store, ident: str) -> Any | None:
    """Resolve a ``read`` entry's ``ref`` — any well-formed universal
    handle (``fi<id>``, ``pc<id>``, ``pa<id>``, …) that resolves live, a
    ``web:<ref_id|slug>`` cache address (a fetched page carries no
    universal handle — gr573684), or a ``bench:<stem>`` results-file
    address (gr618517 — a benchmark run's raw log lives on disk, not in
    the store; resolves to its ``Path``, a truthy non-ref). The reserved
    ``bench:index`` address resolves to the results dir — the run index
    the read renders (gr660023) — and a stem that is a unique substring
    of one held run resolves to that run."""
    ident = ident.strip()
    if ident.lower().startswith("web:"):
        return _resolve_cache_ref(store, "web", ident[4:])
    if ident.lower().startswith("bench:"):
        from precis.bench import raw as bench_raw
        from precis.bench import runner as bench_runner

        try:
            results_dir = bench_runner.default_results_dir()
            name = ident[6:].strip()
            if name.lower() == bench_raw.INDEX_STEM:
                return results_dir
            path = bench_raw.resolve_results(results_dir, name)
            if path is None:
                hits = bench_raw.match_stems(results_dir, name)
                if len(hits) == 1:
                    path = results_dir / f"{hits[0]}.json"
            return path
        except Exception:
            return None
    resolved = store.resolve_handle(ident)
    return resolved


def _read_refusal(ident: str) -> str:
    """The apply-time refusal line for an unresolvable ``read`` ref —
    a ``bench:`` miss names the held run stems matching the attempted
    name (all of them when nothing matched) and the ``bench:index``
    address, so the next tick can aim again without a second miss
    (gr618517, gr660023)."""
    if ident.lower().startswith("bench:"):
        hits = _bench_stem_hits(ident[6:])
        pool = hits or _bench_stems()
        which = f" matching {ident[6:].strip()!r}" if hits else ""
        # The recovery hint leads: apply_reads clips the line to 400 chars.
        head = (
            f"{ident}: no such results file — bench:index lists every run "
            f"with dates and filters by substring; held runs{which}: "
        )
        budget = 390 - len(head)
        shown: list[str] = []
        for stem in pool:
            if sum(len(x) + 2 for x in shown) + len(stem) > budget - 12:
                break
            shown.append(stem)
        listing = ", ".join(shown) or "(none held)"
        if len(pool) > len(shown) and shown:
            listing += f", +{len(pool) - len(shown)} more"
        if not pool:
            # An empty dir is also the retention answer (gr660413).
            from precis.bench import raw as bench_raw

            listing += "; " + bench_raw.RETENTION_LINE
        return head + listing
    return f"{ident}: does not resolve to a held ref/chunk"


def apply_reads(
    store: Store,
    quest_id: int,
    entries: Any,
    *,
    cap: int = 3,
    by: str = "agent",
) -> int:
    """Execute a tick payload's ``read`` — park the held items the model
    wants full text of so the NEXT tick's prompt renders them (gr502680).

    Each entry is ``{"ref": "<universal handle>", "why": ...}`` plus the
    optional selectors ``{"section": "<heading words>", "page": <n>}``
    (gr505186 — a bare read only reaches the body's front page). Entries
    past ``cap``, malformed entries, and refs that don't resolve each land
    as an ``observation`` logbook note — never silent, never a tick
    failure. ``meta.pending_reads`` is REWRITTEN each call: it is exactly
    this tick's requests (the previous batch was consumed into this
    tick's prompt, so a re-sent ref is a fresh request). Returns the
    number of reads queued.
    """
    requests: list[dict[str, Any]] = []
    refused: list[str] = []
    if isinstance(entries, list):
        for e in entries[:cap]:
            if not isinstance(e, dict):
                refused.append("malformed entry (not an object)")
                continue
            ident = str(e.get("ref") or "").strip()
            why = str(e.get("why") or "").strip()[:200]
            if not ident:
                refused.append("entry has no usable `ref`")
                continue
            if ident.startswith(("http://", "https://")):
                refused.append(
                    f"{ident[:80]}: a URL is a `fetch` request "
                    '({"url": ...}), not a `read` — read names a held ref'
                )
                continue
            if _resolve_read_ref(store, ident) is None:
                refused.append(_read_refusal(ident))
                continue
            req: dict[str, Any] = {"ref": ident, "why": why}
            section = e.get("section")
            if section is not None:
                if not isinstance(section, (str, int, float)) or isinstance(
                    section, bool
                ):
                    refused.append(f"{ident}: `section` is not a heading name")
                    continue
                section = str(section).strip()[:120]
                if section:
                    req["section"] = section
            page = e.get("page")
            if page is not None:
                try:
                    page = int(page)
                except (TypeError, ValueError):
                    refused.append(f"{ident}: `page` is not a number")
                    continue
                if isinstance(e.get("page"), bool) or page < 1 or page > 999:
                    refused.append(f"{ident}: `page` out of range (1..999)")
                    continue
                if page > 1:
                    req["page"] = page
            requests.append(req)
        if len(entries) > cap:
            refused.append(
                f"{len(entries) - cap} entr(ies) over the per-tick cap of {cap}"
            )
    elif entries is not None:
        refused.append("`read` is not a list of {ref, why} objects")
    try:
        store.update_ref(quest_id, meta_patch={PENDING_READS_META_KEY: requests})
    except Exception:
        log.warning(
            "apply_reads: failed to park reads for quest %s", quest_id, exc_info=True
        )
    for note in refused:
        try:
            append_entry(
                store,
                quest_id,
                text=f"read refused: {note}"[:400],
                entry_type="observation",
                by=by,
            )
        except Exception:
            log.warning("apply_reads: logbook note failed", exc_info=True)
    if requests:
        try:
            append_entry(
                store,
                quest_id,
                text=(
                    f"read: queued {len(requests)} ref(s) for full text next "
                    f"tick: {', '.join(r['ref'] for r in requests)}"
                )[:400],
                entry_type="observation",
                by=by,
            )
        except Exception:
            log.warning("apply_reads: logbook note failed", exc_info=True)
    return len(requests)


def _fetch_web_page(
    store: Store, url: str, *, hub: Any | None
) -> tuple[int | None, str | None]:
    """One ``fetch`` ``{"url": ...}`` entry: fetch the URL live through the
    ``web`` kind's cache-backed path (SSRF-guarded ``safe_fetch``,
    tier-0 injection scan, 7-day TTL — the same machinery
    ``get(kind='web', id=URL)`` runs; gr573684).

    Returns ``(ref_id, None)`` of the cached ``web`` ref on success, or
    ``(None, refusal)`` — the URL is model-supplied, so every failure mode
    (bad shape, private address, unreachable host, upstream HTTP error)
    is a refusal note, never a raise.
    """
    from precis.utils.url import canonical_url, is_http_url

    try:
        key = canonical_url(url)
    except ValueError:
        return None, f"{url[:80]}: not a valid URL"
    if not is_http_url(key):
        return None, f"{url[:80]}: not an http(s) URL"
    try:
        if hub is None:
            from precis.dispatch import Hub

            hub = Hub(store=store)
        from precis.handlers.web import WebHandler

        WebHandler(hub=hub).get(id=key)
    except Exception as exc:
        return None, f"{url[:80]}: fetch failed ({type(exc).__name__}: {exc!s})"[:160]
    entry = store.get_cache_entry(provider="web", request_hash=WebHandler._hash(key))
    if entry is None:
        return None, f"{url[:80]}: fetched but no cache row resolved"
    return int(entry[0].id), None


def _park_pending_reads(
    store: Store, quest_id: int, reqs: list[dict[str, Any]]
) -> None:
    """Append fetched web refs to ``meta.pending_reads`` — the queue
    :func:`apply_reads` rewrites each tick — so the next tick's
    Requested-reads section renders their bodies (gr573684). Runs after
    ``apply_reads`` in the stage order, so the fresh batch is preserved,
    not clobbered; deduped against entries already parked."""
    quest = store.get_ref(kind="quest", id=quest_id)
    meta = getattr(quest, "meta", None) or {}
    pending = meta.get(PENDING_READS_META_KEY)
    merged: list[Any] = list(pending) if isinstance(pending, list) else []
    have = {str(e.get("ref")) for e in merged if isinstance(e, dict)}
    merged.extend(r for r in reqs if r["ref"] not in have)
    try:
        store.update_ref(quest_id, meta_patch={PENDING_READS_META_KEY: merged})
    except Exception:
        log.warning(
            "apply_fetches: failed to park web reads for quest %s",
            quest_id,
            exc_info=True,
        )


def apply_fetches(
    store: Store,
    quest_id: int,
    entries: Any,
    *,
    hub: Any | None = None,
    cap: int = 2,
    by: str = "agent",
) -> int:
    """Execute a tick payload's ``fetch`` — two entry shapes sharing one
    per-tick cap.

    ``{"paper": "<pa<id> or ref id>", "why": ...}`` asks for a served
    abstract stub's full text through the same acquire path the DOI-hit
    leg uses (``PaperHandler.acquire``; gr502680). A stub with a
    registered identifier (doi/arxiv/s2) goes through ``acquire`` — the
    identifier collapses onto the held stub and re-pins it at the head of
    the ``fetch_oa`` queue, so a repeat request is a no-op pin, never a
    second mint; a stub with no identifier is re-pinned directly. A paper
    that already holds a body is refused ("already held").

    ``{"url": "<https://…>", "why": ...}`` fetches a live page through the
    ``web`` cache (:func:`_fetch_web_page`; gr573684 — the pricing-page /
    vendor-doc monitor a tick had no channel for). The cached ref is
    ``serves``-linked + ``quest:<id>``-tagged like a lit-search hit, and a
    ``web:<ref_id>`` pending read is parked so the next tick's Requested
    reads renders the body. A repeat request inside the 7-day TTL is a
    cache read; after expiry the same request re-fetches — that IS the
    monitor.

    Refusals land as ``observation`` notes; each success logs a
    ``lit-fetch:`` / ``web-fetch:`` marker line. Returns the number of
    fetch requests made.
    """
    if not isinstance(entries, list):
        return 0
    from precis.utils import handle_registry

    made = 0
    refused: list[str] = []
    handler: Any | None = None
    parked: list[dict[str, Any]] = []
    over_cap = 0
    for e in entries:
        # The cap bounds fetches actually made; refused entries (held,
        # unresolvable, malformed) don't spend a slot.
        if made >= cap:
            over_cap += 1
            continue
        if not isinstance(e, dict):
            refused.append("malformed entry (not an object)")
            continue
        why = str(e.get("why") or "").strip()[:200]
        url = str(e.get("url") or "").strip()
        if url:
            web_ref_id, refusal = _fetch_web_page(store, url, hub=hub)
            if web_ref_id is None:
                refused.append(refusal or f"{url[:80]}: fetch failed")
                continue
            if not any(
                ln.src_ref_id == web_ref_id
                for ln in store.links_for(quest_id, direction="in", relation="serves")
            ):
                store.add_link(
                    src_ref_id=web_ref_id,
                    dst_ref_id=quest_id,
                    relation="serves",
                    set_by="agent",
                )
                store.add_tag(
                    web_ref_id,
                    Tag.open(quest_tag_value(quest_id, store)),
                    set_by="system",
                )
            made += 1
            parked.append({"ref": f"web:{web_ref_id}", "why": why})
            try:
                append_entry(
                    store,
                    quest_id,
                    text=(
                        f"{_WEB_FETCH_MARKER} fetched {url[:160]} "
                        f"→ web:{web_ref_id} — text lands in next tick's "
                        "requested reads" + (f" ({why})" if why else "")
                    )[:400],
                    entry_type="observation",
                    by=by,
                )
            except Exception:
                log.warning("apply_fetches: logbook note failed", exc_info=True)
            continue
        ident = str(e.get("paper") or "").strip()
        if not ident:
            refused.append("entry has no usable `paper` or `url`")
            continue
        parsed = handle_registry.parse(ident)
        ref_id: int | None = None
        if parsed is not None and parsed[1] is False and parsed[0] == "paper":
            ref_id = parsed[2]
        elif ident.isdigit():
            ref_id = int(ident)
        ref = store.get_ref(kind="paper", id=ref_id) if ref_id is not None else None
        if ref is None:
            refused.append(f"{ident}: no such held paper")
            continue
        if _paper_body_held(store, ref.id):
            refused.append(
                f"{ident}: full text already held — read its chunks with `read`"
            )
            continue
        try:
            ids = store.identifiers_for_refs([ref.id]).get(ref.id, {})
        except Exception:
            ids = {}
        identifier = next(
            (f"{k}:{ids[k]}" for k in ("doi", "arxiv", "s2") if ids.get(k)), None
        )
        try:
            if identifier is not None:
                if handler is None:
                    from precis.dispatch import Hub
                    from precis.handlers.paper import PaperHandler

                    handler = PaperHandler(
                        hub=hub if hub is not None else Hub(store=store)
                    )
                handler.acquire(
                    identifier=identifier,
                    title=(ref.title or None),
                    reason=f"quest lit-fetch: {why or 'tick-requested'}",
                    verify=True,
                    explicit=False,
                )
            else:
                store.pin_stub_for_fetch(ref.id)
        except Exception as exc:
            log.exception(
                "apply_fetches: acquire failed for quest %s paper %s",
                quest_id,
                ref.id,
            )
            refused.append(f"{ident}: acquire failed ({type(exc).__name__})")
            continue
        made += 1
        try:
            append_entry(
                store,
                quest_id,
                text=(
                    f"{_FETCH_MARKER} requested full text for pa{ref.id}"
                    + (f" — {why}" if why else "")
                )[:400],
                entry_type="observation",
                by=by,
            )
        except Exception:
            log.warning("apply_fetches: logbook note failed", exc_info=True)
    if over_cap:
        refused.append(f"{over_cap} entr(ies) over the per-tick cap of {cap}")
    if parked:
        _park_pending_reads(store, quest_id, parked)
    for note in refused:
        try:
            append_entry(
                store,
                quest_id,
                text=f"fetch refused: {note}"[:400],
                entry_type="observation",
                by=by,
            )
        except Exception:
            log.warning("apply_fetches: logbook note failed", exc_info=True)
    return made


def requested_fetches(store: Store, quest_id: int) -> set[int]:
    """Paper ref ids this quest has already asked full text for — the
    ``lit-fetch:`` marker lines :func:`apply_fetches` writes. Read by the
    serving context's ``full text: requested`` status."""
    try:
        blocks = [
            b
            for b in store.chunks.list_chunks_for_ref(quest_id)
            if b.chunk_kind == "quest_log"
        ]
    except Exception:
        return set()
    ids: set[int] = set()
    for b in blocks:
        text = b.text or ""
        if not text.startswith(_FETCH_MARKER):
            continue
        ids.update(int(m) for m in re.findall(r"\bpa(\d+)\b", text))
    return ids


#: ``meta.content_key`` of the per-quest todo that collects planned searches
#: a tick could not run (:func:`file_unrun_searches`).
UNRUN_SEARCHES_KEY = "quest-unrun-searches"


def _open_unrun_searches_todo(store: Store, quest_id: int) -> int | None:
    """The live, not-closed unrun-searches todo under ``quest_id``, or None."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id FROM refs r WHERE r.kind = 'todo' "
            "AND r.parent_id = %s AND r.retired_at IS NULL "
            "AND r.meta->>'content_key' = %s AND NOT EXISTS ("
            " SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id"
            " WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'"
            " AND t.value = ANY(%s)) "
            "ORDER BY r.ref_id DESC LIMIT 1",
            (quest_id, UNRUN_SEARCHES_KEY, sorted(TERMINAL_TODO_STATUSES)),
        ).fetchone()
    return int(row[0]) if row else None


def file_unrun_searches(
    store: Store, quest_id: int, entries: list[Any], why: str
) -> int:
    """File planned ``searches`` entries a tick did not run onto one open
    todo under the quest, so a dropped dispatch leaves a tracker carrying the
    queries instead of only prose (gripe 476100). Reuses the quest's open
    unrun-searches todo (one comment per call) and mints a fresh one once
    the last is closed. Returns the todo's ref id; raises on a store error
    (the caller logs and moves on)."""
    from precis.handlers._comment_timeline import append_comment

    lines = []
    for raw in entries:
        parsed = _parse_search_entry(raw)
        lines.append(f"- {parsed.query[:200] if parsed else repr(raw)[:200]}")
    detail = f"{len(entries)} planned search(es) not run: {why}.\n" + "\n".join(lines)

    todo_id = _open_unrun_searches_todo(store, quest_id)
    ref = store.get_ref(kind="todo", id=todo_id) if todo_id is not None else None
    if todo_id is not None and ref is not None:
        append_comment(
            store, ref, kind="todo", comment_kind="todo_comment", text=detail
        )
        return todo_id
    handle = _handle("quest", quest_id)
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=f"{handle}: planned lit-searches did not run — run them or fix why",
            meta={"content_key": UNRUN_SEARCHES_KEY, "quest_id": quest_id},
            parent_id=quest_id,
            conn=conn,
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=detail, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
    return int(ref.id)


__all__ = [
    "LINKABLE_KINDS",
    "LOCAL_ENOUGH",
    "LOCAL_KINDS",
    "MAX_JUDGMENTS_PER_TICK",
    "MAX_LINK_PER_QUERY",
    "MAX_QUERIES",
    "PENDING_READS_META_KEY",
    "SEEN_NOT_LINKED_CAP",
    "UNRUN_SEARCHES_KEY",
    "AcquiringSearch",
    "QueryReport",
    "SearchFn",
    "SearchQuery",
    "SearchStep",
    "apply_fetches",
    "apply_judgments",
    "apply_reads",
    "file_unrun_searches",
    "judged_out",
    "make_acquiring_search",
    "relevance_floor",
    "requested_fetches",
    "run_search_step",
]
