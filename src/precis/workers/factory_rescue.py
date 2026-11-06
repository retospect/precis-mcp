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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from psycopg import Connection

from precis.errors import BadInput, NotFound
from precis.store.types import ChunkInsert, Tag
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


__all__ = [
    "BOUNDS",
    "Bound",
    "RescueEvidence",
    "RescueResult",
    "enter_rescue",
]
