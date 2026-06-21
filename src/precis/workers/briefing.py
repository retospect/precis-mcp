"""briefing — the morning news digest, rebuilt on the ``news`` kind.

The successor to the retired ``generate_briefing.py`` from the
daily_briefing monolith. Instead of reading a bespoke ``news_items``
table and a DB-stored prompt, it:

1. pulls recent ``news`` refs (the last ~26h by default) via
   :meth:`Store.list_refs`;
2. formats their headlines + sources + links into an LLM context;
3. asks the summarizer alias (the cluster litellm proxy, reusing
   :class:`precis.workers.llm_summarize.LlmClient`) for a tight brief;
4. persists the brief itself as a pinned ``news`` ref slugged
   ``briefing-<date>`` and tagged ``briefing`` — so it is searchable,
   dated, and reread-able like any other ref. (Delivery to Discord via
   asa_bot is an optional follow-up; pass a ``sink`` callback to push it
   somewhere.)

Run via ``precis worker --only briefing`` or a scheduled cron tick.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.handlers.news import article_blocks
from precis.store import Store
from precis.workers.llm_summarize import LlmClient, LlmConfig

log = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_HOURS = 26  # 24h + 2h overlap (lifted from the old job)
_BRIEF_MAX_TOKENS = 1200
_MAX_ARTICLES = 200

_SYSTEM_PROMPT = (
    "You are a news editor writing a concise morning briefing. You are "
    "given a list of headlines gathered overnight from several sources. "
    "Group them into a few themed sections, lead each item with a crisp "
    "one-line summary, merge duplicates across sources, and skip filler. "
    "Be factual and neutral; do not invent details beyond the headlines. "
    "End with a one-line 'Worth watching' note. Use markdown headings."
)


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


def run_briefing(
    store: Store,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
    now: datetime | None = None,
    client: LlmClient | None = None,
    sink: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Generate (and persist) a morning briefing from recent news refs.

    Returns ``{articles, brief_chars, ref_id}``; ``articles == 0`` means
    no news in the window and no brief was written.

    ``client``/``now``/``sink`` are injectable for tests. ``sink`` (if
    given) receives the rendered brief text for external delivery
    (e.g. posting to Discord via asa_bot).
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

    llm = client or LlmClient(replace(LlmConfig.from_env(), max_tokens=_BRIEF_MAX_TOKENS))
    context = _format_context(refs)
    result = llm.complete(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Date: {now.date().isoformat()}. "
                    f"{len(refs)} headlines overnight:\n\n{context}"
                ),
            },
        ]
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

    if sink is not None:
        try:
            sink(brief)
        except Exception as exc:  # delivery is best-effort; never fail the job
            log.warning("briefing: sink delivery failed: %s", exc)

    log.info("briefing: %d articles → %d-char brief (%s)", len(refs), len(brief), slug)
    return {"articles": len(refs), "brief_chars": len(brief), "ref_id": ref.id}


__all__ = ["run_briefing"]
