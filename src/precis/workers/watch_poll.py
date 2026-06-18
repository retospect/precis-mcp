"""watch_poll — citation-forward corpus growth (docs/design/watching.md).

The *watcher* is the second "attention actor" over the salience field the
dreamer already maintains. It grows the corpus along the citation graph
of the papers we actually engage with — no hand-picked seeds, no second
scorer.

Each pass:

1. selects the top-N most-due salient ``paper`` chunks
   (:meth:`Store.select_salient` with actor ``"watch"``), under
   :func:`as_background_actor` so the watcher's own reads don't self-heat
   the field (the shared echo-chamber guard);
2. for each seed paper, fetches **forward-citations** (papers that cite
   it) from Semantic Scholar via the existing
   :func:`precis.ingest.citations.citations` (``cited_by``);
3. mints each new citing paper as a metadata-only **stub**
   (:meth:`Store.upsert_stub_paper`, ``pdf_sha256 IS NULL``) — idempotent,
   so already-held/already-discovered papers are a no-op. New stubs are
   tagged ``source:semantic-scholar`` + ``discovered-via:cite:<seed>``;
4. rotates the seed (:meth:`Store.touch_attended` with ``"watch"``) so a
   *different* salient paper tops the next pass.

Acquisition is **not** this worker's job: a freshly-minted stub carries a
DOI/arXiv/S2 id, so the existing ``fetch_oa`` worker auto-claims it and
does OA-gated acquisition — an open-access copy is fetched, a paywalled
one stays a discovered stub ("get only if automatically gettable,
otherwise auto-discovered; fetch on demand"). The relevance gate is a
per-seed cap for v1; the embedding-similarity gate
(docs/design/watching.md, §Relevance) is a follow-up since it needs
on-the-fly embedding of external abstracts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from precis.store import Store, as_background_actor
from precis.store.types import Tag

log = logging.getLogger(__name__)

#: Cap on citing-paper stubs minted per seed per pass — the v1 relevance
#: gate. Bounds a seminal seed's citation firehose; dropped overflow is
#: logged, never silently truncated.
_DEFAULT_MAX_PER_SEED = 10

CitedByFetcher = Callable[[str], list[dict[str, Any]]]


def run_watch_pass(
    store: Store,
    *,
    limit: int = 8,
    max_per_seed: int = _DEFAULT_MAX_PER_SEED,
    api_key: str | None = None,
    fetch_cited_by: CitedByFetcher | None = None,
) -> dict[str, int]:
    """Poll forward-citations of the most-due salient papers; mint stubs.

    Returns the BatchResult shape ``{claimed, ok, failed}``:
    ``claimed`` = seed papers polled, ``ok`` = new stubs minted,
    ``failed`` = seeds whose S2 fetch raised.

    ``fetch_cited_by`` is injectable for tests; the default calls
    Semantic Scholar via :func:`precis.ingest.citations.citations`.
    ``api_key`` defaults to ``$SEMANTIC_SCHOLAR_API_KEY`` (S2 works
    keyless but rate-limited).
    """
    if api_key is None:
        api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    fetch = fetch_cited_by or _default_fetch_cited_by(api_key)

    claimed = 0
    minted = 0
    failed = 0

    # All reads in this block are the watcher's own — suppress self-heat
    # so polling a paper doesn't make it look more salient next pass.
    with as_background_actor("watch"):
        seed_chunks = store.select_salient("watch", kinds=("paper",), limit=limit)
        for chunk_id in seed_chunks:
            seed = _seed_for_chunk(store, chunk_id)
            if seed is None:
                # Chunk's ref vanished mid-pass; rotate so we don't spin.
                store.touch_attended("watch", [chunk_id])
                continue
            seed_ref_id, seed_slug, identifier = seed
            if identifier is None:
                # No usable id to query S2 with — rotate and move on.
                store.touch_attended("watch", [chunk_id])
                continue
            claimed += 1
            try:
                cited = fetch(identifier)
            except Exception:
                log.warning(
                    "watch_poll: cited_by fetch failed for paper #%d (%s)",
                    seed_ref_id,
                    identifier,
                    exc_info=True,
                )
                failed += 1
                store.touch_attended("watch", [chunk_id])
                continue

            if len(cited) > max_per_seed:
                log.info(
                    "watch_poll: paper #%d has %d citers; capping to %d "
                    "(dropped %d this pass)",
                    seed_ref_id,
                    len(cited),
                    max_per_seed,
                    len(cited) - max_per_seed,
                )
            for citing in cited[:max_per_seed]:
                if _mint_stub(store, citing, seed_slug=seed_slug):
                    minted += 1
            # Rotate the seed out so the next pass picks a different
            # salient paper — the dreamer's anti-repeat, reused.
            store.touch_attended("watch", [chunk_id])

    return {"claimed": claimed, "ok": minted, "failed": failed}


# ── helpers ────────────────────────────────────────────────────────


def _default_fetch_cited_by(api_key: str) -> CitedByFetcher:
    """Real S2 fetcher: forward-citations via the existing ingest client."""

    def _fetch(identifier: str) -> list[dict[str, Any]]:
        from precis.ingest.citations import citations

        return list(citations(identifier, api_key).get("cited_by", []))

    return _fetch


def _seed_for_chunk(
    store: Store, chunk_id: int
) -> tuple[int, str, str | None] | None:
    """Resolve a salient chunk to ``(paper_ref_id, label, s2_identifier)``.

    A paper's slug (``cite_key``) and its DOI/arXiv/S2 ids all live in
    ``ref_identifiers``, so one read yields both: ``label`` is the
    ``cite_key`` (falling back to the ref id) for the
    ``discovered-via:cite:<label>`` tag, and ``s2_identifier`` is the
    best id to hand Semantic Scholar — a DOI (``doi:<v>``), else an arXiv
    id (``arxiv:<v>``), else a bare S2 / corpus id, or ``None`` when the
    paper carries none. Returns ``None`` if the chunk's ref is gone.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id
              FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
             WHERE c.chunk_id = %s AND r.deleted_at IS NULL
            """,
            (chunk_id,),
        ).fetchone()
    if row is None:
        return None
    ref_id = int(row[0])
    by_scheme = {
        scheme.lower(): value
        for scheme, value, _source in store.list_ref_identifiers(ref_id)
    }
    label = by_scheme.get("cite_key") or str(ref_id)
    identifier: str | None = None
    if "doi" in by_scheme:
        identifier = f"doi:{by_scheme['doi']}"
    elif "arxiv" in by_scheme:
        identifier = f"arxiv:{by_scheme['arxiv']}"
    else:
        for scheme in ("s2", "corpusid", "s2_id", "paperid"):
            if scheme in by_scheme:
                identifier = by_scheme[scheme]
                break
    return ref_id, label, identifier


def _stub_identifiers(citing: dict[str, Any]) -> list[tuple[str, str]]:
    """Build ``(scheme, value)`` pairs for a citing paper's S2 record."""
    ids: list[tuple[str, str]] = []
    doi = citing.get("doi")
    if doi:
        ids.append(("doi", str(doi)))
    arxiv = citing.get("arxiv_id")
    if arxiv:
        ids.append(("arxiv", str(arxiv)))
    s2_id = citing.get("s2_id")
    if s2_id:
        ids.append(("s2", str(s2_id)))
    return ids


def _mint_stub(store: Store, citing: dict[str, Any], *, seed_slug: str) -> bool:
    """Find-or-mint a stub for one citing paper. Returns True if newly minted.

    Idempotent via :meth:`Store.upsert_stub_paper` (dedups on identifier),
    so re-discovering an already-held or already-discovered paper is a
    no-op. New stubs are tagged with provenance.
    """
    identifiers = _stub_identifiers(citing)
    if not identifiers:
        return False  # no id to dedup/fetch on — skip rather than dup
    title = (citing.get("title") or "").strip() or None
    year = citing.get("year")
    year_int = int(year) if isinstance(year, int) else None
    # ``set_by`` is FK-backed by the ``actors`` table and the
    # ``ActorSlug`` type admits only agent/user/system; the watcher is
    # server-side automation, so it writes as ``system``. Watch
    # provenance is carried by the tags below, not by set_by.
    ref_id, created = store.upsert_stub_paper(
        identifiers=identifiers,
        title=title,
        year=year_int,
        set_by="system",
    )
    if created:
        for value in ("source:semantic-scholar", f"discovered-via:cite:{seed_slug}"):
            store.add_tag(ref_id, Tag.open(value), set_by="system")
    return created


__all__ = ["run_watch_pass"]
