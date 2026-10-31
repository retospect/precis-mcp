"""``youtube_bulk`` job_type — a paced serial transcript fetch for many
video ids (gr531240).

The enqueued half of ``put(kind='youtube', args={'op': 'fetch_many',
'ids': [...]})``: one background job walks the list serially so an agent
needing N transcripts never fans out N parallel ``get`` calls against
YouTube's throttle (the filing case — 8 subagents burned the shared
``host:www.youtube.com`` limiter row in ~60 fetches).

Each id goes through the *same* ``YouTubeHandler.get`` cache path a
single interactive call takes — canonical key ``<video_id>:<langs>``,
fresh-hit short-circuit, ``_fetch`` on miss — so results land in the
youtube cache exactly as a single ``get`` would have stored them. A
fresh cache hit is recorded ``skipped`` rather than ``ok``, which is
what makes re-submitting a finished list a no-op for the ids already
done (idempotent resume; the job itself is not resumable mid-list but
the cache is the checkpoint).

Throttling is a per-id event, never a job failure: ``RateLimited`` (the
client's RequestBlocked/IpBlocked, or a limiter-row refusal — both
mapped by ``_fetch``) backs off ~60 s — the wait the refusal's own
``next=`` names — and retries the same id, up to ``_THROTTLE_RETRIES``
backoffs per id and ``_BACKOFF_BUDGET_S`` of sleeps job-wide; an id
still throttled past that is ``failed: rate_limited`` and the loop moves
on. ``drain_sleep`` is the wait so a worker drain wakes it early; a lost
lease stops the loop without a terminal status (``renew_own_lease``
each iteration — the ``embed_batch`` discipline — so a long list can't
be epoch/expiry-reclaimed mid-run).

Runs under ``job_inproc``: deterministic in-proc serial work with no
model and no special host capability — the ``news_poll``/``paper_pull``
shape. Outcome lands in ``meta.progress`` (live tallies), one
``job_result`` chunk with the per-id status map, and a ``job_summary``
line; an all-failures run is ``STATUS:failed`` (the ``paper_pull`` /
``pcb_review`` ruling — a quiet done must not read as success).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput, PrecisError, RateLimited
from precis.liveness import drain_sleep
from precis.utils.youtube_ids import extract_video_id, parse_languages
from precis.workers.executors.job_inproc import renew_own_lease
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

JOB_TYPE = "youtube_bulk"

#: The wait a youtube ``RateLimited`` refusal's own ``next=`` names
#: ("wait about 60 s, then retry"). Serial pacing alone does not unblock
#: a tripped IP block — the wait does.
_THROTTLE_BACKOFF_S = 60.0

#: Backoff retries per id before the id is recorded
#: ``failed: rate_limited`` and the loop moves on — one stubborn id
#: never wedges the list behind it.
_THROTTLE_RETRIES = 3

#: Total backoff seconds one job may spend across all ids; past it a
#: still-throttled id fails immediately. The alternative — every id
#: burning its own 3x60 s against a hard-blocked host — serialises the
#: failure over hours instead of failing fast and letting a re-submit
#: retry after the block lifts.
_BACKOFF_BUDGET_S = 1200.0

#: ``ids`` list ceiling — generous (the filing case was 1,328); the list
#: lives in ``meta.params``, so this also bounds the job row's meta size.
_MAX_IDS = 2000

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
        "languages": {"type": "string"},
    },
    "required": ["ids"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Serialised transcript fetch for a list of YouTube ids — minted by "
    "put(kind='youtube', args={'op': 'fetch_many', 'ids': [...]})."
)

#: Sentinel per-id status: the backoff wait woke on a worker drain —
#: stop the loop without a terminal status, like a cancel.
_DRAIN = "__drain__"

#: Cap on one recorded per-id failure reason — a ``next=``-carrying
#: ``PrecisError.cause`` can run to a paragraph.
_REASON_CAP = 160

#: Failure rows echoed in the ``job_summary`` tail; the full map lives
#: in the ``job_result`` chunk.
_FAILURE_LINES = 10


def parse_ids(raw: Any) -> tuple[list[str], list[str]]:
    """Canonicalise an ``ids`` list: extract each entry's 11-char video
    id (any accepted URL form), dedupe in order. Returns
    ``(clean, rejected)`` — rejected entries are non-empty strings that
    do not parse as a video id or URL."""
    clean: list[str] = []
    bad: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        s = str(item).strip()
        if not s:
            continue
        try:
            vid = extract_video_id(s)
        except BadInput:
            bad.append(s)
            continue
        if vid not in seen:
            seen.add(vid)
            clean.append(vid)
    return clean, bad


def lang_part_for(languages: Any) -> str:
    """The ``<langs>`` half of the canonical cache key for a ``languages``
    param — sorted, ``en`` default."""
    return "+".join(sorted(parse_languages(str(languages or ""))))


def idem_key(video_ids: list[str], lang_part: str) -> str:
    """Content-addressed dedupe: the same id set + language preference
    collapses onto one in-flight job. A terminal job never blocks a
    re-submit — the new run just skips the ids it already cached."""
    digest = hashlib.sha256("\n".join(sorted(video_ids)).encode("utf-8")).hexdigest()[
        :16
    ]
    return f"{JOB_TYPE}:{lang_part}:{digest}"


def _validated_ids(params: dict[str, Any]) -> list[str]:
    """Parse + bound ``params.ids``; raises ``BadInput`` on a malformed
    list. Shared by the submit gate (``validate``) and the dispatch
    re-check."""
    clean, bad = parse_ids(params.get("ids"))
    if bad:
        raise BadInput(
            f"youtube_bulk: {len(bad)} "
            f"entr{'y' if len(bad) == 1 else 'ies'} not parseable as a "
            "video id or URL: "
            + ", ".join(repr(b) for b in bad[:3])
            + ("…" if len(bad) > 3 else ""),
            next="params={'ids': ['<video id or URL>', ...]}",
        )
    if not clean:
        raise BadInput(
            "youtube_bulk needs params.ids — a non-empty list of video ids or URLs",
            next="params={'ids': ['dQw4w9WgXcQ', 'https://youtu.be/…', ...]}",
        )
    if len(clean) > _MAX_IDS:
        raise BadInput(
            f"youtube_bulk takes at most {_MAX_IDS} ids, got {len(clean)}",
            next="split the list across multiple fetch_many puts",
        )
    return clean


def validate(store: Any, params: dict[str, Any]) -> None:
    """``JobHandler.put``'s semantic gate (gr511724): a direct
    ``put(kind='job', job_type='youtube_bulk')`` bypasses the youtube
    handler's ``fetch_many`` checks, so the id list is re-parsed here —
    the same refusal the handler-side put raises."""
    del store
    _validated_ids(params)


def _fetch_one(
    ctx: DispatchContext,
    handler: Any,
    vid: str,
    languages: Any,
    counters: dict[str, float],
) -> str:
    """Fetch one id through the single-``get`` cache path; on
    ``RateLimited`` back off the hinted ~60 s and retry, bounded by
    ``_THROTTLE_RETRIES`` per id and ``_BACKOFF_BUDGET_S`` across the
    job. Returns the per-id status, or ``_DRAIN`` when the wait woke on
    a worker drain."""
    retries = 0
    while True:
        try:
            handler.get(id=vid, languages=languages)
            return "ok"
        except RateLimited:
            counters["backoffs"] += 1
            if (
                retries >= _THROTTLE_RETRIES
                or counters["backoff_s"] >= _BACKOFF_BUDGET_S
            ):
                ctx.append_chunk(
                    "job_event",
                    f"youtube_bulk: {vid} still throttled after {retries} "
                    "backoff(s) — recorded failed; a re-submit retries it",
                )
                return "failed: rate_limited"
            retries += 1
            ctx.append_chunk(
                "job_event",
                f"youtube_bulk: throttled on {vid} — backing off "
                f"{_THROTTLE_BACKOFF_S:g}s "
                f"(retry {retries}/{_THROTTLE_RETRIES})",
            )
            if drain_sleep(_THROTTLE_BACKOFF_S):
                return _DRAIN
            counters["backoff_s"] += _THROTTLE_BACKOFF_S
        except PrecisError as exc:
            return f"failed: {exc.cause[:_REASON_CAP]}"
        except Exception as exc:  # one id never sinks the job
            log.warning("youtube_bulk: %s raised %r", vid, exc, exc_info=True)
            return f"failed: {type(exc).__name__}"


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> dict[str, int] | None:
    del spec
    try:
        video_ids = _validated_ids(ctx.meta.get("params") or {})
    except BadInput as exc:
        ctx.record_failure(f"youtube_bulk: {exc.cause}", failure_class="input")
        return None
    languages = (ctx.meta.get("params") or {}).get("languages")
    lang_part = lang_part_for(languages)

    from precis.dispatch import Hub

    handler = Hub(store=ctx.store).sibling("youtube")

    total = len(video_ids)
    results: dict[str, str] = {}
    counters = {"backoffs": 0.0, "backoff_s": 0.0}
    ok = skipped = 0
    cancelled = False

    for vid in video_ids:
        if ctx.is_cancel_requested():
            cancelled = True
            break
        # The embed_batch discipline: a list longer than the lease
        # window renews each iteration; a lost lease means another
        # worker generation owns the job — stop without a terminal
        # status and let the new claimant drive it.
        if not renew_own_lease(ctx.store, ctx.ref_id, ctx.meta):
            ctx.append_chunk(
                "job_event",
                "youtube_bulk: lease lost mid-drain — another worker "
                "generation reclaimed this job; stopping without a "
                "terminal status",
            )
            return None
        # Fresh-cache probe over the same (provider, request_hash) the
        # get computes — a hit means this id's work is already done,
        # which is also what makes a re-submit a no-op for done ids.
        entry = ctx.store.get_cache_entry(
            provider=handler.provider,
            request_hash=handler._hash(f"{vid}:{lang_part}"),
        )
        if entry is not None and handler._is_fresh(entry[1]):
            status = "skipped"
            skipped += 1
        else:
            status = _fetch_one(ctx, handler, vid, languages, counters)
            if status == _DRAIN:
                cancelled = True
                break
            if status == "ok":
                ok += 1
        results[vid] = status
        ctx.set_meta(
            progress={
                "done": len(results),
                "total": total,
                "ok": ok,
                "skipped": skipped,
                "failed": sum(1 for s in results.values() if s.startswith("failed")),
                "backoffs": int(counters["backoffs"]),
            }
        )

    failed = {v: s for v, s in results.items() if s.startswith("failed")}
    unprocessed = total - len(results)
    ctx.append_chunk(
        "job_result",
        json.dumps(
            {
                "results": results,
                "ok": ok,
                "skipped": skipped,
                "backoffs": int(counters["backoffs"]),
                "backoff_s": counters["backoff_s"],
                "unprocessed": unprocessed,
            },
            sort_keys=True,
        ),
    )
    if ok == 0 and skipped == 0 and failed:
        # The paper_pull / pcb_review ruling: a run that landed nothing
        # is a recorded failure, not a quiet done.
        ctx.record_failure(
            f"youtube_bulk: every id failed ({len(failed)}): "
            + "; ".join(f"{v} {s}" for v, s in list(failed.items())[:3]),
            failure_class="infra",
        )
        return None
    summary = (
        f"{ok} fetched, {skipped} already cached, {len(failed)} failed "
        f"of {total} video(s); {int(counters['backoffs'])} throttle "
        f"backoff(s) ({counters['backoff_s']:g}s)"
    )
    if cancelled:
        summary = f"cancelled after {len(results)} — " + summary
    if unprocessed:
        summary += f"; {unprocessed} unprocessed — re-submit continues"
    if failed:
        shown = "; ".join(
            f"{v} — {s.removeprefix('failed: ')}"
            for v, s in list(failed.items())[:_FAILURE_LINES]
        )
        if len(failed) > _FAILURE_LINES:
            shown += f" (+{len(failed) - _FAILURE_LINES} more in job_result)"
        summary += f"\nfailures: {shown}"
    ctx.append_chunk("job_summary", summary)
    return {"ok": ok, "failed": len(failed), "skipped": skipped}


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
    "SPEC",
    "idem_key",
    "lang_part_for",
    "load",
    "parse_ids",
    "validate",
]
