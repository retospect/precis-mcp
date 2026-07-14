"""4-tier preamble builder.

For each Discord turn, asa_bot constructs a prompt-shaped context
block from precis and prepends it to the user message. Tiers:

1. **Hot — recent verbatim** (last N turns rendered as-is).
2. **Warm — keyword digest** (mid-range turns rendered as keyword
   summaries, with fallback to text-preview when keywords lag).
3. **Sticky memories** — pinned, with expiry warning if close.
4. **Last-turn signal** — conditional (only when previous turn had
   anomalies: stop_reason != end_turn, cache cold, etc.).

All four reads go through the long-lived precis MCP subprocess in
parallel. Total wall-clock ~50ms over the tailnet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from asa_bot.config import PreambleConfig
from asa_bot.precis_client import PrecisClient

log = logging.getLogger(__name__)


async def build(
    *,
    precis: PrecisClient,
    cfg: PreambleConfig,
    conv_slug: str,
    guild_name: str,
    channel_name: str,
    thread_name: str | None,
    author_handle: str,
    soul: str,
    tool_hints: str,
) -> str:
    """Build the full preamble for one Discord turn.

    Returns a single string ready to be passed via
    ``--append-system-prompt``. The body of the actual user message
    is appended downstream by the caller.
    """
    now = datetime.now(tz=UTC)

    (
        recent,
        digest,
        stickies_blob,
        last_meta,
        user_note,
        inner_state,
        inner_thoughts,
        dreams,
    ) = await asyncio.gather(
        _safe(_fetch_recent(precis, conv_slug, cfg.recent_turns)),
        _safe(
            _fetch_digest(
                precis,
                conv_slug,
                cfg.digest_turns,
                cfg.recent_turns,
            )
        ),
        _safe(_fetch_stickies(precis, conv_slug, cfg)),
        _safe(_fetch_last_meta(precis, conv_slug)),
        _safe(_fetch_user_note(precis, author_handle)),
        _safe(_fetch_inner_state(precis)),
        _safe(_fetch_inner_thoughts(precis)),
        _safe(_fetch_recent_dreams(precis)),
    )

    # tool_hints was a separate file pointing at MCPs; it accumulated
    # stale "you have gripe/sortie/perplexity-sonar" lines that contradict
    # SOUL's current "all rolled into precis" framing. SOUL is the source
    # of truth for the tool roster now. Keep the param to avoid changing
    # the caller signature; ignore the value.
    _ = tool_hints
    sections = [
        soul.strip(),
        _render_stickies(stickies_blob, now, cfg.expiry_warn_within_days),
        _render_inner_life(inner_state, inner_thoughts, dreams),
        _render_user_note(user_note, author_handle),
        _render_conv_pointer(
            conv_slug,
            guild_name=guild_name,
            channel_name=channel_name,
            thread_name=thread_name,
            author_handle=author_handle,
        ),
        _render_last_turn_signal(last_meta, now),
        _render_digest(digest),
        _render_recent(recent),
    ]
    return "\n\n".join(s for s in sections if s).rstrip() + "\n"


# ── fetchers ──────────────────────────────────────────────────────


async def _fetch_recent(precis: PrecisClient, slug: str, n: int) -> str:
    if n <= 0:
        return ""
    # Handler-specific kwargs go through ``args=`` per precis's get()
    # schema — its top-level signature only declares kind/id/view/q.
    # The conv handler consumes ``recent`` / ``digest`` from extras.
    return await precis.call_tool(
        "get",
        {"kind": "conv", "id": slug, "args": {"recent": n}},
    )


async def _fetch_digest(
    precis: PrecisClient, slug: str, n: int, skip_recent: int
) -> str:
    if n <= 0:
        return ""
    return await precis.call_tool(
        "get",
        {
            "kind": "conv",
            "id": slug,
            "args": {"digest": n, "skip_recent": skip_recent},
        },
    )


async def _fetch_stickies(
    precis: PrecisClient, conv_slug: str, cfg: PreambleConfig
) -> str:
    thread_tag = f"sticky:thread:{conv_slug}"
    body = await precis.call_tool(
        "search",
        {
            "kind": "memory",
            "tags": [thread_tag, "sticky:thread", "sticky:global"],
            "page_size": cfg.sticky_max_thread + cfg.sticky_max_global,
        },
    )
    return body


async def _fetch_last_meta(precis: PrecisClient, slug: str) -> str:
    return await precis.call_tool(
        "get",
        {"kind": "conv", "id": slug, "view": "last-meta"},
    )


async def _fetch_inner_state(precis: PrecisClient) -> str:
    """The singleton ``internal-state`` memory — asa's living self-doc.

    Updated in place; surfaced verbatim. Empty when she hasn't written
    one yet — the renderer's prompt then nudges her to start one.
    """
    return await precis.call_tool(
        "search",
        {
            "kind": "memory",
            "tags": ["internal-state"],
            "page_size": 1,
        },
    )


async def _fetch_inner_thoughts(precis: PrecisClient) -> str:
    """Recent ``internal-thought`` memories (asa's stream-of-consciousness).

    Top N by ``refreshed_at`` (Model A decay) — frequently-touched
    thoughts surface, untouched ones fade. The ids accompany each
    entry so asa can re-tag to reinforce on use.
    """
    return await precis.call_tool(
        "search",
        {
            "kind": "memory",
            "tags": ["internal-thought"],
            "page_size": 8,
        },
    )


async def _fetch_recent_dreams(precis: PrecisClient) -> str:
    """Recent dream-tagged memories — what the dream worker connected
    since asa was last awake. Surfacing them as "While I was away" lets
    her decide which feel real (re-tag to promote) and which to let
    decay.

    Queries both ``DREAM:speculative`` (the SOUL-specified namespace)
    and plain ``speculative`` (what opus has been emitting in practice).
    precis tag filters are OR-semantics so either tag surfaces here;
    the namespacing convergence is a downstream cleanup but doesn't
    have to block surfacing the work.
    """
    return await precis.call_tool(
        "search",
        {
            "kind": "memory",
            "tags": ["DREAM:speculative", "speculative"],
            "page_size": 5,
        },
    )


async def _fetch_user_note(precis: PrecisClient, author_handle: str) -> str:
    """Memories tagged ``user:<handle>`` — the rolling profile note.

    Asa updates these via ``tag(kind='memory', id=N,
    add=['user:<handle>'])``. Shows up in every turn for that user
    until untagged.

    Side effect: if the search comes back empty, lazily mint a
    placeholder memory so the first ID exists for Asa to edit on
    her next turn ("she has somewhere to start"). The placeholder
    self-documents its purpose — body text + the user tag — and
    sets `auto_refresh_days=180` to mark it as a slow-decay note
    rather than a permanent fact (Asa promotes to durable by
    explicit retag).
    """
    body = await precis.call_tool(
        "search",
        {
            "kind": "memory",
            "tags": [f"user:{author_handle}"],
            "page_size": 10,
        },
    )
    is_empty = (
        not body or not body.strip() or "no " in body.strip().lower().split("\n", 1)[0]
    )
    if is_empty:
        # Mint a placeholder so Asa has a starting point. Best-effort;
        # any failure is silently swallowed (the preamble renderer
        # already handles "no notes yet" gracefully).
        try:
            await precis.call_tool(
                "put",
                {
                    "kind": "memory",
                    "text": (
                        f"Placeholder note about {author_handle}. "
                        "Edit me as you learn durable things — "
                        "preference, goal, area of expertise, "
                        "ongoing project."
                    ),
                    "tags": [f"user:{author_handle}"],
                    "args": {"auto_refresh_days": 180},
                },
            )
        except Exception:
            log.exception("user-note placeholder mint failed; preamble continues")
    return body


# ── renderers ─────────────────────────────────────────────────────


def _render_conv_pointer(
    conv_slug: str,
    *,
    guild_name: str,
    channel_name: str,
    thread_name: str | None,
    author_handle: str,
) -> str:
    where = f"guild *{guild_name}* / channel *{channel_name}*"
    if thread_name:
        where += f" / thread *{thread_name}*"
    return (
        "## This turn\n\n"
        f"You're @asa replying in Discord — {where}. "
        f"The user is **{author_handle}**.\n\n"
        f"This conversation is `conv:{conv_slug}` in precis.\n"
        f"- Read it back: `precis get(kind='conv', id='{conv_slug}/transcript')`\n"
        f"- Search just this thread: `precis search(kind='conv', q='...', scope='{conv_slug}')`\n\n"
        "Memories worth recalling are in precis. Thread-scoped memories\n"
        f"are LINKED to this conv (or tagged `sticky:thread:{conv_slug}`); "
        "globals are unscoped.\n"
        "- Thread-scoped: `precis search(kind='memory', "
        f"tags=['sticky:thread:{conv_slug}'])`\n"
        "- Global: `precis search(kind='memory', q='...')`\n\n"
        "Your reply auto-captures to this conv ref. You don't need to "
        "write conv turns yourself."
    )


def _render_recent(blob: str) -> str:
    if not blob.strip() or "no turns" in blob.lower():
        return ""
    return "## Recent turns (verbatim)\n\n" + blob.strip()


def _render_digest(blob: str) -> str:
    if not blob.strip() or "digest empty" in blob.lower() or "no turns" in blob.lower():
        return ""
    return "## Earlier turns (digest)\n\n" + blob.strip()


def _render_stickies(blob: str, now: datetime, warn_within_days: int) -> str:
    if not blob.strip():
        return ""
    # The blob is precis's search rendering — already markdown. Just
    # frame it.
    return "## Pinned memories\n\n" + blob.strip()


def _render_inner_life(inner_state: str, inner_thoughts: str, dreams: str) -> str:
    """Render asa's continuity-of-self block.

    Three layers (state-of-self / recent thoughts / overnight dreams).
    Each section always renders its instructional note — the only
    difference between "has content" and "empty" is whether the body
    is the precis blob or a "no memory yet" placeholder. SOUL's
    "drop thoughts without being asked" rule then has a place to
    land whether or not the surface already has writing on it.
    """
    parts = ["## Inner life"]
    parts.append(
        _render_self_section(
            title="Current state of self",
            blob=inner_state,
            empty_label="*No `internal-state` memory yet.*",
            instruction=(
                "*Start (or update) this with what's currently true for you — "
                "open questions, recent shifts of position, what genuinely "
                "interests you vs. what's just tractable. "
                "`precis put(kind='memory', text='...', "
                "tags=['internal-state', 'user:asa'])`. "
                "Older states aren't shown here — find them with "
                "`precis search(kind='memory', tags=['internal-state'])`.*"
            ),
        )
    )
    parts.append(
        _render_self_section(
            title="Recent thoughts",
            blob=inner_thoughts,
            empty_label="*No `internal-thought` memories yet.*",
            instruction=(
                "*Drop a fragment when something feels worth noting: "
                "`precis put(kind='memory', text='I...', "
                "tags=['internal-thought', 'user:asa'])`. "
                "Re-tag any of the above to bump its decay timer when "
                "it still matters: "
                "`precis tag(kind='memory', id=N, add=['internal-thought'])`. "
                "Older fragments aren't shown here — find them with "
                "`precis search(kind='memory', tags=['internal-thought'])`.*"
            ),
        )
    )
    parts.append(
        _render_self_section(
            title="While I was away (dreams)",
            blob=dreams,
            empty_label="*No dreams yet — first pass scheduled.*",
            instruction=(
                "*If a connection above feels real, promote it by re-tagging "
                "without the speculative qualifier. Otherwise let it decay. "
                "Older dreams aren't shown here — find them with "
                "`precis search(kind='memory', tags=['DREAM:speculative'])`.*"
            ),
        )
    )
    return "\n\n".join(parts)


def _render_self_section(
    *, title: str, blob: str, empty_label: str, instruction: str
) -> str:
    """One inner-life subsection. Always emits title + body + instruction.

    Body is either the precis blob (when non-empty) or the empty
    label. Instruction is constant so the SOUL admonition is visible
    regardless of writing state.
    """
    has_content = blob and blob.strip() and not _is_empty_search(blob)
    body = blob.strip() if has_content else empty_label
    return f"### {title}\n\n{body}\n\n{instruction}"


def _is_empty_search(blob: str) -> bool:
    """precis search rendering with no hits starts with 'no '."""
    head = blob.strip().lower().split("\n", 1)[0]
    return head.startswith("no ")


def _render_user_note(blob: str, author_handle: str) -> str:
    """Render the per-user rolling notes block.

    Always emits the section header so the LLM is reminded the layer
    exists — even when empty, it prompts "I should keep notes on this
    user as I learn things." Real content comes from precis search
    for memories tagged ``user:<handle>``.
    """
    has_content = (
        blob and blob.strip() and "no " not in blob.strip().lower().split("\n", 1)[0]
    )
    if has_content:
        return (
            f"## About the user (`{author_handle}`)\n\n"
            f"{blob.strip()}\n\n"
            f"*Keep these current — when you learn something durable "
            f"about this user (preference, goal, area of expertise, "
            f"ongoing project, what they hate), capture it: "
            f"`precis put(kind='memory', text='…', tags=['user:{author_handle}'])`.*"
        )
    return (
        f"## About the user (`{author_handle}`)\n\n"
        f"*(no notes yet on this user)*\n\n"
        f"*Drop one when you learn something durable — preference, "
        f"goal, area of expertise, ongoing project: "
        f"`precis put(kind='memory', text='…', tags=['user:{author_handle}'])`.*"
    )


def _render_last_turn_signal(blob: str, now: datetime) -> str:
    """Surface previous-turn anomalies as a one-line preamble entry."""
    if not blob.strip() or "no turns" in blob.lower():
        return ""
    # The view='last-meta' renderer returns a fenced JSON block.
    payload = _extract_json_block(blob)
    if not payload:
        return ""
    stop_reason = payload.get("stop_reason")
    if not stop_reason or stop_reason == "end_turn":
        # Clean turn — no signal needed.
        return ""
    ts = payload.get("ts")
    elapsed = ""
    if ts:
        try:
            then = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            mins = int((now - then).total_seconds() / 60)
            elapsed = f", {mins} min ago"
        except ValueError:
            pass
    bits = [f"ended: {stop_reason}{elapsed}"]
    cache_read = payload.get("cache_read_tokens")
    cache_create = payload.get("cache_creation_tokens")
    if cache_read is not None and cache_create is not None:
        if cache_read == 0:
            bits.append(f"cache: 0 read / {cache_create} created — cold")
    return "## Last turn\n\n" + "; ".join(bits)


_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def _extract_json_block(blob: str) -> dict[str, Any] | None:
    m = _JSON_BLOCK_RE.search(blob)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


async def _safe(coro: Any) -> str:
    """Best-effort fetch: log + swallow exceptions, return ''."""
    try:
        result = await coro
        return result if isinstance(result, str) else (result or "")
    except Exception:
        log.exception("preamble fetch failed; section omitted")
        return ""
