"""Keyword-triggered model routing — content rules at the dispatch seam.

Reto 2026-10-23 (chat-99): an ordered list of
``(keyword | regex → target)`` rules matched against the request's prompt
text (``prompt`` + system prompt + user messages) inside
:func:`precis.utils.llm.router.route`, so every routed call gets it with no
per-caller code. **First match wins**; no match ⇒ the tier/op resolution is
byte-for-byte today's.

A rule's ``target`` reuses the ``llm.op.<source>`` override grammar —
``{"tier": <str>}`` retires, ``{"model": <str>}`` pins a model (the router
synthesizes the rung's transport from the id), ``{"chain": [rung…]}``
carries the full :func:`~precis.utils.llm.router.parse_chain_rungs` grammar.
Scope fields ``sources``/``tiers`` restrict a rule to those
``req.source``/``req.tier`` values; ``None`` matches all.

**Storage is the outbound-policy pattern.** :data:`CODE_RULES` is the
compiled-default layer; the ``llm_keyword_routes`` table is the operator
layer the web ``/llm/rules`` section edits. The
effective list is the DB rows (ordered by ``position``) followed by the
code defaults — the DB block wins by matching first. Rows are read through
a per-process snapshot refreshed at most every :data:`_SNAPSHOT_TTL_S`;
a DB fault (no store, missing table) serves the code defaults alone — the
resolver must never wedge a dispatch.

**Regex safety.** Rules are operator-entered, but a pathological pattern
must not hang the router: pattern length is capped
(:data:`_MAX_PATTERN_CHARS`), backreferences are rejected, and every
pattern is compiled **once at snapshot load** — an invalid pattern disables
that rule at load with a warning, never at match time. Plain keywords are
the preferred form: a keyword compiles to a case-insensitive whole-word
literal (``re.escape`` + ``\\w`` boundaries) and cannot backtrack.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.utils.llm.router import LlmRequest

log = logging.getLogger(__name__)

#: Cap on a rule's pattern length — bounds both the compile cost and how
#: much rope a regex gets. Keywords are short; anything longer is a sign
#: the author wanted a regex and should say so.
_MAX_PATTERN_CHARS = 200

#: Snapshot refresh cadence — one rule-table SELECT per process per 30 s,
#: so a web edit lands within a minute without a per-call query.
_SNAPSHOT_TTL_S = 30.0

_SNAPSHOT_SQL = """
SELECT id, position, pattern, is_regex, target, sources, tiers, enabled, note
  FROM llm_keyword_routes
 ORDER BY position, id
"""


@dataclass(frozen=True)
class KeywordRule:
    """One ordered ``(pattern → target)`` routing rule.

    ``rule_id`` is the ``llm_keyword_routes.id`` for a DB row, ``code:<n>``
    for a :data:`CODE_RULES` entry — the id stamped into
    ``llm_call_log.features.keyword_rule`` when the rule fires. ``compiled``
    is built once at snapshot load (:func:`_compile`); ``None`` means the
    pattern failed validation and the rule is inert.
    """

    rule_id: str
    pattern: str
    is_regex: bool
    #: The ``llm.op.*``-grammar target: ``{"tier"?, "model"?, "chain"?}``.
    target: dict[str, Any]
    sources: frozenset[str] | None = None
    tiers: frozenset[str] | None = None
    enabled: bool = True
    note: str | None = None
    compiled: re.Pattern[str] | None = None


def _compile(pattern: str, is_regex: bool, rule_id: str) -> re.Pattern[str] | None:
    """Compile one rule's pattern at snapshot-load time, or ``None`` when it
    fails validation (the rule is inert — never a dispatch-time fault)."""
    pat = pattern.strip()
    if not pat:
        log.warning("llm-keyword: rule %s has an empty pattern — disabled", rule_id)
        return None
    if len(pat) > _MAX_PATTERN_CHARS:
        log.warning(
            "llm-keyword: rule %s pattern is %d chars (cap %d) — disabled",
            rule_id,
            len(pat),
            _MAX_PATTERN_CHARS,
        )
        return None
    if is_regex:
        if re.search(r"\\[1-9]", pat):
            log.warning(
                "llm-keyword: rule %s uses a backreference — disabled "
                "(keep regexes simple; a plain keyword is preferred)",
                rule_id,
            )
            return None
        try:
            return re.compile(pat, re.IGNORECASE)
        except re.error as exc:
            log.warning(
                "llm-keyword: rule %s regex %r does not compile (%s) — disabled",
                rule_id,
                pat,
                exc,
            )
            return None
    return re.compile(r"(?<!\w)" + re.escape(pat) + r"(?!\w)", re.IGNORECASE)


#: The compiled-default layer — empty today; a shipped built-in rule lives
#: here, under the DB rows in match order.
CODE_RULES: tuple[KeywordRule, ...] = ()


def _row_to_rule(row: tuple[Any, ...]) -> KeywordRule:
    rid = str(row[0])
    return KeywordRule(
        rule_id=rid,
        pattern=str(row[2]),
        is_regex=bool(row[3]),
        target=row[4] if isinstance(row[4], dict) else {},
        sources=frozenset(row[5]) if row[5] else None,
        tiers=frozenset(row[6]) if row[6] else None,
        enabled=bool(row[7]),
        note=row[8],
        compiled=_compile(str(row[2]), bool(row[3]), rid),
    )


_snapshot_lock = threading.Lock()
#: The loaded DB rule rows (in match order). ``None`` = never loaded or the
#: last refresh failed — :func:`_refresh` then serves the code defaults.
_snapshot: list[KeywordRule] | None = None
_snapshot_at = 0.0


def _refresh_snapshot() -> list[KeywordRule] | None:
    """The cached DB rule rows; ``None`` on a resolver fault.

    Mirrors :func:`precis.utils.outbound_policy._refresh_snapshot`: a failed
    refresh keeps the TTL discipline (no hammering a sick DB) and the caller
    falls back to the code defaults.
    """
    global _snapshot, _snapshot_at
    now = time.monotonic()
    if now - _snapshot_at < _SNAPSHOT_TTL_S:
        return _snapshot
    if not _snapshot_lock.acquire(blocking=False):
        return _snapshot  # a refresh is in flight — serve the stale copy
    try:
        now = time.monotonic()
        if now - _snapshot_at < _SNAPSHOT_TTL_S:
            return _snapshot
        try:
            from precis.budget import meter

            store = meter.active_store()
            if store is None:
                raise RuntimeError("no bound store")
            with store.pool.connection() as conn:
                rows = conn.execute(_SNAPSHOT_SQL).fetchall()
            _snapshot = [_row_to_rule(r) for r in rows]
        except Exception:
            _snapshot = None
            _snapshot_at = now
            log.debug("keyword_route: snapshot refresh failed", exc_info=True)
            return None
        _snapshot_at = now
        return _snapshot
    finally:
        _snapshot_lock.release()


def reset_cache() -> None:
    """Drop the snapshot cache — tests, and after a direct rule write."""
    global _snapshot, _snapshot_at
    with _snapshot_lock:
        _snapshot = None
        _snapshot_at = 0.0


def rules() -> list[KeywordRule]:
    """The effective rule list in match order — DB rows then
    :data:`CODE_RULES` — including disabled rows (the ``/llm/rules`` page
    renders them; :func:`match` skips them)."""
    return [*(_refresh_snapshot() or []), *CODE_RULES]


def match(text: str, *, source: str | None, tier: str | None) -> KeywordRule | None:
    """The first enabled, in-scope rule whose pattern hits ``text``, or
    ``None``. Case-insensitive; plain keywords match whole-word.
    Never raises — a matcher fault degrades to no-match."""
    if not text.strip():
        return None
    try:
        for rule in rules():
            if not rule.enabled or rule.compiled is None:
                continue
            if rule.sources is not None and (source or "") not in rule.sources:
                continue
            if rule.tiers is not None and (tier or "") not in rule.tiers:
                continue
            if rule.compiled.search(text):
                return rule
    except Exception:
        log.warning("llm-keyword: match failed — normal routing", exc_info=True)
    return None


def request_text(req: LlmRequest) -> str:
    """The text rules match on: ``req.prompt`` + the system prompt + the
    user-message text blocks — the same content the call sends."""
    from precis.utils.llm.router import _content_text, _read_system_prompt

    parts = [req.prompt or ""]
    system = _read_system_prompt(req.system_prompt)
    if system:
        parts.append(system)
    for m in req.messages or ():
        parts.append(_content_text(m.get("content", "")))
    return "\n".join(parts)


def match_request(req: LlmRequest) -> KeywordRule | None:
    """:func:`match` on one :class:`LlmRequest` — scoped to its
    ``source``/``tier``; ``None`` for no match or a matcher fault."""
    return match(request_text(req), source=req.source or None, tier=req.tier.value)


__all__ = [
    "CODE_RULES",
    "KeywordRule",
    "match",
    "match_request",
    "request_text",
    "reset_cache",
    "rules",
]
