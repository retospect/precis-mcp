"""``paper_ingest`` job_type — the deferred acquire for a get-asked DOI.

``get(kind='paper', id='<DOI>')`` on a well-formed DOI no live paper
carries used to answer a bare NotFound — gr551840: one probing client
re-asked the same DOI 30× in a day, every call a fresh session, every
answer an error. Reto ruling 2026-11-02: someone was interested, however
briefly — the get now mints this job instead of erroring, so the DOI
rides the same ingest path ``put(kind='paper', doi=…)`` uses
(``Store.upsert_stub_paper`` + ``Store.pin_stub_for_fetch`` → the
``fetch_oa`` worker), and the ack names the job instead of apologising.

Only single or batch paper gets without a view can mint this job.
An explicit view requests stored data, including default-renderer
aliases such as ``meta``, ``text``, ``body``, and ``full`` (gr715493).

The get does no network work and no fetch: minting the row is the whole
call — idem key ``paper_ingest:<canonical doi>`` makes repeat gets
dedupe onto the same row, and a terminal job inside
:data:`REQUEUE_WINDOW_HOURS` gates the re-mint (the "failed recently"
half of the ruling — the get ack reports that attempt instead of
re-spending the cascade).

Spend bounds (gr551840 review): the fetch cascade a minted stub joins
can end on the paid OpenAlex Content leg (~$0.01/file, gr494831), and
the idem key only dedupes ONE DOI's retries — N distinct probing or
hallucinated DOIs used to mint N jobs. Two caps bound that: a
fleet-wide rolling-24h cap on get-triggered mints
(:data:`GET_DOI_QUEUE_DAILY_DEFAULT`, env
:data:`GET_DOI_QUEUE_DAILY_ENV`) counted on the ``discovered_via='get'``
marker the mint stamps, and a per-batch cap the handler applies before
ever reaching here. A capped get answers the old not-ingested hint
naming ``put`` — the explicit mint stays unbounded.

Dispatch mirrors ``PaperHandler.acquire``'s DOI branch at store/ingest
level — the lint-imports contract forbids ``precis.workers →
precis.handlers``, so the S2 → registry verify tree is re-expressed here
rather than called. S2 first, then the DOI registries on a miss; a
resolver *fault* mints unverified (a fault is not evidence of a bad
DOI); a genuine miss everywhere fails the job — that failure is what
the get-side gate reports and what stops the hallucination from
littering the stub backlog. An S2 hit is also checked for the gr710828
poisoned cluster before its aliases are adopted: when the record would
hand the stub other ids (arXiv / S2 paperId) while the registry names a
different work, the S2 record is dropped and the registry record that
established the mismatch is reused as the DOI's own verification —
re-asking the registries can miss on a second pass and fail a job the
first pass confirmed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.errors import Conflict
from precis.store.types import Tag
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

JOB_TYPE = "paper_ingest"

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"doi": {"type": "string", "minLength": 7}},
    "required": ["doi"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Acquire one DOI-requested paper: verify it against Semantic Scholar "
    "and the DOI registries, mint/find its stub, pin it to the fetch_oa "
    "queue (the get-on-unheld-DOI deferred half, gr551840)."
)

#: Re-mint window on a terminal job (gr551840 ruling, "failed recently"):
#: mirrors ``fetch_oa._RETRY_WINDOW_HOURS`` — inside it the get ack
#: reports the last attempt's failure instead of re-queueing.
REQUEUE_WINDOW_HOURS = 24

#: Env pin + default for the fleet-wide rolling-24h cap on get-triggered
#: mints (gr551840 review — the paid-leg bound; see the module docstring).
#: Same env-override shape as ``handlers.structure``'s
#: ``PRECIS_STRUCT_NEB_QUEST_DAILY``.
GET_DOI_QUEUE_DAILY_ENV = "PRECIS_GET_DOI_QUEUE_DAILY"
GET_DOI_QUEUE_DAILY_DEFAULT = 50

#: The ``meta.discovered_via`` value a get-minted job carries — the daily
#: cap's marker (rhymes with the ``discovered-via:get`` tag the dispatch
#: stamps on the stub it mints).
GET_ORIGIN_VALUE = "get"

#: The marker an *explicit* get resubmit carries instead
#: (``args={'queue': 'explicit'}``, gr740575): a deliberate ask past the
#: cap is the same "explicit mint stays unbounded" lane
#: ``put(kind='paper', doi=…)`` rides, so it neither counts toward the
#: get-origin cap nor is refused by it.
GET_EXPLICIT_ORIGIN_VALUE = "get-explicit"

#: Advisory-lock idem key serialising the daily-cap count + mint across
#: distinct DOIs — the per-DOI idem lock alone would let a parallel
#: probing burst race the count past the cap.
_CAP_LOCK_IDEM = f"{JOB_TYPE}:get-daily-cap"


def _get_doi_queue_daily() -> int:
    raw = os.environ.get(GET_DOI_QUEUE_DAILY_ENV)
    try:
        return max(1, int(raw)) if raw else GET_DOI_QUEUE_DAILY_DEFAULT
    except ValueError:
        return GET_DOI_QUEUE_DAILY_DEFAULT


def idem_key(doi: str) -> str:
    return f"{JOB_TYPE}:{doi}"


def validate(store: Store, params: dict[str, Any]) -> None:
    """``JobHandler.put``'s semantic gate (gr511724): a direct job put
    skips the DOI normaliser the get path applies, so re-check the shape
    here — a DOI that can't normalise to ``10.<registrant>/<suffix>``
    can never run."""
    from precis.errors import BadInput
    from precis.identity import normalize_doi

    doi = normalize_doi(str(params.get("doi") or ""))
    if doi is None or not doi.startswith("10.") or "/" not in doi:
        raise BadInput(
            f"paper_ingest needs params.doi as a bare DOI (got {params.get('doi')!r})",
            next="params={'doi': '10.<registrant>/<suffix>'}",
        )


@dataclass(frozen=True)
class QueueOutcome:
    """What :func:`ensure_queued` decided for one DOI.

    ``queued`` — a fresh job row was minted. ``reused`` — a live job
    already carries the idem key (returned instead of queuing another).
    ``blocked`` — the newest job ended ``failed``/``cancelled`` inside
    :data:`REQUEUE_WINDOW_HOURS`; ``status``/``finished_at``/``error``
    describe that attempt so the caller can say so. ``capped`` — the
    fleet-wide daily cap on get-triggered mints is spent (no job named;
    the caller answers the plain not-ingested hint)."""

    state: str
    job_id: int | None
    status: str | None = None
    finished_at: str | None = None
    error: str | None = None


def _within_window(finished_at: str | None, *, now: datetime) -> bool:
    """The terminal stamp is inside :data:`REQUEUE_WINDOW_HOURS`."""
    if not finished_at:
        return False
    try:
        finished = datetime.fromisoformat(finished_at)
    except ValueError:
        return False
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=UTC)
    return now < finished + timedelta(hours=REQUEUE_WINDOW_HOURS)


def ensure_queued(store: Store, doi: str, *, explicit: bool = False) -> QueueOutcome:
    """Mint (or reuse) the ``paper_ingest`` job for ``doi``.

    One advisory-locked tx: the idem lock lands BEFORE any read
    (gr551840 review — the live-job and terminal-window checks used to
    run ahead of ``mint_job_once``'s own lock acquisition, leaving a
    race window where two concurrent gets could both pass a stale
    check). A live job under the idem key is returned (idempotent —
    the probe loop gr551840 answers stays one job no matter how often
    the get repeats); the newest job having ended ``failed``/
    ``cancelled`` inside :data:`REQUEUE_WINDOW_HOURS` blocks the
    re-mint; the fleet-wide daily cap refuses a fresh mint once
    get-triggered mints hit :func:`_get_doi_queue_daily` in the rolling
    24 h; anything else mints fresh, stamped ``discovered_via='get'``
    so the cap can count it.

    ``explicit=True`` (the caller resubmitted the get with
    ``args={'queue': 'explicit'}`` after the capped answer named that
    spelling — gr740575) is the deliberate lane the review left open
    for ``put``: it skips the daily-cap count/check and stamps
    :data:`GET_EXPLICIT_ORIGIN_VALUE` so the mint never counts toward
    the cap either. The reuse and recent-failure doors are unchanged —
    explicit only lifts the spend bound, not the per-DOI gates."""
    from precis.workers._mint import (
        active_job_id,
        idem_lock_key,
        latest_job_for_idem,
        meta_marked_mints_24h,
        mint_job_once,
    )

    idem = idem_key(doi)
    with store.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (idem_lock_key(idem),))
        live = active_job_id(conn, idem)
        if live is not None:
            return QueueOutcome("reused", live)
        last = latest_job_for_idem(conn, idem)
        if last is not None:
            job_id, status, finished_at, error = last
            if status in ("failed", "cancelled") and _within_window(
                finished_at, now=datetime.now(UTC)
            ):
                return QueueOutcome(
                    "blocked",
                    job_id,
                    status=status,
                    finished_at=finished_at,
                    error=error,
                )
        if not explicit:
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (idem_lock_key(_CAP_LOCK_IDEM),),
            )
            minted_24h = meta_marked_mints_24h(
                conn,
                job_type=JOB_TYPE,
                meta_key="discovered_via",
                meta_value=GET_ORIGIN_VALUE,
            )
            if minted_24h >= _get_doi_queue_daily():
                return QueueOutcome("capped", None)
        job_id, _reused = mint_job_once(
            store,
            conn=conn,
            job_type=JOB_TYPE,
            executor="job_inproc",
            title=f"ingest DOI {doi}",
            params={"doi": doi},
            idem_key=idem,
            meta_extra={
                "discovered_via": (
                    GET_EXPLICIT_ORIGIN_VALUE if explicit else GET_ORIGIN_VALUE
                )
            },
        )
        return QueueOutcome("queued", job_id)


def _s2_lookup(doi: str) -> dict[str, Any] | None:
    """S2 record for the DOI (propagates resolver faults — mirrors
    ``handlers.paper._lookup_acquire_metadata``; patched in tests)."""
    from precis.ingest.semantic_scholar import get_paper_by_id

    return get_paper_by_id(f"doi:{doi}", propagate=True)


def _registry_lookup(doi: str) -> dict[str, Any] | None:
    """Crossref/DataCite/JaLC record for the DOI (propagates registry faults —
    mirrors ``handlers.paper._lookup_doi_registry``; patched in tests)."""
    from precis import settings
    from precis.ingest.doi_registry import resolve_registry_doi

    return resolve_registry_doi(
        doi, mailto=settings.get_str("contact.crossref_mailto") or ""
    )


def _registry_disagreement(
    doi: str, meta: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Mirror of ``handlers.paper._registry_disagreement``: ``(registry
    record, its title)`` when the registry clearly names another work
    than ``meta``; ``(record, None)`` when it corroborates ``meta``;
    ``(None, None)`` when inconclusive (registry miss/fault, tokenless
    titles, unjudgeable bylines). The record returns with the verdict so
    the poisoned-cluster check's caller reuses it instead of re-asking
    the registries."""
    s2_title = str(meta.get("title") or "").strip()
    if not s2_title:
        return None, None
    try:
        reg = _registry_lookup(doi)
    except Exception:
        return None, None
    reg_title = str((reg or {}).get("title") or "").strip()
    if not reg_title:
        return reg, None
    from precis.ingest.verify_metadata import title_overlap
    from precis.utils.title_merge import content_tokens, first_author_agreement

    if not content_tokens(reg_title) or not content_tokens(s2_title):
        return reg, None
    if (
        title_overlap(reg_title, s2_title) > 0
        and first_author_agreement(meta.get("authors"), (reg or {}).get("authors"))
        is not False
    ):
        return reg, None
    return reg, reg_title


def _poisoned_s2_cluster_registry(
    meta: dict[str, Any], doi: str
) -> dict[str, Any] | None:
    """Mirror of ``handlers.paper._poisoned_s2_cluster_registry``: the
    registry record establishing that ``doi``'s S2 cluster belongs to
    another work (gr710828 — S2 echoes the DOI back onto a mis-merged
    cluster, and adopting that cluster's arXiv / S2 aliases would mint
    the poisoned alias set onto this DOI's stub), else ``None``."""
    if not (meta.get("arxiv_id") or meta.get("s2_id")):
        return None
    reg, disagree = _registry_disagreement(doi, meta)
    return reg if disagree is not None else None


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    """Verify the DOI, mint (or collapse onto) its stub, pin it to the
    ``fetch_oa`` queue. Mirrors ``PaperHandler.acquire``'s DOI branch at
    store level — see the module docstring for the verify tree."""
    params = ctx.meta.get("params") or {}
    doi = str(params.get("doi") or "").strip()
    if not doi:
        ctx.record_failure("paper_ingest: params.doi is empty", failure_class="input")
        return

    identifiers: list[tuple[str, str]] = [("doi", doi)]
    meta: dict[str, Any] | None = None
    registry_meta: dict[str, Any] | None = None
    resolver_err: Exception | None = None
    s2_meta: dict[str, Any] | None = None
    unverified = False
    stub_title: str | None = None
    year: int | None = None
    authors: Any = None

    try:
        meta = _s2_lookup(doi)
    except Exception as exc:
        resolver_err = exc
    poisoned_reg: dict[str, Any] | None = None
    if meta and (meta.get("arxiv_id") or meta.get("s2_id")):
        # gr710828, mirrored from acquire: S2 echoes the DOI back onto a
        # cluster that actually belongs to another work — adopt none of
        # its aliases, and reuse the registry record that established
        # the mismatch as the DOI's own verification (a second registry
        # ask can miss and fail a job the first ask confirmed).
        poisoned_reg = _poisoned_s2_cluster_registry(meta, doi)
        if poisoned_reg is not None:
            log.info(
                "paper_ingest: doi %s resolved to a mismatched S2 cluster "
                "(registry names %r) — using the registry record",
                doi,
                (poisoned_reg.get("title") or ""),
            )
            meta = None
    if meta:
        from precis.ingest.semantic_scholar import s2_stub_meta

        s2_meta = s2_stub_meta(meta, now=datetime.now(UTC))
        stub_title = (meta.get("title") or "").strip() or None
        raw_year = meta.get("year")
        year = int(raw_year) if isinstance(raw_year, int) else None
        authors = meta.get("authors")
        for kind_key, meta_key in (
            ("doi", "doi"),
            ("arxiv", "arxiv_id"),
            ("s2", "s2_id"),
        ):
            val = meta.get(meta_key)
            if val:
                pair = (
                    kind_key,
                    str(val).lower() if kind_key == "doi" else str(val),
                )
                if pair not in identifiers:
                    identifiers.append(pair)
    else:
        # S2 miss, fault, or poisoned cluster — the DOI registries
        # arbitrate (acquire's DOI rule: S2's index lags fresh records
        # and skips whole registries, so a clean miss still asks
        # Crossref/DataCite before refusing). A poisoned cluster already
        # stashed the registry record, so don't ask again here.
        if poisoned_reg is not None:
            registry_meta = poisoned_reg
            resolver_err = None
        else:
            try:
                registry_meta = _registry_lookup(doi)
            except Exception as exc:
                resolver_err = resolver_err or exc
        if registry_meta is not None:
            resolver_err = None
            stub_title = (registry_meta.get("title") or "").strip() or None
            raw_year = registry_meta.get("year")
            year = int(raw_year) if isinstance(raw_year, int) else None
            authors = registry_meta.get("authors")
            reg_doi = registry_meta.get("doi")
            if reg_doi:
                pair = ("doi", str(reg_doi).lower())
                if pair not in identifiers:
                    identifiers.append(pair)
        elif resolver_err is not None:
            unverified = True
        else:
            # A genuine miss everywhere — the DOI resolves nowhere.
            # Failing (not minting) keeps hallucinated DOIs out of the
            # stub backlog, and this terminal record is exactly what the
            # get-side gate reports during the requeue window.
            ctx.record_failure(
                f"paper_ingest: DOI {doi} did not resolve via Semantic "
                "Scholar or the DOI registries — likely hallucinated or "
                "mistyped",
                failure_class="input",
            )
            return

    from psycopg.types.json import Jsonb

    try:
        with ctx.store.tx() as conn:
            ref_id, created = ctx.store.upsert_stub_paper(
                identifiers=identifiers,
                title=stub_title,
                year=year,
                authors=authors,
                set_by="agent",
                s2_meta=s2_meta,
                conn=conn,
            )
            if created:
                ctx.store.add_tag(
                    ref_id,
                    Tag.open("discovered-via:get"),
                    set_by="system",
                    conn=conn,
                )
                if unverified:
                    ctx.store.add_tag(
                        ref_id,
                        Tag.open("acquire:unverified"),
                        set_by="agent",
                        conn=conn,
                    )
                    patch: dict[str, Any] = {"acquire_unverified": True}
                    if resolver_err is not None:
                        patch["acquire_unverified_reason"] = str(resolver_err)[:300]
                    conn.execute(
                        "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                        (Jsonb(patch), ref_id),
                    )
                if registry_meta is not None:
                    # The registry-verified stub carries its bibliographic
                    # fields on mint — same patch acquire applies (gr514384).
                    reg_patch: dict[str, Any] = {}
                    reg_source = str(registry_meta.get("source") or "")
                    if reg_source:
                        reg_patch["acquire_registry"] = reg_source
                    journal = registry_meta.get("journal")
                    if isinstance(journal, str) and journal.strip():
                        reg_patch["journal"] = journal.strip()
                    for key in ("record_type", "version", "publisher", "entry_type"):
                        val = registry_meta.get(key)
                        if isinstance(val, str) and val.strip():
                            reg_patch[key] = val.strip()
                    abstract = registry_meta.get("abstract")
                    if isinstance(abstract, str) and abstract.strip():
                        reg_patch["abstract"] = abstract.strip()
                    ctx.store.update_paper_fields(
                        ref_id,
                        authors=registry_meta.get("authors") or None,
                        meta_patch=reg_patch,
                        source=f"acquire:{reg_source or 'registry'}",
                        # ``paper_authors.source`` has no ``datacite`` tier —
                        # ``pdf`` is the sanctioned programmatic fallback.
                        authors_source=(
                            "crossref" if reg_source == "crossref" else "pdf"
                        ),
                        conn=conn,
                    )
            # Explicit semantics (restamp_min_hours=None): a get-asked DOI is
            # a "fetch NOW" — the job's own idem gate is the rate limit, so
            # the pin always re-arms one immediate fetch retry.
            ctx.store.pin_stub_for_fetch(ref_id, conn=conn, restamp_min_hours=None)

    except Conflict as exc:
        ctx.record_failure(f"paper_ingest {doi}: {exc}", failure_class="input")
        return

    state = "minted stub" if created else "already tracked"
    ctx.append_chunk(
        "job_summary",
        f"paper_ingest {doi}: {state} paper id=pa{ref_id}"
        + (" (unverified)" if unverified else "")
        + " — pinned to front of fetch queue",
    )
    ctx.set_meta(
        doi=doi,
        paper_ref_id=ref_id,
        stub_created=created,
        unverified=unverified,
    )


SPEC = JobTypeSpec(
    name=JOB_TYPE,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=None,
    dispatch=_dispatch,
    validate=validate,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "DESCRIPTION",
    "GET_DOI_QUEUE_DAILY_DEFAULT",
    "GET_DOI_QUEUE_DAILY_ENV",
    "JOB_TYPE",
    "REQUEUE_WINDOW_HOURS",
    "SPEC",
    "QueueOutcome",
    "ensure_queued",
    "idem_key",
    "load",
    "validate",
]
