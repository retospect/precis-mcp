"""Who changed a ref or link, with which model, and why — for the revisions log.

Migration 0185's triggers write one ``revisions`` row per transaction that
changes a covered part of a ref or link (docs/backlog/local-mesh-upkeep.md
§2b). The trigger cannot see Python, so the reason, actor and model reach
it as transaction-local settings: ``precis.reason``, ``precis.actor``,
``precis.model`` and ``precis.event`` (``'merged-into'`` is the only
honoured override).

A caller wraps its writes in :func:`revision_context`; the store's pool
(:class:`precis.store.pool.PrecisPool`) calls :func:`apply_revision_context`
on every connection it hands out inside one. That runs
``set_config(name, value, true)`` — transaction-scoped, so it is safe under
pgbouncer transaction pooling. Never a session ``SET``: on the prod DSN a
session setting leaks to whichever client gets the server connection next.

Outside any context nothing is sent; a covered write then logs
``reason='(unrecorded)'`` with the DB role as actor, and the nightly count
of those rows names the write paths still to wrap. Nested contexts merge:
an inner field that is ``None`` inherits the outer one.

**Trust (migration 0214, docs/backlog/graph-node-trust-state.md slice 1).**
The same context carries the producer's trust origin
(``precis.trust_origin``: human | extracted | computed | generated), the
actor type (``precis.trust_actor_type``) and at most one explicit
:class:`TrustAct` (``precis.trust_act``). The refs/links triggers derive
``trust_origin`` / ``trust_confidence`` / ``trust_write_version`` from them:
a writer with an origin is *converted* (its new rows and content changes are
``<origin>/hunch`` version 1); a writer without one is an unconverted,
compatibility-mode writer (unknown/unknown version 0) and is refused when it
changes version-1 content. ``considered`` exists only through a ``consider``
act. These settings are transport, not authentication: callers that accept
input from outside (MCP, web) decide which origin and acts they may send.
Inside a :func:`precis.store.pool.pin_connection` scope the pool re-sends the
active context on every checkout, so a nested context does not leak into the
outer writes that follow it. :func:`trust_producer` converts a shared write
door without overriding a caller that is already converted.
"""

from __future__ import annotations

import functools
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Literal, ParamSpec, TypeVar, get_args

import psycopg
from psycopg import Connection

_P = ParamSpec("_P")
_R = TypeVar("_R")

#: The single event a caller may name; the trigger derives the rest
#: (edited / retired / restored / deleted) from the row change.
MERGED_INTO = "merged-into"

#: A producer origin a converted writer may send. ``unknown`` is not one:
#: it is the legacy value, reachable only through a legacy-import or carry act.
TrustOrigin = Literal["human", "extracted", "computed", "generated"]
TRUST_ORIGINS: tuple[str, ...] = get_args(TrustOrigin)

#: Every stored origin / confidence value (the 0214 CHECK vocabulary).
STORED_ORIGINS: tuple[str, ...] = (*TRUST_ORIGINS, "unknown")
STORED_CONFIDENCES: tuple[str, ...] = ("hunch", "considered", "unknown")

#: Who sent the write: recorded on each act, never a capability by itself.
TrustActorType = Literal["human", "agent", "worker", "operator"]
TRUST_ACTOR_TYPES: tuple[str, ...] = get_args(TrustActorType)

TrustAction = Literal["consider", "demote", "correct-origin", "legacy-import", "carry"]
TRUST_ACTIONS: tuple[str, ...] = get_args(TrustAction)


@dataclass(frozen=True, slots=True)
class TrustAct:
    """One explicit trust act (``precis.trust_act``), validated here and
    again by the 0214 triggers.

    A *targeted* act names ``target_id`` and the content sha it was decided
    on (``expected_sha``; refused when stale). A *create-time* act leaves
    ``target_id`` None and names ``create_match`` — the ref kind or link
    relation of the new row it binds to (the first such row the transaction
    inserts). ``op_id`` makes a retry a replay: the same op_id and payload
    records nothing new; a changed payload is refused.
    """

    action: TrustAction
    target_kind: Literal["ref", "link"]
    reason: str
    target_id: int | None = None
    create_match: str | None = None
    expected_sha: str | None = None
    origin: str | None = None
    confidence: str | None = None
    carry_from: int | None = None
    op_id: str = ""

    def __post_init__(self) -> None:
        if self.action not in TRUST_ACTIONS:
            raise ValueError(
                f"trust act must be one of {TRUST_ACTIONS}, got {self.action!r}"
            )
        if self.target_kind not in ("ref", "link"):
            raise ValueError(
                f"trust act target_kind is ref or link, got {self.target_kind!r}"
            )
        if not self.reason.strip():
            raise ValueError("a trust act needs a reason")
        if (self.target_id is None) == (self.create_match is None):
            raise ValueError(
                "a trust act names exactly one of target_id / create_match"
            )
        if self.origin is not None and self.origin not in STORED_ORIGINS:
            raise ValueError(f"trust act origin must be one of {STORED_ORIGINS}")
        if self.confidence is not None and self.confidence not in STORED_CONFIDENCES:
            raise ValueError(
                f"trust act confidence must be one of {STORED_CONFIDENCES}"
            )
        if self.action == "demote" and self.confidence not in (
            None,
            "hunch",
            "unknown",
        ):
            raise ValueError("demote lowers confidence to hunch or unknown")
        if self.action == "correct-origin" and self.origin not in TRUST_ORIGINS:
            raise ValueError(f"correct-origin names a known origin {TRUST_ORIGINS}")
        if self.action == "carry" and self.carry_from is None:
            raise ValueError("carry names its predecessor (carry_from)")
        if self.action == "legacy-import" and self.confidence == "considered":
            raise ValueError(
                "legacy-import cannot mint considered; record a consider act"
            )
        if not self.op_id:
            object.__setattr__(self, "op_id", uuid.uuid4().hex)

    def to_json(self) -> str:
        """The ``precis.trust_act`` payload, null fields dropped."""
        return json.dumps(
            {k: v for k, v in asdict(self).items() if v is not None}, sort_keys=True
        )


@dataclass(frozen=True, slots=True)
class RevisionContext:
    """The settings the revisions and trust triggers read for this
    transaction."""

    reason: str | None = None
    actor: str | None = None
    model: str | None = None
    event: str | None = None
    trust_origin: str | None = None
    trust_actor_type: str | None = None
    trust_act: TrustAct | None = None
    trust_run: str | None = None


_current: ContextVar[RevisionContext | None] = ContextVar(
    "precis_revision_context", default=None
)


#: Process-wide fallback actor for verb writes with no enclosing context
#: (:func:`precis.runtime.dispatch._write_context`). ``None`` means the
#: generic ``agent``. Set once at boot by :mod:`precis.acting_user`, and
#: only by a stdio ``precis serve`` — one process per caller there, so a
#: process value is a per-caller value. Never set on a shared server.
_process_actor: str | None = None


def set_process_actor(actor: str | None) -> None:
    """Set (or clear, with ``None``) the process-wide fallback write actor."""
    global _process_actor
    _process_actor = actor or None


def process_actor() -> str | None:
    """The process-wide fallback write actor, or ``None``."""
    return _process_actor


def current_revision_context() -> RevisionContext | None:
    """The active context, or ``None`` outside any :func:`revision_context`."""
    return _current.get()


@contextmanager
def revision_context(
    reason: str | None = None,
    *,
    actor: str | None = None,
    model: str | None = None,
    event: str | None = None,
    trust_origin: str | None = None,
    trust_actor_type: str | None = None,
    trust_act: TrustAct | None = None,
    trust_run: str | None = None,
) -> Iterator[RevisionContext]:
    """Attach ``reason`` / ``actor`` / ``model`` (and the trust producer)
    to every write in the block.

    ``event`` may only be :data:`MERGED_INTO` (the merge path retiring the
    absorbed side); anything else is a programming error. ``trust_origin``
    makes the block a converted writer (module docstring; ``""`` makes it
    unconverted again under a converted outer block); ``trust_act`` is the
    one explicit act the block records.
    """
    if event is not None and event != MERGED_INTO:
        raise ValueError(
            f"revision event override must be {MERGED_INTO!r}, got {event!r}"
        )
    if trust_origin is not None and trust_origin not in (*TRUST_ORIGINS, ""):
        raise ValueError(
            f"trust_origin must be one of {TRUST_ORIGINS} (or '' to clear an "
            f"inherited one), got {trust_origin!r}"
        )
    if trust_actor_type is not None and trust_actor_type not in TRUST_ACTOR_TYPES:
        raise ValueError(
            f"trust_actor_type must be one of {TRUST_ACTOR_TYPES}, "
            f"got {trust_actor_type!r}"
        )
    outer = _current.get() or RevisionContext()
    ctx = RevisionContext(
        reason=reason if reason is not None else outer.reason,
        actor=actor if actor is not None else outer.actor,
        model=model if model is not None else outer.model,
        event=event if event is not None else outer.event,
        trust_origin=trust_origin if trust_origin is not None else outer.trust_origin,
        trust_actor_type=(
            trust_actor_type if trust_actor_type is not None else outer.trust_actor_type
        ),
        trust_act=trust_act if trust_act is not None else outer.trust_act,
        trust_run=trust_run if trust_run is not None else outer.trust_run,
    )
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


@contextmanager
def trust_producer(
    origin: TrustOrigin,
    *,
    actor_type: TrustActorType,
    reason: str | None = None,
    actor: str | None = None,
    conn: Connection | None = None,
) -> Iterator[RevisionContext]:
    """Make a shared write door a converted writer *unless* its caller
    already is one: the outer context's origin and actor win, so a verb
    keeps its own producer while a worker or CLI calling the same door
    gets ``origin``. Use :func:`revision_context` to override instead.

    ``conn``: a transaction the caller already opened. The pool sends the
    context only at checkout, so a door writing through a connection it
    was handed sends it here, and the outer context (or none) is sent back
    on exit so the caller's later writes keep their own producer."""
    outer = _current.get()
    if outer is not None and outer.trust_origin:
        with revision_context(reason) as ctx:
            yield ctx
        return
    with revision_context(
        reason,
        actor=actor if outer is None or outer.actor is None else None,
        trust_origin=origin,
        trust_actor_type=actor_type,
    ) as ctx:
        if conn is None:
            yield ctx
            return
        apply_revision_context(conn)
        try:
            yield ctx
        finally:
            _restore_outer(conn, outer)


def _restore_outer(conn: Connection, outer: RevisionContext | None) -> None:
    """Send ``outer`` (or clear) to ``conn`` after a nested producer. A
    transaction the door's failure already aborted keeps nothing to reset."""
    token = _current.set(outer)
    try:
        apply_revision_context(conn, clear=True)
    except psycopg.errors.InFailedSqlTransaction:
        pass
    finally:
        _current.reset(token)


def _live_conn(
    args: tuple[object, ...], kwargs: dict[str, object]
) -> Connection | None:
    """The connection a door was handed, positionally or as ``conn=``."""
    for value in (kwargs.get("conn"), *args):
        if isinstance(value, Connection):
            return value
    return None


def trust_door(
    origin: TrustOrigin, *, actor_type: TrustActorType = "worker"
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorate a shared ref/link write door with :func:`trust_producer`:
    its own producer when called from a worker, CLI or web route, the
    caller's when called inside a converted verb. A connection argument
    (``conn=`` or positional) gets the context sent to its transaction."""

    def deco(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(fn)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with trust_producer(
                origin, actor_type=actor_type, conn=_live_conn(args, kwargs)
            ):
                return fn(*args, **kwargs)

        return wrapper

    return deco


def apply_revision_context(conn: Connection, *, clear: bool = False) -> None:
    """Send the active context to ``conn``'s current transaction.

    No-op outside a context (unless ``clear``: then every setting is reset
    to empty, so a pinned transaction's earlier nested context does not
    linger), and on an autocommit connection (there is no transaction for a
    local setting to live in). On a regular pooled connection this opens
    the transaction the caller's statements then run in, which the pool
    commits on exit as before.
    """
    ctx = _current.get()
    if (ctx is None and not clear) or conn.autocommit:
        return
    ctx = ctx or RevisionContext()
    conn.execute(
        "SELECT set_config('precis.reason', %s, true), "
        "set_config('precis.actor', %s, true), "
        "set_config('precis.model', %s, true), "
        "set_config('precis.event', %s, true), "
        "set_config('precis.trust_origin', %s, true), "
        "set_config('precis.trust_actor_type', %s, true), "
        "set_config('precis.trust_act', %s, true), "
        "set_config('precis.trust_run', %s, true)",
        (
            ctx.reason or "",
            ctx.actor or "",
            ctx.model or "",
            ctx.event or "",
            ctx.trust_origin or "",
            ctx.trust_actor_type or "",
            ctx.trust_act.to_json() if ctx.trust_act is not None else "",
            ctx.trust_run or "",
        ),
    )
