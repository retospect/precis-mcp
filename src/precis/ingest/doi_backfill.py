"""DOI backfill for ``kind='paper'`` refs that carry none — recovers what
breaks the retraction-watch button (the Crossref check is keyed entirely
on ``ref_identifiers(id_kind='doi')``). Two phases, cheapest-and-most-
confident first:

* **Phase A — deterministic id path.** For cohort refs that carry an S2
  paper-id or arXiv id, batch-resolve via
  :func:`precis.ingest.semantic_scholar.get_papers_batch` and take the
  returned ``doi`` verbatim. High confidence: the id already names the
  exact paper, so no similarity gamble.
* **Phase B — title match.** For the rest, reuse
  :func:`precis.ingest.metadata_resolve._resolve_one`'s similarity/year/
  ownership GATING, but write ONLY the recovered DOI — never
  ``apply_resolution`` (that would also rewrite title/authors/cards,
  which a bare DOI recovery has no business touching).

Dry-run by default: ``apply=False`` does the network lookups and reports
what it WOULD write, writing nothing. Every write goes through
``store.set_ref_identifier``, which refuses to steal a DOI already owned
by a different live ref — checked ahead of the write too
(``store.identifier_owner``) so a collision routes to the review lane
instead of raising mid-batch.

Promoted from a scratch driver dry-run-validated against prod
(2026-08-12); ``batch_fn``/``crossref_fn``/``s2_fn`` are injectable so
this is unit-tested with no network, mirroring how
:func:`precis.ingest.metadata_resolve.resolve_triage` injects its
resolver callables.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from precis.errors import BadInput
from precis.ingest.crossref import lookup_crossref
from precis.ingest.metadata_resolve import CrossrefFn, S2Fn, _resolve_one
from precis.ingest.semantic_scholar import get_papers_batch, lookup_s2
from precis.store import Ref, Store

log = logging.getLogger(__name__)

#: Source tags stamped on writes — distinct per phase so a write is
#: reversible/auditable back to which recovery path produced it.
SOURCE_ID = "backfill-dois-s2id"
SOURCE_TITLE = "backfill-dois-title"

#: Batch-resolver callable — real client by default, injectable for tests.
BatchFn = Callable[[list[str], str], "list[dict[str, Any] | None]"]


def _is_real_doi(doi: str | None) -> bool:
    """A Crossref-usable publisher DOI — not arXiv's DataCite mint
    (``10.48550/arXiv.*``), which the retraction check (Crossref-keyed)
    can't resolve, so writing it as a ``doi`` is noise that never makes
    the paper checkable."""
    if not doi:
        return False
    return not str(doi).strip().lower().startswith("10.48550/arxiv")


@dataclass
class BackfillResult:
    """Counts + the per-lane detail for one :func:`backfill_dois` run."""

    cohort: list[int] = field(default_factory=list)
    #: Phase A: ``{ref_id: doi}`` recovered via the deterministic id path.
    recovered_id: dict[int, str] = field(default_factory=dict)
    #: Phase A: ref_ids whose deterministic DOI is owned by another live ref.
    id_owned_elsewhere: list[int] = field(default_factory=list)
    #: Phase A: ref_ids where the write itself raised something other than
    #: an ownership conflict (transient DB error etc.) — distinct from
    #: ``id_owned_elsewhere`` so the CLI never miscounts a real failure as
    #: a duplicate-DOI conflict.
    id_write_failed: list[int] = field(default_factory=list)
    #: Phase B: ``{ref_id: doi}`` recovered via the title-match track.
    recovered_title: dict[int, str] = field(default_factory=dict)
    #: Phase B: confident title match whose S2 record carries no real DOI
    #: (a preprint) — nothing to recover, not an error.
    arxiv_only: list[int] = field(default_factory=list)
    #: Phase B: ``(ref_id, reason)`` needing a human look.
    review: list[tuple[int, str]] = field(default_factory=list)

    @property
    def total_recovered(self) -> int:
        return len(self.recovered_id) + len(self.recovered_title)


def _cohort(
    store: Store, *, limit: int | None, ids: list[int] | None, order: str
) -> list[Ref]:
    """DOI-less paper refs worth attempting: an explicit ``ids`` set, or
    ``kind='paper'`` refs with no DOI that carry either a non-empty title
    or a recoverable id (s2/arxiv). ``order`` is ``desc`` (newest,
    worst-case preprints), ``asc`` (oldest, most likely to have acquired a
    DOI), or ``random`` (unbiased recovery-rate estimate).

    ``ids`` is filtered to live refs — :meth:`Store.fetch_refs_by_ids`
    defaults to ``include_deleted=True`` (built for link-rendering, which
    wants to show a tombstoned endpoint), which would otherwise let
    ``--apply --ids <tombstoned-ref>`` write a DOI onto a dead row; the
    SQL-scan branch below already excludes soft-deleted refs."""
    if ids:
        refs_map = store.fetch_refs_by_ids(ids, include_deleted=False)
        return [refs_map[i] for i in ids if i in refs_map]
    order_sql = {
        "desc": "ORDER BY r.ref_id DESC",
        "asc": "ORDER BY r.ref_id ASC",
        "random": "ORDER BY random()",
    }[order]
    sql = (
        "SELECT r.ref_id FROM refs r "
        "WHERE r.kind='paper' AND r.deleted_at IS NULL "
        "  AND NOT EXISTS (SELECT 1 FROM ref_identifiers d "
        "                  WHERE d.ref_id=r.ref_id AND d.id_kind='doi') "
        "  AND ( coalesce(btrim(r.title),'') <> '' "
        "        OR EXISTS (SELECT 1 FROM ref_identifiers x "
        "                   WHERE x.ref_id=r.ref_id "
        "                     AND x.id_kind IN ('s2','arxiv')) ) "
        + order_sql
        + (" LIMIT %s" if limit else "")
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql, ((limit,) if limit else ())).fetchall()
    ordered = [int(r[0]) for r in rows]
    refs_map = store.fetch_refs_by_ids(ordered)
    return [refs_map[i] for i in ordered if i in refs_map]


def _draft_cohort(store: Store, ident: str) -> list[Ref]:
    """The DOI-less source refs a draft actually cites — the exact set the
    retraction button walks (``cited_paper_refs`` renders the body), minus
    the ones that already have a DOI."""
    from precis.export.retraction import cited_paper_refs

    draft = store.get_ref(kind="draft", id=int(ident))
    if draft is None:
        log.warning("backfill-dois: draft %s not found", ident)
        return []
    refs, _unresolved = cited_paper_refs(store, draft)
    dois = store.dois_for_refs([r.id for r in refs])
    return [r for r in refs if r.id not in dois]


def _ident(store: Store, ref_id: int, kind: str) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers WHERE ref_id=%s AND id_kind=%s LIMIT 1",
            (ref_id, kind),
        ).fetchone()
    return str(row[0]) if row else None


def _request_id(store: Store, ref_id: int) -> str | None:
    """The S2 batch request id for a ref: its bare S2 paper-id if present,
    else the arXiv id in the lib's ``ARXIV:<id>`` prefixed form."""
    s2 = _ident(store, ref_id, "s2")
    if s2:
        return s2
    arxiv = _ident(store, ref_id, "arxiv")
    if arxiv:
        return f"ARXIV:{arxiv}"
    return None


def _phase_a(
    store: Store,
    refs: list[Ref],
    *,
    api_key: str,
    apply: bool,
    batch_fn: BatchFn,
) -> tuple[dict[int, str], list[int], list[int], list[int]]:
    """Deterministic id path. Returns ``(recovered {ref_id: doi}, owned
    [ref_id], write_failed [ref_id], remaining [ref_id])`` — ``remaining``
    = refs that got no DOI this way and fall through to the title track."""
    keyed: list[tuple[int, str]] = []
    for ref in refs:
        rid = _request_id(store, ref.id)
        if rid:
            keyed.append((ref.id, rid))
    recovered: dict[int, str] = {}
    owned_elsewhere: list[int] = []
    write_failed: list[int] = []
    if keyed:
        req_ids = [k[1] for k in keyed]
        metas = batch_fn(req_ids, api_key)
        for (ref_id, _rid), meta in zip(keyed, metas):
            doi = (meta or {}).get("doi")
            if not _is_real_doi(doi):
                continue
            owner = store.identifier_owner("doi", str(doi))
            if owner is not None and owner != ref_id:
                owned_elsewhere.append(ref_id)
                continue
            recovered[ref_id] = str(doi)
            if apply:
                try:
                    store.set_ref_identifier(ref_id, "doi", str(doi), source=SOURCE_ID)
                except BadInput:
                    # A live-owner conflict that only surfaced at write time
                    # (raced with the pre-check above) — same lane as a
                    # pre-checked collision, not a real failure.
                    log.info(
                        "backfill-dois: id-write #%s raced to a live owner", ref_id
                    )
                    recovered.pop(ref_id, None)
                    owned_elsewhere.append(ref_id)
                except Exception:
                    # Anything else (transient DB error etc.) is NOT a
                    # duplicate-DOI conflict — don't let it masquerade as
                    # one in the CLI's "owned by another ref" tally.
                    log.exception("backfill-dois: id-write #%s failed", ref_id)
                    recovered.pop(ref_id, None)
                    write_failed.append(ref_id)
    remaining = [r.id for r in refs if r.id not in recovered]
    return recovered, owned_elsewhere, write_failed, remaining


def _phase_b(
    store: Store,
    ref_ids: list[int],
    *,
    mailto: str,
    api_key: str,
    apply: bool,
    timeout: float,
    delay: float,
    crossref_fn: CrossrefFn,
    s2_fn: S2Fn,
) -> tuple[dict[int, str], list[int], list[tuple[int, str]]]:
    """Title track over the still-DOI-less refs — reuse ``_resolve_one``'s
    similarity/year/ownership gating, but write ONLY the recovered DOI
    (surgical: no title/author/card rewrite, unlike ``apply_resolution``).

    Returns ``(recovered {ref_id: doi}, arxiv_only [ref_id], review
    [(ref_id, reason)])``. ``arxiv_only`` = a confident title match whose
    S2 record carried no DOI (an unpublished preprint — nothing to hunt)."""
    refs_map = store.fetch_refs_by_ids(ref_ids)
    recovered: dict[int, str] = {}
    arxiv_only: list[int] = []
    review: list[tuple[int, str]] = []
    for ref_id in ref_ids:
        ref = refs_map.get(ref_id)
        if ref is None:
            continue
        try:
            res = _resolve_one(
                store,
                ref,
                mailto=mailto,
                s2_api_key=api_key,
                crossref_fn=crossref_fn,
                s2_fn=s2_fn,
                call_timeout=timeout,
            )
        except Exception:
            log.exception("backfill-dois: title #%s errored", ref_id)
            continue
        hit_net = res.track in ("doi", "title")
        if res.verdict == "auto" and _is_real_doi(res.doi):
            owner = store.identifier_owner("doi", str(res.doi))
            if owner is not None and owner != ref_id:
                review.append((ref_id, f"doi-owned-by-#{owner}"))
            else:
                recovered[ref_id] = str(res.doi)
                if apply:
                    try:
                        store.set_ref_identifier(
                            ref_id, "doi", str(res.doi), source=SOURCE_TITLE
                        )
                    except Exception:
                        log.exception("backfill-dois: title-write #%s failed", ref_id)
                        recovered.pop(ref_id, None)
                        review.append((ref_id, "write-failed"))
        elif res.verdict == "auto" and not _is_real_doi(res.doi):
            arxiv_only.append(ref_id)
        elif res.verdict == "review":
            review.append((ref_id, f"{res.reason} sim={res.sim}"))
        if delay > 0 and hit_net:
            time.sleep(delay)
    return recovered, arxiv_only, review


def backfill_dois(
    store: Store,
    *,
    apply: bool = False,
    limit: int | None = None,
    ids: list[int] | None = None,
    order: str = "desc",
    draft: str | None = None,
    mailto: str = "",
    s2_api_key: str = "",
    call_timeout: float = 20.0,
    delay: float = 0.5,
    do_id_phase: bool = True,
    do_title_phase: bool = True,
    batch_fn: BatchFn = get_papers_batch,
    crossref_fn: CrossrefFn = lookup_crossref,
    s2_fn: S2Fn = lookup_s2,
) -> BackfillResult:
    """Recover DOIs for the DOI-less paper cohort. ``apply=False``
    (default) plans without writing; ``apply=True`` writes.

    Cohort: ``draft`` (a draft's DOI-less cited refs) takes priority over
    ``ids`` (an explicit ref_id set), which takes priority over the
    ``limit``/``order`` SQL scan. ``do_id_phase``/``do_title_phase`` let a
    caller run just one phase (e.g. a cheap id-only pass)."""
    if draft is not None:
        refs = _draft_cohort(store, draft)
    else:
        refs = _cohort(store, limit=limit, ids=ids, order=order)

    result = BackfillResult(cohort=[r.id for r in refs])
    remaining = [r.id for r in refs]

    if do_id_phase:
        (
            result.recovered_id,
            result.id_owned_elsewhere,
            result.id_write_failed,
            remaining,
        ) = _phase_a(store, refs, api_key=s2_api_key, apply=apply, batch_fn=batch_fn)

    if do_title_phase:
        result.recovered_title, result.arxiv_only, result.review = _phase_b(
            store,
            remaining,
            mailto=mailto,
            api_key=s2_api_key,
            apply=apply,
            timeout=call_timeout,
            delay=delay,
            crossref_fn=crossref_fn,
            s2_fn=s2_fn,
        )

    return result


__all__ = ["BackfillResult", "_is_real_doi", "backfill_dois"]
