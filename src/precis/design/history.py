"""Design history — envelope revisions, checkpoints, branches.

The versioning axis, called **design history** and never "design state"
(the glossary reserves *state* for a block's physical discrete states —
:mod:`precis.design.states`). Three primitives, each earning its keep by
making a specific failure impossible:

- **envelope revisions** (spec §1.5). The phase loop tightens an envelope
  whenever a late failure invalidates an early assumption. Each tightening
  mints a revision; every scored result records the revision it was scored
  under. That is the whole mechanism by which a stale result becomes
  *detectable* rather than silently trusted.
- **checkpoints.** A labelled snapshot a design can be restored to. The
  payload is the owning kind's own serialised tree, **opaque here** —
  core does not know the renter's block shape and must not learn it, so
  :func:`save_checkpoint` stores what it is handed and
  :func:`load_checkpoint` hands it straight back.
- **branches.** A pin creates a branch, never an overwrite (spec §5.6), so
  a pin is always reversible and both candidates stay inspectable. The
  response is a *comparison*, not just a new design:
  :class:`BranchResult` carries the headline numbers, the delta against the
  parent, and — when the branch didn't work — what broke.

Branch storage is a **naive full copy** through the renter's
retire-all/reinsert-all persist pattern with block uids preserved; that
carried-forward uid is what makes the copy diffable block-by-block.
Copy-on-write structural sharing waits until branch count measurably hurts
(the spec's own naive-first-and-measure rule), and this module's surface
doesn't change when it arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeGuard

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis.design._db import read_conn, write_conn

_CHECKPOINT_COLS = (
    "id, ref_id, label, envelope_revision, headline, payload, reason, set_by, "
    "created_at"
)
_BRANCH_COLS = (
    "id, ref_id, parent_ref_id, parent_branch_id, reason, headline, "
    "envelope_revision, set_by, created_at"
)


@dataclass(frozen=True)
class Checkpoint:
    """A labelled snapshot. ``payload`` is the renter's own serialisation."""

    id: int
    ref_id: int
    label: str
    envelope_revision: int
    headline: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    set_by: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class Branch:
    """A branch record — the design plus where it came from and why."""

    id: int
    ref_id: int
    reason: str
    parent_ref_id: int | None = None
    parent_branch_id: int | None = None
    headline: dict[str, Any] = field(default_factory=dict)
    envelope_revision: int = 0
    set_by: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class BranchResult:
    """What ``pin`` returns: a comparison, not just a new design (spec
    §5.6). ``delta_vs_parent`` is per headline key; ``infeasible`` is None
    for a branch that solved, else ``{what_broke, which_constraint}``."""

    branch_id: int
    ref_id: int
    reason: str
    headline: dict[str, Any] = field(default_factory=dict)
    delta_vs_parent: dict[str, dict[str, Any]] = field(default_factory=dict)
    infeasible: dict[str, Any] | None = None


# ── envelope revisions ─────────────────────────────────────────────────────


def current_revision(store: Any, ref_id: int, *, conn: Connection | None = None) -> int:
    """The design's current envelope revision. 0 means "never tightened",
    which is a legitimate state for a fresh design — not a missing row."""
    with read_conn(store, conn) as c:
        row = c.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM design_envelope_revisions "
            "WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    return int(row[0]) if row is not None else 0


def mint_envelope_revision(
    store: Any,
    ref_id: int,
    *,
    reason: str | None = None,
    block_uid: int | None = None,
    set_by: str | None = None,
    conn: Connection | None = None,
) -> int:
    """Mint the next envelope revision for a design and return it.

    Computed in the INSERT rather than read-then-write, so two concurrent
    tightenings can't both mint the same number by reading the same max;
    the ``UNIQUE (ref_id, revision)`` index turns the remaining race into a
    loud failure rather than a duplicate revision.
    """
    with write_conn(store, conn) as c:
        row = c.execute(
            "INSERT INTO design_envelope_revisions "
            "(ref_id, revision, block_uid, reason, set_by) "
            "SELECT %s, COALESCE(MAX(revision), 0) + 1, %s, %s, %s "
            "FROM design_envelope_revisions WHERE ref_id = %s "
            "RETURNING revision",
            (ref_id, block_uid, reason, set_by, ref_id),
        ).fetchone()
    assert row is not None
    return int(row[0])


def revision_log(
    store: Any, ref_id: int, *, conn: Connection | None = None
) -> list[dict[str, Any]]:
    """Every tightening, newest last — what tightened, when, and why."""
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT revision, block_uid, reason, set_by, created_at "
                "FROM design_envelope_revisions WHERE ref_id = %s "
                "ORDER BY revision ASC",
                (ref_id,),
            )
            return [dict(r) for r in cur.fetchall()]


# ── checkpoints ────────────────────────────────────────────────────────────


def save_checkpoint(
    store: Any,
    ref_id: int,
    *,
    label: str,
    payload: dict[str, Any],
    headline: dict[str, Any] | None = None,
    reason: str | None = None,
    set_by: str | None = None,
    envelope_revision: int | None = None,
    conn: Connection | None = None,
) -> Checkpoint:
    """Store a snapshot under ``label``, replacing any snapshot already
    under that label for this design.

    ``envelope_revision`` defaults to the design's current one, so a
    restored checkpoint can always be compared against what the design has
    tightened since.
    """
    text = (label or "").strip()
    if not text:
        raise ValueError("a checkpoint needs a non-empty label")
    with write_conn(store, conn) as c:
        revision = (
            envelope_revision
            if envelope_revision is not None
            else current_revision(store, ref_id, conn=c)
        )
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO design_checkpoints "
                f"(ref_id, label, envelope_revision, headline, payload, reason, set_by) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
                f"ON CONFLICT (ref_id, label) DO UPDATE SET "
                f"envelope_revision = EXCLUDED.envelope_revision, "
                f"headline = EXCLUDED.headline, payload = EXCLUDED.payload, "
                f"reason = EXCLUDED.reason, set_by = EXCLUDED.set_by, "
                f"created_at = now() "
                f"RETURNING {_CHECKPOINT_COLS}",
                (
                    ref_id,
                    text,
                    revision,
                    Jsonb(headline or {}),
                    Jsonb(payload),
                    reason,
                    set_by,
                ),
            )
            row = cur.fetchone()
    assert row is not None
    return _checkpoint_from_row(row)


def load_checkpoint(
    store: Any, ref_id: int, label: str, *, conn: Connection | None = None
) -> Checkpoint | None:
    """One checkpoint by label, payload verbatim — the restore read. The
    caller (the kind that wrote the payload) is the only thing that knows
    how to apply it."""
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_CHECKPOINT_COLS} FROM design_checkpoints "
                "WHERE ref_id = %s AND label = %s",
                (ref_id, label),
            )
            row = cur.fetchone()
    return _checkpoint_from_row(row) if row is not None else None


def list_checkpoints(
    store: Any, ref_id: int, *, conn: Connection | None = None
) -> list[Checkpoint]:
    """A design's checkpoints, newest first, WITHOUT their payloads — the
    list is a menu, and a payload per row would make reading the menu cost
    as much as a restore."""
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, ref_id, label, envelope_revision, headline, reason, "
                "set_by, created_at FROM design_checkpoints "
                "WHERE ref_id = %s ORDER BY created_at DESC, id DESC",
                (ref_id,),
            )
            rows = cur.fetchall()
    return [_checkpoint_from_row({**r, "payload": {}}) for r in rows]


def delete_checkpoint(
    store: Any, ref_id: int, label: str, *, conn: Connection | None = None
) -> bool:
    """Drop a checkpoint. True when a row was removed."""
    with write_conn(store, conn) as c:
        rows = c.execute(
            "DELETE FROM design_checkpoints WHERE ref_id = %s AND label = %s "
            "RETURNING id",
            (ref_id, label),
        ).fetchall()
    return bool(rows)


def _checkpoint_from_row(row: dict[str, Any]) -> Checkpoint:
    return Checkpoint(
        id=int(row["id"]),
        ref_id=int(row["ref_id"]),
        label=row["label"],
        envelope_revision=int(row["envelope_revision"]),
        headline=dict(row["headline"] or {}),
        payload=dict(row["payload"] or {}),
        reason=row["reason"],
        set_by=row["set_by"],
        created_at=row["created_at"],
    )


# ── branches ───────────────────────────────────────────────────────────────


def record_branch(
    store: Any,
    ref_id: int,
    *,
    reason: str,
    parent_ref_id: int | None = None,
    parent_branch_id: int | None = None,
    headline: dict[str, Any] | None = None,
    envelope_revision: int | None = None,
    set_by: str | None = None,
    conn: Connection | None = None,
) -> Branch:
    """Record that ``ref_id`` is a branch of ``parent_ref_id``.

    Called by the renting kind AFTER it has copied the design (uids
    preserved). Core owns the branch ledger, not the copy: the copy is the
    renter's own persist pattern and core has no business knowing its
    tables.
    """
    text = (reason or "").strip()
    if not text:
        raise ValueError(
            "a branch needs a one-line reason — a branch list nobody can read "
            "is a tree browser with extra steps"
        )
    with write_conn(store, conn) as c:
        revision = (
            envelope_revision
            if envelope_revision is not None
            else current_revision(store, parent_ref_id or ref_id, conn=c)
        )
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO design_branches "
                f"(ref_id, parent_ref_id, parent_branch_id, reason, headline, "
                f" envelope_revision, set_by) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
                f"ON CONFLICT (ref_id) DO UPDATE SET "
                f"parent_ref_id = EXCLUDED.parent_ref_id, "
                f"parent_branch_id = EXCLUDED.parent_branch_id, "
                f"reason = EXCLUDED.reason, headline = EXCLUDED.headline, "
                f"envelope_revision = EXCLUDED.envelope_revision, "
                f"set_by = EXCLUDED.set_by "
                f"RETURNING {_BRANCH_COLS}",
                (
                    ref_id,
                    parent_ref_id,
                    parent_branch_id,
                    text,
                    Jsonb(headline or {}),
                    revision,
                    set_by,
                ),
            )
            row = cur.fetchone()
    assert row is not None
    return _branch_from_row(row)


def get_branch(
    store: Any, ref_id: int, *, conn: Connection | None = None
) -> Branch | None:
    """The branch record for a design, or None when it is not a branch."""
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_BRANCH_COLS} FROM design_branches WHERE ref_id = %s",
                (ref_id,),
            )
            row = cur.fetchone()
    return _branch_from_row(row) if row is not None else None


def list_branches(
    store: Any, *, parent_ref_id: int | None = None, conn: Connection | None = None
) -> list[Branch]:
    """Branches, newest first — every one, or only those off one parent.

    One line per branch is the entire browsing surface by design (spec
    §5.6): no tree browser, because a tree browser's context cost is paid
    on every read.
    """
    with read_conn(store, conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            if parent_ref_id is None:
                cur.execute(
                    f"SELECT {_BRANCH_COLS} FROM design_branches "
                    "ORDER BY created_at DESC, id DESC"
                )
            else:
                cur.execute(
                    f"SELECT {_BRANCH_COLS} FROM design_branches "
                    "WHERE parent_ref_id = %s ORDER BY created_at DESC, id DESC",
                    (parent_ref_id,),
                )
            rows = cur.fetchall()
    return [_branch_from_row(r) for r in rows]


def _branch_from_row(row: dict[str, Any]) -> Branch:
    return Branch(
        id=int(row["id"]),
        ref_id=int(row["ref_id"]),
        reason=row["reason"],
        parent_ref_id=(
            int(row["parent_ref_id"]) if row["parent_ref_id"] is not None else None
        ),
        parent_branch_id=(
            int(row["parent_branch_id"])
            if row["parent_branch_id"] is not None
            else None
        ),
        headline=dict(row["headline"] or {}),
        envelope_revision=int(row["envelope_revision"]),
        set_by=row["set_by"],
        created_at=row["created_at"],
    )


def headline_delta(
    headline: dict[str, Any] | None, parent_headline: dict[str, Any] | None
) -> dict[str, dict[str, Any]]:
    """Per-key comparison of two headline bags.

    Numeric keys get ``{from, to, delta, pct}``; ``pct`` is None when the
    parent value is zero (no honest percentage exists). Non-numeric or
    one-sided keys still appear, with ``{from, to}`` only — a key that
    exists on one side and not the other is exactly the kind of change a
    caller wants to see, not one to drop for being awkward to subtract.
    """
    left = dict(parent_headline or {})
    right = dict(headline or {})
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(set(left) | set(right)):
        was, now = left.get(key), right.get(key)
        if was == now:
            continue
        item: dict[str, Any] = {"from": was, "to": now}
        if _is_number(was) and _is_number(now):
            delta = float(now) - float(was)
            item["delta"] = delta
            item["pct"] = (delta / float(was) * 100.0) if float(was) != 0.0 else None
        out[key] = item
    return out


def branch_result(
    branch: Branch,
    *,
    parent_headline: dict[str, Any] | None = None,
    infeasible: dict[str, Any] | None = None,
) -> BranchResult:
    """Assemble the ``pin`` response from a stored branch + the parent's
    headline numbers."""
    return BranchResult(
        branch_id=branch.id,
        ref_id=branch.ref_id,
        reason=branch.reason,
        headline=dict(branch.headline),
        delta_vs_parent=headline_delta(branch.headline, parent_headline),
        infeasible=dict(infeasible) if infeasible else None,
    )


def _is_number(value: Any) -> TypeGuard[float]:
    """True for real numbers only — bool is an int in Python and a boolean
    headline flag has no meaningful delta. A TypeGuard so the caller's
    arithmetic type-checks off the same test that guards it."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)
