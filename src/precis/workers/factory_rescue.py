"""``factory_rescue`` — one rescue tag, one gripe per failure signature.

Dark-factory td574946: every factory failure ends in done or rescue.
S1 built :func:`enter_rescue`; S2 built the stall watchdog
(:func:`run_stall_pass`); S3 adds the second tick pass,
:func:`run_rescue_pass` — a node sitting in RESCUE gets ONE read-only
diagnose task (``claude-sub`` lane, ``meta.rescue_for``), a ``rescue:
REARM`` verdict re-arms it once with a corrected brief, and anything
else files ONE ask-user todo blocking the node.

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
  ``cluster:dark-factory``, the same cluster label triage puts on engine
  design gripes. An engine gripe still never re-enters a build lane by
  the intake door: ``_file_sig_gripe`` writes no ``fix:code`` and no
  Claude-Code/human ``filed_by`` (gr591017 — the tag itself is no
  dispatch hold; holding it also held human-filed engine gripes).

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
from precis.plans.logbook import log_plan_event
from precis.plans.nodes import node_kind, validate_plan_meta
from precis.plans.templates import PENDING_SHA, REVIEW_TASK_CLASS
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import BLOCKER_TERMINAL_STATUSES, TERMINAL_TODO_STATUSES
from precis.todo.guards import check_ask_meta
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


def _held_tags(open_tags: Any, *, ignore: str | None = None) -> str | None:
    """The hold an open tag grants, or ``None`` — spec ruling 1.

    ``ignore`` skips a prefix the caller accounts for itself (the rescue
    pass passes ``"rescue:"`` — being in rescue is why the node is
    judged, never a hold)."""
    for v in open_tags:
        if ignore is not None and str(v).startswith(ignore):
            continue
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


def _ancestor_ids(conn: Any, ref_id: int) -> list[int]:
    """The parent chain above ``ref_id`` (the ``_held_db`` walk, 12 deep)."""
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
    return ancestors


def _held_db(store: Store, ref_id: int) -> str | None:
    """``_held_in_plan`` for a node no ``_Plan`` covers (the tick todo)."""
    open_tags = [str(t.value) for t in store.tags_for(ref_id) if t.namespace == "open"]
    held = _held_tags(open_tags)
    if held:
        return held
    with store.pool.connection() as conn:
        ids = [ref_id, *_ancestor_ids(conn, ref_id)]
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


def _lane_verdict(
    store: Store, now: datetime, plan: Any, n: Any, params: dict[str, Any]
) -> str | None:
    """``None`` to judge normally; the ``error_line`` for a no-fit stall;
    ``"parked …"`` when the fit lanes are full or backed off (the live jobs
    on them are judged by their own deadlines).

    A reviewer without a lane pin is judged on the pick
    ``plan_dispatch._review_lane`` would make — the same three tables,
    shrunk first by every build vendor, then by the original builder's
    alone, then the full table — so a reviewer waiting on a full
    alternative-vendor lane stays held instead of being rescued on a
    free lane of the builder's vendor the dispatcher would never use.
    """
    from precis.plans import lanes as _lanes
    from precis.workers.job_types import plan_dispatch as pd

    lane_table = _lanes.load_lanes(store)
    task_class = str(n.meta.get("task_class") or "code")
    tables: list[tuple[tuple[Any, ...], str]] = [(lane_table, "")]
    if pd._is_reviewer(n) and "lane" not in params:
        task_class = REVIEW_TASK_CLASS
        vendors = pd._builder_vendors(plan, n, lane_table)
        exclude_all = frozenset(vendors)
        exclude_first = frozenset(vendors[:1])
        tables = [
            (
                tuple(ln for ln in lane_table if ln.vendor not in exclude_all),
                "every build vendor",
            ),
            (
                tuple(ln for ln in lane_table if ln.vendor not in exclude_first),
                "the original builder's vendor",
            ),
            (lane_table, ""),
        ]
    for table, label in tables:
        fit = [ln for ln in table if ln.enabled and ln.fits(task_class)]
        if not fit:
            continue  # nothing enabled in this step's table: next step
        if _lanes.pick_lane(store, task_class, lanes=table) is not None:
            return None
        counts = _lanes.live_lane_counts(store)
        backoffs = _lanes.lane_backoffs(store, now)
        full = [ln.name for ln in fit if counts.get(ln.name, 0) >= ln.cap]
        backed = [ln.name for ln in fit if ln.name in backoffs]
        why = ", ".join(
            [f"full: {', '.join(sorted(full))}" if full else ""]
            + [f"backed off: {', '.join(sorted(backed))}" if backed else ""]
        ).strip(", ")
        scope = f" outside {label}" if label else ""
        return f"parked ({why or 'fit lanes busy'}{scope})"
    return f"no enabled lane fits {task_class}"


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
        # The tick re-queues a cancelled newest job — but only an op on
        # its re-queue allowlist (run_slice plus read-only ops; a
        # side-effecting cancel — a land, a release step, a pr_action —
        # keeps the old skip, gr587772): what reaches rescue is one of
        # those or a node a silent tick cannot reach.
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
    job_type, params = pd._resolve_op(n)
    if job_type == pd.DEFAULT_OP:
        verdict = _lane_verdict(store, now, plan, n, params)
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


# ── Diagnose and re-arm (td574946 S3) ──────────────────────────────
#
# ``run_rescue_pass`` is the ``factory_rescue_tick`` job's second pass —
# it acts on a node that sits in RESCUE. A task-class rescue
# (``BOUNDS[c].engine is False``) with no earlier re-arm mints ONE
# diagnose task — a read-only Claude session on the ``claude-sub`` lane
# whose ``meta.rescue_for`` marks it so it is judged through its node,
# never selected — and links the node ``blocked-by`` it, so the tick
# leaves the node alone while it waits (``rescue:`` is not in
# ``_DOABLE_EXCLUSION_TAGS``). The session's verdict rides its final
# message: ``rescue: REARM — <corrected brief>`` re-arms the node once
# (tag off, ``STATUS:doing`` back to ``open``, ``meta.rescue`` plus the
# ``rearmed_*`` floor stamps, a
# ``brief:`` comment the rebuilt run reads back through
# ``run_slice._brief_parts``, resuming on ``slice/<id>`` — the tree and
# branch are reused; nothing pins the run to the host holding the tree,
# the known gap); ``rescue: NOFIX — <why>``, a failed or unparsable
# diagnose, a second rescue (``meta.rescue.n >= 1``) and every engine
# class file ONE ``ask-user`` ruling todo blocking the node. A rescued
# gripe is skipped with a note (``meta.ask.blocks`` takes a todo handle
# only). Read-only while the factory is switched off; ``dry_run`` lists
# ``would`` and writes nothing.

#: Lane the diagnose task is pinned to (migration 0280's ``claude-sub``
#: account: task_classes ``*``, adapter ``claude_p``, the session MCP on).
DIAGNOSE_LANE = "claude-sub"

#: ``meta.task_class`` of the diagnose task: off ``code`` so a clean
#: no-commit exit closes it done (``run_slice._no_commit_kind``); the
#: task's ``meta.retries`` is 1 — one failure is final, no climb to a
#: second model.
DIAGNOSE_TASK_CLASS = "diagnose"

#: ``pg_advisory_xact_lock`` key (the "rescuep3" bytes) serialising the
#: rescue pass's mint/ask/re-arm writes (the ``_SIG_LOCK_KEY`` pattern):
#: two ticks, or a tick and a hand run, mint one diagnose task, file one
#: ask and re-arm once per rescue episode.
_RESCUE_PASS_LOCK_KEY = 0x7265736375657033

#: Cap on the diagnose task's brief (its whole instruction) and on the
#: re-arm ``brief:`` comment.
_DIAGNOSE_BODY_CAP = 6000
_REARM_BRIEF_CAP = 6000
#: Per-section caps inside the diagnose brief; when the whole would
#: exceed the body cap the job tail is shrunk first, then the evidence
#: comment, then the gripe body.
_EVIDENCE_CAP = 2500
_GRIP_BODY_CAP = 1500
_JOB_TAIL_CAP = 3000
#: ``meta.ask.context`` cap (``todo.guards._ASK_CONTEXT_CAP``, restated).
_ASK_CONTEXT_CAP = 600
#: The ``<why>`` fragment on the rescue ask's title.
_ASK_WHY_CAP = 60

_DIAGNOSIS_RE = re.compile(
    r"rescue:\s*(REARM|NOFIX)\b[\s:]*(?:[—–-]+\s*)?(.*)",
    re.IGNORECASE | re.DOTALL,
)
#: A ``rescue:`` verdict token for the disagreement check: two verdict
#: lines at line starts that disagree are no verdict at all.
_DIAGNOSIS_LINE_RE = re.compile(r"rescue:\s*(REARM|NOFIX)\b", re.IGNORECASE)
#: Markdown line-start markers a verdict token may directly follow.
_MD_MARKERS = " \t-*#>"
#: The evidence comment's ``branch: X @ Y`` line.
_BRANCH_RE = re.compile(r"^branch:\s*(\S+)\s*@\s*(\S+)", re.MULTILINE)


@dataclass
class RescuePassResult:
    """What one rescue pass did (or, read-only, would have done).

    ``diagnosing`` / ``rearmed`` / ``asked`` carry the node ids this pass
    wrote for; ``would`` the ``(node id, action)`` pairs a dry run lists
    (``"diagnose"`` / ``"rearm"`` / ``"ask"``); ``waiting`` the nodes a
    hold or an in-flight diagnose covered; ``read_only`` the kill-switch
    reason that forced the dry run.
    """

    dry_run: bool
    read_only: str | None = None
    diagnosing: list[int] = field(default_factory=list)
    rearmed: list[int] = field(default_factory=list)
    asked: list[int] = field(default_factory=list)
    would: list[tuple[int, str]] = field(default_factory=list)
    waiting: int = 0
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "diagnose_minted": len(self.diagnosing),
            "rearmed": len(self.rearmed),
            "asked": len(self.asked),
            "would": len(self.would),
            "waiting": self.waiting,
            "dry_run": self.dry_run,
        }
        if self.read_only:
            out["read_only"] = self.read_only
        return out

    def summary(self) -> str:
        return (
            f"factory_rescue_tick: diagnose minted={len(self.diagnosing)} "
            f"rearmed={len(self.rearmed)} asked={len(self.asked)} "
            f"would={len(self.would)} waiting={self.waiting} "
            f"dry_run={self.dry_run}"
        )

    def render(self) -> str:
        lines = [self.summary()]
        if self.read_only:
            lines.append(
                f"read-only ({self.read_only}): would lists what it "
                "would do; nothing was written"
            )
        shown = self.notes[:_NOTES_CAP]
        lines += [f"- {n}" for n in shown]
        if len(self.notes) > len(shown):
            lines.append(f"- … {len(self.notes) - len(shown)} more")
        return "\n".join(lines)


def parse_diagnosis(text: str) -> tuple[str, str] | None:
    """``(REARM|NOFIX, rest)`` of one ``rescue:`` verdict line, or ``None``.

    The ``plan_dispatch.parse_verdict`` grammar minus the fallback token
    scan: a leading ``handoff:`` is stripped, the FIRST non-empty line
    must carry the verdict — ``rescue: REARM — <corrected brief>`` (the
    brief may span the lines after; DOTALL) or ``rescue: NOFIX — <why>``
    — and an empty rest is no verdict for either. Two verdict lines at
    line starts that disagree are no verdict either; anything else is
    ``None``, and the node's ask then says "unparsable".
    """
    body = text.strip()
    if body.lower().startswith("handoff:"):
        body = body[len("handoff:") :].strip()
    verdicts: set[str] = set()
    for hit in _DIAGNOSIS_LINE_RE.finditer(body):
        i = hit.start() - 1
        while i >= 0 and body[i] in _MD_MARKERS:
            i -= 1
        if i < 0 or body[i] == "\n":
            verdicts.add(hit.group(1).upper())
    if len(verdicts) > 1:
        return None
    m = _DIAGNOSIS_RE.match(body)
    if m is None:
        return None
    verdict, rest = m.group(1).upper(), m.group(2).strip()
    if not rest:
        return None
    return verdict, rest


def _rescued_nodes(store: Store) -> list[tuple[int, str, Any]]:
    """``(ref id, class, tag set time)`` for every live todo carrying an
    open ``rescue:<cls>`` tag (``rescue-sig:`` is a different tag; the
    LIKE keeps it out), STATUS open or doing. A diagnose task
    (``meta.rescue_for``) is judged through its node, never selected.
    The tag's ``ref_tags.created_at`` dates this rescue episode."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (r.ref_id) r.ref_id, t.value, rt.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value LIKE 'rescue:%%'
               AND NOT (r.meta ? 'rescue_for')
               AND COALESCE(
                     (SELECT s.value FROM ref_tags rs
                        JOIN tags s ON s.tag_id = rs.tag_id
                       WHERE rs.ref_id = r.ref_id AND s.namespace = 'STATUS'
                       LIMIT 1),
                     'open') IN ('open', 'doing')
             ORDER BY r.ref_id, rt.created_at
            """,
        ).fetchall()
    return [
        (int(ref_id), str(value)[len("rescue:") :], tag_at)
        for ref_id, value, tag_at in rows
    ]


def _rescued_gripes(store: Store) -> list[int]:
    """Live gripes carrying ``rescue:<cls>`` — the pass can neither
    diagnose nor ask them (``meta.ask.blocks`` takes a todo handle
    only); each is a note, left for the intake lane's own path."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value LIKE 'rescue:%%'
             ORDER BY r.ref_id
            """,
        ).fetchall()
    return [int(r[0]) for r in rows]


def _rescued_hold(store: Store, n: Any) -> str | None:
    """The hold that leaves a rescued node waiting (the pass's rule a):
    the ``_held_tags`` set minus the node's own ``rescue:`` tag — being
    in rescue is why the node is judged, never a hold — plus STATUS
    paused on it or an ancestor. ``blocked-by`` and open asks are NOT
    holds here: the diagnose task and the episode's ask are exactly
    those blockers, and the pass judges them itself."""
    held = _held_tags(n.open_tags, ignore="rescue:")
    if held is not None:
        return held
    with store.pool.connection() as conn:
        ids = [n.id, *_ancestor_ids(conn, n.id)]
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
    return "paused" if paused else None


def _episode_ask(conn: Any, ref_id: int, tag_at: Any) -> int | None:
    """The ask this rescue episode already filed: a live ``blocked-by``
    target ask-shaped (``meta.ask`` or an ``ask-user`` tag) minted at or
    after the ``rescue:`` tag went on. Status-free on purpose — a node
    still tagged after its ask was answered is not asked again; the
    plan tick's ``_step_rearm`` clears the tag when the answer re-arms
    the node (td584381 S1a F1)."""
    row = conn.execute(
        """
        SELECT b.ref_id FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
         WHERE l.src_ref_id = %s AND l.relation = 'blocked-by'
           AND b.retired_at IS NULL AND b.created_at >= %s
           AND (b.meta ? 'ask' OR EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = b.ref_id AND t.namespace = 'OPEN'
                    AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')))
         ORDER BY b.ref_id LIMIT 1
        """,
        (ref_id, tag_at),
    ).fetchone()
    return int(row[0]) if row else None


def _diagnose_task_id(conn: Any, ref_id: int, tag_at: Any) -> int | None:
    """The live todo minted to diagnose this node in this episode
    (``meta.rescue_for`` and minted at or after the tag), or ``None``."""
    row = conn.execute(
        """
        SELECT r.ref_id FROM refs r
         WHERE r.kind = 'todo' AND r.retired_at IS NULL
           AND r.meta->>'rescue_for' = %s
           AND r.created_at >= %s
         ORDER BY r.ref_id DESC LIMIT 1
        """,
        (str(ref_id), tag_at),
    ).fetchone()
    return int(row[0]) if row else None


def _plan_root_of(n: Any) -> int | None:
    """The integer in the node's open ``plan:<id>`` tag; a node outside
    every plan (the tick's recurring todo) falls back to its own
    ``parent_id``, then ``None``."""
    for v in n.open_tags:
        if str(v).startswith("plan:") and str(v)[5:].isdigit():
            return int(str(v)[5:])
    return n.parent_id


def _cap(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def _line1(text: str, cap: int = 120) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "?"
    return _cap(line, cap)


def _newest_comment(store: Store, ref_id: int, prefix: str) -> str | None:
    """The ref's newest comment starting ``prefix`` (by ``(ord, id)``)."""
    chunks = sorted(
        (
            c
            for c in store.chunks.list_chunks_for_ref(ref_id)
            if c.chunk_kind == "todo_comment" and c.text
        ),
        key=lambda c: (int(c.ord), int(c.id)),
        reverse=True,
    )
    for c in chunks:
        text = str(c.text).strip()
        if text.lower().startswith(prefix):
            return text
    return None


def _sig_gripe_of(store: Store, ref_id: int) -> tuple[int, str, str] | None:
    """``(id, title, body)`` of the node's signature gripe — its
    ``related-to`` target carrying an open ``rescue-sig:`` tag — or
    ``None``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT b.ref_id, b.title FROM links l
              JOIN refs b ON b.ref_id = l.dst_ref_id
              JOIN ref_tags rt ON rt.ref_id = b.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE l.src_ref_id = %s AND l.relation = 'related-to'
               AND b.retired_at IS NULL AND b.kind = 'gripe'
               AND t.namespace = 'OPEN' AND t.value LIKE 'rescue-sig:%%'
             ORDER BY b.ref_id LIMIT 1
            """,
            (ref_id,),
        ).fetchone()
    if row is None:
        return None
    gid, title = int(row[0]), str(row[1] or "")
    body = "\n\n".join(
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(gid)
        if c.chunk_kind == "gripe_body" and c.text
    )
    return gid, title, body


def _job_tail(store: Store, job_id: int) -> str:
    """The last 40 lines of the job's newest ``job_event`` chunk plus its
    ``job_summary`` chunk."""
    chunks = [
        c
        for c in store.chunks.list_chunks_for_ref(job_id)
        if c.chunk_kind in ("job_event", "job_summary") and c.text
    ]
    if not chunks:
        return ""
    events = [c for c in chunks if c.chunk_kind == "job_event"]
    summaries = [c for c in chunks if c.chunk_kind == "job_summary"]
    tail = (
        str(max(events, key=lambda c: (int(c.ord), int(c.id))).text) if events else ""
    )
    tail = "\n".join(tail.splitlines()[-_LOG_TAIL_LINES:])
    if summaries:
        summary = str(max(summaries, key=lambda c: (int(c.ord), int(c.id))).text)
        tail += f"\n\njob summary:\n{summary}" if tail else f"job summary:\n{summary}"
    return tail


def _branch_of(plan: Any, n: Any, evidence: str | None) -> tuple[str, str | None]:
    """``(slice/<id>, head sha)`` — the evidence comment's ``branch: X @
    Y`` line when it carries one, else ``slice_branch(id)`` plus the
    newest committing job's head (``_branch_head``)."""
    from precis.workers.job_types.run_slice import slice_branch

    branch, head = slice_branch(n.id), _branch_head(plan, n)
    if evidence:
        m = _BRANCH_RE.search(evidence)
        if m:
            if m.group(1) != "?":
                branch = m.group(1)
            if m.group(2) != "?":
                head = m.group(2)
    return branch, head


def _diagnose_body(store: Store, plan: Any, n: Any, cls: str) -> str:
    """The diagnose task's whole brief — what the session is, the
    evidence, the read-only bounds, the verdict grammar — under the
    ``_DIAGNOSE_BODY_CAP`` cap (the evidence shrinks before the grammar
    does)."""
    from precis.workers.job_types.run_slice import _brief_parts

    node_body, latest = _brief_parts(store, n.id)
    evidence = _newest_comment(store, n.id, "rescue:") or "(no rescue: comment)"
    sig = _sig_gripe_of(store, n.id)
    jobs = plan.jobs(n)
    tail = _job_tail(store, jobs[-1].id) if jobs else ""
    branch, head = _branch_of(plan, n, evidence)
    ev_cap, gripe_cap, tail_cap = _EVIDENCE_CAP, _GRIP_BODY_CAP, _JOB_TAIL_CAP
    while True:
        lines = [
            f"You are a read-only diagnosis of td{n.id}, rescued under "
            f"class {cls}. Decide whether a corrected brief lets a fresh "
            "build succeed and answer with the grammar at the end — "
            "nothing else.",
            "",
            "## The rescued todo",
            f"td{n.id}: {n.title}",
            _cap(node_body, 800),
        ]
        if latest:
            lines += ["", "## Its newest brief:/handoff: comment", _cap(latest, 500)]
        lines += ["", "## The rescue evidence", _cap(evidence, ev_cap)]
        if sig is not None:
            gid, gtitle, gbody = sig
            lines += [
                "",
                "## The signature gripe",
                f"gr{gid}: {gtitle}",
                _cap(gbody, gripe_cap),
            ]
        lines += [
            "",
            "## Its newest job's tail",
            _cap(tail, tail_cap) if tail else "(no jobs on the node)",
            "",
            "## Its branch",
            f"{branch} @ {head or '?'}",
            "",
            "## What you may do",
            "Read through the precis MCP (`get`, `search` of todo, gripe, "
            f"job) and run read-only git (`git log {branch}`, `git show "
            f"{head or '<sha>'}`, `git diff origin/main...{branch}`; the "
            "branch may not be in your clone — then say so and use the "
            "head sha and the log tail). You may NOT `put`, `edit`, "
            "`tag`, `link` or `delete` anything, mint nothing, commit "
            "nothing, push or ship nothing, and do not tag this todo "
            "done (the tick reads your reply and closes it).",
            "",
            "## How to answer",
            "Reply with ONE first line, exactly one of:",
            "- `rescue: REARM — <corrected brief>` (the brief may "
            "continue on the lines after; name files, tests and what "
            "the existing branch already does, so the builder continues "
            "from it)",
            "- `rescue: NOFIX — <why>` (the cause is outside the brief: "
            "engine, infrastructure, a denied path, a missing secret, a "
            "task that cannot be done as asked, or you cannot tell)",
            "Then, in a line or two, what you read.",
        ]
        body = "\n".join(lines)
        if len(body) <= _DIAGNOSE_BODY_CAP:
            return body
        if tail_cap > 600:
            tail_cap = 600
        elif ev_cap > 1000:
            ev_cap = 1000
        elif gripe_cap > 500:
            gripe_cap = 500
        else:
            return _cap(body, _DIAGNOSE_BODY_CAP)


def _read_diagnosis(
    store: Store, task_id: int, since: Any
) -> tuple[tuple[str, str] | None, str | None]:
    """``(verdict, raw line)`` from the diagnose task's comments posted
    at or after ``since`` — the newest job's ``created_at``, so an older
    comment never counts. A comment STARTING ``rescue:`` wins over a
    ``handoff:`` whose own body starts ``rescue:``, newest first within
    each class (the ``plan_dispatch._read_verdict`` shape)."""
    from precis.workers.job_types import plan_dispatch as pd

    comments: list[Any] = []
    for c in pd._comments(store, task_id):
        if c.created_at < since:
            break
        comments.append(c)
    for c in comments:
        text = str(c.text)
        if text.strip().lower().startswith("rescue:"):
            return parse_diagnosis(text), text.strip()
    for c in comments:
        text = str(c.text)
        body = text.strip().lower()
        if body.startswith("handoff:") and (
            body[len("handoff:") :].strip().startswith("rescue:")
        ):
            return parse_diagnosis(text), text.strip()
    return None, None


def _mint_diagnose(
    store: Store,
    res: RescuePassResult,
    n: Any,
    cls: str,
    tag_at: Any,
    plan: Any,
) -> None:
    """Mint the ONE diagnose task for the node and link the node
    ``blocked-by`` it — one transaction under the advisory lock, the
    existence check repeated inside, so two ticks (or a hand run) mint
    one. Never ``templates.mint_plan_task``: a task stamped afterwards
    would sit one window as a ``code`` node the tick could dispatch."""
    if res.dry_run:
        res.would.append((n.id, "diagnose"))
        res.notes.append(f"td{n.id}: would mint a diagnose task ({cls})")
        return
    meta: dict[str, Any] = {
        "op": {"type": "run_slice", "params": {"lane": DIAGNOSE_LANE}},
        "task_class": DIAGNOSE_TASK_CLASS,
        "retries": 1,
        "rescue_for": n.id,
    }
    problems = validate_plan_meta(meta)
    if problems:
        raise BadInput(
            f"diagnose task meta: {'; '.join(problems)}",
            next="fix the meta shape before minting",
        )
    body = _diagnose_body(store, plan, n, cls)
    root = _plan_root_of(n)
    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_RESCUE_PASS_LOCK_KEY,))
        # Recheck under the lock: a racing tick that minted while this
        # pass read sees its task committed here, and this pass writes
        # nothing (the ``enter_rescue`` tag-check pattern).
        if _diagnose_task_id(conn, n.id, tag_at) is not None:
            res.notes.append(
                f"td{n.id}: a diagnose task appeared under the lock; skipped"
            )
            return
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=f"diagnose td{n.id}'s rescue ({cls})",
            meta=meta,
            parent_id=root,
            conn=conn,
        )
        # A parented put carries a default llm_tier; the pinned lane owns it.
        conn.execute(
            "UPDATE refs SET meta = meta - 'llm_tier' WHERE ref_id = %s",
            (ref.id,),
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        # A node outside every plan: the task tags itself plan root so
        # the tick can still dispatch it.
        store.add_tag(
            ref.id,
            Tag.open(f"plan:{root if root is not None else ref.id}"),
            set_by="system",
            conn=conn,
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_link(
            src_ref_id=n.id,
            dst_ref_id=ref.id,
            relation="blocked-by",
            set_by="system",
            conn=conn,
        )
    res.diagnosing.append(n.id)
    res.notes.append(f"td{n.id}: minted diagnose task td{ref.id} ({cls})")


def _rearm_node(
    store: Store,
    now: datetime,
    res: RescuePassResult,
    plan: Any,
    n: Any,
    cls: str,
    tag_at: Any,
    task_id: int,
    brief: str,
    pd: Any,
) -> None:
    """REARM, one transaction: the ``rescue:<cls>`` tag off, a
    ``STATUS:doing`` node back to ``open`` (``_rescued_nodes`` selects
    doing nodes, but ``plan_dispatch._step_dispatch`` only runs
    ``STATUS:open`` — without the reset the cleared rescue leaves the
    node unrunnable), ``meta.rescue`` plus the re-arm floor stamps
    (``rearmed_after_job`` floors the attempt count at the newest ended
    job; ``rearmed_ask_at`` + a non-"land again" ``rearmed_answer`` keep
    ``plan_dispatch._reland_params`` from re-minting a land of the old
    branch), the audit comment, then the corrected ``brief:`` comment
    LAST — the newest ``brief:``/``handoff:`` is what the rebuilt run
    reads — and the diagnose task closed done."""
    del tag_at
    if res.dry_run:
        res.would.append((n.id, "rearm"))
        res.notes.append(f"td{n.id}: would re-arm from td{task_id}")
        return
    ended = [j for j in plan.jobs(n) if j.status in pd._JOB_ENDED]
    # The ``_step_rearm`` rule (gr586688, td584381 S1d): a succeeded
    # ``land_slice`` as the newest ended job means the slice already
    # landed — flooring the todo on its own land would hold it open for
    # a duplicate build of landed work.
    landed = bool(
        ended
        and ended[-1].status == "succeeded"
        and ended[-1].meta.get("job_type") == "land_slice"
    )
    evidence = _newest_comment(store, n.id, "rescue:")
    branch, head = _branch_of(plan, n, evidence)
    stamp: dict[str, Any] = {
        "rescue": {
            "n": 1,
            "at": _iso(now),
            "diagnose": f"td{task_id}",
            "cls": cls,
        },
        "rearmed_ask_at": _iso(now),
        "rearmed_answer": "rescue: REARM",
    }
    if ended and not landed:
        stamp["rearmed_after_job"] = ended[-1].id
    at = f" at {head}" if head else ""
    resume = (
        f"Resume: the run reuses this todo's worktree and branch — "
        f"continue on {branch}{at} (`git log origin/main..HEAD` lists the "
        "commits already there). If that list is empty or the branch is "
        "gone, the run starts fresh from origin/main: rebuild from this "
        "brief."
    )
    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_RESCUE_PASS_LOCK_KEY,))
        # Recheck under the lock: a racer that removed the tag (or
        # re-armed) while this pass read wins; this pass writes nothing.
        tags_now = store.tags_for(n.id, conn=conn)
        current = [str(t.value) for t in tags_now if t.namespace == "open"]
        if f"rescue:{cls}" not in current:
            res.notes.append(f"td{n.id}: rescue tag gone under the lock; not re-armed")
            return
        # The same for a racer that moved the node off the statuses
        # ``_rescued_nodes`` selects (a close, a pause) — and a ``doing``
        # node must come back ``open`` or the cleared rescue still
        # leaves it undispatchable.
        status = next((str(t.value) for t in tags_now if t.prefix == "STATUS"), "open")
        if status not in ("open", "doing"):
            res.notes.append(f"td{n.id}: STATUS {status} under the lock; not re-armed")
            return
        store.remove_tag(n.id, Tag.open(f"rescue:{cls}"), conn=conn)
        if status == "doing":
            store.add_tag(
                n.id,
                Tag.closed("STATUS", "open"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
        store.stamp_ref_meta(n.id, stamp, conn=conn)
        _comment(
            store,
            conn,
            n.id,
            "todo_comment",
            f"rescue: re-armed from td{task_id} (n=1)",
        )
        _comment(
            store,
            conn,
            n.id,
            "todo_comment",
            f"brief: {_cap(brief, _REARM_BRIEF_CAP)}\n\n{resume}",
        )
        store.add_tag(
            task_id,
            Tag.closed("STATUS", "done"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
    res.rearmed.append(n.id)
    res.notes.append(f"td{n.id}: re-armed from td{task_id} (n=1); resumes on {branch}")


def _ask_node(
    store: Store,
    now: datetime,
    res: RescuePassResult,
    plan: Any,
    n: Any,
    cls: str,
    tag_at: Any,
    why: str,
    *,
    diagnose: str | None,
    reply: str | None,
    pd: Any,
) -> int | None:
    """File the ONE ruling ask for this rescue episode — the
    ``plan_dispatch._mint_ask`` insert shape (``_mint_ask`` itself is
    unusable: it reads ``plan.jobs(n)[-1]`` and is retry-cap worded; a
    rescued node may have no job) — link the node ``blocked-by`` it,
    stamp ``meta.rescue`` (an existing ``n`` kept; ``n=1`` when a
    diagnose ran this episode) and comment the node. Returns the ask's
    id, or ``None`` read-only / when a racer filed first."""
    if res.dry_run:
        res.would.append((n.id, "ask"))
        res.notes.append(f"td{n.id}: would ask ({_line1(why)})")
        return None
    root = _plan_root_of(n)
    evidence = _newest_comment(store, n.id, "rescue:")
    branch, head = _branch_of(plan, n, evidence)
    title = (
        f"td{n.id}: in rescue ({cls}) — {_cap(why, _ASK_WHY_CAP)}: "
        "retry, rewrite the brief, or drop it?"
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(pd._ASK_OPTIONS),
            "blocks": f"td{n.id}",
            "context": _cap(f"{branch} @ {head or '?'}: {why}", _ASK_CONTEXT_CAP),
        }
    }
    check_ask_meta(meta)
    lines = [f"td{n.id} sits in rescue ({cls}): {why}."]
    if diagnose is not None:
        lines += [
            "",
            f"Diagnose {diagnose} ran on it; its reply: "
            f"{_cap(reply or 'no usable reply', 400)}",
        ]
    if evidence:
        lines += ["", "The rescue evidence on the node:", evidence]
    lines += [
        "",
        f"Branch: {branch} @ {head or '?'}",
        "",
        "'retry with a different lane/model' re-arms the node — closing "
        "this ask gives it a fresh retry budget and the plan tick clears "
        f"the `rescue:{cls}` tag itself; 'rewrite the brief' — leave a "
        "`brief:` comment or edit the node's body and close this ask, "
        "the rebuild runs on the new brief; 'won't do' — close the node "
        "STATUS:won't-do.",
    ]
    body = "\n".join(lines)
    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_RESCUE_PASS_LOCK_KEY,))
        # Recheck under the lock: a racer's ask for this episode is
        # committed here; this pass files no second one.
        if _episode_ask(conn, n.id, tag_at) is not None:
            res.notes.append(f"td{n.id}: an ask appeared under the lock; skipped")
            return None
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        if root is not None:
            store.add_tag(ref.id, Tag.open(f"plan:{root}"), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_link(
            src_ref_id=n.id,
            dst_ref_id=ref.id,
            relation="blocked-by",
            conn=conn,
        )
        prev = n.meta.get("rescue")
        rescue_meta = dict(prev) if isinstance(prev, dict) else {}
        rescue_meta["at"] = _iso(now)
        rescue_meta["ask"] = f"td{ref.id}"
        if diagnose is not None:
            rescue_meta["n"] = 1
            rescue_meta["diagnose"] = diagnose
        store.stamp_ref_meta(n.id, {"rescue": rescue_meta}, conn=conn)
        _comment(
            store,
            conn,
            n.id,
            "todo_comment",
            f"rescue: asked td{ref.id} ({_line1(why, 80)})",
        )
    res.asked.append(n.id)
    res.notes.append(f"td{n.id}: asked td{ref.id} ({_line1(why)})")
    if root is not None:
        log_plan_event(
            store, n.id, f"ask td{ref.id} raised (blocks td{n.id})", root=root
        )
    return int(ref.id)


def _close_diagnose(
    store: Store,
    res: RescuePassResult,
    task_id: int,
    status: str,
    note: str | None,
    *,
    superseded: Any = (),
    ask_id: int | None = None,
) -> None:
    """Close the diagnose task once its verdict is spent (``done``
    after REARM or NOFIX, ``won't-do`` with a comment after a failure
    or an unparsable reply), and close a retry-cap ask the tick filed
    on it as ``superseded by td<ask>`` — no ask left orphaned."""
    if res.dry_run:
        return
    with store.atomic() as conn:
        store.add_tag(
            task_id,
            Tag.closed("STATUS", status),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        if note:
            _comment(store, conn, task_id, "todo_comment", note)
        for b in superseded:
            store.add_tag(
                b.id,
                Tag.closed("STATUS", "won't-do"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
            _comment(
                store,
                conn,
                b.id,
                "todo_comment",
                f"superseded by td{ask_id}",
            )


def _judge_diagnose(
    store: Store,
    now: datetime,
    res: RescuePassResult,
    plan: Any,
    n: Any,
    cls: str,
    tag_at: Any,
    task_id: int,
    pd: Any,
) -> None:
    """A diagnose task already covers this episode: read its outcome —
    still in flight (``waiting``), a failure shape (file the node's ask
    and close it won't-do), a succeeded job (parse the verdict: REARM,
    NOFIX or unparsable)."""
    dplan = _subtree_plan(store, task_id)
    d = dplan.nodes.get(task_id)
    if d is None:
        res.notes.append(f"td{n.id}: diagnose task td{task_id} not loadable; skipped")
        return
    cap_asks = dplan.open_asks(d)
    djobs = dplan.jobs(d)
    dj = djobs[-1] if djobs else None
    failed: str | None = None
    if any(v.startswith("rescue:") for v in d.open_tags):
        failed = "the diagnose task is itself in rescue"
    elif d.status == "won't-do":
        failed = "the diagnose task is won't-do"
    elif cap_asks:
        failed = "the tick's own retry-cap ask blocks it"
    elif dj is not None and dj.status in ("failed", "cancelled"):
        failed = f"diagnose job jo{dj.id} {dj.status}"
    if failed is not None:
        ask_id = _ask_node(
            store,
            now,
            res,
            plan,
            n,
            cls,
            tag_at,
            failed,
            diagnose=f"td{task_id}",
            reply=None,
            pd=pd,
        )
        _close_diagnose(
            store,
            res,
            task_id,
            "won't-do",
            f"rescue: diagnose ended — {failed}",
            superseded=cap_asks,
            ask_id=ask_id,
        )
        return
    if dj is None or dj.status not in pd._JOB_TERMINAL:
        res.waiting += 1
        res.notes.append(f"td{n.id}: diagnose td{task_id} in flight")
        return
    verdict, raw = _read_diagnosis(store, task_id, dj.created_at)
    if verdict is None:
        why = "the diagnose task's reply is unparsable (no rescue: verdict line)"
        if raw:
            why += f" — it said: {_line1(raw, 100)}"
        ask_id = _ask_node(
            store,
            now,
            res,
            plan,
            n,
            cls,
            tag_at,
            why,
            diagnose=f"td{task_id}",
            reply=raw,
            pd=pd,
        )
        _close_diagnose(
            store,
            res,
            task_id,
            "won't-do",
            (
                f"rescue: unparsable reply — asked td{ask_id}"
                if ask_id
                else "rescue: unparsable reply"
            ),
        )
        return
    if verdict[0] == "REARM":
        _rearm_node(store, now, res, plan, n, cls, tag_at, task_id, verdict[1], pd)
        return
    ask_id = _ask_node(
        store,
        now,
        res,
        plan,
        n,
        cls,
        tag_at,
        f"NOFIX — {_line1(verdict[1])}",
        diagnose=f"td{task_id}",
        reply=raw,
        pd=pd,
    )
    del ask_id
    _close_diagnose(store, res, task_id, "done", None)


def _judge_rescued(
    store: Store,
    now: datetime,
    res: RescuePassResult,
    ref_id: int,
    cls: str,
    tag_at: Any,
    pd: Any,
) -> None:
    """One rescued node's path: held → waiting; an ask already filed
    this episode → skip; a diagnose task → judge it; an engine class,
    an unknown class or a second rescue → the ask directly; else mint
    the diagnose task."""
    plan = _subtree_plan(store, ref_id)
    n = plan.nodes.get(ref_id)
    if n is None:
        res.notes.append(f"td{ref_id}: node not loadable; skipped")
        return
    held = _rescued_hold(store, n)
    if held is not None:
        res.waiting += 1
        res.notes.append(f"td{ref_id}: held ({held}); not judged")
        return
    with store.pool.connection() as conn:
        ask = _episode_ask(conn, ref_id, tag_at)
        task_id = _diagnose_task_id(conn, ref_id, tag_at)
    if ask is not None:
        res.notes.append(f"td{ref_id}: ask td{ask} already covers this episode")
        return
    if task_id is not None:
        _judge_diagnose(store, now, res, plan, n, cls, tag_at, task_id, pd)
        return
    bound = BOUNDS.get(cls)
    if bound is None or bound.engine:
        why = (
            f"unknown rescue class {cls!r} (treated as engine)"
            if bound is None
            else (
                f"{cls} is an engine class — the factory's own machinery, not the brief"
            )
        )
        _ask_node(
            store,
            now,
            res,
            plan,
            n,
            cls,
            tag_at,
            why,
            diagnose=None,
            reply=None,
            pd=pd,
        )
        return
    prev = n.meta.get("rescue")
    if isinstance(prev, dict) and int(prev.get("n") or 0) >= 1:
        _ask_node(
            store,
            now,
            res,
            plan,
            n,
            cls,
            tag_at,
            "already re-armed once (meta.rescue.n >= 1); a second rescue "
            "is a human call",
            diagnose=str(prev.get("diagnose")) if prev.get("diagnose") else None,
            reply=None,
            pd=pd,
        )
        return
    _mint_diagnose(store, res, n, cls, tag_at, plan)


def run_rescue_pass(
    store: Store, now: datetime, dry_run: bool = False
) -> RescuePassResult:
    """One pass over every node sitting in RESCUE (see the section
    header). ``now`` is passed in (timezone-aware UTC). The pass writes
    nothing while the factory is switched off: ``read_only_reason``
    forces the dry run that still lists every ``would`` action, and one
    failing node is a note — the pass goes on."""
    from precis.workers.job_types import plan_dispatch as pd

    res = RescuePassResult(dry_run=dry_run)
    reason = pd.read_only_reason(store)
    if reason is not None:
        res.dry_run = True
        res.read_only = reason
    for gid in _rescued_gripes(store):
        res.notes.append(
            f"gr{gid}: a rescued gripe cannot be diagnosed or asked "
            "(meta.ask.blocks takes a todo handle only); skipped"
        )
    for ref_id, cls, tag_at in _rescued_nodes(store):
        try:
            _judge_rescued(store, now, res, ref_id, cls, tag_at, pd)
        except Exception as exc:  # one failing node must not stop the pass
            log.warning(
                "factory_rescue: rescue pass on td%d raised",
                ref_id,
                exc_info=True,
            )
            res.notes.append(
                f"td{ref_id}: rescue pass failed ({type(exc).__name__}: {exc})"
            )
    return res


__all__ = [
    "BOUNDS",
    "DIAGNOSE_LANE",
    "DIAGNOSE_TASK_CLASS",
    "STAGE_DEADLINES",
    "TICK_SILENT_AFTER",
    "Bound",
    "RescueEvidence",
    "RescuePassResult",
    "RescueResult",
    "StallResult",
    "enter_rescue",
    "parse_diagnosis",
    "run_rescue_pass",
    "run_stall_pass",
]
