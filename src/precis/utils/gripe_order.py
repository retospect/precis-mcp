"""The shared gripe-queue ordering (gr595573).

One ``sort=`` vocabulary, two surfaces: the ``/gripes`` web list and the
MCP gripe browse (``get(kind='gripe', id='/open')`` /
``search(kind='gripe', sort=…)``). Ruling 2026-11-08: the queue drains —
the default order on both surfaces is ``drain``: the machine
``dispatch_score`` first (higher = hotter, gr604593; a set ``prio`` is a
multiplier on it, never the key), then size ascending (``size:s`` <
``size:m`` < ``size:l`` < unsized), then oldest first within a size.
The alternate sorts re-group the same queue, oldest first inside each
grouping:

- ``oldest`` / ``newest`` — filing time (``created_at``).
- ``prio`` — the human-set ``refs.prio`` ladder (``urgent`` 1 <
  ``high`` 3 < ``normal``/unset 5 < ``low`` 8), then the size ladder,
  oldest inside — "priority and size" (gr604514).
- ``size`` — the ``s`` → ``l`` → unsized ladder.
- ``source`` — who filed: human, then agent, then watchdog. *Human* is a
  ``user:<login>`` filing tag, a ``filed_by.human`` stamp or a
  ``user:`` write principal; *watchdog* is an ``origin:<slug>``
  system-filing tag or a worker stamp (``filed_by.source`` / a
  ``worker:<job>`` principal — ``worker:cli`` stays agent); the rest is
  agent-filed.
- ``status`` — the :data:`STATUS_RANK` workflow order.

:func:`order_by_sql` emits the ``ORDER BY`` fragment for ``refs r``:
``Store._LIST_ORDER_BY`` whitelists it under ``gripe_<sort>`` keys for
``list_refs``, and the web ``_rows`` query interpolates the same string —
one helper, so the surfaces can't drift. Callers select a *name*; the
SQL is static, never built from caller text.
"""

from __future__ import annotations

from precis.utils.dispatch_score import score_sql as _score_sql

#: The ``sort=`` values both surfaces accept, drain first (the default).
SORT_VALUES: tuple[str, ...] = (
    "drain",
    "oldest",
    "newest",
    "prio",
    "size",
    "source",
    "status",
)

#: The default — the queue that drains.
DEFAULT_SORT = "drain"

#: Workflow rank for ``status`` — the lifecycle order the ``/gripes``
#: list groups by; off-vocabulary drift values rank last. Single-sourced
#: here so the web chips' display order and the SQL rank can't fork.
STATUS_RANK: tuple[str, ...] = (
    "open",
    "triaged",
    "fixing",
    "ready_for_fix",
    "in_review",
    "verify",
    "auto_verify",
    "user_verify",
    "needs_spec",
    "parked",
    "done",
    "wontfix",
)

#: Correlated tag lookups used by the rank CASEs — the same subquery
#: shape the ``/gripes`` ``_rows`` SELECT already runs for PRIO/project.
#: Prefix tests are ``split_part``/``starts_with`` equalities rather than
#: ``LIKE 'x:%'``: the fragments reach psycopg-parametrised queries,
#: where a lone ``%`` reads as a botched placeholder (``%%`` escaping
#: can't live in a shared constant). ``size:`` values are case-folded —
#: triage writes ``size:S``, ci_watch ``size:s``; one axis.
_SIZE_LETTER_SQL = """\
(SELECT lower(split_part(t.value, ':', 2))
   FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
  WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
    AND split_part(t.value, ':', 1) = 'size' LIMIT 1)"""

_STATUS_VALUE_SQL = """\
(SELECT t.value
   FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
  WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1)"""

_USER_TAG_SQL = """\
EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
        WHERE rt.ref_id = r.ref_id AND split_part(t.value, ':', 1) = 'user')"""

_ORIGIN_TAG_SQL = """\
EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
        WHERE rt.ref_id = r.ref_id AND split_part(t.value, ':', 1) = 'origin')"""

_SIZE_RANK_SQL = (
    f"CASE {_SIZE_LETTER_SQL} "
    "WHEN 's' THEN 0 WHEN 'm' THEN 1 WHEN 'l' THEN 2 ELSE 3 END"
)

_STATUS_RANK_SQL = (
    f"CASE COALESCE({_STATUS_VALUE_SQL}, 'open') "
    + " ".join(f"WHEN '{v}' THEN {i}" for i, v in enumerate(STATUS_RANK))
    + f" ELSE {len(STATUS_RANK)} END"
)

#: Filing-source label: human / agent / watchdog — a ``human`` stamp
#: wins over an ``origin:`` tag (a watchdog relaying a person's words
#: still filed *their* report); the rank is the ruling's display order.
_SOURCE_LABEL_SQL = f"""\
CASE
  WHEN {_USER_TAG_SQL}
    OR (r.meta->'filed_by') ? 'human'
    OR starts_with(r.meta->'filed_by'->>'principal', 'user:')
    THEN 'human'
  WHEN {_ORIGIN_TAG_SQL}
    OR r.meta->'filed_by'->>'source' IS NOT NULL
    OR (starts_with(r.meta->'filed_by'->>'principal', 'worker:')
        AND r.meta->'filed_by'->>'principal' <> 'worker:cli')
    THEN 'watchdog'
  ELSE 'agent'
END"""

_SOURCE_RANK_SQL = (
    f"CASE {_SOURCE_LABEL_SQL} WHEN 'human' THEN 0 WHEN 'agent' THEN 1 ELSE 2 END"
)

_OLDEST_FIRST = "r.created_at ASC, r.ref_id ASC"

_ORDER_BY: dict[str, str] = {
    "drain": f"{_score_sql()} DESC, {_SIZE_RANK_SQL} ASC, {_OLDEST_FIRST}",
    "oldest": _OLDEST_FIRST,
    "newest": "r.created_at DESC, r.ref_id DESC",
    # Unset reads as the normal rung (``prio_age``'s coalesce — a set
    # PRIO:low still sinks under unjudged work), then size, then oldest.
    "prio": f"coalesce(r.prio, 5) ASC, {_SIZE_RANK_SQL} ASC, {_OLDEST_FIRST}",
    "size": f"{_SIZE_RANK_SQL} ASC, {_OLDEST_FIRST}",
    "source": f"{_SOURCE_RANK_SQL} ASC, {_OLDEST_FIRST}",
    "status": f"{_STATUS_RANK_SQL} ASC, {_OLDEST_FIRST}",
}


def order_by_sql(sort: str) -> str:
    """The static ``ORDER BY`` fragment behind one ``sort=`` name.

    ``KeyError`` on an unknown name — every caller allowlists
    :data:`SORT_VALUES` first, so a miss is a code bug, not user input.
    """
    return _ORDER_BY[sort]


def size_letter_sql() -> str:
    """Scalar SELECT for a row's ``size:<letter>`` — the value
    ``sort='size'``/``drain`` ranks on, so a displayed size can't drift
    from the sort key."""
    return _SIZE_LETTER_SQL


def source_label_sql() -> str:
    """Scalar SELECT for a row's filing-source label (``human`` /
    ``agent`` / ``watchdog``) — the same classification
    ``sort='source'`` ranks on."""
    return _SOURCE_LABEL_SQL
