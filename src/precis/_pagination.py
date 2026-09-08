"""Process-local pagination cache for MCP frame-size chunking.

The MCP stdio transport limits a single tool result to a frame
that's roughly 32 KiB on common clients. A ``get(kind='material')``
or ``search`` response large enough to overflow that frame would
fail to round-trip — handlers had no good way to detect or react.

This module gives the runtime a "chunk + cache" affordance: when
``dispatch_with_status`` builds a response larger than
``PRECIS_MAX_BODY_BYTES`` (default 24 KiB to leave headroom for
JSON framing), it asks ``PaginationCache.split`` to return the
head plus a cursor. The tail lives in this cache, keyed by cursor,
TTL-pruned. The agent retrieves the rest via the ``more`` MCP tool
which calls back into ``PaginationCache.pop``.

The cache is per-process, so a minted cursor is only ever
retrievable from *this same process* — fine for a long-lived server
(MCP `precis serve`, `precis repl`), fatal for a one-shot `precis
eval` invocation, which exits the moment it prints the head and
takes the cache with it. ``PaginationCache.split``'s
``cursor_capable=`` flag (wired from ``PrecisRuntime.long_lived``)
tells it which situation this call is in: when the caller can't
possibly come back for the cursor, ``split`` skips minting one
altogether and renders a different footer that says so instead of
handing out an instruction that can never be satisfied.

Splitting is textual, not structural — the renderer already emits
GitHub-flavoured Markdown, so we split on ``\\n## `` (H2 section)
boundaries. Sections fit inside a single chunk; only the boundary
between sections moves to the next page. Each page ends with a
loud ``more(cursor='...')`` footer so the agent knows the body is
incomplete and pagination is in flight.

The cache is per-process: a worker restart drops all cursors. The
agent's recovery is to re-issue the original call. Acceptable for
v1; revisit if cursor reuse across restarts becomes a real need.

Re-derivable cursors (gr330197)
--------------------------------
Under fleet load, the few-minute TTL above can lapse before a
queued follow-up ``more(cursor=...)`` call actually lands — the
agent then holds an unreadable page 1 with no recovery. For a
``get()`` call specifically (read-only, deterministic given the
same ``kind``/``id``/``view`` args — the case
:meth:`~precis.runtime.dispatch.DispatchMixin.dispatch_with_status`
opts into via the ``recipe=`` argument below), the cursor doesn't
have to be an opaque cache key: it can carry enough to *redo* the
call and re-derive the same page, turning an expired-cursor error
into a transparent retry.

``RecipeSeed`` is that self-describing payload — ``verb``, ``args``,
the page number the cursor should produce when redeemed, and a
``body_hash`` anchor (a hash of the *first* render's
``response.body``, computed by the dispatcher before hints/cost are
appended, so a hint-cooldown difference between the original render
and the replay can never look like drift). :func:`encode_recipe_cursor`
/ :func:`decode_recipe_cursor` (de)serialise it into the cursor
string itself (prefixed ``rr1.``, base64 JSON) — no server-side
lookup needed to recover it. :meth:`PaginationCache.split` still
caches the tail under that same string for the fast, common,
same-process case; the cursor only needs decoding when the cache
entry is actually gone.

The re-render + hash-compare + page reconstruction (via
:meth:`PaginationCache.render_recipe_page`) lives in
:meth:`~precis.runtime.dispatch.DispatchMixin.fetch_more`, not here —
this module has no access to the dispatcher needed to replay a call.
Restricted to ``verb == "get"`` by the dispatcher (enforced again on
decode, defensively, in case a cursor is tampered with): ``get()`` is
the one verb assumed side-effect-free and idempotent, so replaying it
from an opaque token carries no more risk than the agent re-issuing
it by hand — which was already the documented manual recovery this
automates. ``search()`` and everything else keeps the plain opaque
``uuid4`` cursor: too easy for ranking/embeddings/DB state to shift
between calls for a hash check to be a reliable "nothing changed"
signal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

#: Soft cap on rendered body bytes before chunking kicks in. Set
#: to a value safely under the MCP stdio frame ceiling (typically
#: ~32 KiB) so JSON framing + envelope have room. Tunable via the
#: ``PRECIS_MAX_BODY_BYTES`` env var for clients that tolerate
#: bigger frames.
DEFAULT_MAX_BODY_BYTES = 24576

#: How long a cursor lives in the cache: one hour, matching the agent
#: prompt-cache TTL, so a caller whose context is still warm can always
#: still drain its own pages. Was 5 minutes, which under fleet load
#: routinely expired mid-drain — the caller was then holding a page-1 it
#: could not continue (gr330197). Draining is not a tight loop: an agent
#: interleaves real work between pages, so the window has to cover a
#: working session, not a round-trip. Tunable via
#: ``PRECIS_PAGINATION_TTL_S``.
DEFAULT_TTL_SECONDS = 3600

#: Hard ceiling on number of pending cursors. Prevents an
#: adversarial caller from filling RAM with truncated bodies.
#:
#: Coupled to :data:`DEFAULT_TTL_SECONDS`: entries hold the whole
#: remaining body, and steady-state occupancy is arrival-rate × TTL, so
#: the 5-min → 1-hour change multiplies occupancy ~12×. Raised 256 → 1024
#: to keep the longer TTL meaningful — evicting a live cursor early is
#: the very failure the TTL change exists to stop, and eviction is by
#: soonest-expiry, so under pressure the oldest *valid* cursor dies. The
#: ceiling stays a real bound: 1024 × a ~50 KiB tail ≈ 50 MiB worst case.
DEFAULT_MAX_CURSORS = 1024

#: Marker the agent sees at the bottom of a chunked head. The
#: footer is appended *inside* the body (it's not metadata) so
#: existing rendering / logging paths see the pagination hint
#: without protocol changes.
#:
#: Deliberately loud: a terse "Next: more(...)" hint reads as
#: trailing noise, and consumers were treating a first-frame head
#: as a complete result and acting on it (e.g. a long YouTube
#: transcript summarised as if it ended mid-sentence). The footer
#: states, in order: that the body is *incomplete*, roughly how much
#: remains, the exact call to continue, and that this page must not
#: be mistaken for the whole result. ``{cursor}`` and the literal
#: ``more(cursor='...')`` call are preserved for the ``more`` tool.
#:
#: gr330197: the original wording forbade summarising/quoting/acting
#: on the content *at all* until every page was drained — a caller
#: whose cursor then expired under fleet load was left holding a
#: page-1 it was explicitly forbidden to use, with no escape hatch.
#: Softened to forbid the actual failure mode (treating a partial
#: page *as if it were complete*) rather than all use — a partial
#: excerpt, honestly labelled partial, is fine.
_FOOTER_TEMPLATE = (
    "\n\n---\n"
    "⚠️ **Truncated — this is NOT the complete result.** It was cut to fit the "
    "response frame; about {remaining} more follows on the next page. Call "
    "`more(cursor='{cursor}')` to fetch it, then keep following each page's "
    "cursor until no footer remains. Do not summarise, quote, or act on this "
    "page as if it were the complete result — a partial excerpt is fine as "
    "long as you say it's partial.\n"
)

#: Footer for a cursor-incapable (short-lived) caller — a one-shot
#: process (``precis eval``) whose :class:`PaginationCache` dies with
#: it before any ``more(cursor=...)`` retry could ever reach it. Points
#: at ``PRECIS_MAX_BODY_BYTES`` (the one lever that actually helps in
#: that situation) and a long-lived session instead of an instruction
#: that would just fail with "unknown cursor". Kept as its own
#: template (not a branch inside :data:`_FOOTER_TEMPLATE`) so neither
#: wording can drift onto the other's call site by accident.
_SHORT_LIVED_FOOTER_TEMPLATE = (
    "\n\n---\n"
    "⚠️ **Truncated — this is NOT the complete result.** It was cut to fit "
    "the response frame; about {remaining} more was cut and there is no way "
    "to retrieve it from here — this call is running in a one-shot process "
    "(e.g. `precis eval`) with no long-lived pagination cache, so a page-"
    "continuation cursor would die with the process before it could ever be "
    "redeemed; none is offered. To see more in one page, re-run with a "
    "higher `PRECIS_MAX_BODY_BYTES` (default "
    f"{DEFAULT_MAX_BODY_BYTES}) or a narrower query; for full pagination use "
    "a long-lived session (the MCP server, or `precis repl`).\n"
)

#: Optional trailing sentence appended after :data:`_FOOTER_TEMPLATE` when a
#: caller supplies ``alt_hint`` to :meth:`PaginationCache.split`. Some kinds
#: (e.g. ``skill``) support targeted section access that makes draining
#: every page wasteful — a full drain-before-acting warning is right for a
#: kind like ``youtube`` (a transcript genuinely needs full context) but
#: pure overhead for a skill body when one section would do. Kept as a
#: *separate* template (rather than folded into ``_FOOTER_TEMPLATE``) so
#: the no-hint case stays byte-identical to the pre-``alt_hint`` footer.
_ALT_HINT_SENTENCE_TEMPLATE = "If you only need part of this document: {alt_hint}\n"

#: Byte ceiling on ``alt_hint`` content (post-clamp). Bounds the footer-
#: reserve contribution below to a fixed constant regardless of what a
#: caller passes — see :func:`_clamp_text`.
_ALT_HINT_MAX_BYTES = 320

#: Trailing sentence appended after :data:`_FOOTER_TEMPLATE` /
#: :data:`_SHORT_LIVED_FOOTER_TEMPLATE` when the dispatcher can name the
#: ``kind`` that produced this body (gr330197). Whether or not a cursor
#: was minted, the underlying content is *already* indexed and directly
#: retrievable — a cursor expiring (or never having existed at all, in
#: the short-lived case) doesn't strand the reader the way the bare
#: "no such cursor" error used to. Kept as its own sentence (rather than
#: folded into either footer template) for the same reason as
#: ``alt_hint``: the no-``kind``-available case must stay byte-identical
#: to the pre-gr330197 footer.
_KIND_FALLBACK_SENTENCE_TEMPLATE = (
    "Also cached and searchable directly: "
    "search(kind='{kind}', q='<keywords>') skips paging entirely.\n"
)

#: Byte ceiling on the interpolated ``kind`` (post-clamp) — see
#: :func:`_clamp_text`. Registered kind names are short identifiers
#: (the longest today is ~20 bytes); 32 is generous headroom without
#: letting a future oddly-long kind name blow the reserve below, while
#: keeping this sentence's footer-reserve cost modest — it rides on
#: nearly every truncated response (any call with ``kind=``), unlike
#: ``alt_hint`` (a deliberate per-handler opt-in).
_KIND_FALLBACK_MAX_BYTES = 32


def _clamp_text(text: str | None, max_bytes: int) -> str | None:
    """Normalise/bound optional footer-sentence text to ``max_bytes``.

    Strips to ``None`` on empty input. Truncates on a UTF-8 char boundary
    (ellipsis included) so a reserve computed from a fixed-width
    placeholder is always a safe upper bound — a caller passing an
    unexpectedly long value degrades to a truncated one, not a frame
    overflow. Shared by :data:`_ALT_HINT_MAX_BYTES` (``alt_hint``) and
    :data:`_KIND_FALLBACK_MAX_BYTES` (``kind``).
    """
    if not text:
        return None
    value = text.strip()
    if not value:
        return None
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    # Reserve 3 bytes for the "…" marker, then walk back to a valid
    # UTF-8 char boundary (continuation bytes are 10xxxxxx).
    cut = max_bytes - 3
    while cut > 0 and (raw[cut] & 0xC0) == 0x80:
        cut -= 1
    return raw[:cut].decode("utf-8", errors="strict") + "…"


def _clamp_alt_hint(alt_hint: str | None) -> str | None:
    """Normalise/bound ``alt_hint`` so it can never blow the footer reserve.

    See :func:`_clamp_text`.
    """
    return _clamp_text(alt_hint, _ALT_HINT_MAX_BYTES)


def _clamp_kind(kind: str | None) -> str | None:
    """Normalise/bound ``kind`` so it can never blow the footer reserve.

    See :func:`_clamp_text`.
    """
    return _clamp_text(kind, _KIND_FALLBACK_MAX_BYTES)


def _build_footer(
    *, cursor: str, remaining: str, alt_hint: str | None, kind: str | None = None
) -> str:
    """Render the full pagination footer, with the optional sentences."""
    footer = _FOOTER_TEMPLATE.format(cursor=cursor, remaining=remaining)
    if alt_hint:
        footer += _ALT_HINT_SENTENCE_TEMPLATE.format(alt_hint=alt_hint)
    if kind:
        footer += _KIND_FALLBACK_SENTENCE_TEMPLATE.format(kind=kind)
    return footer


def _build_short_lived_footer(
    *, remaining: str, alt_hint: str | None, kind: str | None = None
) -> str:
    """Render the truncation footer for a cursor-incapable (short-lived)
    caller — see :data:`_SHORT_LIVED_FOOTER_TEMPLATE`."""
    footer = _SHORT_LIVED_FOOTER_TEMPLATE.format(remaining=remaining)
    if alt_hint:
        footer += _ALT_HINT_SENTENCE_TEMPLATE.format(alt_hint=alt_hint)
    if kind:
        footer += _KIND_FALLBACK_SENTENCE_TEMPLATE.format(kind=kind)
    return footer


def _human_bytes(n: int) -> str:
    """Render a byte count as a compact human-readable size.

    Bounded width by construction (it steps up to KB / MB), so it
    is safe to use in the footer-reserve upper bound below.
    """
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _max_body_bytes() -> int:
    """Resolve the soft cap from env at call time.

    Read on every check rather than once at import so operators
    can tune the cap without a process restart. The cost is a dict
    lookup per response — negligible against the rendering cost.
    """
    raw = os.environ.get("PRECIS_MAX_BODY_BYTES")
    if not raw:
        return DEFAULT_MAX_BODY_BYTES
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "PRECIS_MAX_BODY_BYTES=%r is not an int; using default",
            raw,
        )
        return DEFAULT_MAX_BODY_BYTES
    if value <= 0:
        return DEFAULT_MAX_BODY_BYTES
    return value


def _ttl_seconds() -> float:
    """Resolve cursor TTL from env at call time."""
    raw = os.environ.get("PRECIS_PAGINATION_TTL_S")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "PRECIS_PAGINATION_TTL_S=%r is not a float; using default",
            raw,
        )
        return DEFAULT_TTL_SECONDS
    if value <= 0:
        return DEFAULT_TTL_SECONDS
    return value


#: Prefix marking a cursor as a self-describing re-derivable recipe
#: rather than an opaque cache key — see the module docstring's
#: "Re-derivable cursors" section. Checked by
#: :func:`decode_recipe_cursor` and, on the dispatcher side, by
#: ``DispatchMixin.fetch_more`` to decide whether a cache-miss cursor
#: is worth attempting to replay at all.
_RECIPE_CURSOR_PREFIX = "rr1."


def hash_body(body: str) -> str:
    """SHA-256 hex digest of ``body``, used as the drift-detection
    anchor for a :class:`RecipeSeed` chain.

    Callers hash ``response.body`` specifically (not the full
    ``hints`` + ``cost``-appended render) — hints are best-effort,
    per-request, cooldown-deduped noise (:class:`precis.hints.HintBus`)
    that can legitimately differ between the original render and a
    later replay with nothing in the underlying content having
    changed; hashing them in would make drift detection cry wolf.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RecipeSeed:
    """Self-describing recipe for a re-derivable pagination cursor.

    ``page`` is the page number *this* cursor, once redeemed (via the
    ordinary in-process cache hit in :meth:`PaginationCache.pop`, or
    the re-render fallback in
    ``DispatchMixin.fetch_more``/:meth:`PaginationCache.render_recipe_page`
    when the cache entry is gone), must produce. ``body_hash`` is
    constant across an entire chain — see :func:`hash_body`.

    Passed into :meth:`PaginationCache.split` by the dispatcher only
    for a ``get()`` call (the one verb assumed side-effect-free and
    replayable); every other verb keeps the plain opaque ``uuid4``
    cursor.
    """

    verb: str
    args: dict[str, Any]
    body_hash: str
    page: int


def encode_recipe_cursor(recipe: RecipeSeed) -> str:
    """Serialise ``recipe`` into a cursor string.

    Raises (``TypeError``/``ValueError`` from ``json.dumps``) if
    ``recipe.args`` isn't JSON-safe — callers should catch this and
    fall back to a plain opaque cursor rather than propagate it;
    :meth:`PaginationCache.split` does exactly that.
    """
    payload = {
        "v": recipe.verb,
        "a": recipe.args,
        "p": recipe.page,
        "h": recipe.body_hash,
    }
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return _RECIPE_CURSOR_PREFIX + base64.urlsafe_b64encode(raw.encode("utf-8")).decode(
        "ascii"
    )


def decode_recipe_cursor(cursor: str) -> RecipeSeed | None:
    """Inverse of :func:`encode_recipe_cursor`.

    Returns ``None`` for a cursor that isn't ``rr1.``-prefixed (an
    ordinary opaque cursor, or garbage) or that fails to decode —
    never raises. Malformed/tampered input degrades to "not a
    recipe", which the dispatcher treats the same as any other unknown
    cursor.
    """
    if not cursor.startswith(_RECIPE_CURSOR_PREFIX):
        return None
    try:
        raw = base64.urlsafe_b64decode(
            cursor[len(_RECIPE_CURSOR_PREFIX) :].encode("ascii")
        )
        payload = json.loads(raw)
        return RecipeSeed(
            verb=str(payload["v"]),
            args=dict(payload["a"]),
            body_hash=str(payload["h"]),
            page=int(payload["p"]),
        )
    except Exception:
        return None


@dataclass(frozen=True, slots=True)
class _CachedTail:
    """One pending tail keyed by cursor.

    Holds the remaining body plus expiry. Kept private — callers
    interact via :class:`PaginationCache`'s public methods.
    """

    body: str
    expires_at: float
    #: The ``alt_hint`` the head page was split with, if any. Carried
    #: forward so a recursive re-split (tail still oversized) in
    #: :meth:`PaginationCache.pop` reuses the same hint on the next
    #: page's footer instead of silently dropping it.
    alt_hint: str | None = None
    #: The ``kind`` the head page was split with, if any. Carried
    #: forward the same way as ``alt_hint`` — see gr330197's
    #: search-fallback footer sentence.
    kind: str | None = None
    #: The recipe for *this* tail's own next cursor, if the chain is
    #: re-derivable. ``None`` for an ordinary opaque-cursor page.
    #: Carried forward through :meth:`PaginationCache.pop` so the
    #: re-derivable chain doesn't collapse into an opaque cursor the
    #: first time a page happens to be served from cache.
    recipe: RecipeSeed | None = None


class PaginationCache:
    """Thread-safe pagination cache for chunked responses.

    Constructed once per :class:`PrecisRuntime`; the runtime calls
    :meth:`split` on every outbound body. The ``more`` MCP tool
    calls :meth:`pop` to retrieve the next page.

    Eviction policy: TTL pruned on every operation. If the
    cursor-count ceiling is hit, the oldest entries (smallest
    ``expires_at``) drop first.
    """

    def __init__(
        self,
        *,
        max_cursors: int = DEFAULT_MAX_CURSORS,
    ) -> None:
        self._entries: dict[str, _CachedTail] = {}
        self._lock = Lock()
        self._max_cursors = max_cursors

    def _now(self) -> float:
        return time.monotonic()

    def _prune_expired(self) -> None:
        """Drop expired entries. Caller must hold ``self._lock``."""
        now = self._now()
        expired = [c for c, e in self._entries.items() if e.expires_at <= now]
        for cursor in expired:
            self._entries.pop(cursor, None)

    def _maybe_evict_oldest(self) -> None:
        """Drop the oldest entry if we're over the cursor ceiling.

        Caller must hold ``self._lock``. Cheap O(n) scan; n is at
        most :data:`DEFAULT_MAX_CURSORS`.
        """
        if len(self._entries) < self._max_cursors:
            return
        oldest = min(self._entries.items(), key=lambda item: item[1].expires_at)
        self._entries.pop(oldest[0], None)
        log.info(
            "pagination cache full; evicted oldest cursor %r",
            oldest[0],
        )

    def split(
        self,
        body: str,
        *,
        alt_hint: str | None = None,
        cursor_capable: bool = True,
        kind: str | None = None,
        recipe: RecipeSeed | None = None,
    ) -> tuple[str, str | None]:
        """Split ``body`` into a head + cached tail if oversized.

        Returns ``(head, cursor)``. When ``body`` fits inside
        :func:`_max_body_bytes`, returns ``(body, None)`` and
        nothing is cached. When ``body`` is too large, returns
        ``(head_with_footer, cursor)`` and caches the remaining
        text under ``cursor``.

        Splitting is greedy: take the largest run of sections that
        fits the limit (with footer-space reserved), keep the rest
        for the next page. Sections shorter than the limit go to
        one page; a single section longer than the limit falls
        through to a paragraph split, then a hard byte split.

        ``alt_hint``, when given, is appended to the footer as a
        one-sentence pointer to a cheaper alternative to draining
        every page (e.g. a skill's targeted-section access). Omitting
        it (the default) leaves the footer byte-identical to before
        this parameter existed — see :func:`_clamp_alt_hint` for how
        it's bounded so it can't blow the footer reserve.

        ``cursor_capable=False`` (wired from ``PrecisRuntime.long_lived``
        being ``False``) is for a caller whose process exits before a
        ``more(cursor=...)`` retry could ever reach this cache — a
        one-shot ``precis eval`` invocation. In that mode the overflow
        is still computed and truncated, but the tail is discarded
        rather than cached, no cursor is minted (the return is always
        ``(head, None)``), and the footer points at
        ``PRECIS_MAX_BODY_BYTES`` / a long-lived session instead of an
        unsatisfiable cursor instruction — see
        :data:`_SHORT_LIVED_FOOTER_TEMPLATE`.

        ``kind``, when given, is appended to the footer as a one-sentence
        pointer at ``search(kind=..., q=...)`` as a cached-content
        fallback (gr330197) — see :data:`_KIND_FALLBACK_SENTENCE_TEMPLATE`.

        ``recipe``, when given (only for a ``get()`` call — see
        :class:`RecipeSeed`), makes the minted cursor self-describing
        instead of an opaque ``uuid4`` key, so it survives falling out
        of this cache entirely (TTL expiry under fleet load, gr330197)
        — ``DispatchMixin.fetch_more`` can decode it and re-render the
        page from scratch. Falls back to an opaque cursor if encoding
        ``recipe`` fails (e.g. non-JSON-safe args) rather than raising.
        """
        alt_hint = _clamp_alt_hint(alt_hint)
        kind = _clamp_kind(kind)
        cap = _max_body_bytes()
        if len(body.encode("utf-8")) <= cap:
            return body, None

        # A recipe cursor's *content* (verb/args/page/body_hash) never
        # depends on where the split boundary ends up landing — encode
        # it up front so its (possibly much-longer-than-uuid4) byte
        # length can be reserved for accurately below, instead of
        # discovering after the fact that a large ``args`` payload blew
        # the frame cap.
        candidate_cursor: str | None = None
        candidate_recipe: RecipeSeed | None = None
        if cursor_capable and recipe is not None:
            try:
                candidate_cursor = encode_recipe_cursor(recipe)
                candidate_recipe = RecipeSeed(
                    verb=recipe.verb,
                    args=recipe.args,
                    body_hash=recipe.body_hash,
                    page=recipe.page + 1,
                )
            except Exception:
                log.debug(
                    "recipe cursor encoding failed; minting an opaque cursor instead",
                    exc_info=True,
                )
        cursor_len = (
            len(candidate_cursor.encode("utf-8"))
            if candidate_cursor is not None
            else None
        )

        head, tail = _greedy_split(
            body,
            cap,
            alt_hint=alt_hint,
            kind=kind,
            cursor_capable=cursor_capable,
            cursor_len=cursor_len,
        )
        if not tail:
            # Body fits after all (multi-byte UTF-8 made the
            # initial check pessimistic). No cursor needed.
            return body, None

        remaining = _human_bytes(len(tail.encode("utf-8")))

        if not cursor_capable:
            footer = _build_short_lived_footer(
                remaining=remaining, alt_hint=alt_hint, kind=kind
            )
            return head + footer, None

        cursor = candidate_cursor if candidate_cursor is not None else uuid.uuid4().hex
        stored_recipe = candidate_recipe

        footer = _build_footer(
            cursor=cursor, remaining=remaining, alt_hint=alt_hint, kind=kind
        )
        head_with_footer = head + footer

        with self._lock:
            self._prune_expired()
            self._maybe_evict_oldest()
            self._entries[cursor] = _CachedTail(
                body=tail,
                expires_at=self._now() + _ttl_seconds(),
                alt_hint=alt_hint,
                kind=kind,
                recipe=stored_recipe,
            )
        return head_with_footer, cursor

    def pop(self, cursor: str) -> str | None:
        """Retrieve and remove the tail for ``cursor``.

        Returns the cached tail, possibly re-split if it's still
        too big (recursive cursor — the new cursor is in the
        body's footer). Returns ``None`` when the cursor is
        unknown or expired; the ``more`` tool surfaces that as a
        clean error to the agent (or, for a re-derivable ``get()``
        cursor, falls back to ``DispatchMixin``'s re-render path
        instead of erroring — see the module docstring).

        Pops the entry: a cursor is single-use. The agent that
        needs to re-read must hold onto the body it received.
        """
        with self._lock:
            self._prune_expired()
            entry = self._entries.pop(cursor, None)
        if entry is None:
            return None
        if entry.expires_at <= self._now():
            return None
        # Recursive split: the tail may itself overflow. Reuse the
        # original page's alt_hint/kind/recipe so none of them
        # silently vanish after the first ``more()`` call.
        head, _maybe_next_cursor = self.split(
            entry.body, alt_hint=entry.alt_hint, kind=entry.kind, recipe=entry.recipe
        )
        return head

    def render_recipe_page(
        self,
        body: str,
        page: int,
        *,
        alt_hint: str | None,
        kind: str | None,
        verb: str,
        args: dict[str, Any],
        body_hash: str,
    ) -> str:
        """Rebuild page ``page`` (1-based) of a freshly re-rendered ``body``.

        Used only by ``DispatchMixin.fetch_more``'s recipe-cursor
        fallback, after it has already re-run the original ``get()``
        call and verified (via :func:`hash_body`) that the content
        hasn't drifted since the cursor chain was minted — this method
        does *not* re-check that.

        Walks the same greedy-split boundaries :meth:`split`/:meth:`pop`
        would have produced (same cap, same ``alt_hint``/``kind``), so
        page ``N`` here is byte-identical to what an unexpired cursor
        chain would have served. If more remains after page ``page``,
        mints (and caches, for the fast same-process path on the *next*
        call) a further re-derivable cursor for ``page + 1``; if
        ``page`` is the last page, returns it bare (no footer).
        """
        alt_hint = _clamp_alt_hint(alt_hint)
        kind = _clamp_kind(kind)
        cap = _max_body_bytes()
        current = body
        head = body
        next_recipe: RecipeSeed | None = None
        next_cursor: str | None = None
        for i in range(page):
            if len(current.encode("utf-8")) <= cap:
                head, current = current, ""
                break
            # This step's own next-cursor (were it minted) — computed
            # up front, same as :meth:`split`, so its actual byte
            # length (not the default uuid4 assumption) is reserved
            # for. Reproduces exactly what the original chain would
            # have reserved at this same step, so the boundary lands
            # in the same place.
            seed = RecipeSeed(verb=verb, args=args, body_hash=body_hash, page=i + 2)
            step_recipe: RecipeSeed | None = seed
            try:
                step_cursor = encode_recipe_cursor(seed)
            except Exception:
                log.debug(
                    "recipe cursor encoding failed during page rebuild; "
                    "minting an opaque cursor instead",
                    exc_info=True,
                )
                step_recipe = None
                step_cursor = None
            head, current = _greedy_split(
                current,
                cap,
                alt_hint=alt_hint,
                kind=kind,
                cursor_capable=True,
                cursor_len=(
                    len(step_cursor.encode("utf-8"))
                    if step_cursor is not None
                    else None
                ),
            )
            next_recipe, next_cursor = step_recipe, step_cursor
        if not current:
            return head

        remaining = _human_bytes(len(current.encode("utf-8")))
        cursor = next_cursor if next_cursor is not None else uuid.uuid4().hex

        footer = _build_footer(
            cursor=cursor, remaining=remaining, alt_hint=alt_hint, kind=kind
        )
        with self._lock:
            self._prune_expired()
            self._maybe_evict_oldest()
            self._entries[cursor] = _CachedTail(
                body=current,
                expires_at=self._now() + _ttl_seconds(),
                alt_hint=alt_hint,
                kind=kind,
                recipe=next_recipe,
            )
        return head + footer

    def __len__(self) -> int:
        """Number of pending cursors. Useful for tests/diagnostics."""
        with self._lock:
            self._prune_expired()
            return len(self._entries)


# ── Splitting helpers ──────────────────────────────────────────────


_SECTION_DELIMITER = "\n## "
_PARAGRAPH_DELIMITER = "\n\n"
#: Length (hex chars = bytes, ASCII) of the opaque ``uuid.uuid4().hex``
#: cursor the reserve constants below assume. A re-derivable recipe
#: cursor (:class:`RecipeSeed`) is almost always longer than this
#: (it carries the verb/args/page/hash as base64 JSON) — see
#: ``cursor_len`` below for how that extra length gets reserved for.
_DEFAULT_CURSOR_BYTES = 32

#: Footer space we keep in reserve when picking the head's byte
#: budget so ``head + footer`` stays under the frame cap. Derived
#: from the template itself — rendered with a full-width cursor and
#: a generous ``remaining`` token — so it self-corrects whenever the
#: footer wording changes and can never silently under-reserve. The
#: ``remaining`` readout is bounded-width by ``_human_bytes`` (it
#: steps up to KB/MB), so ``"8888.8 MB"`` is a safe upper bound.
_FOOTER_RESERVE_BYTES = len(
    _FOOTER_TEMPLATE.format(
        cursor="f" * _DEFAULT_CURSOR_BYTES, remaining="8888.8 MB"
    ).encode("utf-8")
)

#: Extra reserve for the optional ``alt_hint`` sentence, on top of
#: :data:`_FOOTER_RESERVE_BYTES`. Derived from the sentence template
#: rendered with an all-ASCII placeholder at :data:`_ALT_HINT_MAX_BYTES` —
#: :func:`_clamp_alt_hint` guarantees any hint it passes through encodes
#: to no more than that many bytes, so this is always a safe upper bound.
#: Only added to the budget when a call actually supplies ``alt_hint``,
#: so the no-hint case's reserve (and therefore its footer) is unchanged.
_ALT_HINT_RESERVE_BYTES = len(
    _ALT_HINT_SENTENCE_TEMPLATE.format(alt_hint="x" * _ALT_HINT_MAX_BYTES).encode(
        "utf-8"
    )
)

#: Extra reserve for the optional ``kind`` search-fallback sentence, on
#: top of :data:`_FOOTER_RESERVE_BYTES` — same idea as
#: :data:`_ALT_HINT_RESERVE_BYTES`, bounded by :func:`_clamp_kind`.
_KIND_FALLBACK_RESERVE_BYTES = len(
    _KIND_FALLBACK_SENTENCE_TEMPLATE.format(kind="x" * _KIND_FALLBACK_MAX_BYTES).encode(
        "utf-8"
    )
)

#: Reserve for :data:`_SHORT_LIVED_FOOTER_TEMPLATE`, the cursor-incapable
#: sibling of :data:`_FOOTER_RESERVE_BYTES` — no cursor placeholder needed
#: (the footer never mints one), just a bounded-width ``remaining`` readout.
_SHORT_LIVED_FOOTER_RESERVE_BYTES = len(
    _SHORT_LIVED_FOOTER_TEMPLATE.format(remaining="8888.8 MB").encode("utf-8")
)


def _greedy_split(
    body: str,
    cap_bytes: int,
    *,
    alt_hint: str | None = None,
    kind: str | None = None,
    cursor_capable: bool = True,
    cursor_len: int | None = None,
) -> tuple[str, str]:
    """Return ``(head, tail)`` such that head fits inside ``cap_bytes``.

    Strategy:
    1. Try ``\\n## `` (H2 section) boundaries first — preserves the
       rendered hierarchy.
    2. Fall back to paragraph boundaries (``\\n\\n``) when one
       section alone exceeds the cap.
    3. Last resort: hard-cut on a UTF-8 char boundary.

    ``cursor_len``, when given, is the *actual* byte length of the
    cursor that will be minted for this split (known ahead of time for
    a re-derivable :class:`RecipeSeed` cursor, whose length depends on
    ``args``/``page`` and so can exceed the
    :data:`_DEFAULT_CURSOR_BYTES` the module-level reserve constants
    assume). Any excess over that default gets added to the reserve so
    a large ``args`` payload can never push ``head + footer`` over
    ``cap_bytes``.
    """
    # Reserve some bytes for the footer (plus the optional alt_hint /
    # kind sentences, if given); the rest is available to the head.
    # ``cursor_capable`` picks which footer template's reserve applies —
    # the short-lived footer has no cursor placeholder so it reserves a
    # different (fixed) width. For very small caps the reserve can
    # dominate — clamp to a minimum of 1 byte for the head budget so
    # the chunker still makes forward progress.
    reserve = (
        _FOOTER_RESERVE_BYTES if cursor_capable else _SHORT_LIVED_FOOTER_RESERVE_BYTES
    )
    if alt_hint:
        reserve += _ALT_HINT_RESERVE_BYTES
    if kind:
        reserve += _KIND_FALLBACK_RESERVE_BYTES
    if cursor_len is not None and cursor_len > _DEFAULT_CURSOR_BYTES:
        reserve += cursor_len - _DEFAULT_CURSOR_BYTES
    budget = max(cap_bytes - reserve, 1)

    head, tail = _split_on_delimiter(body, _SECTION_DELIMITER, budget)
    if head and tail:
        return head, tail

    head, tail = _split_on_delimiter(body, _PARAGRAPH_DELIMITER, budget)
    if head and tail:
        return head, tail

    return _hard_split(body, budget)


def _split_on_delimiter(
    body: str, delimiter: str, budget_bytes: int
) -> tuple[str, str]:
    """Greedy delimiter split. Returns ``("", "")`` when impossible.

    A successful split returns a non-empty head AND non-empty tail —
    that's the signal to the caller that this delimiter level
    worked. When no boundary fits inside ``budget_bytes`` (either
    the first piece is already too big, or there's no delimiter at
    all), returns two empty strings.
    """
    if not body or delimiter not in body:
        return "", ""

    parts = body.split(delimiter)
    head = parts[0]
    if len(head.encode("utf-8")) > budget_bytes:
        # First section already exceeds the budget — caller falls
        # through to a finer split.
        return "", ""

    accepted_parts = [head]
    consumed_bytes = len(head.encode("utf-8"))
    cursor = 1
    while cursor < len(parts):
        next_piece = delimiter + parts[cursor]
        next_size = len(next_piece.encode("utf-8"))
        if consumed_bytes + next_size > budget_bytes:
            break
        accepted_parts.append(next_piece)
        consumed_bytes += next_size
        cursor += 1

    if cursor >= len(parts):
        # The whole body fit. No tail — caller's outer check
        # already verified body exceeds the cap, so this is a
        # pathological case (multi-byte UTF-8 made the initial
        # encode-check pessimistic). Return empty tail; caller
        # treats it as "body fits after all".
        return body, ""

    head_text = "".join(accepted_parts)
    # The tail's first piece is ``parts[cursor]`` without the
    # leading delimiter — we want the delimiter at the START of
    # the tail so the next call re-finds the boundary.
    tail_pieces = [delimiter.lstrip("\n") + parts[cursor], *parts[cursor + 1 :]]
    tail_text = delimiter.join(tail_pieces)
    # Reinstate the leading newline so the tail starts with
    # ``## `` cleanly rather than mid-newline.
    if not tail_text.startswith("##"):
        tail_text = "## " + parts[cursor]
        if cursor + 1 < len(parts):
            tail_text += delimiter + delimiter.join(parts[cursor + 1 :])
    return head_text, tail_text


def _hard_split(body: str, budget_bytes: int) -> tuple[str, str]:
    """Byte-budget split that respects UTF-8 character boundaries.

    Last-resort fallback when no delimiter-based split works. We
    pick the largest valid UTF-8 prefix under ``budget_bytes`` and
    let the tail carry the rest.
    """
    raw = body.encode("utf-8")
    if len(raw) <= budget_bytes:
        return body, ""

    cut = budget_bytes
    # Walk back until ``raw[cut]`` is a start-of-codepoint byte.
    # In UTF-8 continuation bytes have the bit pattern 10xxxxxx
    # (i.e. byte & 0xC0 == 0x80); decoding partway through one of
    # those would raise UnicodeDecodeError.
    while cut > 0 and (raw[cut] & 0xC0) == 0x80:
        cut -= 1
    head = raw[:cut].decode("utf-8", errors="strict")
    tail = raw[cut:].decode("utf-8", errors="strict")
    return head, tail


__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_TTL_SECONDS",
    "PaginationCache",
    "RecipeSeed",
    "decode_recipe_cursor",
    "encode_recipe_cursor",
    "hash_body",
]
