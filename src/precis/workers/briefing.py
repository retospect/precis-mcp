"""briefing — the morning news digest, rebuilt on the ``news`` kind.

The successor to the retired ``generate_briefing.py`` from the
daily_briefing monolith. Instead of reading a bespoke ``news_items``
table and a DB-stored prompt, it:

1. pulls recent ``news`` refs (the last ~26h by default) via
   :meth:`Store.list_refs`;
2. formats their headlines + sources + links into an LLM context;
3. asks the cloud-super reasoning tier (via the LLM router — ADR 0046 — routed
   onto ``claude_agent`` / direct Anthropic OAuth, not the litellm proxy) for a
   tight brief;
4. persists the brief itself as a pinned ``news`` ref slugged
   ``briefing-<date>`` and tagged ``briefing`` — searchable, dated,
   reread-able like any other ref;
5. optionally **delivers** it to ``deliver_to`` (e.g. a Discord channel)
   by queuing a ``message`` ref — asa_bot (the one process with a Discord
   socket) posts it verbatim. Idempotent per brief-date.

Run via ``precis worker --only briefing`` or a scheduled cron tick.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import urllib.error
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.handlers.news import article_blocks
from precis.store import Store
from precis.store.types import BlockInsert
from precis.utils.llm.router import DispatchClient, DispatchError, Tier
from precis.utils.prompt import (
    AssemblyContext,
    Layer,
    LiteLLMAdapter,
    Module,
    Profile,
    assemble,
)

log = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_HOURS = 26  # 24h + 2h overlap (lifted from the old job)
_MAX_ARTICLES = 200
#: The pre-router-migration litellm ``max_tokens`` cap for this pass. The
#: ``claude_agent`` transport has no native completion-length flag (only
#: ``--max-turns`` / ``--max-budget-usd``), so this is now a best-effort
#: post-hoc truncation (see
#: :class:`~precis.utils.llm.router.ClaudeAgentProvider`) rather than a real
#: generation-time stop — but it keeps the daily brief bounded instead of
#: running unchecked to the $2 cost ceiling.
_BRIEF_MAX_TOKENS = 1200

# The briefing is a once-a-day call whose US section asks the model to
# separate operational signal from spectacle — analytically demanding, so it
# runs on the router's ``CLOUD_SUPER`` tier (opus-class reasoning), not the
# free ``LOCAL_SMALL`` tier the per-chunk glosses use (ADR 0046). A
# ``PRECIS_BRIEFING_MODEL`` override still wins — but it must now name a real
# model id the ``claude`` CLI accepts (e.g. ``claude-opus-4-8``), not the
# retired litellm ``claude-opus`` alias: routing folds through
# :func:`~precis.utils.llm.router.dispatch` onto ``claude_agent`` (direct
# Anthropic OAuth), so litellm is no longer in the loop for this pass.
# NB the bare ``opus`` alias was retired in the model-router consolidation —
# the (now-defunct, for this pass) litellm proxy 400s on unknown names, which
# silently failed every briefing job from 2026-07-04 until it was pinned to a
# served alias — the very fragility this router migration removes.

#: A single transient dispatch failure — a ``claude_agent`` subprocess hiccup,
#: a breaker-scoped pause, a dropped connection — must not silently lose a
#: whole day's briefing (the job just bubbles child-failed with no retry, and
#: the daily cron won't backfill a missed tick). Retry the completion a few
#: times with exponential backoff. A permanent 4xx from the (legacy, still
#: exercised by tests / a directly-injected client) litellm-shaped error is
#: re-raised on the first failure so real misconfiguration fails fast instead
#: of stalling on backoff.
_LLM_RETRY_ATTEMPTS = int(os.environ.get("PRECIS_BRIEFING_RETRY_ATTEMPTS") or 3)
_LLM_RETRY_BACKOFF_S = float(os.environ.get("PRECIS_BRIEFING_RETRY_BACKOFF_S") or 2.0)

_SYSTEM_PROMPT = (
    "You are a news editor writing a concise morning briefing. You are "
    "given a list of headlines gathered overnight from several sources. "
    "Group them into a few themed sections, lead each item with a crisp "
    "one-line summary, merge duplicates across sources, and skip filler. "
    "Be factual and neutral; do not invent details beyond the headlines. "
    "Each headline in the input carries its source URL — preserve it: render "
    "every kept item as a markdown link on its summary text ('[summary](url)') "
    "so each entry is clickable back to the source. When you merge duplicates, "
    "link to the most authoritative source. "
    "\n\n"
    "Always include a dedicated '## United States' section. US coverage now "
    "arrives as a high-volume flood of announcements, controversies, and "
    "reactions engineered to saturate attention ('flood the zone'). Handle "
    "it in two parts: (1) 'The noise' — one or two lines naming the "
    "dominant spectacle/outrage cycle of the day, without amplifying it; "
    "then (2) 'What the government is actually doing' — the operational "
    "signal: concrete executive orders and actions, agency and regulatory "
    "moves, appointments and personnel changes, budget and spending, "
    "legislation, and court rulings. Prioritize verifiable operational "
    "actions over rhetoric, and say plainly when a loud story is noise with "
    "no operational substance behind it. "
    "\n\n"
    "End with a one-line 'Worth watching' note. Use markdown headings."
)


def _briefing_user_block(ctx: AssemblyContext) -> str:
    """The VARIABLE (per-run) user turn — date + the overnight headlines."""
    e = ctx.extras
    return f"Date: {e['date']}. {e['count']} headlines overnight:\n\n{e['context']}"


#: The briefing prompt as an ordered module list (ADR 0038 step 2, helper
#: profile). CACHED (→ ``system``): the editor persona/instruction, stable
#: across runs. VARIABLE (→ ``user``): the dated headline context. Packaged
#: by the shared :class:`LiteLLMAdapter`, reusing the summarizer machinery.
_BRIEFING_MODULES: list[Module] = [
    Module(
        id="briefing.system",
        layer=Layer.CACHED,
        build=lambda _ctx: _SYSTEM_PROMPT,
    ),
    Module(
        id="briefing.user",
        layer=Layer.VARIABLE,
        build=_briefing_user_block,
    ),
]


def _build_briefing_messages(
    *, date: str, count: int, context: str
) -> list[dict[str, str]]:
    """Assemble the briefing chat messages via the shared assembler + adapter.

    Reproduces the hand-rolled ``[system, user]`` pair byte-for-byte: the
    ``_SYSTEM_PROMPT`` persona (CACHED → system) + the dated headline
    context (VARIABLE → user)."""
    ctx = AssemblyContext(
        store=None,
        ref_id=0,
        model="summarizer",
        profile=Profile.HELPER,
        extras={"date": date, "count": count, "context": context},
    )
    return LiteLLMAdapter.render(assemble(_BRIEFING_MODULES, ctx))


def _format_context(refs: list[Any], max_chars: int = 120_000) -> str:
    """Render recent news refs into an LLM context block."""
    lines: list[str] = []
    total = 0
    for ref in refs:
        url = (ref.meta or {}).get("url", "")
        source = (ref.meta or {}).get("source", "")
        when = ref.updated_at.strftime("%Y-%m-%d %H:%M UTC") if ref.updated_at else "?"
        entry = f"- [{source}] {ref.title} ({when}) {url}".rstrip()
        if total + len(entry) > max_chars:
            lines.append(f"\n[…{len(refs) - len(lines)} more headlines omitted]")
            break
        lines.append(entry)
        total += len(entry) + 1
    return "\n".join(lines)


def _is_transient_llm_error(exc: BaseException) -> bool:
    """Whether an ``llm.complete`` failure is worth retrying.

    Transient: a :class:`~precis.utils.llm.router.DispatchError` (the router's
    uniform error contract for a ``claude_agent`` subprocess hiccup or a
    breaker-scoped pause — it doesn't expose the old litellm-HTTP path's finer
    4xx/5xx distinction, so any dispatch failure is retried, bounded by
    ``attempts``) — plus, for a directly-injected legacy litellm-shaped client
    (still exercised by :mod:`tests.test_briefing_retry`), a dropped
    connection, a timeout, or a 5xx/429.
    Permanent: a legacy 4xx (a bad request / retired model alias won't fix
    itself) or a malformed-response ``RuntimeError`` — re-raised immediately so
    real misconfiguration fails fast instead of stalling on backoff.
    """
    if isinstance(exc, DispatchError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, urllib.error.URLError | ConnectionError | TimeoutError)


def _complete_with_retry(
    llm: Any,
    messages: list[dict[str, str]],
    *,
    attempts: int = _LLM_RETRY_ATTEMPTS,
    backoff_s: float = _LLM_RETRY_BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """``llm.complete`` with bounded exponential-backoff retries on transient
    proxy failures; permanent errors propagate on the first attempt."""
    for attempt in range(1, attempts + 1):
        try:
            return llm.complete(messages)
        except Exception as exc:
            if attempt >= attempts or not _is_transient_llm_error(exc):
                raise
            wait = backoff_s * (2 ** (attempt - 1))
            log.warning(
                "briefing: transient LLM error (attempt %d/%d), retrying in %.1fs: %s",
                attempt,
                attempts,
                wait,
                exc,
            )
            sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def run_briefing(
    store: Store,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
    now: datetime | None = None,
    client: Any = None,
    sink: Callable[[str], None] | None = None,
    deliver_to: str | None = None,
) -> dict[str, Any]:
    """Generate (and persist) a morning briefing from recent news refs.

    Returns ``{articles, brief_chars, ref_id}``; ``articles == 0`` means
    no news in the window and no brief was written.

    ``deliver_to`` is a delivery target (e.g. ``conv:discord/<g>/<c>/<t>``,
    preferred so the brief mirrors into the conv thread's history); when
    set, the brief is queued as a ``message`` ref (which fires
    ``pg_notify('precis.messages')``) so asa_bot posts it verbatim — the
    worker needs no transport socket of its own. Delivery is idempotent
    per brief-date, so a job retry can't double-post.
    ``client``/``now``/``sink`` are injectable for tests; ``sink`` (if
    given) also receives the brief text for custom delivery.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=lookback_hours)

    refs = store.list_refs(
        kind="news",
        updated_after=cutoff,
        order_by="updated_desc",
        limit=_MAX_ARTICLES,
    )
    # Don't fold prior briefings back into the next brief.
    refs = [r for r in refs if not (r.slug or "").startswith("briefing-")]

    if not refs:
        log.info("briefing: no news in the last %dh — nothing to brief", lookback_hours)
        return {"articles": 0, "brief_chars": 0, "ref_id": None}

    # Fold through the router (ADR 0046) instead of holding a raw litellm
    # client — so this cloud-tier call gets the budget breaker + the route-log
    # (llm_call_log starts capturing real data on this pass). tools_needed=True
    # lands on claude_agent (free-text final answer + system prompt honored,
    # no tools advertised since mcp_config is left unset) rather than the
    # tool-less claude_p judge shape, which would drop the system prompt and
    # demand a parseable JSON block this pass's prose brief never has.
    llm = client or DispatchClient(
        tier=Tier.CLOUD_SUPER,
        model=os.environ.get("PRECIS_BRIEFING_MODEL") or None,
        tools_needed=True,
        max_tokens=_BRIEF_MAX_TOKENS,
        source="briefing",
        log_call=True,
    )
    context = _format_context(refs)
    result = _complete_with_retry(
        llm,
        _build_briefing_messages(
            date=now.date().isoformat(), count=len(refs), context=context
        ),
    )
    brief = result.text.strip()

    date_tag = now.date().isoformat()
    slug = f"briefing-{date_tag}"
    request_hash = hashlib.sha256(f"briefing:{date_tag}".encode()).hexdigest()
    ref, _cache = store.put_cache_entry(
        kind="news",
        slug=slug,
        title=f"Morning briefing — {date_tag}",
        body_blocks=article_blocks(brief, embedder=None),
        provider="news",
        request_hash=request_hash,
        ttl_seconds=None,
        ref_meta={"briefing": True, "date": date_tag, "source": "briefing"},
        cache_meta={"articles": len(refs), "date": date_tag},
    )
    from precis.handlers._link_tag_ops import apply_tag_ops

    apply_tag_ops(
        store,
        "news",
        ref.id,
        tags=["briefing", "category:news", f"published:{date_tag}"],
        untags=None,
    )

    if deliver_to:
        _deliver(store, deliver_to, brief, date_tag)
    if sink is not None:
        try:
            sink(brief)
        except Exception as exc:  # delivery is best-effort; never fail the job
            log.warning("briefing: sink delivery failed: %s", exc)

    log.info("briefing: %d articles → %d-char brief (%s)", len(refs), len(brief), slug)
    return {"articles": len(refs), "brief_chars": len(brief), "ref_id": ref.id}


def _deliver(store: Store, target: str, brief: str, date_tag: str) -> None:
    """Queue the brief as one or more ``message`` refs for verbatim delivery.

    Mirrors ``MessageHandler.put``: insert a ``message`` ref + body chunk
    and fire ``pg_notify('precis.messages', {ref_id, target, author})``
    in the same tx (``author='asa'`` + ``meta.proactive=True`` so asa_bot
    can attribute the mirrored conv turn without an extra fetch), so
    asa_bot (the one process holding a Discord socket) posts
    it. The worker itself needs no socket — delivery is just a DB write.

    A brief routinely runs past Discord's 2000-char per-message limit;
    asa_bot posts each ``message`` verbatim (it does not chunk), so a long
    brief was cut off mid-URL, dropping the tail of the digest (gr51155).
    Split the brief into Discord-sized parts here (:func:`split_message`,
    on line/word boundaries so no markdown link is broken) and queue each
    part as its own ``message`` ref, in order, each with its own notify —
    Postgres delivers a single tx's notifications in send order, so a
    serial asa_bot listener posts the parts in sequence.

    Idempotent per brief-date: if *any* delivery message for ``date_tag``
    already exists, skip the whole set — so a job retry (or a same-day
    re-run) can't double-post. Best-effort: a failure is logged, never
    fatal (the brief is persisted as a ``news`` ref regardless)."""
    import json

    from precis.utils.msgsplit import split_message

    parts = split_message(brief)
    if not parts:
        return
    total = len(parts)
    try:
        with store.tx() as conn:
            existing = conn.execute(
                "SELECT 1 FROM refs WHERE kind = 'message' "
                "AND meta->>'briefing_date' = %s AND deleted_at IS NULL LIMIT 1",
                (date_tag,),
            ).fetchone()
            if existing is not None:
                log.info("briefing: %s already delivered — skipping", date_tag)
                return
            for i, part in enumerate(parts, start=1):
                suffix = f" ({i}/{total})" if total > 1 else ""
                meta = {
                    "target": target,
                    "status": "queued",
                    "reason": f"briefing {date_tag}{suffix}",
                    "briefing_date": date_tag,
                    "author": "asa",
                    "proactive": True,
                }
                if total > 1:
                    meta["briefing_part"] = i
                    meta["briefing_parts"] = total
                ref = store.insert_ref(
                    kind="message",
                    slug=None,
                    title=f"Morning briefing — {date_tag}{suffix}",
                    meta=meta,
                    conn=conn,
                )
                store.insert_blocks(
                    ref.id,
                    [
                        BlockInsert(
                            pos=0, text=part, meta={"chunk_kind": "message_body"}
                        )
                    ],
                    conn=conn,
                )
                conn.execute(
                    "SELECT pg_notify('precis.messages', %s)",
                    (
                        json.dumps(
                            {"ref_id": ref.id, "target": target, "author": "asa"}
                        ),
                    ),
                )
    except Exception as exc:
        log.warning("briefing: delivery to %s failed: %s", target, exc)


__all__ = ["run_briefing"]
