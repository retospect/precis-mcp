"""``factory_rescue`` — one rescue tag, one gripe per failure signature.

Dark-factory td574946, slice S1: every factory failure ends in done or
rescue. This module is the mechanism itself — no caller exists yet;
later slices wire the stall watchdog, the diagnose task and the cap
exits to :func:`enter_rescue`.

A node in RESCUE carries the open tag ``rescue:<class>``. ``enter_rescue``
is the one writer: it tags the node once, files or finds ONE live gripe
per failure signature (``rescue-sig:<sha12>``), links node → gripe, and
records the evidence as a comment. The signature normalises the failing
test / error line / last log line so a job handle or timestamp does not
split one failure into many gripes; the lookup+create run under one
``pg_advisory_xact_lock`` inside ``store.atomic()`` so two concurrent
callers cannot both file (the ``release_poll`` mint-lock pattern).

Two classes of failure read differently downstream:

* task classes (``builder`` … ``intake``) — the work failed; the gripe
  collects every node the same signature defeated;
* engine classes (``engine=True`` in :data:`BOUNDS`) — the factory's own
  machinery failed; their gripe additionally carries
  ``cluster:dark-factory``, a ``gripe_dispatch`` hold tag, so an engine
  gripe never re-enters a build lane by the intake door.

A plan root is tagged and commented but never linked: ``land_slice``
takes a root's lowest ``related-to`` gripe as the ``resolved:`` target,
so a rescue link there would misfile the land.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.errors import BadInput, NotFound
from precis.plans.nodes import node_kind
from precis.plans.templates import PENDING_SHA
from precis.store.types import ChunkInsert, Tag
from precis.todo import BLOCKER_TERMINAL_STATUSES, TERMINAL_TODO_STATUSES
from precis.todo.views import _fetch_subtree
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: ``pg_advisory_xact_lock`` key (the "rescue__" bytes) serialising the
#: signature-gripe lookup+create inside :func:`enter_rescue`.
_SIG_LOCK_KEY = 0x7265736375655F5F

#: Gripe STATUS values that retire a signature gripe: a closed one is
#: never reused — the same failure surfacing again files a fresh gripe.
_TERMINAL_GRIPES: tuple[str, ...] = ("done", "won't-do", "abandoned", "wontfix")

#: Comment chunk kinds, mirroring ``handlers.todo._COMMENT_KIND`` /
#: ``handlers.gripe``'s (workers never import handlers).
_COMMENT_KIND = {"todo": "todo_comment", "gripe": "gripe_comment"}

#: Log lines kept on the evidence comment and the gripe body.
_LOG_TAIL_LINES = 40
#: Normalised signature-key length cap.
_KEY_CAP = 300

_HEX_RUN = re.compile(r"[0-9a-f]{7,}")
_DIGIT_RUN = re.compile(r"\d+")
_WS_RUN = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Bound:
    """One failure class's bounds, as data (td574946 S1).

    ``retries`` is the budget a caller honours before rescuing (ruling
    2: attempt 2 on the same lane, attempt 3 climbs one rung, then
    rescue); ``engine`` marks the classes whose failure is the factory's
    own machinery — their gripe carries ``cluster:dark-factory``.
    """

    retries: int
    engine: bool
    note: str = ""


#: The failure classes a node can enter rescue under. Nothing reads the
#: table in S1 but ``enter_rescue``'s ``engine`` flag; callers land in
#: later slices.
BOUNDS: dict[str, Bound] = {
    "builder": Bound(retries=3, engine=False),
    "land": Bound(retries=3, engine=False),
    "review": Bound(retries=2, engine=False),
    "release": Bound(retries=2, engine=False),
    "lane": Bound(
        retries=0, engine=False, note="a lane waits on a deadline, not retries"
    ),
    "intake": Bound(retries=3, engine=False),
    "stalled": Bound(retries=0, engine=True),
    "cancelled": Bound(retries=0, engine=True),
    "terminal-root": Bound(retries=0, engine=True),
    "tick-raises": Bound(retries=0, engine=True),
    "tick-silent": Bound(retries=0, engine=True),
    "post-land-write": Bound(retries=0, engine=True),
    "wait-deadline": Bound(retries=0, engine=True),
    "infra": Bound(retries=0, engine=True),
}


@dataclass(frozen=True, slots=True)
class RescueEvidence:
    """What a caller knows about the failure.

    ``log_tail`` keeps only its last 40 lines on write. ``failing_test``
    wins the signature key, then ``error_line``, then the last non-empty
    ``log_tail`` line — in that order so the strongest identity leads.
    """

    step: str = ""
    log_tail: str = ""
    job: str | None = None
    diff_stat: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    failing_test: str | None = None
    error_line: str | None = None


@dataclass(frozen=True, slots=True)
class RescueResult:
    """What one ``enter_rescue`` call did.

    ``skipped`` non-empty means the call wrote nothing: the kill-switch
    reason, or the node's existing ``rescue:`` tag. ``tagged`` is this
    call's tag write; ``gripe_id`` the signature gripe the node joined
    (the node itself when it is a gripe); ``gripe_created`` whether this
    call filed it.
    """

    skipped: str = ""
    tagged: bool = False
    gripe_id: int | None = None
    gripe_created: bool = False


def _norm_key(text: str) -> str:
    """Collapse a signature key's volatile parts.

    Hex runs of 7+ chars → ``<sha>`` FIRST: a sha containing digits must
    still collapse as one blob — digit runs first would shatter it.
    Then digit runs → ``#`` (job ids, timestamps), whitespace collapsed,
    cut at 300 chars.
    """
    s = _HEX_RUN.sub("<sha>", text.lower())
    s = _DIGIT_RUN.sub("#", s)
    return _WS_RUN.sub(" ", s).strip()[:_KEY_CAP]


def _sig_key(cls: str, ev: RescueEvidence) -> tuple[str, str]:
    """``(normalised key, 12-char signature)`` for this failure."""
    key = ev.failing_test or ev.error_line
    if not key:
        tail = [ln for ln in ev.log_tail.splitlines() if ln.strip()]
        key = tail[-1] if tail else ""
    key = _norm_key(key)
    sig = hashlib.sha256(f"{cls}\n{key}".encode()).hexdigest()[:12]
    return key, sig


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-_LOG_TAIL_LINES:])


def _comment(
    store: Store, conn: Connection, ref_id: int, chunk_kind: str, text: str
) -> None:
    """Append ``text`` as the ref's next comment chunk (the
    ``gripe_dispatch._comment`` shape: ord = max ord + 1, append-only)."""
    row = conn.execute(
        "SELECT coalesce(max(ord), -1) + 1 FROM chunks WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(
                ord=int(row[0]) if row else 0,
                text=text,
                meta={"chunk_kind": chunk_kind},
            )
        ],
        conn=conn,
    )


def _file_sig_gripe(
    store: Store,
    conn: Connection,
    cls: str,
    key: str,
    sig: str,
    node_handle: str,
    ev: RescueEvidence,
    bound: Bound,
) -> int:
    """Insert the signature gripe (the ``bounded_heal._file_cap_gripe``
    shape): one ref, one ``gripe_body`` chunk, ``STATUS:open``, the
    ``rescue-sig:<sig>`` + ``origin:factory-rescue`` tags — and
    ``cluster:dark-factory`` for an engine class. Never ``fix:code`` and
    never a Claude-Code/human ``filed_by``, so it can never be a
    ``gripe_dispatch.candidates`` row."""
    title = f"[rescue] {cls}: {key}"
    if len(title) > 200:
        title = title[:197].rstrip() + "…"
    body_lines = [
        f"rescue class: {cls}",
        f"signature key: {key}",
        f"first node: {node_handle}",
    ]
    if ev.job:
        body_lines.append(f"job: {ev.job}")
    if ev.step:
        body_lines.append(f"step: {ev.step}")
    if ev.branch:
        body_lines.append(f"branch: {ev.branch}")
    if ev.head_sha:
        body_lines.append(f"head: {ev.head_sha}")
    body_lines += ["", "log tail:", _tail(ev.log_tail)]
    ref = store.insert_ref(kind="gripe", slug=None, title=title, meta={}, conn=conn)
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="\n".join(body_lines),
                meta={"chunk_kind": "gripe_body"},
            )
        ],
        conn=conn,
    )
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", "open"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    store.add_tag(ref.id, Tag.open(f"rescue-sig:{sig}"), set_by="system", conn=conn)
    store.add_tag(ref.id, Tag.open("origin:factory-rescue"), set_by="system", conn=conn)
    if bound.engine:
        store.add_tag(
            ref.id,
            Tag.open("cluster:dark-factory"),
            set_by="system",
            conn=conn,
        )
    return int(ref.id)


def _evidence_comment(cls: str, ev: RescueEvidence) -> str:
    lines = [f"rescue: {cls}"]
    if ev.job:
        lines.append(f"job: {ev.job}")
    if ev.step:
        lines.append(f"step: {ev.step}")
    if ev.branch or ev.head_sha:
        lines.append(f"branch: {ev.branch or '?'} @ {ev.head_sha or '?'}")
    if ev.diff_stat:
        lines += ["diff stat:", ev.diff_stat]
    lines += ["log tail:", _tail(ev.log_tail)]
    return "\n".join(lines)


def _live_sig_gripe(conn: Connection, sig: str) -> int | None:
    """The live gripe carrying ``rescue-sig:<sig>``, or ``None``."""
    row = conn.execute(
        """
        SELECT g.ref_id FROM refs g
         WHERE g.kind = 'gripe' AND g.retired_at IS NULL
           AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = g.ref_id AND t.namespace = 'OPEN'
                          AND t.value = %s)
           AND COALESCE(
                 (SELECT s.value FROM ref_tags rs
                    JOIN tags s ON s.tag_id = rs.tag_id
                   WHERE rs.ref_id = g.ref_id AND s.namespace = 'STATUS'
                   LIMIT 1),
                 'open') <> ALL(%s)
         ORDER BY g.ref_id LIMIT 1
        """,
        (f"rescue-sig:{sig}", list(_TERMINAL_GRIPES)),
    ).fetchone()
    return int(row[0]) if row else None


def enter_rescue(
    store: Store, node: int, cls: str, evidence: RescueEvidence
) -> RescueResult:
    """Put ``node`` (a todo or gripe ref id) in RESCUE under class ``cls``.

    Writes, in order: the ``rescue:<cls>`` tag (once — a repeat call is
    a no-op), a ``related-to`` link to the signature gripe (never for a
    plan root — see the module docstring), the evidence comment. The
    signature gripe is found-or-filed under one transaction and the
    advisory lock, so N nodes on one signature share exactly one gripe;
    a node that IS a gripe (class ``intake``) is its own signature
    gripe — nothing is filed and no self-link is attempted. The tag
    check runs twice: once unlocked as a fast path, then again under
    the advisory lock so two racing calls on one node cannot both tag.
    """
    from precis.workers.job_types.plan_dispatch import read_only_reason

    reason = read_only_reason(store)
    if reason is not None:
        log.info("factory_rescue: read-only (%s) — no writes", reason)
        return RescueResult(skipped=reason)

    ref = store.fetch_refs_by_ids([node], include_deleted=False).get(node)
    if ref is None:
        raise NotFound(
            f"ref {node} not found (or retired)",
            next="enter_rescue takes a live todo or gripe ref id",
        )
    if ref.kind not in ("todo", "gripe"):
        raise BadInput(
            f"rescue takes a todo or gripe ref, got kind {ref.kind!r}",
            options=["todo", "gripe"],
        )

    open_tags = [str(t.value) for t in store.tags_for(node) if t.namespace == "open"]
    held = next((t for t in open_tags if t.startswith("rescue:")), None)
    if held is not None:
        return RescueResult(skipped=f"already in rescue ({held})")

    bound = BOUNDS.get(cls)
    if bound is None:
        raise BadInput(
            f"unknown rescue class {cls!r}",
            options=sorted(BOUNDS),
        )

    key, sig = _sig_key(cls, evidence)
    kind = str(ref.kind)
    node_handle = handle_registry.format_handle(kind, node)
    is_gripe = kind == "gripe"
    # A plan root carries ``plan:<its own id>``; linking it to the rescue
    # gripe would make land_slice._plan_gripe write ``resolved:`` on it.
    is_plan_root = f"plan:{node}" in open_tags

    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SIG_LOCK_KEY,))
        # Recheck under the lock: a racer that passed the unlocked check
        # while the winner was mid-transaction sees its committed
        # ``rescue:`` tag here (READ COMMITTED snapshots per statement).
        held = next(
            (
                str(t.value)
                for t in store.tags_for(node, conn=conn)
                if t.namespace == "open" and str(t.value).startswith("rescue:")
            ),
            None,
        )
        if held is not None:
            return RescueResult(skipped=f"already in rescue ({held})")
        if is_gripe:
            gripe_id, gripe_created = node, False
        else:
            found = _live_sig_gripe(conn, sig)
            gripe_created = found is None
            gripe_id = (
                _file_sig_gripe(
                    store, conn, cls, key, sig, node_handle, evidence, bound
                )
                if found is None
                else int(found)
            )
        store.add_tag(node, Tag.open(f"rescue:{cls}"), set_by="system", conn=conn)
        if not is_gripe and not is_plan_root:
            store.add_link(
                src_ref_id=node,
                dst_ref_id=gripe_id,
                relation="related-to",
                set_by="system",
                conn=conn,
            )
        _comment(
            store, conn, node, _COMMENT_KIND[kind], _evidence_comment(cls, evidence)
        )
        if not is_gripe and not gripe_created:
            # A second or later node on one signature leaves a line on the
            # shared gripe naming the node and the commit it held.
            note = f"rescue: {cls} — {node_handle}"
            if evidence.branch:
                note += f" · branch {evidence.branch}"
            if evidence.head_sha:
                note += f" @ {evidence.head_sha}"
            _comment(store, conn, gripe_id, "gripe_comment", note)
    log.warning(
        "factory_rescue: %s -> %s (gripe %s, %s)",
        node_handle,
        cls,
        gripe_id,
        "filed" if gripe_created else "found",
    )
    return RescueResult(tagged=True, gripe_id=gripe_id, gripe_created=gripe_created)


# ── Stall watchdog (td574946 S2) ────────────────────────────────────
#
# ``run_stall_pass`` is the first caller of ``enter_rescue``: the
# ``factory_rescue_tick`` job's DB-only pass. It finds the plan nodes
# that went silent past their stage deadline and sends each to RESCUE —
# never dispatched, a cancelled newest job, queued/build/land/review
# overrunning, a sha-in-prod gate that never opens, a root closed under
# an open node, a land's post-land write that never landed — plus the
# ``tick-silent`` heartbeat on plan_dispatch itself. A hold is not a
# stall: ``waiting-for:``, ``halt``, paused, an open ask, ``rescue:`` or
# a live ``blocked-by`` target exempt a node. Release plans belong to
# ``release_poll::watch_stuck`` and are skipped. Read-only (kill switch
# or ``dry_run``) it lists ``would_rescue`` and writes nothing.

#: ``plan_dispatch`` is silent once its newest succeeded job's STATUS
#: stamp is this old (its recurring todo is rescued once, class
#: ``tick-silent``).
TICK_SILENT_AFTER = timedelta(minutes=30)

#: Per-stage stall deadlines. Release stages are deliberately absent —
#: ``release_poll::watch_stuck`` owns them.
STAGE_DEADLINES: dict[str, timedelta] = {
    "idle": timedelta(minutes=20),
    "queued": timedelta(minutes=30),
    "build": timedelta(hours=5),
    "land": timedelta(hours=6),
    "review": timedelta(minutes=45),
    "gate": timedelta(hours=72),
}

_NOTES_CAP = 60


@dataclass
class StallResult:
    """What one stall pass did (or, read-only, would have done).

    ``rescued`` / ``would_rescue`` carry ``(ref id, class, stage)``;
    ``held`` counts the judged-shaped nodes an exemption covered;
    ``read_only`` is the kill-switch reason that forced the dry run.
    """

    dry_run: bool
    read_only: str | None = None
    rescued: list[tuple[int, str, str]] = field(default_factory=list)
    would_rescue: list[tuple[int, str, str]] = field(default_factory=list)
    held: int = 0
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rescued": len(self.rescued),
            "would_rescue": len(self.would_rescue),
            "held": self.held,
            "dry_run": self.dry_run,
        }
        if self.read_only:
            out["read_only"] = self.read_only
        return out

    def summary(self) -> str:
        return (
            f"factory_rescue_tick: rescued={len(self.rescued)} "
            f"would_rescue={len(self.would_rescue)} held={self.held} "
            f"dry_run={self.dry_run}"
        )

    def render(self) -> str:
        lines = [self.summary()]
        if self.read_only:
            lines.append(
                f"read-only ({self.read_only}): would_rescue lists what it "
                "would rescue; nothing was written"
            )
        shown = self.notes[:_NOTES_CAP]
        lines += [f"- {n}" for n in shown]
        if len(self.notes) > len(shown):
            lines.append(f"- … {len(self.notes) - len(shown)} more")
        return "\n".join(lines)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _held_tags(open_tags: Any) -> str | None:
    """The hold an open tag grants, or ``None`` — spec ruling 1."""
    for v in open_tags:
        if str(v).startswith("waiting-for:"):
            return f"waiting-for ({v})"
        if v == "halt" or str(v).startswith("halt:"):
            return f"halt ({v})"
        # ``rescue:`` only — ``rescue-sig:`` is a different tag.
        if str(v).startswith("rescue:"):
            return f"in rescue ({v})"
        if v == "ask-user" or str(v).startswith("ask-user:"):
            return f"ask-user ({v})"
    return None


def _held_in_plan(plan: Any, n: Any) -> str | None:
    held = _held_tags(n.open_tags)
    if held:
        return held
    if plan.paused(n):
        return "paused"
    if plan.open_asks(n):
        return "an open ask blocks it"
    if plan.blocked(n):
        return "blocked-by a live node"
    return None


def _held_db(store: Store, ref_id: int) -> str | None:
    """``_held_in_plan`` for a node no ``_Plan`` covers (the tick todo)."""
    open_tags = [str(t.value) for t in store.tags_for(ref_id) if t.namespace == "open"]
    held = _held_tags(open_tags)
    if held:
        return held
    with store.pool.connection() as conn:
        ancestors: list[int] = []
        seen = {ref_id}
        cur = ref_id
        for _ in range(12):
            row = conn.execute(
                "SELECT parent_id FROM refs WHERE ref_id = %s", (cur,)
            ).fetchone()
            if row is None or row[0] is None or int(row[0]) in seen:
                break
            cur = int(row[0])
            seen.add(cur)
            ancestors.append(cur)
        ids = [ref_id, *ancestors]
        paused = conn.execute(
            """
            SELECT 1 FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.ref_id = ANY(%s) AND r.retired_at IS NULL
               AND t.namespace = 'STATUS' AND t.value = 'paused' LIMIT 1
            """,
            (ids,),
        ).fetchone()
        if paused:
            return "paused"
        live_blocker = conn.execute(
            """
            SELECT 1 FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
             WHERE l.src_ref_id = ANY(%s) AND l.relation = 'blocked-by'
               AND b.retired_at IS NULL
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                       WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open') <> ALL(%s)
             LIMIT 1
            """,
            (ids, sorted(BLOCKER_TERMINAL_STATUSES)),
        ).fetchone()
        if live_blocker:
            return "blocked-by a live node"
        ask = conn.execute(
            """
            SELECT 1 FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
             WHERE l.src_ref_id = %s AND l.relation = 'blocked-by'
               AND b.retired_at IS NULL
               AND (b.meta ? 'ask' OR EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = b.ref_id AND t.namespace = 'OPEN'
                        AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')))
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                       WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open') <> ALL(%s)
             LIMIT 1
            """,
            (ref_id, sorted(BLOCKER_TERMINAL_STATUSES)),
        ).fetchone()
    if ask:
        return "an open ask blocks it"
    return None


def _signal_rows(store: Store, ids: list[int]) -> dict[int, tuple[Any, ...]]:
    """``{ref_id: (created_at, tag_set, chunk_at, event_at)}`` — the
    signal sources of the stall clock, one query for every ref."""
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.created_at,
                   (SELECT max(rt.created_at) FROM ref_tags rt
                     WHERE rt.ref_id = r.ref_id),
                   (SELECT max(c.created_at) FROM chunks c
                     WHERE c.ref_id = r.ref_id),
                   (SELECT max(e.ts) FROM ref_events e
                     WHERE e.ref_id = r.ref_id)
              FROM refs r WHERE r.ref_id = ANY(%s)
            """,
            (ids,),
        ).fetchall()
    return {int(r[0]): tuple(r[1:]) for r in rows}


def _node_signals(
    n: Any, jobs: list[Any], rows: dict[int, tuple[Any, ...]]
) -> tuple[datetime, str, list[str]]:
    """``(newest ts, its label, the log lines)`` — the newest signal of a
    node is the latest of its created_at, its tag set times, its newest
    comment chunk, its newest event, and per child job its created_at,
    STATUS stamp and newest chunk."""
    signals: list[tuple[str, datetime]] = []
    created, tag_set, chunk_at, event_at = rows.get(n.id, (n.created_at,) + (None,) * 3)
    signals.append(("todo created", created))
    if tag_set is not None:
        signals.append(("tag set", tag_set))
    if chunk_at is not None:
        signals.append(("newest comment", chunk_at))
    if event_at is not None:
        signals.append(("newest event", event_at))
    for j in jobs:
        jrow = rows.get(j.id)
        jcreated = jrow[0] if jrow else j.created_at
        signals.append((f"job jo{j.id} created", jcreated))
        if jrow is not None and jrow[1] is not None:
            signals.append((f"job jo{j.id} status set", jrow[1]))
        if jrow is not None and jrow[2] is not None:
            signals.append((f"job jo{j.id} newest comment", jrow[2]))
    label, ts = max(signals, key=lambda s: s[1])
    lines = [f"signals for td{n.id}:"] + [f"- {lbl}: {_iso(t)}" for lbl, t in signals]
    return ts, label, lines


def _gate_kind(n: Any) -> str | None:
    gate = n.meta.get("gate") or {}
    return str(gate.get("kind")) if isinstance(gate, dict) else None


def _is_sha_gate(n: Any) -> bool:
    return node_kind(n.meta) == "gate" and _gate_kind(n) == "sha-in-prod"


def _lane_verdict(store: Store, now: datetime, n: Any) -> str | None:
    """``None`` to judge normally; the ``error_line`` for a no-fit stall;
    ``"parked"`` when the fit lanes are full or backed off (the live jobs
    on them are judged by their own deadlines)."""
    from precis.plans import lanes as _lanes

    task_class = str(n.meta.get("task_class") or "code")
    fit = [ln for ln in _lanes.load_lanes(store) if ln.enabled and ln.fits(task_class)]
    if not fit:
        return f"no enabled lane fits {task_class}"
    if _lanes.pick_lane(store, task_class) is not None:
        return None
    counts = _lanes.live_lane_counts(store)
    backoffs = _lanes.lane_backoffs(store, now)
    full = [ln.name for ln in fit if counts.get(ln.name, 0) >= ln.cap]
    backed = [ln.name for ln in fit if ln.name in backoffs]
    why = ", ".join(
        [f"full: {', '.join(sorted(full))}" if full else ""]
        + [f"backed off: {', '.join(sorted(backed))}" if backed else ""]
    ).strip(", ")
    return f"parked ({why or 'fit lanes busy'})"


def _stage_for(
    store: Store, now: datetime, plan: Any, n: Any, is_gate: bool
) -> tuple[str, str, Any, str] | None:
    """``(class, stage, newest job or None, stable error_line)`` by stage
    precedence: cancelled, post-land-write, gate, queued, land/review/
    build by live job, idle. ``None`` when the node is parked."""
    from precis.workers.job_types import plan_dispatch as pd

    jobs = plan.jobs(n)
    j = jobs[-1] if jobs else None
    if not is_gate and j is not None and j.status == "cancelled":
        # gr574402: the tick notes the cancelled job but never redispatches.
        return "cancelled", "idle", j, "newest job cancelled"
    if is_gate:
        sha = ((n.meta.get("gate") or {}).get("args") or {}).get("sha")
        if str(sha) == PENDING_SHA and not plan.blocked(n):
            return (
                "post-land-write",
                "idle",
                j,
                "sha-in-prod gate still holds the placeholder sha "
                "after the build landed",
            )
        return "stalled", "gate", j, "stage gate silent past its deadline"
    live = next((x for x in reversed(jobs) if x.status not in pd._JOB_TERMINAL), None)
    if live is not None:
        if live.status == "queued":
            return (
                "stalled",
                "queued",
                live,
                "stage queued silent past its deadline",
            )
        jt = str(live.meta.get("job_type") or "")
        if jt == "land_slice":
            return "stalled", "land", live, "stage land silent past its deadline"
        if jt == "run_slice" and pd._is_reviewer(n):
            return (
                "stalled",
                "review",
                live,
                "stage review silent past its deadline",
            )
        return "stalled", "build", live, "stage build silent past its deadline"
    job_type, _params = pd._resolve_op(n)
    if job_type == pd.DEFAULT_OP:
        verdict = _lane_verdict(store, now, n)
        if verdict is not None:
            if verdict.startswith("parked"):
                return "parked", "parked", j, verdict
            return "stalled", "idle", j, verdict
    return "stalled", "idle", j, "stage idle silent past its deadline"


def _branch_head(plan: Any, n: Any) -> str | None:
    """The head the newest committing ``run_slice`` job left on the
    slice's branch — the first token of the newest
    ``meta.branch_commits`` line (run_slice stamps
    ``git log --oneline origin/main..HEAD``, newest first)."""
    for j in reversed(plan.jobs(n)):
        if j.meta.get("job_type") != "run_slice":
            continue
        commits = j.meta.get("branch_commits") or []
        first = str(commits[0]).split() if commits else []
        if first:
            return first[0]
    return None


def _landed_line(store: Store, plan: Any, gate: Any) -> str | None:
    """The ``landed: <sha>`` comment the gate's build left, if any."""
    for b in plan.blockers.get(gate.id, []):
        chunks = sorted(
            store.chunks.list_chunks_for_ref(b.id),
            key=lambda c: int(c.ord),
            reverse=True,
        )
        for c in chunks:
            for line in str(c.text).splitlines():
                if line.strip().lower().startswith("landed:"):
                    return line.strip()
    return None


def _rescue(
    store: Store,
    res: StallResult,
    ref_id: int,
    cls: str,
    stage: str,
    ev: RescueEvidence,
) -> None:
    if res.dry_run:
        res.would_rescue.append((ref_id, cls, stage))
        res.notes.append(f"td{ref_id}: would rescue ({cls}, stage {stage})")
        return
    try:
        r = enter_rescue(store, ref_id, cls, ev)
    except Exception as exc:  # one failing node must not stop the pass
        log.warning("factory_rescue: rescue of td%d raised", ref_id, exc_info=True)
        res.notes.append(f"td{ref_id}: rescue failed ({type(exc).__name__}: {exc})")
        return
    if r.tagged:
        res.rescued.append((ref_id, cls, stage))
        res.notes.append(
            f"td{ref_id}: rescued ({cls}, stage {stage}, gripe gr{r.gripe_id})"
        )
    elif r.skipped:
        res.notes.append(f"td{ref_id}: rescue skipped ({r.skipped})")


def _evidence(
    n: Any,
    cls: str,
    stage: str,
    j: Any,
    newest: datetime,
    label: str,
    lines: list[str],
    deadline: timedelta,
    now: datetime,
    error_line: str,
    *,
    branch: str | None = None,
    head_sha: str | None = None,
) -> RescueEvidence:
    deadline_min = int(deadline.total_seconds() // 60)
    age_min = int((now - newest).total_seconds() // 60)
    step = (
        f"stage {stage}; deadline {deadline_min} min; newest signal {label} "
        f"at {_iso(newest)}, {age_min} min ago"
    )
    job = handle_registry.format_handle("job", j.id) if j is not None else None
    return RescueEvidence(
        step=step,
        log_tail="\n".join(lines),
        job=job,
        branch=branch,
        head_sha=head_sha,
        error_line=error_line,
    )


def _judge_node(
    store: Store, now: datetime, plan: Any, n: Any, is_gate: bool, res: StallResult
) -> None:
    held = _held_in_plan(plan, n)
    if held is not None:
        res.held += 1
        return
    verdict = _stage_for(store, now, plan, n, is_gate)
    if verdict is None:
        return
    cls, stage, j, error_line = verdict
    if cls == "parked":
        res.held += 1
        res.notes.append(f"td{n.id}: {error_line} — a parked node is not a stall")
        return
    jobs = plan.jobs(n)
    ids = [n.id] + [x.id for x in jobs]
    newest, label, lines = _node_signals(n, jobs, _signal_rows(store, ids))
    deadline = STAGE_DEADLINES[stage]
    if now - newest <= deadline:
        return
    branch = head_sha = None
    head = _branch_head(plan, n)
    if head is not None:
        from precis.workers.job_types.run_slice import slice_branch

        branch, head_sha = slice_branch(n.id), head
    if cls == "post-land-write":
        landed = _landed_line(store, plan, n)
        if landed:
            lines.append(f"build: {landed}")
    if error_line.startswith("no enabled lane"):
        lines.append(f"lane check: {error_line}")
    ev = _evidence(
        n,
        cls,
        stage,
        j,
        newest,
        label,
        lines,
        deadline,
        now,
        error_line,
        branch=branch,
        head_sha=head_sha,
    )
    _rescue(store, res, n.id, cls, stage, ev)


def _release_skip_roots(plan: Any) -> set[int]:
    """Roots whose nodes the watchdog never judges: ``release-pass`` plans
    (release stages belong to ``release_poll::watch_stuck``) and any root
    with a live non-terminal ``release-stuck:*`` todo under it."""
    skip: set[int] = set()
    for root in plan.roots:
        rn = plan.nodes.get(root)
        if rn is not None and "release-pass" in rn.open_tags:
            skip.add(root)
            continue
        for tn in plan.todos():
            if tn.root != root or tn.status in TERMINAL_TODO_STATUSES:
                continue
            if any(v.startswith("release-stuck:") for v in tn.open_tags):
                skip.add(root)
                break
    return skip


def _subtree_plan(store: Store, root: int) -> Any:
    """A ``_Plan`` over one (possibly terminal) root's subtree — the
    ``_load`` shape so ``paused`` / ``blocked`` / ``open_asks`` work."""
    from precis.workers.job_types import plan_dispatch as pd

    rows = _fetch_subtree(store, root)
    placement = {int(r["id"]): (root, int(r["depth"])) for r in rows}
    if not placement:
        return pd._Plan(roots=[root], nodes={}, blockers={})
    ids = sorted(placement)
    with store.pool.connection() as conn:
        ref_rows = conn.execute(
            """
            SELECT r.ref_id, r.kind, r.parent_id, r.title, r.meta, r.created_at,
                   r.prio,
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1)
              FROM refs r
             WHERE r.ref_id = ANY(%s) AND r.retired_at IS NULL
            """,
            (ids,),
        ).fetchall()
        tag_rows = conn.execute(
            """
            SELECT rt.ref_id, t.value, rt.created_at
              FROM ref_tags rt JOIN tags t USING (tag_id)
             WHERE rt.ref_id = ANY(%s) AND t.namespace = 'OPEN'
            """,
            (ids,),
        ).fetchall()
        link_rows = conn.execute(
            """
            SELECT l.src_ref_id, b.ref_id, b.created_at, b.meta ? 'ask',
                   EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = b.ref_id AND t.namespace = 'OPEN'
                              AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')),
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS' LIMIT 1)
              FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
             WHERE l.relation = 'blocked-by' AND l.src_ref_id = ANY(%s)
               AND b.retired_at IS NULL
            """,
            (ids,),
        ).fetchall()
    nodes: dict[int, Any] = {}
    for ref_id, kind, parent_id, title, meta, created_at, prio, status in ref_rows:
        r, depth = placement[int(ref_id)]
        nodes[int(ref_id)] = pd._Node(
            id=int(ref_id),
            kind=str(kind),
            parent_id=int(parent_id) if parent_id is not None else None,
            title=str(title or ""),
            meta=dict(meta or {}),
            status=str(status or ("open" if kind == "todo" else "queued")),
            created_at=created_at,
            prio=int(prio) if prio is not None else None,
            depth=depth,
            root=r,
        )
    for ref_id, value, set_at in tag_rows:
        node = nodes.get(int(ref_id))
        if node is not None:
            node.open_tags[str(value)] = set_at
    for node in nodes.values():
        if node.parent_id is not None and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node.id)
    blockers: dict[int, list[Any]] = {}
    for src, dst, created_at, has_ask, ask_tag, status in link_rows:
        blockers.setdefault(int(src), []).append(
            pd._Blocker(
                id=int(dst),
                status=str(status or "open"),
                is_ask=bool(has_ask) or bool(ask_tag),
                created_at=created_at,
            )
        )
    return pd._Plan(roots=[root], nodes=nodes, blockers=blockers)


def _terminal_root_ids(conn: Any) -> list[int]:
    """``plan:<n>`` roots (from open tags on live todos) whose own STATUS
    is terminal — ``_candidate_roots`` drops them, so the watchdog finds
    the non-terminal nodes they closed over here."""
    values = conn.execute(
        """
        SELECT DISTINCT t.value
          FROM ref_tags rt JOIN tags t USING (tag_id)
          JOIN refs r ON r.ref_id = rt.ref_id
         WHERE t.namespace = 'OPEN' AND t.value LIKE 'plan:%%'
           AND r.kind = 'todo' AND r.retired_at IS NULL
        """
    ).fetchall()
    ids = {int(v[5:]) for (v,) in values if str(v)[5:].isdigit()}
    if not ids:
        return []
    rows = conn.execute(
        """
        SELECT r.ref_id FROM refs r
         WHERE r.ref_id = ANY(%s) AND r.kind = 'todo' AND r.retired_at IS NULL
           AND COALESCE(
                 (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                 'open') = ANY(%s)
         ORDER BY r.ref_id
        """,
        (sorted(ids), sorted(TERMINAL_TODO_STATUSES)),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _heartbeat(store: Store, now: datetime, res: StallResult) -> None:
    """The tick-silent check: plan_dispatch's newest succeeded job by
    STATUS stamp older than ``TICK_SILENT_AFTER`` (or none ever) rescues
    the ONE recurring tick todo, once — its ``rescue:`` tag then exempts
    it. A lane ticker (``--only plan_tick``) runs the pass with no job
    row, so a factory ticked only by the lane is not covered."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT max(rt.created_at)
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job' AND r.retired_at IS NULL
               AND r.meta->>'job_type' = 'plan_dispatch'
               AND t.namespace = 'STATUS' AND t.value = 'succeeded'
            """
        ).fetchone()
        last_pass = row[0] if row else None
        if last_pass is not None and now - last_pass <= TICK_SILENT_AFTER:
            return
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND r.meta->>'job_type' = 'plan_dispatch'
               AND r.meta ? 'schedule'
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open') <> ALL(%s)
             ORDER BY r.ref_id LIMIT 1
            """,
            (sorted(TERMINAL_TODO_STATUSES),),
        ).fetchall()
    if not rows:
        res.notes.append("tick-silent: no live recurring plan_dispatch todo")
        return
    tid = int(rows[0][0])
    held = _held_db(store, tid)
    if held is not None:
        res.notes.append(f"tick-silent: tick todo td{tid} held ({held}); not judged")
        return
    created, tag_set, chunk_at, event_at = _signal_rows(store, [tid])[tid]
    signals = [
        (lbl, ts)
        for lbl, ts in (
            ("todo created", created),
            ("tag set", tag_set),
            ("newest comment", chunk_at),
            ("newest event", event_at),
        )
        if ts is not None
    ]
    newest = max(ts for _lbl, ts in signals)
    lines = [f"signals for td{tid}:"] + [f"- {lbl}: {_iso(ts)}" for lbl, ts in signals]
    if last_pass is None:
        # No pass ever ran: the tick todo's own signal is the clock — a
        # todo armed under TICK_SILENT_AFTER ago had no window to pass.
        if now - newest <= TICK_SILENT_AFTER:
            return
        age_min = int((now - newest).total_seconds() // 60)
        step = (
            f"plan_dispatch ran no pass in {age_min} min (no succeeded pass recorded)"
        )
    else:
        age_min = int((now - last_pass).total_seconds() // 60)
        step = (
            f"plan_dispatch ran no pass in {age_min} min "
            f"(newest succeeded pass at {_iso(last_pass)})"
        )
    ev = RescueEvidence(
        step=step, log_tail="\n".join(lines), error_line="plan_dispatch has run no pass"
    )
    _rescue(store, res, tid, "tick-silent", "heartbeat", ev)


def _judge_terminal_roots(
    store: Store, now: datetime, plan: Any, res: StallResult
) -> None:
    """Non-terminal, non-held nodes under a root whose STATUS is terminal
    — the root was closed over open work: class ``terminal-root``, the
    idle deadline on the node's own newest signal (so a root closed
    together with its nodes in one tick is not flagged mid-write)."""
    with store.pool.connection() as conn:
        roots = _terminal_root_ids(conn)
    for root in roots:
        mini = _subtree_plan(store, root)
        rn = mini.nodes.get(root)
        if rn is not None and "release-pass" in rn.open_tags:
            continue
        if any(
            v.startswith("release-stuck:")
            for tn in mini.todos()
            if tn.status not in TERMINAL_TODO_STATUSES
            for v in tn.open_tags
        ):
            continue
        judged = [
            n
            for n in mini.todos()
            if n.id != root
            and n.status not in TERMINAL_TODO_STATUSES
            and n.id not in plan.nodes
        ]
        rows = _signal_rows(
            store, [n.id for n in judged] + [j.id for n in judged for j in mini.jobs(n)]
        )
        for n in judged:
            held = _held_in_plan(mini, n)
            if held is not None:
                res.held += 1
                continue
            jobs = mini.jobs(n)
            newest, label, lines = _node_signals(n, jobs, rows)
            lines.append(f"root td{root} STATUS {rn.status if rn else '?'}")
            deadline = STAGE_DEADLINES["idle"]
            if now - newest <= deadline:
                continue
            head = _branch_head(mini, n)
            branch = head_sha = None
            if head is not None:
                from precis.workers.job_types.run_slice import slice_branch

                branch, head_sha = slice_branch(n.id), head
            ev = _evidence(
                n,
                "terminal-root",
                "idle",
                jobs[-1] if jobs else None,
                newest,
                label,
                lines,
                deadline,
                now,
                "plan root is closed under an open node",
                branch=branch,
                head_sha=head_sha,
            )
            _rescue(store, res, n.id, "terminal-root", "idle", ev)


def run_stall_pass(store: Store, now: datetime, dry_run: bool = False) -> StallResult:
    """One watchdog pass over every plan subtree (see the section header).

    ``now`` is passed in (timezone-aware UTC). The pass is DB-only and
    writes nothing while the factory is switched off: ``read_only_reason``
    forces a dry run that still lists every ``would_rescue`` node.
    """
    from precis.workers.job_types import plan_dispatch as pd

    res = StallResult(dry_run=dry_run)
    reason = pd.read_only_reason(store)
    if reason is not None:
        res.dry_run = True
        res.read_only = reason
    _heartbeat(store, now, res)
    plan = pd._load(store)
    skip_roots = _release_skip_roots(plan)
    for n in plan.todos():
        is_gate = _is_sha_gate(n)
        if not is_gate and not pd._dispatchable(plan, n):
            continue
        if n.root in skip_roots or n.status in TERMINAL_TODO_STATUSES:
            res.held += 1
            continue
        _judge_node(store, now, plan, n, is_gate, res)
    _judge_terminal_roots(store, now, plan, res)
    return res


__all__ = [
    "BOUNDS",
    "STAGE_DEADLINES",
    "TICK_SILENT_AFTER",
    "Bound",
    "RescueEvidence",
    "RescueResult",
    "StallResult",
    "enter_rescue",
    "run_stall_pass",
]
