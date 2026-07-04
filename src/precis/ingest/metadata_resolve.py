"""PDF-free metadata re-resolution — Bucket B of the triage backlog.

`fix-metadata`/remediate re-derives from a PDF on disk, so a paper with a
missing/garbled PDF, or no DOI at all, dead-ends in `needs-triage`. This
resolves from what we ALREADY hold:

* **Track 1 — DOI in hand:** Crossref by the stored DOI (authoritative).
* **Track 2 — no DOI:** a Semantic Scholar *title search* (query title from
  `refs.title` when usable, else the first line of chunk 0) recovers a DOI
  + canonical metadata, gated on title similarity so a wrong hit can't
  overwrite. The recovered DOI is the prize — it makes the paper citable,
  fetchable, and identifier-dedup-eligible.

Trust: Track 1 auto-applies unless Crossref's own title is junk (book
front-matter → discard list). Track 2 auto-applies only at/above
``_AUTO_SIM`` with a compatible year; ``[_REVIEW_SIM, _AUTO_SIM)`` is
surfaced for review, never auto-written. Nothing is deleted here;
not-a-paper candidates (book cruft, held-without-chunks) are only flagged.

Reuses ``lookup_crossref`` / ``lookup_s2`` (both carry tenacity backoff),
``update_paper_fields`` + ``set_ref_identifier`` + ``rewrite_cards``, and
drops the ``needs-triage`` tag on a successful apply. Network-bound, so it
runs on-cluster; unit-tested with injected resolver fns.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any

from precis.ingest.cards import rewrite_cards
from precis.ingest.crossref import lookup_crossref
from precis.ingest.pdf_sidecar import is_garbage_title, is_pii
from precis.ingest.semantic_scholar import lookup_s2
from precis.store import Store, Tag
from precis.utils.authors import to_name_dicts

log = logging.getLogger(__name__)

#: Title-search similarity floors (pg_trgm), mirroring the dedup gates.
_AUTO_SIM = 0.85
_REVIEW_SIM = 0.6

TRIAGE_TAG = Tag.open("needs-triage")

#: Book front-matter DOIs (Elsevier `b<isbn>` chapter DOIs) whose Crossref
#: record is itself "Index" / "Dedication" — not a paper.
_BOOK_DOI_RE = re.compile(r"/b97[89]", re.IGNORECASE)

#: Resolver callables — real clients by default, injectable for tests.
CrossrefFn = Callable[[str, str], "dict[str, Any] | None"]
S2Fn = Callable[[str, str], "dict[str, Any] | None"]

#: Network guards. The Crossref/S2 clients carry their own tenacity backoff
#: (bounded — 5 attempts — so they can't loop forever), but under
#: rate-limiting a single call can still cost tens of seconds. A wall-clock
#: cap per call keeps one wedged lookup from stalling the batch, and a
#: politeness delay between papers spreads requests so we don't provoke the
#: 429s in the first place. Both env-tunable via the CLI.
_DEFAULT_CALL_TIMEOUT = 20.0
_DEFAULT_DELAY = 0.5
#: Sentinel: the network call exceeded its wall-clock budget.
_TIMED_OUT: Any = object()


def _bounded(fn: Callable[..., Any], *args: Any, timeout: float) -> Any:
    """Run ``fn(*args)`` with a wall-clock cap. Returns ``_TIMED_OUT`` if it
    overran (the underlying thread is abandoned, not force-killed — it dies
    on its own once the client's bounded retries give up)."""
    if timeout <= 0:
        return fn(*args)
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args)
    try:
        return fut.result(timeout=timeout)
    except FuturesTimeout:
        return _TIMED_OUT
    finally:
        ex.shutdown(wait=False)


@dataclass
class Resolution:
    """The verdict + recovered metadata for one triage paper."""

    ref_id: int
    verdict: str  # 'auto' | 'review' | 'discard' | 'miss'
    track: str  # 'doi' | 'title' | '-'
    reason: str
    title: str = ""
    author_dicts: list[dict[str, str]] = field(default_factory=list)
    year: int | None = None
    journal: str = ""
    abstract: str = ""
    doi: str | None = None
    arxiv: str | None = None
    sim: float | None = None

    def line(self) -> str:
        sim = f" sim={self.sim:.2f}" if self.sim is not None else ""
        got = f" -> {self.title[:48]}" if self.title else ""
        return f"[{self.verdict}:{self.track}] #{self.ref_id} {self.reason}{sim}{got}"


def _years_compatible(a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return True
    return abs(int(a) - int(b)) <= 1


def _query_title(store: Store, ref: Any) -> str | None:
    """A title to search S2 with: the stored title if it's usable, else
    the first substantial line of the first body chunk."""
    t = (ref.title or "").strip()
    if t and not is_garbage_title(t) and not is_pii(t):
        return t
    block = store.get_block(ref.id, pos=0)
    if block is None or not block.text:
        return None
    for raw in block.text.splitlines():
        line = raw.strip()
        # Strip trailing footnote/superscript digits a header often carries.
        line = re.sub(r"\s+\d+$", "", line).strip()
        if len(line) >= 12 and not is_garbage_title(line):
            return line
    return None


def _similarity(store: Store, a: str, b: str) -> float:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT similarity(%s, %s)", (a, b)).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _from_meta(ref_id: int, meta: dict[str, Any], *, track: str) -> Resolution:
    """Shape a resolver dict into a Resolution (verdict filled by caller)."""
    return Resolution(
        ref_id=ref_id,
        verdict="miss",
        track=track,
        reason="",
        title=(meta.get("title") or "").strip(),
        author_dicts=to_name_dicts(meta.get("authors") or []),
        year=meta.get("year"),
        journal=(meta.get("journal") or "").strip(),
        abstract=(meta.get("abstract") or "").strip(),
        doi=meta.get("doi"),
        arxiv=meta.get("arxiv_id"),
    )


def _resolve_one(
    store: Store,
    ref: Any,
    *,
    mailto: str,
    s2_api_key: str,
    crossref_fn: CrossrefFn,
    s2_fn: S2Fn,
    call_timeout: float = _DEFAULT_CALL_TIMEOUT,
) -> Resolution:
    rid = ref.id
    # Not-a-paper: a held-flag with no ingested body is a broken import.
    if ref.pdf_sha256 is not None:
        with store.pool.connection() as conn:
            has_body = conn.execute(
                "SELECT 1 FROM chunks WHERE ref_id=%s AND ord>=0 LIMIT 1", (rid,)
            ).fetchone()
        if has_body is None:
            return Resolution(rid, "discard", "-", "held-flag-without-chunks")

    stored_doi = _stored_doi(store, rid)

    # ── Track 1: resolve the stored DOI ──────────────────────────────
    if stored_doi:
        if _BOOK_DOI_RE.search(stored_doi):
            return Resolution(rid, "discard", "doi", "book-frontmatter-doi")
        meta = _bounded(crossref_fn, stored_doi, mailto, timeout=call_timeout)
        if meta is _TIMED_OUT:
            return Resolution(rid, "miss", "doi", "crossref-timeout")
        if meta is None:
            return Resolution(rid, "miss", "doi", "crossref-miss")
        res = _from_meta(rid, meta, track="doi")
        res.doi = res.doi or stored_doi
        if not res.title or is_garbage_title(res.title):
            res.verdict, res.reason = "discard", "resolved-title-junk"
            return res
        res.verdict, res.reason = "auto", "crossref-resolved"
        return res

    # ── Track 2: title search recovers a DOI ─────────────────────────
    qtitle = _query_title(store, ref)
    if not qtitle:
        return Resolution(rid, "miss", "title", "no-query-title")
    cand = _bounded(s2_fn, qtitle, s2_api_key, timeout=call_timeout)
    if cand is _TIMED_OUT:
        return Resolution(rid, "miss", "title", "s2-timeout")
    if cand is None or not (cand.get("doi") or cand.get("arxiv_id")):
        return Resolution(rid, "miss", "title", "s2-miss")
    res = _from_meta(rid, cand, track="title")
    if not res.title or is_garbage_title(res.title):
        return Resolution(rid, "discard", "title", "s2-title-junk")
    res.sim = _similarity(store, qtitle, res.title)
    years_ok = _years_compatible(ref.year, res.year)
    if res.sim < _REVIEW_SIM:
        res.verdict, res.reason = "miss", "below-review-threshold"
        return res
    if res.sim < _AUTO_SIM or not years_ok:
        res.verdict, res.reason = (
            "review",
            ("year-mismatch" if not years_ok else "low-similarity"),
        )
        return res
    # High-confidence — but a recovered DOI owned by another live ref means
    # this is a duplicate, not a metadata fix: hand to review, don't write.
    if res.doi:
        owner = store.identifier_owner("doi", res.doi)
        if owner is not None and owner != rid:
            res.verdict, res.reason = "review", f"recovered-doi-owned-by-#{owner}"
            return res
    res.verdict, res.reason = "auto", "s2-title-resolved"
    return res


def _stored_doi(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id=%s AND id_kind='doi' LIMIT 1",
            (ref_id,),
        ).fetchone()
    return str(row[0]) if row else None


def apply_resolution(
    store: Store, res: Resolution, *, source: str = "resolve-metadata"
) -> None:
    """Write a resolved paper's metadata + attach its DOI/arXiv + rebuild
    cards + drop the needs-triage tag, in one transaction."""
    meta_patch: dict[str, Any] = {}
    if res.abstract:
        meta_patch["abstract"] = res.abstract
    if res.journal:
        meta_patch["journal"] = res.journal
    with store.tx() as conn:
        store.update_paper_fields(
            res.ref_id,
            title=res.title or None,
            year=res.year,
            authors=res.author_dicts or None,
            meta_patch=meta_patch or None,
            source=source,
            conn=conn,
        )
        for scheme, value in (("doi", res.doi), ("arxiv", res.arxiv)):
            if value:
                store.set_ref_identifier(
                    res.ref_id, scheme, str(value), source=source, conn=conn
                )
        author_names = [a["name"] for a in res.author_dicts if a.get("name")]
        rewrite_cards(
            conn,
            res.ref_id,
            title=res.title,
            author_names=author_names,
            abstract=res.abstract,
            keywords=[],
        )
        store.remove_tag(res.ref_id, TRIAGE_TAG, conn=conn)


def _triage_refs(store: Store, limit: int | None) -> list[Any]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE r.kind='paper' AND r.deleted_at IS NULL "
            "  AND t.namespace='OPEN' AND t.value='needs-triage' "
            "ORDER BY r.ref_id" + (" LIMIT %s" if limit else ""),
            ((limit,) if limit else ()),
        ).fetchall()
    ids = [int(r[0]) for r in rows]
    refs_map = store.fetch_refs_by_ids(ids)
    return [refs_map[i] for i in ids if i in refs_map]


def resolve_triage(
    store: Store,
    *,
    apply: bool = False,
    limit: int | None = None,
    mailto: str = "",
    s2_api_key: str = "",
    call_timeout: float = _DEFAULT_CALL_TIMEOUT,
    delay: float = _DEFAULT_DELAY,
    crossref_fn: CrossrefFn = lookup_crossref,
    s2_fn: S2Fn = lookup_s2,
) -> list[Resolution]:
    """Resolve the needs-triage cohort. ``apply=False`` (default) plans
    without writing; ``apply=True`` writes the ``auto`` verdicts.

    ``call_timeout`` caps each network lookup; ``delay`` is a politeness
    pause between papers that actually hit the network (skipped for the
    no-network verdicts so a big discard set doesn't drag)."""
    results: list[Resolution] = []
    for ref in _triage_refs(store, limit):
        try:
            res = _resolve_one(
                store,
                ref,
                mailto=mailto,
                s2_api_key=s2_api_key,
                crossref_fn=crossref_fn,
                s2_fn=s2_fn,
                call_timeout=call_timeout,
            )
        except Exception:
            log.exception("resolve-metadata: ref #%s failed", ref.id)
            results.append(Resolution(ref.id, "miss", "-", "error"))
            continue
        if apply and res.verdict == "auto":
            try:
                apply_resolution(store, res)
            except Exception:
                log.exception("resolve-metadata: apply #%s failed", ref.id)
                res.verdict, res.reason = "review", "apply-failed"
        results.append(res)
        # Space out only the calls that touched the network (doi/title
        # tracks that weren't a pre-network skip like book-frontmatter-doi).
        hit_network = res.track in ("doi", "title") and not res.reason.startswith(
            "book-frontmatter"
        )
        if delay > 0 and hit_network:
            time.sleep(delay)
    return results


__all__ = ["Resolution", "apply_resolution", "resolve_triage"]
