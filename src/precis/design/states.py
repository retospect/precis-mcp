"""Discrete block states + stimulus-labelled transitions.

One mechanism for a phenomenon that is true at BOTH scales: Howell-style
compliant bistables and hard stops at macro scale, photoswitches and
conformers in atomic mode. Shape owned by
``blocktree-library-build-plan.md`` §Slice 2 and reproduced exactly::

    state:       (block_uid, name, envelope?, port_pose_overrides?)
    transition:  (block_uid, from_state, to_state, driver_kind, driver_ref,
                  params)

``driver_kind`` is a **closed** enum (:data:`DRIVER_KINDS`), extended only
by migration. A block with no declared states has exactly one implicit
state, so nothing existing changes shape.

**Current state is per BLOCK, never per design.** Two independently
switchable blocks sitting in different states is the whole photoswitch
case; one pointer per design cannot represent it.

**Transitions are directed edges.** Forward and reverse are separate rows
because a molecular ratchet is precisely the case where their barriers
differ (addendum A9) — one barrier per unordered pair would make ratchets
inexpressible. No extra machinery is needed for ratchets beyond that.

.. warning::

   **A9 HYSTERESIS RULE — read before caching anything.** A state-carrying
   block is the ONE place in the whole model where history is load-bearing:
   its state is **not** a function of the parameter vector. A bistable snap
   switches at a different threshold going up than coming down, so "state
   given configuration" is not a function at all. Any cache key, memo,
   surrogate or Pareto-front entry for something evaluated over a
   state-carrying block **MUST include that block's current discrete
   state** — use :func:`cache_key` (or :func:`state_cache_key` when you
   already hold the state map) rather than hand-rolling the key, and
   :func:`state_carrying_uids` to find out whether the rule applies at all.
   A configuration-only key doesn't degrade gracefully here; it returns a
   confidently wrong answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis.design._db import read_conn, write_conn

#: CLOSED enum, mirroring migration ``0162_design_core.sql``'s CHECK.
#: ``mechanical`` (force/displacement-driven snap-through) is the macro
#: adopters' member; the other five come from blocktree slice 2.
DRIVER_KINDS: tuple[str, ...] = (
    "light",
    "reaction",
    "redox",
    "ph",
    "thermal",
    "mechanical",
)

_STATE_COLS = "block_uid, name, envelope, port_pose_overrides, descr"
_TRANSITION_COLS = "block_uid, from_state, to_state, driver_kind, driver_ref, params"


class StateError(ValueError):
    """A malformed state or transition. Raised at write time."""


@dataclass(frozen=True)
class BlockState:
    """One declared state of one block. ``envelope`` overrides the block's
    own envelope in this state (None = unchanged); ``port_pose_overrides``
    moves the ports this state moves, keyed by port name — an UNQUALIFIED
    JSON payload here, vetted by the owning domain, which reads it as a
    rigid delta on that port (``se``: ``{'direction'?, 'pose'?, 'rot'?}``,
    :func:`precis_se.ops._vet_port_pose_overrides`). This core stores and
    returns it verbatim; what a port even has to move is the domain's
    vocabulary, not this table's."""

    block_uid: int
    name: str
    envelope: str | None = None
    port_pose_overrides: dict[str, Any] | None = None
    descr: str | None = None


@dataclass(frozen=True)
class Transition:
    """A directed, stimulus-labelled edge between two of a block's states.

    ``driver_ref`` points at whatever drives it — an ``rxn`` slug for a
    reaction, a wavelength for light, a named actuator for mechanical —
    and ``params`` carries the per-driver numbers (quantum yield, barrier
    height, snap force).
    """

    block_uid: int
    from_state: str
    to_state: str
    driver_kind: str
    driver_ref: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


def validate_driver_kind(value: Any) -> str:
    """Vet a ``driver_kind`` against the closed enum, loudly."""
    if not isinstance(value, str) or value not in DRIVER_KINDS:
        raise StateError(
            f"unknown driver_kind {value!r} — the vocabulary is closed: "
            f"{', '.join(DRIVER_KINDS)} (extend it by migration, not by write)"
        )
    return value


# ── states ─────────────────────────────────────────────────────────────────


def set_states(
    store: Any,
    ref_id: int,
    block_uid: int,
    states: Iterable[BlockState],
    *,
    conn: Connection | None = None,
) -> None:
    """Declare a block's states, replacing whatever it had.

    Upsert-and-prune rather than the renter's delete-all/reinsert-all: a
    state row is an FK **target** (transitions and the current-state
    pointer both reference it), so deleting a state the caller is about to
    re-insert identically would cascade away transitions that never
    changed. States that survive the write keep their edges; states the
    caller dropped take theirs with them, which is correct — a transition
    to a state that no longer exists must not survive.

    GUARDED against silently unposing a block: ``design_block_state`` has
    ``ON DELETE CASCADE`` to a state row, so pruning a state that is a
    block's CURRENT state would delete that pointer with no trace — a
    later :func:`current_state` reads back None, indistinguishable from
    "never posed". A pruned-but-posed state raises :class:`StateError`
    instead; re-pose the block (:func:`set_current_state` to a surviving
    state) or keep the state in the declaration.
    """
    rows = list(states)
    names = [s.name for s in rows]
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise StateError(
            f"block {block_uid} declares state name(s) twice: {', '.join(dupes)}"
        )
    for state in rows:
        if not state.name or not state.name.strip():
            raise StateError(f"block {block_uid} has a state with an empty name")
    keep = [s.name.strip() for s in rows]
    with write_conn(store, conn) as c:
        posed = c.execute(
            "SELECT state_name FROM design_block_state "
            "WHERE ref_id = %s AND block_uid = %s "
            "AND NOT (state_name = ANY(%s))",
            (ref_id, block_uid, keep),
        ).fetchone()
        if posed is not None:
            raise StateError(
                f"block {block_uid} is currently posed in state "
                f"{posed[0]!r}, which this call would prune — re-pose the "
                "block (set_current_state to a surviving state) or include "
                f"{posed[0]!r} in the declaration"
            )
        c.execute(
            "DELETE FROM design_states WHERE ref_id = %s AND block_uid = %s "
            "AND NOT (name = ANY(%s))",
            (ref_id, block_uid, keep),
        )
        for state in rows:
            c.execute(
                "INSERT INTO design_states "
                "(ref_id, block_uid, name, envelope, port_pose_overrides, descr) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (ref_id, block_uid, name) DO UPDATE SET "
                "envelope = EXCLUDED.envelope, "
                "port_pose_overrides = EXCLUDED.port_pose_overrides, "
                "descr = EXCLUDED.descr",
                (
                    ref_id,
                    block_uid,
                    state.name.strip(),
                    state.envelope,
                    (
                        Jsonb(state.port_pose_overrides)
                        if state.port_pose_overrides is not None
                        else None
                    ),
                    state.descr,
                ),
            )


def states_for(
    store: Any, ref_id: int, block_uid: int, *, conn: Connection | None = None
) -> list[BlockState]:
    """One block's declared states. Empty means the block has exactly one
    implicit state — not that something is missing."""
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_STATE_COLS} FROM design_states "
                "WHERE ref_id = %s AND block_uid = %s ORDER BY name ASC",
                (ref_id, block_uid),
            )
            return [_state_from_row(r) for r in cur.fetchall()]


def design_states(
    store: Any, ref_id: int, *, conn: Connection | None = None
) -> dict[int, list[BlockState]]:
    """Every declared state in a design, grouped by block uid."""
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_STATE_COLS} FROM design_states WHERE ref_id = %s "
                "ORDER BY block_uid ASC, name ASC",
                (ref_id,),
            )
            rows = cur.fetchall()
    out: dict[int, list[BlockState]] = {}
    for row in rows:
        out.setdefault(int(row["block_uid"]), []).append(_state_from_row(row))
    return out


def state_carrying_uids(
    store: Any, ref_id: int, *, conn: Connection | None = None
) -> set[int]:
    """Blocks in this design that declare more than one state — the ones
    the A9 hysteresis rule applies to. A single-state block is a constant
    and needs nothing in a cache key."""
    with read_conn(store, conn) as c:
        rows = c.execute(
            "SELECT block_uid FROM design_states WHERE ref_id = %s "
            "GROUP BY block_uid HAVING count(*) > 1",
            (ref_id,),
        ).fetchall()
    return {int(r[0]) for r in rows}


def _state_from_row(row: dict[str, Any]) -> BlockState:
    return BlockState(
        block_uid=int(row["block_uid"]),
        name=row["name"],
        envelope=row["envelope"],
        port_pose_overrides=(
            dict(row["port_pose_overrides"])
            if row["port_pose_overrides"] is not None
            else None
        ),
        descr=row["descr"],
    )


# ── transitions ────────────────────────────────────────────────────────────


def set_transitions(
    store: Any,
    ref_id: int,
    block_uid: int,
    transitions: Iterable[Transition],
    *,
    conn: Connection | None = None,
) -> None:
    """Replace a block's transitions. Endpoints must be declared states —
    the composite FK enforces it, so a typo'd state name fails the write
    instead of producing an edge to nowhere."""
    rows = list(transitions)
    for t in rows:
        validate_driver_kind(t.driver_kind)
        if t.from_state == t.to_state:
            raise StateError(
                f"block {block_uid} has a transition from {t.from_state!r} to "
                "itself — a self-edge drives nothing"
            )
    with write_conn(store, conn) as c:
        c.execute(
            "DELETE FROM design_transitions WHERE ref_id = %s AND block_uid = %s",
            (ref_id, block_uid),
        )
        for t in rows:
            c.execute(
                "INSERT INTO design_transitions "
                "(ref_id, block_uid, from_state, to_state, driver_kind, "
                " driver_ref, params) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    ref_id,
                    block_uid,
                    t.from_state,
                    t.to_state,
                    t.driver_kind,
                    t.driver_ref,
                    Jsonb(t.params or {}),
                ),
            )


def transitions_for(
    store: Any, ref_id: int, block_uid: int, *, conn: Connection | None = None
) -> list[Transition]:
    """One block's transitions, in a stable order."""
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_TRANSITION_COLS} FROM design_transitions "
                "WHERE ref_id = %s AND block_uid = %s "
                "ORDER BY from_state ASC, to_state ASC, driver_kind ASC",
                (ref_id, block_uid),
            )
            return [_transition_from_row(r) for r in cur.fetchall()]


def _transition_from_row(row: dict[str, Any]) -> Transition:
    return Transition(
        block_uid=int(row["block_uid"]),
        from_state=row["from_state"],
        to_state=row["to_state"],
        driver_kind=row["driver_kind"],
        driver_ref=row["driver_ref"],
        params=dict(row["params"] or {}),
    )


# ── current state, per block ───────────────────────────────────────────────


def set_current_state(
    store: Any,
    ref_id: int,
    block_uid: int,
    state_name: str,
    *,
    set_by: str | None = None,
    conn: Connection | None = None,
) -> None:
    """Pose one block into one of its declared states.

    Keyed on ``(ref_id, block_uid)``, not ``block_uid`` alone: a uid is
    preserved across branch copies, so an upsert keyed on the bare uid
    would steal and overwrite the parent design's (or a sibling branch's)
    row for the same block instead of posing this design's own copy.
    """
    with write_conn(store, conn) as c:
        c.execute(
            "INSERT INTO design_block_state (ref_id, block_uid, state_name, set_by) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (ref_id, block_uid) DO UPDATE SET "
            "state_name = EXCLUDED.state_name, "
            "set_by = EXCLUDED.set_by, set_at = now()",
            (ref_id, block_uid, state_name, set_by),
        )


def current_state(
    store: Any, ref_id: int, block_uid: int, *, conn: Connection | None = None
) -> str | None:
    """The state a block is currently in for this design, or None when it
    has never been posed (implicitly: its only state, or no declared
    states at all).

    Takes ``ref_id`` because a uid is preserved across branch copies: two
    designs can share a block_uid and hold independent current states.
    """
    with read_conn(store, conn) as c:
        row = c.execute(
            "SELECT state_name FROM design_block_state "
            "WHERE ref_id = %s AND block_uid = %s",
            (ref_id, block_uid),
        ).fetchone()
    return str(row[0]) if row is not None else None


def current_states(
    store: Any, ref_id: int, *, conn: Connection | None = None
) -> dict[int, str]:
    """``{block_uid: state_name}`` for every posed block in a design — the
    map a cache key is built from."""
    with read_conn(store, conn) as c:
        rows = c.execute(
            "SELECT block_uid, state_name FROM design_block_state "
            "WHERE ref_id = %s ORDER BY block_uid ASC",
            (ref_id,),
        ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


# ── the A9 cache-key rule ──────────────────────────────────────────────────


def state_cache_key(base: str, states: Mapping[int, str]) -> str:
    """``base`` + the state map, as one deterministic key string.

    See this module's A9 warning. Sorted by uid so two callers holding the
    same states in different dict order produce the same key, and blocks
    are named by **uid** (stable across saves), never by label.
    """
    if not states:
        return base
    suffix = ";".join(f"{uid}={states[uid]}" for uid in sorted(states))
    return f"{base}|states:{suffix}"


def cache_key(
    store: Any, ref_id: int, base: str, *, conn: Connection | None = None
) -> str:
    """:func:`state_cache_key` with the design's current states read for
    you — the version that is hard to get wrong, and the one callers should
    reach for.

    Only **state-carrying** blocks (more than one declared state) enter the
    key: a single-state block never changes, so including it would split
    the cache for nothing.
    """
    carrying = state_carrying_uids(store, ref_id, conn=conn)
    if not carrying:
        return base
    states = {
        uid: name
        for uid, name in current_states(store, ref_id, conn=conn).items()
        if uid in carrying
    }
    return state_cache_key(base, states)
