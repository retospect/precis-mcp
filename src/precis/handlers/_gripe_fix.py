"""Gripe-fix loop state on the gripe (gr519361).

The coordinator's working state for a builder wave — which batch a gripe
is in, the brief that assigned it, where the builder stopped, the review
verdict, who is working it, the landed sha — lives on the gripe itself
(Reto 2026-10-26; ``docs/backlog/gripe-fix-loop-on-the-gripe.md``). This
module is the state model behind ``edit(kind='gripe', mode='fix')``,
the ``fix:`` card line, ``view='fix'`` and the ``/queue`` and ``/batch``
list views in :mod:`precis.handlers.gripe`.

Stored (the smallest set; everything else is derived):

- tag ``batch:<slug>`` — one at a time, the edit replaces it;
- chunks ``gripe_brief`` / ``gripe_handoff`` / ``gripe_review``
  (migration 0254) — the texts, in ord order; a review's first line is
  ``LAND — …`` or ``FIX — …``;
- ``refs.meta.fix`` — ``host``, ``session``, ``tree``, ``landed``.
  ``host`` is the fleet host (what ``scripts/fleet-report`` sends);
  ``session`` is ``<vendor>:<window>`` — the builder's tmux window name
  (``claude:claude-tables-s2``, ``devin:devin-tables-s2``,
  ``codex:codex-castor-s2``, what ``scripts/fleet-wave launch`` records),
  or ``claude:<session id>`` for a session with no window — so the fleet
  row ``<vendor>:<host>:<project>:<tree>`` resolves from its prefix. The
  key's presence is the one marker of "in the loop": the card line and
  ``/queue`` (``list_refs(meta_has='fix')``) both key on it, so a bare
  ``batch:`` tag set through ``tag()`` is membership history, not state.

Derived — :meth:`FixState.queue`: no brief → ``unbriefed``; newest event
a brief → ``queued`` (no session) or ``in-build``; a handoff →
``in-review``; a review ``LAND`` → ``landing``, ``FIX`` → ``fix-round``;
``landed`` set → ``awaiting-deploy``; a terminal STATUS → none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.errors import ForeignKeyViolation, UniqueViolation

from precis.errors import BadInput, Upstream
from precis.store import Tag
from precis.store.types import ChunkInsert, ChunkRow, Ref
from precis.utils.handle_registry import try_format

BRIEF_KIND = "gripe_brief"
HANDOFF_KIND = "gripe_handoff"
REVIEW_KIND = "gripe_review"
FIX_KINDS: tuple[str, ...] = (BRIEF_KIND, HANDOFF_KIND, REVIEW_KIND)
#: Short label per chunk kind for acks and the dossier.
KIND_LABEL = {BRIEF_KIND: "brief", HANDOFF_KIND: "handoff", REVIEW_KIND: "review"}

BATCH_PREFIX = "batch:"
META_KEY = "fix"
#: ``meta.fix`` keys the edit writes; ``''`` clears one.
META_FIELDS: tuple[str, ...] = ("host", "session", "tree", "landed")
VERDICTS: tuple[str, ...] = ("land", "fix")
QUEUES: tuple[str, ...] = (
    "unbriefed",
    "queued",
    "in-build",
    "in-review",
    "fix-round",
    "landing",
    "awaiting-deploy",
)
#: The ``args=`` keys ``edit(mode='fix')`` reads.
EDIT_KEYS: tuple[str, ...] = (
    "batch",
    "host",
    "session",
    "tree",
    "brief",
    "handoff",
    "review",
    "findings",
    "landed",
)
_TERMINAL = frozenset({"done", "wontfix"})
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_NO_SPACE_RE = re.compile(r"^\S{1,120}$")
_EDIT_NEXT = (
    "edit(kind='gripe', id={id}, mode='fix', args={{'batch': '<slug>', "
    "'brief': '<text>'}})"
)


@dataclass(frozen=True)
class FixEvent:
    kind: str
    ord: int
    text: str
    created_at: datetime

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)


@dataclass(frozen=True)
class FixState:
    ref_id: int
    batch: str | None
    host: str | None
    session: str | None
    tree: str | None
    landed: str | None
    status: str | None
    events: tuple[FixEvent, ...]
    in_loop: bool

    @property
    def newest(self) -> FixEvent | None:
        return self.events[-1] if self.events else None

    def newest_of(self, kind: str) -> FixEvent | None:
        for ev in reversed(self.events):
            if ev.kind == kind:
                return ev
        return None

    @property
    def rounds(self) -> int:
        return sum(1 for ev in self.events if ev.kind == BRIEF_KIND)

    @property
    def verdict(self) -> str | None:
        ev = self.newest_of(REVIEW_KIND)
        return verdict_of(ev.text) if ev is not None else None

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL

    @property
    def queue(self) -> str | None:
        if self.terminal or not self.in_loop:
            return None
        if self.landed:
            return "awaiting-deploy"
        ev = self.newest
        if ev is None:
            return "unbriefed"
        if ev.kind == BRIEF_KIND:
            return "in-build" if self.session else "queued"
        if ev.kind == HANDOFF_KIND:
            return "in-review"
        return "landing" if verdict_of(ev.text) == "land" else "fix-round"

    @property
    def who(self) -> str:
        """``session@host`` (either half may be missing)."""
        if self.session and self.host:
            return f"{self.session}@{self.host}"
        return self.session or (f"@{self.host}" if self.host else "")

    @property
    def last_at(self) -> datetime | None:
        return self.events[-1].created_at if self.events else None


def verdict_of(text: str) -> str | None:
    """``'land'`` / ``'fix'`` from a review's first token, else ``None``."""
    head = text.strip().split(None, 1)[0].strip(" —-:").lower() if text.strip() else ""
    return head if head in VERDICTS else None


def status_of(tags: list[Any]) -> str | None:
    for t in tags:
        s = str(t)
        if s.startswith("STATUS:"):
            return s[len("STATUS:") :]
    return None


def batch_of(tags: list[Any]) -> str | None:
    for t in tags:
        s = str(t)
        if s.startswith(BATCH_PREFIX):
            return s[len(BATCH_PREFIX) :]
    return None


def _events(chunks: list[ChunkRow]) -> tuple[FixEvent, ...]:
    return tuple(
        FixEvent(kind=c.chunk_kind, ord=c.ord, text=c.text, created_at=c.created_at)
        for c in sorted(chunks, key=lambda c: c.ord)
        if c.chunk_kind in FIX_KINDS
    )


def state_from(ref: Ref, tags: list[Any], events: tuple[FixEvent, ...]) -> FixState:
    """The one constructor: ``meta.fix`` presence is the in-loop marker."""
    meta = (ref.meta or {}).get(META_KEY)
    fix = meta if isinstance(meta, dict) else {}
    return FixState(
        ref_id=ref.id,
        batch=batch_of(tags),
        host=fix.get("host") or None,
        session=fix.get("session") or None,
        tree=fix.get("tree") or None,
        landed=fix.get("landed") or None,
        status=status_of(tags),
        events=events,
        in_loop=isinstance(meta, dict),
    )


def build_state(ref: Ref, tags: list[Any], chunks: list[ChunkRow]) -> FixState:
    return state_from(ref, tags, _events(chunks))


def fix_state(
    store: Any,
    ref: Ref,
    tags: list[Any] | None = None,
    chunks: list[ChunkRow] | None = None,
) -> FixState:
    """The state of one gripe; ``tags``/``chunks`` skip the reads when held."""
    if tags is None:
        tags = store.tags_for(ref.id)
    if chunks is None:
        chunks = store.chunks.list_chunks_for_ref(ref.id)
    return build_state(ref, tags, chunks)


def bulk_states(store: Any, refs: list[Ref]) -> list[FixState]:
    """States for many refs in two queries (tags bulk + one chunk scan)."""
    if not refs:
        return []
    ids = [r.id for r in refs]
    tag_rows = store.ref_tags_bulk(ids)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.ref_id, c.ord, c.chunk_kind, c.text, c.created_at "
            "FROM chunks c WHERE c.ref_id = ANY(%s) AND c.ord >= 0 "
            "AND c.chunk_kind = ANY(%s) AND c.retired_at IS NULL "
            "ORDER BY c.ref_id, c.ord",
            (ids, list(FIX_KINDS)),
        ).fetchall()
    by_ref: dict[int, list[FixEvent]] = {}
    for ref_id, ord_, kind, text, created_at in rows:
        by_ref.setdefault(int(ref_id), []).append(
            FixEvent(kind=kind, ord=ord_, text=text, created_at=created_at)
        )
    out: list[FixState] = []
    for r in refs:
        tags = [
            f"{ns}:{val}" if ns != "OPEN" else val for ns, val in tag_rows.get(r.id, [])
        ]
        out.append(state_from(r, tags, tuple(by_ref.get(r.id, []))))
    return out


# ── unpicked staleness (gr520909) ─────────────────────────────────────
#
# A gripe nobody picked up for ~"a week or 3" (Reto: the spec pins 14
# days) gets a visible stale mark on its card, in list rows and in the
# /open + session-digest header. Unpicked is derived — never stored —
# so picking the gripe up (any ``meta.fix`` key, a STATUS move past
# ``triaged``, or a live job/todo edge) clears the mark on next read.

#: Age at which a still-unpicked gripe reads as stale.
UNPICKED_AFTER = timedelta(days=14)
#: "No status change past triaged" — the only STATUS values that still
#: count as unpicked (a missing STATUS counts too).
UNPICKED_STATUSES = frozenset({"open", "triaged"})
#: Linked-ref kinds that count as picked-up work.
_WORK_LINK_KINDS: tuple[str, ...] = ("job", "todo")
#: Terminal STATUS values for those kinds — a linked job/todo that
#: already finished is not a pickup.
_WORK_TERMINAL_VALUES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "cancelled",
    "done",
    "won't-do",
    "abandoned",
)


def claimed_work_ids(store: Any, ref_ids: list[int]) -> set[int]:
    """ref_ids with a live (non-terminal, un-retired) job/todo link.

    One query: any edge in either direction counts, but only when the
    linked job/todo is itself live — a ``STATUS:done`` todo or a
    ``STATUS:failed`` job is a closed attempt, not a claim.
    """
    if not ref_ids:
        return set()
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT g.ref_id FROM refs g "
            "JOIN links l ON (l.src_ref_id = g.ref_id OR l.dst_ref_id = g.ref_id) "
            "JOIN refs w ON w.ref_id = CASE WHEN l.src_ref_id = g.ref_id "
            "THEN l.dst_ref_id ELSE l.src_ref_id END "
            "WHERE g.ref_id = ANY(%s) AND w.kind = ANY(%s) "
            "AND w.retired_at IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = w.ref_id AND t.namespace = 'STATUS' "
            "AND t.value = ANY(%s) "
            "AND (rt.expires_at IS NULL OR rt.expires_at > now()))",
            (list(ref_ids), list(_WORK_LINK_KINDS), list(_WORK_TERMINAL_VALUES)),
        ).fetchall()
    return {int(r[0]) for r in rows}


def stale_unpicked(
    store: Any, refs: list[Ref], *, now: datetime | None = None
) -> dict[int, int]:
    """``{ref_id: days_old}`` for the live gripes nobody has picked up.

    Unpicked = all of: no ``meta.fix`` (in the fix loop = picked), age
    ≥ :data:`UNPICKED_AFTER`, STATUS missing or in
    :data:`UNPICKED_STATUSES`, and no live job/todo link. Callers pass
    an already-filtered ref set; the age/meta prefilter in Python keeps
    the two SQL reads off the common path (a fresh gripe costs nothing).
    """
    now = now or datetime.now(UTC)
    old = [
        r
        for r in refs
        if not isinstance((r.meta or {}).get(META_KEY), dict)
        and now - r.created_at >= UNPICKED_AFTER
    ]
    if not old:
        return {}
    ids = [r.id for r in old]
    claimed = claimed_work_ids(store, ids)
    tag_rows = store.ref_tags_bulk(ids)
    out: dict[int, int] = {}
    for r in old:
        if r.id in claimed:
            continue
        status = next((v for ns, v in tag_rows.get(r.id, []) if ns == "STATUS"), None)
        if status is not None and status not in UNPICKED_STATUSES:
            continue
        out[r.id] = (now - r.created_at).days
    return out


# ── rendering ──────────────────────────────────────────────────────────


def _age(ts: datetime | None, now: datetime | None = None) -> str:
    if ts is None:
        return "-"
    now = now or datetime.now(UTC)
    s = max(0, int((now - ts).total_seconds()))
    if s < 3600:
        return f"{s // 60}m"
    if s < 48 * 3600:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def handle(ref_id: int) -> str:
    return try_format("gripe", ref_id) or f"gripe:{ref_id}"


def card_line(state: FixState) -> str | None:
    """The title card's ``fix:`` line, or ``None`` when not in the loop."""
    if not state.in_loop:
        return None
    bits = []
    if state.batch:
        bits.append(f"batch={state.batch}")
    bits.append(f"queue={state.queue or 'none'}")
    if state.who:
        bits.append(state.who)
    if state.tree:
        bits.append(f"tree={state.tree}")
    brief = state.newest_of(BRIEF_KIND)
    if brief is not None:
        bits.append(f"brief #{brief.ord} (round {state.rounds})")
    handoff = state.newest_of(HANDOFF_KIND)
    if handoff is not None:
        bits.append(f"handoff #{handoff.ord}")
    review = state.newest_of(REVIEW_KIND)
    if review is not None:
        bits.append(f"review {(state.verdict or '?').upper()} #{review.ord}")
    if state.landed:
        bits.append(f"landed {state.landed}")
    return "fix: " + " · ".join(bits) + " · view='fix'"


def render_fix_view(ref: Ref, state: FixState, head: str) -> str:
    """``view='fix'``: fields, then the newest brief/handoff/review in full."""
    lines = [head, ""]
    line = card_line(state)
    lines.append(line or "fix: not in the gripe-fix loop")
    if not state.in_loop:
        lines.append("start: " + _EDIT_NEXT.format(id=ref.id))
        return "\n".join(lines)
    for kind in FIX_KINDS:
        ev = state.newest_of(kind)
        if ev is None:
            continue
        older = [e.ord for e in state.events if e.kind == kind and e.ord != ev.ord]
        stamp = ev.created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
        tail = f" · older: #{', #'.join(str(o) for o in older)}" if older else ""
        lines += ["", f"## {ev.label} #{ev.ord} · {stamp}{tail}", ev.text.rstrip()]
    return "\n".join(lines)


def render_queue(
    states: list[FixState],
    titles: dict[int, str],
    *,
    queue: str | None,
    host: str | None,
    batch: str | None,
) -> str:
    """``get(id='/queue')``: counts per queue, then the rows grouped by queue."""
    now = datetime.now(UTC)
    live = [s for s in states if s.queue is not None]
    counts = {q: sum(1 for s in live if s.queue == q) for q in QUEUES}
    shown = [
        s
        for s in live
        if (queue is None or s.queue == queue)
        and (host is None or s.host == host)
        and (batch is None or s.batch == batch)
    ]
    head = f"# gripe-fix queue — {len(live)} in loop · " + " · ".join(
        f"{q} {n}" for q, n in counts.items() if n
    )
    lines = [head]
    filters = [
        f"{k}={v}" for k, v in (("queue", queue), ("host", host), ("batch", batch)) if v
    ]
    if filters:
        lines.append(f"filter: {' '.join(filters)} → {len(shown)} rows")
    for q in QUEUES:
        rows = [s for s in shown if s.queue == q]
        if not rows:
            continue
        lines += ["", f"## {q} ({len(rows)})"]
        rows.sort(key=lambda s: (s.batch or "~", s.ref_id))
        for s in rows:
            title = " ".join((titles.get(s.ref_id) or "").split())[:60]
            bits = [handle(s.ref_id), f"batch={s.batch or '-'}", s.who or "-"]
            if s.tree:
                bits.append(f"tree={s.tree}")
            if s.landed:
                bits.append(f"landed {s.landed}")
            bits.append(_age(s.last_at, now))
            bits.append(title)
            lines.append("  ".join(bits))
    if not shown:
        lines.append("" if not live else "\nno rows match the filter")
        if not live:
            lines.append("no gripes in the loop — " + _EDIT_NEXT.format(id="<id>"))
    lines += [
        "",
        "filters: args={'queue': '<" + "|".join(QUEUES) + ">', 'host': '<host>', "
        "'batch': '<slug>'}",
        "next: get(kind='gripe', id=<id>, view='fix') · get(kind='gripe', "
        "id='/batch', args={'batch': '<slug>'}) · edit(kind='gripe', id=[...], "
        "mode='fix', args={...})",
    ]
    return "\n".join(lines)


def render_batches(states: list[FixState]) -> str:
    """``get(id='/batch')``: the census — one row per batch."""
    live = [s for s in states if s.queue is not None]
    by_batch: dict[str, list[FixState]] = {}
    for s in live:
        by_batch.setdefault(s.batch or "(no batch)", []).append(s)
    lines = [f"# gripe-fix batches — {len(by_batch)} live · {len(live)} gripes"]
    for name in sorted(by_batch):
        rows = by_batch[name]
        mix = {q: sum(1 for s in rows if s.queue == q) for q in QUEUES}
        who = sorted({s.who for s in rows if s.who})
        trees = sorted({s.tree for s in rows if s.tree})
        landed = sorted({s.landed for s in rows if s.landed})
        bits = [
            name,
            f"{len(rows)} gripe{'s' if len(rows) != 1 else ''}",
            ", ".join(f"{q} {n}" for q, n in mix.items() if n),
        ]
        if who:
            bits.append(" ".join(who))
        if trees:
            bits.append("tree=" + ",".join(trees))
        if landed:
            bits.append("landed " + ",".join(landed))
        lines.append("  ".join(bits))
    if not by_batch:
        lines.append("no gripes in the loop — " + _EDIT_NEXT.format(id="<id>"))
    lines += [
        "",
        "next: get(kind='gripe', id='/batch', args={'batch': '<slug>'}) — the "
        "brief in force and every member's body and comments (a builder's "
        "file) · get(kind='gripe', id='/queue')",
    ]
    return "\n".join(lines)


def render_batch(
    batch: str,
    states: list[FixState],
    bodies: dict[int, str],
    comments: dict[int, list[tuple[int, str]]],
) -> str:
    """``get(id='/batch', args={'batch': slug})``: the brief once, then
    each member's title card, body and comments — what a builder without
    MCP access receives as a file."""
    members = sorted((s for s in states if s.batch == batch), key=lambda s: s.ref_id)
    lines = [
        f"# batch {batch} — {len(members)} gripe{'s' if len(members) != 1 else ''}"
    ]
    if not members:
        lines.append(
            "no gripe carries batch:" + batch + " — get(kind='gripe', id='/batch') "
            "lists the live batches"
        )
        return "\n".join(lines)
    # The brief in force: the newest brief per member; identical texts
    # render once with the member list.
    by_text: dict[str, list[int]] = {}
    for s in members:
        ev = s.newest_of(BRIEF_KIND)
        if ev is not None:
            by_text.setdefault(ev.text.strip(), []).append(s.ref_id)
    if not by_text:
        lines.append(
            "brief: none yet — edit(kind='gripe', id=[...], mode='fix', "
            "args={'brief': '<text>'})"
        )
    for text, ids in by_text.items():
        scope = "all" if len(ids) == len(members) else ", ".join(handle(i) for i in ids)
        lines += ["", f"## brief ({scope})", text]
    for s in members:
        title = " ".join((bodies.get(s.ref_id) or "").split("\n", 1)[0].split())[:80]
        lines += [
            "",
            f"## {handle(s.ref_id)} — {title}",
            f"queue={s.queue or 'none'} · STATUS:{s.status or '?'}"
            + (f" · {s.who}" if s.who else "")
            + (f" · tree={s.tree}" if s.tree else ""),
            "",
            (bodies.get(s.ref_id) or "").rstrip(),
        ]
        for ord_, text in comments.get(s.ref_id, []):
            lines.append(f"- comment {ord_}: {text.strip()}")
    return "\n".join(lines)


# ── the edit ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FixPatch:
    """A validated ``edit(mode='fix')`` request. ``None`` = untouched;
    ``''`` on a meta field or ``batch`` = clear."""

    batch: str | None = None
    host: str | None = None
    session: str | None = None
    tree: str | None = None
    brief: str | None = None
    handoff: str | None = None
    review: str | None = None
    findings: str | None = None
    landed: str | None = None

    @property
    def empty(self) -> bool:
        return all(getattr(self, k) is None for k in EDIT_KEYS)


def validate_patch(ref_id: Any, raw: dict[str, Any]) -> FixPatch:
    """Parse and check every key before any write (mcp-authoring rule 5)."""
    nxt = _EDIT_NEXT.format(id=ref_id if ref_id is not None else "<id>")
    unknown = sorted(k for k, v in raw.items() if k not in EDIT_KEYS and v is not None)
    if unknown:
        raise BadInput(
            f"edit(kind='gripe', mode='fix') does not take {unknown}",
            options=list(EDIT_KEYS),
            next=nxt,
        )
    vals: dict[str, str | None] = {}
    for k in EDIT_KEYS:
        v = raw.get(k)
        if v is None:
            continue
        if not isinstance(v, str):
            raise BadInput(
                f"args['{k}'] must be a string, got {type(v).__name__}", next=nxt
            )
        vals[k] = v.strip()
    if not vals:
        raise BadInput(
            "edit(kind='gripe', mode='fix') needs at least one key",
            options=list(EDIT_KEYS),
            next=nxt,
        )
    batch = vals.get("batch")
    if batch and not _SLUG_RE.match(batch):
        raise BadInput(
            f"batch {batch!r} is not a slug (lowercase letters, digits, '.', '_', '-')",
            next=_EDIT_NEXT.format(id=ref_id).replace("<slug>", batch.lower()),
        )
    for k in ("host", "session", "tree"):
        v = vals.get(k)
        if v and not _NO_SPACE_RE.match(v):
            raise BadInput(
                f"args['{k}'] is one space-free token (got {v!r}); session reads "
                "<vendor>:<window> — claude:claude-<tree> | devin:devin-<tree> | "
                "codex:codex-<tree> (or claude:<session id> with no window)",
                next=nxt,
            )
    for k in ("brief", "handoff"):
        if k in vals and not vals[k]:
            raise BadInput(f"args['{k}'] is empty — it is the text itself", next=nxt)
    review = vals.get("review")
    if review is not None:
        review = review.lower()
        if review not in VERDICTS:
            raise BadInput(
                f"review verdict {vals['review']!r} is not one of {list(VERDICTS)}",
                options=list(VERDICTS),
                next=f"edit(kind='gripe', id={ref_id}, mode='fix', args={{'review': "
                "'land', 'findings': '<what was checked>'})",
            )
        vals["review"] = review
    if "findings" in vals and review is None:
        raise BadInput(
            "args['findings'] rides with args['review']",
            next=f"edit(kind='gripe', id={ref_id}, mode='fix', args={{'review': "
            "'fix', 'findings': '...'}})",
        )
    landed = vals.get("landed")
    if landed and not _SHA_RE.match(landed):
        raise BadInput(
            f"landed {landed!r} is not a 7-40 char lowercase hex sha",
            next=f"edit(kind='gripe', id={ref_id}, mode='fix', args={{'landed': "
            "'<sha on main>'}})",
        )
    return FixPatch(**vals)


def plan_status(status: str | None, patch: FixPatch) -> str | None:
    """The STATUS the edit flips to, or ``None`` when it stays.

    A brief or batch takes an ``open``/``triaged`` gripe to
    ``ready_for_fix``; a session, handoff, review or landed sha takes
    anything below ``in_review`` to ``in_review`` (the triage tick leases
    ``STATUS:open``, so a gripe in the loop must leave it). ``verify``
    and the terminal values are never touched here.
    """
    if status in ("in_review", "verify") or status in _TERMINAL:
        return None
    active = patch.session or patch.handoff or patch.review or patch.landed
    if active and status in (None, "open", "triaged", "ready_for_fix"):
        return "in_review"
    if (patch.brief or patch.batch) and status in (None, "open", "triaged"):
        return "ready_for_fix"
    return None


def review_text(verdict: str, findings: str | None) -> str:
    head = verdict.upper()
    return f"{head} — {findings}" if findings else head


def apply_patch(
    store: Any,
    ref: Ref,
    tags: list[Any],
    patch: FixPatch,
    *,
    dry_run: bool,
) -> str:
    """Write the patch in one transaction and return the ack body.

    Refuses a terminal gripe. Chunks are appended brief, handoff, review
    (ords continue the shared timeline); the ``batch:`` tag is replaced;
    ``meta.fix`` is merged with ``''`` clearing a key; the STATUS flip
    from :func:`plan_status` goes in the same transaction.
    """
    rid = ref.id
    chunks = store.chunks.list_chunks_for_ref(rid)
    before = fix_state(store, ref, tags, chunks)
    if before.terminal:
        raise BadInput(
            f"gripe id={rid} is STATUS:{before.status} — a closed gripe takes no "
            "fix-loop state",
            next=f"tag(kind='gripe', id={rid}, add=['STATUS:open']) to reopen, "
            "then repeat the edit",
        )
    changes: list[str] = []
    # tags
    old_batch_tags = [t for t in tags if str(t).startswith(BATCH_PREFIX)]
    new_batch_tag: Tag | None = None
    if patch.batch is not None:
        if patch.batch and patch.batch != before.batch:
            new_batch_tag = Tag.parse_strict(
                f"{BATCH_PREFIX}{patch.batch}", kind="gripe"
            )
            changes.append(
                f"batch {before.batch} → {patch.batch}"
                if before.batch
                else f"batch={patch.batch}"
            )
        elif not patch.batch and before.batch:
            changes.append(f"batch {before.batch} removed")
    # meta
    old_meta = (ref.meta or {}).get(META_KEY)
    fix = dict(old_meta) if isinstance(old_meta, dict) else {}
    for k in META_FIELDS:
        v = getattr(patch, k)
        if v is None:
            continue
        if v:
            if fix.get(k) != v:
                changes.append(f"{k}={v}" if not fix.get(k) else f"{k} {fix[k]} → {v}")
            fix[k] = v
        elif k in fix:
            changes.append(f"{k} cleared")
            del fix[k]
    # chunks
    inserts: list[tuple[str, str]] = []
    if patch.brief:
        inserts.append((BRIEF_KIND, patch.brief))
    if patch.handoff:
        inserts.append((HANDOFF_KIND, patch.handoff))
    if patch.review:
        inserts.append((REVIEW_KIND, review_text(patch.review, patch.findings)))
    ords = _plan_ords(inserts, chunks)
    for (kind, _text), (_k, o) in zip(inserts, ords, strict=True):
        label = KIND_LABEL[kind]
        if kind == BRIEF_KIND:
            changes.append(f"brief #{o} appended (round {before.rounds + 1})")
        elif kind == REVIEW_KIND:
            changes.append(f"review {(patch.review or '').upper()} #{o} appended")
        else:
            changes.append(f"{label} #{o} appended")
    new_status = plan_status(before.status, patch)
    if new_status:
        changes.append(f"STATUS:{before.status} → STATUS:{new_status}")
    if not changes:
        return f"gripe id={rid}: nothing to change (state already as asked)"
    # the derived queue after the edit (projected, no re-read needed)
    events = list(before.events)
    now = datetime.now(UTC)
    for (kind, text), (_k, o) in zip(inserts, ords, strict=True):
        events.append(FixEvent(kind=kind, ord=o, text=text, created_at=now))
    after = FixState(
        ref_id=rid,
        batch=(patch.batch or None) if patch.batch is not None else before.batch,
        host=fix.get("host"),
        session=fix.get("session"),
        tree=fix.get("tree"),
        landed=fix.get("landed"),
        status=new_status or before.status,
        events=tuple(events),
        in_loop=True,
    )
    queue_line = (
        f"queue {before.queue or 'none'} → {after.queue or 'none'}"
        if before.queue != after.queue
        else f"queue {after.queue or 'none'}"
    )
    first = f"gripe id={rid}: " + " · ".join(changes) + f" · {queue_line}"
    if dry_run:
        return f"dry run: {first} (nothing written)"

    def write(ords: list[tuple[str, int]]) -> None:
        with store.tx() as conn:
            if new_batch_tag is not None or (patch.batch == "" and old_batch_tags):
                for t in old_batch_tags:
                    store.remove_tag(rid, t, conn=conn)
                if new_batch_tag is not None:
                    store.add_tag(rid, new_batch_tag, set_by="agent", conn=conn)
            store.update_ref(rid, meta_patch={META_KEY: fix}, conn=conn)
            if inserts:
                store.chunks.insert_chunks(
                    rid,
                    [
                        ChunkInsert(ord=o, text=text, meta={"chunk_kind": kind})
                        for (kind, text), (_k, o) in zip(inserts, ords, strict=True)
                    ],
                    conn=conn,
                )
            if new_status:
                store.add_tag(
                    rid,
                    Tag.closed("STATUS", new_status),
                    set_by="agent",
                    replace_prefix=True,
                    conn=conn,
                )

    # Same contract as _comment_timeline.append_comment: a concurrent
    # append (triage tick, auto_gripe, a comment) can take the planned
    # ord — recompute once and retry; a second collision surfaces. A
    # missing chunk_kind seed (0254 not applied here) is server-side
    # drift, named as Upstream, never a raw driver error.
    try:
        try:
            write(ords)
        except UniqueViolation:
            write(_plan_ords(inserts, store.chunks.list_chunks_for_ref(rid)))
    except ForeignKeyViolation as e:
        raise Upstream(
            f"could not append fix-loop chunks to gripe id={rid} — "
            "likely missing chunk_kind seed",
            next=[
                "verify: SELECT slug FROM chunk_kinds WHERE slug IN "
                "('gripe_brief','gripe_handoff','gripe_review')",
                "re-apply migration 0254_gripe_fix_chunk_kinds against this DB",
            ],
        ) from e
    return (
        first + f"\nnext: get(kind='gripe', id={rid}, view='fix') · "
        "get(kind='gripe', id='/queue')"
    )


def _plan_ords(
    inserts: list[tuple[str, str]], chunks: list[ChunkRow]
) -> list[tuple[str, int]]:
    """``max(existing ord, 0) + 1`` onward — the shared timeline's rule."""
    next_ord = max((c.ord for c in chunks), default=0) + 1
    out: list[tuple[str, int]] = []
    for kind, _text in inserts:
        out.append((kind, next_ord))
        next_ord += 1
    return out


__all__ = [
    "BATCH_PREFIX",
    "BRIEF_KIND",
    "EDIT_KEYS",
    "FIX_KINDS",
    "HANDOFF_KIND",
    "META_KEY",
    "QUEUES",
    "REVIEW_KIND",
    "UNPICKED_AFTER",
    "UNPICKED_STATUSES",
    "VERDICTS",
    "FixEvent",
    "FixPatch",
    "FixState",
    "apply_patch",
    "build_state",
    "bulk_states",
    "card_line",
    "claimed_work_ids",
    "fix_state",
    "plan_status",
    "render_batch",
    "render_batches",
    "render_fix_view",
    "render_queue",
    "stale_unpicked",
    "state_from",
    "validate_patch",
    "verdict_of",
]
