"""Quest ``needs`` — a tick's channel for asking for a tool that does not
exist (gr485483).

A quest tick model cannot call tools — its whole output is one structured
JSON payload applied by the executor (``tick._TickRun``'s stage machine;
the roadmap/weave bodies' ``_ask``/``client.complete`` replies are the same
shape). So when a tick stalls because a tool, data source, simulation lane
or catalog entry is missing, the model's only move is to SAY so: every
body's tick schema accepts an optional ``needs`` list of
``{"tool", "why", "would_unblock"}`` entries.

The contract is deliberately asymmetric: the model *declares* a need; the
*executor* (this module, called from each body's apply path) decides what
to do with it — and what it does is file the gripe: one per
``(quest, normalised tool)``, deduped against the quest's other open
needs-gripes (a repeat sighting becomes a comment, not a second gripe),
rate-limited per quest per day, tagged ``quest:<id>`` +
``needs-tool:<slug>``, and linked ``gripe --blocks--> quest`` so the
quest reads ``blocked-by`` its own requests (gr485483's visibility half —
``quest_gaps`` shows "blocked on gr<N>" and the dry-rest alert cites the
handles — lives in :func:`open_tool_gripes`).

Dedup is a **marker protocol**, same shape as the health_digest router's
``watchdog-condition:`` gripes (:mod:`precis.workers.health_digest`): the
gripe's first body line is ``quest-need: qu<N> <tool-key>``, and the live
scan of open marker gripes is the single source of truth — survives
restarts, no in-memory state. Two pieces:

* :func:`parse_needs` — the lenient reader. A malformed ``needs`` value
  (wrong top-level type, a non-dict entry, an entry with no usable
  ``tool``) is dropped and counted, never a failure — mirroring the tick's
  own degrade-don't-crash convention for ``ledger_ops``/``dialectic_ops``.
* :func:`apply_needs` — the apply path: parses, writes one ``observation``
  logbook entry when entries were dropped, then files/bumps the per-
  ``(quest, tool)`` gripe and records the handle in the logbook.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.utils.env import env_int

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: ``needs`` entries accepted per tick — a real "I am missing X" is a short
#: list; a looping payload must not fan out unboundedly (same cap
#: discipline as ``dialectic_ops``).
NEEDS_MAX = 3

#: Per-field length caps — short strings, like a gap line. ``tool`` doubles
#: as the dedup key and the ``needs-tool:`` tag value (tags cap at 200 and
#: carry no whitespace, so the slug caps shorter still).
TOOL_CHARS = 80
FIELD_CHARS = 240

#: Open tag on a quest-filed needs gripe naming the tool slug —
#: ``needs-tool:<slug>``. Together with ``quest:<id>`` it makes the
#: (quest, tool) pair findable from the tag index, not just the marker.
NEEDS_TAG_PREFIX = "needs-tool:"

#: First body line of a quest-filed needs gripe — the dedup/auto-discover
#: key, ``quest-need: qu<N> <tool-key>`` (same single-source-of-truth
#: marker protocol as ``health_digest._ROUTER_MARKER_PREFIX``).
_MARKER_PREFIX = "quest-need: "

#: Non-terminal gripe STATUSes a needs gripe still "counts" under for dedup
#: — mirrors ``health_digest._MARKER_GRIPE_LIVE_STATUSES`` (triaged etc.
#: keep suppressing a duplicate; only done/wontfix/retired release it).
_LIVE_GRIPE_STATUSES: tuple[str, ...] = (
    "open",
    "triaged",
    "ready_for_fix",
    "in_review",
)


@dataclass(frozen=True)
class Need:
    """One validated ``needs`` entry. ``tool_key`` is the normalised form of
    ``tool`` — lowercased, whitespace/punctuation collapsed to ``-`` — used
    for dedup and as the ``needs-tool:`` tag value."""

    tool: str
    tool_key: str
    why: str
    would_unblock: str


def tool_key(tool: str) -> str:
    """Normalise a tool name for dedup/tagging: lowercase, every run of
    non-alphanumerics becomes one ``-``, capped to a tag-safe length."""
    return re.sub(r"[^a-z0-9]+", "-", tool.lower()).strip("-")[:60]


def parse_needs(raw: Any) -> tuple[list[Need], int]:
    """Validate a ``needs`` field value → ``(needs, dropped)``.

    ``raw`` is the field's value as parsed out of the model payload —
    expected to be a list of ``{"tool", "why", "would_unblock"}`` dicts.
    Anything else (non-list, non-dict entries, blank ``tool``, entries past
    :data:`NEEDS_MAX`) is dropped and counted; the caller turns a nonzero
    ``dropped`` into a logbook note. Strings are truncated to the field
    caps rather than rejected — a verbose model is still making a claim.
    """
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        return [], 1
    needs: list[Need] = []
    dropped = 0
    for e in raw[:NEEDS_MAX]:
        if not isinstance(e, dict):
            dropped += 1
            continue
        tool = str(e.get("tool") or "").strip()[:TOOL_CHARS]
        if not tool or not tool_key(tool):
            dropped += 1
            continue
        needs.append(
            Need(
                tool=tool,
                tool_key=tool_key(tool),
                why=str(e.get("why") or "").strip()[:FIELD_CHARS],
                would_unblock=str(e.get("would_unblock") or "").strip()[:FIELD_CHARS],
            )
        )
    dropped += max(0, len(raw) - NEEDS_MAX)
    return needs, dropped


@dataclass(frozen=True)
class NeedsOutcome:
    """What :func:`apply_needs` did with one ``needs`` field value."""

    needs: tuple[Need, ...]
    dropped: int
    #: gripe ids filed or bumped this call.
    gripes: tuple[int, ...]
    #: quest-log entries this call wrote (the caller's ``added`` counter
    #: counts them toward engagement).
    logbook_entries: int


# ── the marker-gripe scan ─────────────────────────────────────────────


def _open_need_gripes(store: Store, quest_id: int) -> dict[str, int]:
    """``{tool_key: gripe_ref_id}`` for every live (non-terminal, not
    retired) marker gripe this quest has open — the dedup map. Keyed on the
    ``blocks`` edge the filing path writes, so an unlinked marker gripe
    (edge removed by hand) doesn't wedge dedup."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, c.text
              FROM links l
              JOIN refs r ON r.ref_id = l.src_ref_id
              JOIN chunks c
                     ON c.ref_id = r.ref_id
                    AND c.chunk_kind = 'gripe_body' AND c.ord = 0
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE l.dst_ref_id = %s AND l.relation = 'blocks'
               AND r.kind = 'gripe' AND r.retired_at IS NULL
               AND t.namespace = 'STATUS' AND t.value = ANY(%s)
               AND c.text LIKE %s
            """,
            (quest_id, list(_LIVE_GRIPE_STATUSES), f"{_MARKER_PREFIX}qu{quest_id} %"),
        ).fetchall()
    out: dict[str, int] = {}
    for gripe_id, text in rows:
        first = (text or "").splitlines()[0]
        key = first[len(_MARKER_PREFIX) :].strip().partition(" ")[2].strip()
        if key:
            out.setdefault(key, int(gripe_id))
    return out


def _filed_last_24h(store: Store, quest_id: int) -> int:
    """Marker gripes this quest filed in the last 24 h — the rate-limit
    count. Any status counts (a closed-then-refiled pair still spent two
    filings); ``retired_at`` rows too."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(*)
              FROM refs r
              JOIN chunks c
                     ON c.ref_id = r.ref_id
                    AND c.chunk_kind = 'gripe_body' AND c.ord = 0
             WHERE r.kind = 'gripe' AND c.text LIKE %s
               AND r.created_at > now() - interval '24 hours'
            """,
            (f"{_MARKER_PREFIX}qu{quest_id} %",),
        ).fetchone()
    return int(row[0]) if row else 0


def _max_new_per_day() -> int:
    """New needs-gripes a quest may file per 24 h (default 3). Bumps of
    already-open marker gripes are comments, not filings — uncapped."""
    return env_int("PRECIS_QUEST_NEEDS_MAX_PER_DAY", 3, lo=1, hi=100)


def _file_one(store: Store, quest_id: int, need: Need) -> int:
    """Insert one needs gripe (ref + marker body + STATUS:open + the two
    open tags + the ``blocks`` edge to the quest), in one transaction —
    the ``health_digest._file_router_gripe`` worker-side pattern."""
    from precis.store.types import ChunkInsert, Tag

    body = (
        f"{_MARKER_PREFIX}qu{quest_id} {need.tool_key}\n"
        f"quest qu{quest_id} tick reported a missing capability: {need.tool}\n"
        f"why: {need.why or '(unsaid)'}\n"
        f"would unblock: {need.would_unblock or '(unsaid)'}\n"
        "Filed by the quest tick executor (gr485483) — a tick declared this "
        "tool/data-source/sim-lane/catalog entry does not exist. Repeat "
        "sightings bump this gripe as comments; closing it clears the "
        "quest's `needs-tool` gap."
    )
    title = f"quest qu{quest_id} needs {need.tool}"
    if len(title) > 200:
        title = title[:197].rstrip() + "…"
    with store.tx() as conn:
        ref = store.insert_ref(kind="gripe", slug=None, title=title, meta={}, conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "gripe_body"})],
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.add_tag(ref.id, Tag.open(f"quest:{quest_id}"), set_by="system", conn=conn)
        store.add_tag(
            ref.id,
            Tag.open(f"{NEEDS_TAG_PREFIX}{need.tool_key}"),
            set_by="system",
            conn=conn,
        )
        store.add_link(
            src_ref_id=ref.id,
            dst_ref_id=quest_id,
            relation="blocks",
            set_by="system",
            conn=conn,
        )
    return int(ref.id)


def _bump_one(store: Store, gripe_id: int, need: Need) -> None:
    """A repeat sighting of an already-open (quest, tool) need — a comment,
    not a second gripe (mirrors ``_auto_close_marker_gripe``'s comment
    append)."""
    from precis.store.types import ChunkInsert

    text = (
        f"still needed — another tick reported `{need.tool}` "
        f"(why: {need.why or '(unsaid)'}; "
        f"would unblock: {need.would_unblock or '(unsaid)'})"
    )
    next_pos = len(store.chunks.list_chunks_for_ref(gripe_id))
    with store.tx() as conn:
        store.chunks.insert_chunks(
            gripe_id,
            [
                ChunkInsert(
                    ord=next_pos, text=text, meta={"chunk_kind": "gripe_comment"}
                )
            ],
            conn=conn,
        )


def apply_needs(
    store: Store, quest_id: int, raw: Any, *, by: str = "agent"
) -> NeedsOutcome:
    """Parse + apply a tick payload's ``needs`` — the executor half of the
    contract (gr485483): malformed entries drop with a logbook note (never
    a tick failure); each valid need dedups onto the quest's open marker
    gripe for that tool (a comment bump), files a fresh gripe when none is
    open, or is skipped when the quest's daily filing budget is spent.
    Per-need failures are logged and skipped, never raised — a gripe-write
    bug must not sink the tick's other writes.
    """
    from precis.quest.logbook import append_entry

    needs, dropped = parse_needs(raw)
    gripes: list[int] = []
    entries = 0
    if dropped:
        append_entry(
            store,
            quest_id,
            text=(
                f"dropped {dropped} malformed `needs` "
                f"entr{'y' if dropped == 1 else 'ies'} — each is "
                "{tool, why, would_unblock} with a non-empty `tool`"
            ),
            entry_type="observation",
            by=by,
        )
        entries += 1
        log.info("quest %s: dropped %d malformed needs entries", quest_id, dropped)

    if needs:
        open_map = _open_need_gripes(store, quest_id)
        filed_today = _filed_last_24h(store, quest_id)
        cap = _max_new_per_day()
        for need in needs:
            try:
                gid = open_map.get(need.tool_key)
                if gid is not None:
                    _bump_one(store, gid, need)
                    gripes.append(gid)
                    text = f"needs: bumped gripe gr{gid} — still need `{need.tool}`"
                elif filed_today >= cap:
                    text = (
                        f"needs: not filing `{need.tool}` — {cap} new "
                        "needs-gripes already filed in the last 24h; "
                        "re-declare tomorrow"
                    )
                else:
                    gid = _file_one(store, quest_id, need)
                    open_map[need.tool_key] = gid
                    filed_today += 1
                    gripes.append(gid)
                    text = f"needs: filed gripe gr{gid} — missing `{need.tool}`"
                append_entry(
                    store, quest_id, text=text, entry_type="observation", by=by
                )
                entries += 1
            except Exception:
                log.exception(
                    "quest %s: needs apply failed for tool %r", quest_id, need.tool
                )
    return NeedsOutcome(
        needs=tuple(needs),
        dropped=dropped,
        gripes=tuple(gripes),
        logbook_entries=entries,
    )


def open_tool_gripes(store: Store, quest_id: int) -> list[tuple[int, str]]:
    """``[(gripe_id, tool_key)]`` for the quest's live marker gripes — the
    read shared by ``quest_gaps``'s ``needs-tool`` gap and the dry-rest
    alert's citation list."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, c.text
              FROM links l
              JOIN refs r ON r.ref_id = l.src_ref_id
              JOIN chunks c
                     ON c.ref_id = r.ref_id
                    AND c.chunk_kind = 'gripe_body' AND c.ord = 0
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE l.dst_ref_id = %s AND l.relation = 'blocks'
               AND r.kind = 'gripe' AND r.retired_at IS NULL
               AND t.namespace = 'STATUS' AND t.value = ANY(%s)
               AND c.text LIKE %s
             ORDER BY r.ref_id
            """,
            (quest_id, list(_LIVE_GRIPE_STATUSES), f"{_MARKER_PREFIX}qu{quest_id} %"),
        ).fetchall()
    out: list[tuple[int, str]] = []
    for gripe_id, text in rows:
        first = (text or "").splitlines()[0]
        key = first[len(_MARKER_PREFIX) :].strip().partition(" ")[2].strip()
        out.append((int(gripe_id), key))
    return out


__all__ = [
    "FIELD_CHARS",
    "NEEDS_MAX",
    "NEEDS_TAG_PREFIX",
    "TOOL_CHARS",
    "Need",
    "NeedsOutcome",
    "apply_needs",
    "open_tool_gripes",
    "parse_needs",
    "tool_key",
]
