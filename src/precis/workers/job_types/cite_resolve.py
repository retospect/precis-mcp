"""``cite_resolve`` job_type — "deep link these papers now" (gr513768
slice 7, the spec's on-request trigger).

The access trigger (``CITERES:pending`` on first ``get``) is the slow
burn; this job is the other documented door: a work order naming a set
of papers that resolves their in-text citations *immediately*, inside
the dispatch, rather than waiting for the dark pass's cadence. The
access queue is bypassed entirely — the job stamps each named paper's
attempt lease and runs :func:`precis.workers.cite_resolve.resolve_one_paper`
on it directly, so an on-request paper never waits behind
access-marked ones.

Per named paper, in order:

1. **``bib_parse`` bump** — only papers whose
   ``meta.bib_parse_version`` is behind the current
   :data:`~precis.workers.bib_parse.BIB_PARSE_VERSION` go through
   :func:`run_bib_parse_pass` with ``ref_ids`` restricted to the named
   set (the spec's one allowed bump path — an access mark never does
   this).
2. **``bib_mark``** — same restriction, so the paper's
   ``chunk_citations`` whitelist exists before the resolve leg reads
   it.
3. **Resolve** — :func:`resolve_one_paper` (OpenAlex leg → chunk sweep
   → outcome counts → passage sweep when that layer is lit).

The job reports progress on ``refs.meta.cite_resolve_progress``
(``done``/``total``/``eta_s`` — the spec's "reports an ETA") and
finishes with a per-paper ``{resolved, ambiguous, unheld}`` count on
``meta.cite_resolve`` plus a ``job_summary`` chunk — the spec's
acceptance record. A paper that throws is recorded ``failed`` and the
job continues with the next; only a *complete* failure set lands
``STATUS:failed``.

Runs under ``job_inproc`` only: the per-paper legs are the shared
worker functions, in-process, minutes-scale per paper. Minted
directly::

    put(kind='job', job_type='cite_resolve', executor='job_inproc',
        params={'ref_ids': [<pa ref_id>, ...]})

``precis-cite-resolve-help`` documents the recipe.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

JOB_TYPE = "cite_resolve"

#: One work order caps at 50 papers — the dispatch runs them
#: synchronously, and a bigger set belongs to several jobs (or to the
#: access trigger, which has no deadline at all).
_MAX_REF_IDS = 50

#: ``bib_mark`` batches per paper before giving up — a pathological
#: paper's body is bounded, but the loop guards anyway (one batch is
#: ~``batch_size`` chunks).
_BIB_MARK_MAX_BATCHES = 40
_BIB_MARK_BATCH = 500


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref_ids": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
            "minItems": 1,
            "maxItems": _MAX_REF_IDS,
        },
    },
    "required": ["ref_ids"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Deep-link a named set of papers: bump bib_parse/bib_mark for them, "
    "then resolve every in-text citation to a chunk→paper cites link "
    "(chunk→chunk where the passage layer is lit) — ahead of the access "
    "queue."
)


def idem_key(ref_ids: list[int]) -> str:
    """One deep-link work order per distinct paper set — a re-submit of
    the same set reuses the in-flight job instead of doubling it."""
    digest = hashlib.blake2b(
        ",".join(str(i) for i in sorted(set(ref_ids))).encode("utf-8"),
        digest_size=12,
    ).hexdigest()
    return f"{JOB_TYPE}:{digest}"


def validate(store: Store, params: dict[str, Any]) -> None:
    """``JobHandler.put``'s semantic gate: every named ref must be a live
    paper — refuse the whole submit with the bad ids named rather than
    landing a job that fails per-paper at dispatch."""
    ref_ids = [int(r) for r in params["ref_ids"]]
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, kind, retired_at IS NOT NULL FROM refs "
            "WHERE ref_id = ANY(%s)",
            (ref_ids,),
        ).fetchall()
    live_papers = {int(r[0]) for r in rows if str(r[1]) == "paper" and not r[2]}
    bad = [r for r in ref_ids if r not in live_papers]
    if bad:
        raise BadInput(
            f"cite_resolve takes live paper refs; not papers or retired: {bad}",
            next="params={'ref_ids': [<pa… ref ids>]} — get(kind='paper', "
            "id='pa<id>') confirms the ref before submitting",
        )


def _papers_needing_bib_parse(store: Store, ref_ids: list[int]) -> list[int]:
    """The named papers whose ``meta.bib_parse_version`` is behind the
    current version — the only ones the job bumps ``bib_parse`` for
    (spec slice 7: the bump path exists *only* for named papers that
    lack current entries)."""
    from precis.workers.bib_parse import _META_VERSION_KEY, BIB_PARSE_VERSION

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE ref_id = ANY(%(ids)s) AND ("
            "NOT (meta ? %(mk)s) OR COALESCE((meta->>%(mk)s)::int, 0) < %(ver)s"
            ")",
            {"ids": ref_ids, "mk": _META_VERSION_KEY, "ver": BIB_PARSE_VERSION},
        ).fetchall()
    return [int(r[0]) for r in rows]


def _bib_parse_client() -> Any:
    """The same SMALL-tier routed client ``cli/_worker_passes`` builds
    for the standing ``bib_parse`` pass (messy-line parse fallback +
    Crossref adjudication)."""
    import os

    from precis.utils.llm.router import DispatchClient, Tier

    return DispatchClient(
        tier=Tier.SMALL,
        model=os.environ.get("PRECIS_BIB_PARSE_MODEL") or "summarizer",
        max_tokens=int(os.environ.get("PRECIS_BIB_PARSE_MAX_TOKENS") or 2000),
        source="bib_parse",
        log_call=True,
        log_blobs=False,
    )


def _job_embedder() -> Any:
    """The passage layer's embedder — resolved only when that layer is
    lit; any build failure degrades to ``None`` (the sweep no-ops, the
    ref-level links still land)."""
    from precis.workers.cite_resolve import passage_enabled
    from precis.workers.embed import resolve_embedder

    if not passage_enabled():
        return None
    try:
        return resolve_embedder()
    except Exception:
        log.warning(
            "cite_resolve job: embedder unavailable — passage layer skipped",
            exc_info=True,
        )
        return None


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    from precis import settings
    from precis.workers.bib_mark import run_bib_mark_pass
    from precis.workers.bib_parse import run_bib_parse_pass
    from precis.workers.cite_resolve import resolve_one_paper

    params = ctx.meta.get("params") or {}
    ref_ids = [int(r) for r in params["ref_ids"]]
    total = len(ref_ids)
    email = (settings.get_str("contact.polite_email") or "").strip()
    embedder = _job_embedder()
    client: Any | None = None
    started = time.monotonic()

    results: dict[str, Any] = {}
    done = 0

    def _progress() -> None:
        elapsed = time.monotonic() - started
        eta = int(elapsed / done * (total - done)) if done else None
        ctx.set_meta(cite_resolve_progress={"done": done, "total": total, "eta_s": eta})

    _progress()
    for ref_id in ref_ids:
        if ctx.is_cancel_requested():
            break
        # 1 — the bib_parse bump, named papers only.
        try:
            if ref_id in _papers_needing_bib_parse(ctx.store, [ref_id]):
                if client is None:
                    client = _bib_parse_client()
                run_bib_parse_pass(
                    ctx.store,
                    client=client,
                    batch_size=1,
                    ref_ids=[ref_id],
                    mailto=email,
                )
            # 2 — bib_mark, same restriction.
            for _ in range(_BIB_MARK_MAX_BATCHES):
                r = run_bib_mark_pass(
                    ctx.store, batch_size=_BIB_MARK_BATCH, ref_ids=[ref_id]
                )
                if r["chunks_swept"] == 0 or r["failed"]:
                    break
            # 3 — the resolve leg (the job IS the high-priority claim).
            out = resolve_one_paper(ctx.store, ref_id, email=email, embedder=embedder)
            results[str(ref_id)] = {
                "resolved": out["resolved"],
                "ambiguous": out["ambiguous"],
                "unheld": out["unheld"],
            }
        except Exception as exc:
            log.warning("cite_resolve job: paper %s failed", ref_id, exc_info=True)
            results[str(ref_id)] = {"failed": str(exc)[:200]}
        done += 1
        _progress()
        if ctx.renew_lease is not None and not ctx.renew_lease():
            return  # another generation owns the row — stop without a status

    ok = sum(1 for r in results.values() if "failed" not in r)
    failed = sum(1 for r in results.values() if "failed" in r)
    cancelled = done < total
    lines = [
        f"cite_resolve deep-link: {ok}/{total} papers resolved"
        + (f", {failed} failed" if failed else "")
        + (f", {total - done} skipped (cancelled)" if cancelled else "")
    ]
    for rid, res in results.items():
        if "failed" in res:
            lines.append(f"  pa{rid}: failed — {res['failed']}")
        else:
            lines.append(
                f"  pa{rid}: resolved={res['resolved']} "
                f"ambiguous={res['ambiguous']} unheld={res['unheld']}"
            )
    ctx.append_chunk("job_summary", "\n".join(lines))
    ctx.set_meta(cite_resolve=results)
    if failed == total:
        ctx.record_failure("cite_resolve: every named paper failed")


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


__all__ = ["SPEC", "idem_key", "load", "validate"]
