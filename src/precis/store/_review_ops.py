"""The review ledger and the revision log (migration 0185). Mixin on
:class:`precis.store.Store`.

Backs docs/backlog/local-mesh-upkeep.md §2 and §2b:

- ``reviews`` — who reviewed a chunk, ref or link (a human, or a model at
  a version), at which content sha, with which verdict. A review is
  *current* while its sha equals the target's sha now, so an edit makes
  it stale with no write path invalidating it. The sha registry is SQL
  (``precis_target_sha``): one definition for the triggers and for here.
- ``revisions`` — one row per transaction that changed a covered part of
  a ref or link, written by triggers. Read-only from Python; the reason,
  actor and model reach the trigger through
  :func:`precis.store.revision_context.revision_context`.

Write helper: :meth:`record_target_review`. Read helpers:
:meth:`target_sha`, :meth:`reviews_for`, :meth:`revisions_for`,
:meth:`state_at`, :meth:`unrecorded_revision_count`.

Trust state (migration 0214, docs/backlog/graph-node-trust-state.md slice
1): every ref and link carries ``trust_origin`` / ``trust_confidence`` /
``trust_write_version``, stamped by triggers from the write context, and
its acts land in ``revisions.trust_acts``. :meth:`trust_state` and
:meth:`trust_acts_for` read them; :meth:`record_trust_act` is the one door
for an explicit act (consider, demote, correct-origin, legacy-import) on an
existing target and returns its receipt; :meth:`trust_compat_report`
measures the compatibility window; :meth:`activate_trust_strict` is the
coordinator's one-way strict switch.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, get_args

import psycopg
from psycopg import Connection
from psycopg import sql as _sql

from precis.errors import BadInput, NotFound
from precis.store.revision_context import TrustAct, revision_context

TargetKind = Literal["chunk", "ref", "link", "measure"]
ReviewVerdict = Literal["proposed", "approved", "rejected"]

_TARGET_KINDS: tuple[str, ...] = get_args(TargetKind)
_VERDICTS: tuple[str, ...] = get_args(ReviewVerdict)


@dataclass(frozen=True, slots=True)
class Review:
    """One ``reviews`` row, with ``current`` resolved against the target's
    sha now (False when the target is gone). ``model`` None = a human."""

    review_id: int
    target_kind: str
    target_id: int
    actor: str
    model: str | None
    version: str
    content_sha: str
    verdict: str
    note: str | None
    at: datetime
    current: bool


@dataclass(frozen=True, slots=True)
class Revision:
    """One ``revisions`` row. ``prev_state`` is the full prior row, plus the
    replaced body chunks under ``"chunks"`` for a body change. ``new_sha``
    is None until the writing transaction commits, or for a delete."""

    revision_id: int
    target_kind: str
    target_id: int
    at: datetime
    event: str
    actor: str
    model: str | None
    reason: str
    prev_sha: str | None
    new_sha: str | None
    prev_state: dict[str, Any]
    trust_acts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TrustState:
    """A ref's or link's trust stamp now (0214). ``version`` 0 = unstamped
    legacy or compatibility write; ``sha`` is the content sha an explicit
    act must name as ``expected_sha``."""

    target_kind: str
    target_id: int
    origin: str
    confidence: str
    version: int
    sha: str | None

    @property
    def label(self) -> str:
        """``origin · confidence``, the one rendering every surface uses."""
        return f"{self.origin} · {self.confidence}"


@dataclass(frozen=True, slots=True)
class TrustReceipt:
    """What :meth:`ReviewsMixin.record_trust_act` recorded: the act as the
    ledger holds it, the state after, and whether this call was a replay."""

    act: dict[str, Any]
    state: TrustState
    replayed: bool


_TRUST_TABLE = {"ref": ("refs", "ref_id"), "link": ("links", "link_id")}


def _trust_table(target_kind: str) -> tuple[str, str]:
    try:
        return _TRUST_TABLE[target_kind]
    except KeyError:
        raise BadInput(
            f"trust state covers refs and links, not {target_kind!r}",
            options=list(_TRUST_TABLE),
        ) from None


def _check_kind(target_kind: str) -> None:
    if target_kind not in _TARGET_KINDS:
        raise BadInput(
            f"unknown review target kind {target_kind!r}", options=list(_TARGET_KINDS)
        )


class ReviewsMixin:
    """``reviews`` / ``revisions`` access; needs ``self.pool``."""

    pool: Any

    def _review_conn(self, conn: Connection | None) -> AbstractContextManager[Any]:
        return nullcontext(conn) if conn is not None else self.pool.connection()

    def target_sha(
        self, target_kind: str, target_id: int, *, conn: Connection | None = None
    ) -> str | None:
        """The target's content sha now, or None when it does not exist."""
        _check_kind(target_kind)
        with self._review_conn(conn) as c:
            row = c.execute(
                "SELECT precis_target_sha(%s, %s)", (target_kind, target_id)
            ).fetchone()
        return None if row is None else row[0]

    def record_target_review(
        self,
        target_kind: str,
        target_id: int,
        *,
        actor: str,
        verdict: str,
        model: str | None = None,
        version: str = "0",
        note: str | None = None,
        conn: Connection | None = None,
    ) -> Review:
        """Append a review of the target at its sha now. ``verdict`` is
        ``proposed`` for a machine write awaiting review, else ``approved``
        or ``rejected``. Raises NotFound when the target does not exist."""
        _check_kind(target_kind)
        if verdict not in _VERDICTS:
            raise BadInput(
                f"unknown review verdict {verdict!r}", options=list(_VERDICTS)
            )
        if not actor.strip():
            raise BadInput("a review needs an actor")
        with self._review_conn(conn) as c:
            row = c.execute(
                "INSERT INTO reviews (target_kind, target_id, actor, model, "
                "version, content_sha, verdict, note) "
                "SELECT %s, %s, %s, %s, %s, s.sha, %s, %s "
                "  FROM (SELECT precis_target_sha(%s, %s) AS sha) s "
                " WHERE s.sha IS NOT NULL "
                "RETURNING review_id, target_kind, target_id, actor, model, "
                "          version, content_sha, verdict, note, at",
                (
                    target_kind,
                    target_id,
                    actor,
                    model,
                    version,
                    verdict,
                    note,
                    target_kind,
                    target_id,
                ),
            ).fetchone()
        if row is None:
            raise NotFound(f"no {target_kind} {target_id} to review")
        rid, kind, tid, who, mdl, ver, sha, verd, nt, at = row
        return Review(rid, kind, tid, who, mdl, ver, sha, verd, nt, at, current=True)

    def reviews_for(
        self, target_kind: str, target_id: int, *, conn: Connection | None = None
    ) -> list[Review]:
        """Every review of the target, newest first, each marked current or
        not against the target's sha now."""
        _check_kind(target_kind)
        with self._review_conn(conn) as c:
            rows = c.execute(
                "SELECT review_id, target_kind, target_id, actor, model, version, "
                "       content_sha, verdict, note, at, "
                "       coalesce(content_sha = precis_target_sha(target_kind, target_id), false) "
                "  FROM reviews WHERE target_kind = %s AND target_id = %s "
                " ORDER BY at DESC, review_id DESC",
                (target_kind, target_id),
            ).fetchall()
        return [Review(*r) for r in rows]

    def revisions_for(
        self, target_kind: str, target_id: int, *, conn: Connection | None = None
    ) -> list[Revision]:
        """The target's revision chain, oldest first."""
        if target_kind not in ("ref", "link"):
            raise BadInput(
                f"revisions cover refs and links, not {target_kind!r}",
                options=["ref", "link"],
            )
        with self._review_conn(conn) as c:
            rows = c.execute(
                "SELECT revision_id, target_kind, target_id, at, event, actor, "
                "       model, reason, prev_sha, new_sha, prev_state, trust_acts "
                "  FROM revisions WHERE target_kind = %s AND target_id = %s "
                " ORDER BY at, revision_id",
                (target_kind, target_id),
            ).fetchall()
        return [Revision(*r) for r in rows]

    def state_at(
        self,
        target_kind: str,
        target_id: int,
        sha: str,
        *,
        conn: Connection | None = None,
    ) -> dict[str, Any] | None:
        """The target's row as it stood at ``sha``: the prior state of the
        first revision that left it, or None when ``sha`` is the current
        one (nothing to diff) or never appeared in the chain."""
        for rev in self.revisions_for(target_kind, target_id, conn=conn):
            if rev.prev_sha == sha:
                return rev.prev_state
        return None

    def unrecorded_revision_count(
        self, since: datetime, *, conn: Connection | None = None
    ) -> int:
        """Revisions since ``since`` that no write path gave a reason —
        the nightly count that names the paths still to wrap."""
        with self._review_conn(conn) as c:
            row = c.execute(
                "SELECT count(*) FROM revisions "
                " WHERE reason = '(unrecorded)' AND at >= %s",
                (since,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    # ── trust state (0214) ────────────────────────────────────────────────

    def trust_state(
        self, target_kind: str, target_id: int, *, conn: Connection | None = None
    ) -> TrustState | None:
        """The target's trust stamp and content sha now, or None when gone."""
        table, pk = _trust_table(target_kind)
        query = _sql.SQL(
            "SELECT trust_origin, trust_confidence, trust_write_version, "
            "       precis_target_sha({kind}, {pk}) FROM {table} WHERE {pk} = %s"
        ).format(
            kind=_sql.Literal(target_kind),
            pk=_sql.Identifier(pk),
            table=_sql.Identifier(table),
        )
        with self._review_conn(conn) as c:
            row = c.execute(query, (target_id,)).fetchone()
        if row is None:
            return None
        origin, confidence, version, sha = row
        return TrustState(target_kind, target_id, origin, confidence, int(version), sha)

    def trust_states(
        self, target_kind: str, target_ids: list[int], *, conn: Connection | None = None
    ) -> dict[int, tuple[str, str, int]]:
        """``{id: (origin, confidence, version)}`` for a batch (no sha)."""
        table, pk = _trust_table(target_kind)
        if not target_ids:
            return {}
        query = _sql.SQL(
            "SELECT {pk}, trust_origin, trust_confidence, trust_write_version "
            "  FROM {table} WHERE {pk} = ANY(%s)"
        ).format(pk=_sql.Identifier(pk), table=_sql.Identifier(table))
        with self._review_conn(conn) as c:
            rows = c.execute(query, (list(target_ids),)).fetchall()
        return {int(r[0]): (r[1], r[2], int(r[3])) for r in rows}

    def trust_acts_for(
        self, target_kind: str, target_id: int, *, conn: Connection | None = None
    ) -> list[dict[str, Any]]:
        """Every recorded trust act on the target, oldest first."""
        _trust_table(target_kind)
        with self._review_conn(conn) as c:
            rows = c.execute(
                "SELECT a FROM revisions r, "
                "     jsonb_array_elements(r.trust_acts) WITH ORDINALITY AS t(a, o) "
                " WHERE r.target_kind = %s AND r.target_id = %s "
                " ORDER BY r.revision_id, t.o",
                (target_kind, target_id),
            ).fetchall()
        return [dict(r[0]) for r in rows]

    def record_trust_act(
        self,
        act: TrustAct,
        *,
        actor: str,
        actor_type: str,
        model: str | None = None,
    ) -> TrustReceipt:
        """Record one targeted act on an existing ref or link and return
        its receipt. The trigger applies it under the row lock: a stale
        ``expected_sha`` is refused, the same ``op_id`` with the same
        payload is a replay, a changed payload conflicts. Raises NotFound
        when the target does not exist and BadInput on a refusal."""
        if act.target_id is None:
            raise BadInput(
                "record_trust_act takes a targeted act (target_id=); a "
                "create-time act rides on the write that creates the row"
            )
        table, pk = _trust_table(act.target_kind)
        touch = _sql.SQL(
            "UPDATE {table} SET trust_origin = trust_origin WHERE {pk} = %s RETURNING 1"
        ).format(table=_sql.Identifier(table), pk=_sql.Identifier(pk))
        try:
            with revision_context(
                act.reason,
                actor=actor,
                model=model,
                trust_act=act,
                trust_actor_type=actor_type,
            ):
                with self.pool.connection() as conn:
                    prior = conn.execute(
                        "SELECT precis_trust_recorded_act(%s, %s, %s)",
                        (act.target_kind, act.target_id, act.op_id),
                    ).fetchone()
                    hit = conn.execute(touch, (act.target_id,)).fetchone()
        except psycopg.errors.CheckViolation as exc:
            refusal = trust_refusal(exc)
            if refusal is None:
                raise
            raise refusal from exc
        if hit is None:
            raise NotFound(
                f"no {act.target_kind} {act.target_id} to record a trust act on"
            )
        state = self.trust_state(act.target_kind, act.target_id)
        assert state is not None
        recorded = next(
            (
                a
                for a in self.trust_acts_for(act.target_kind, act.target_id)
                if a.get("op_id") == act.op_id
            ),
            None,
        )
        if recorded is None:
            raise BadInput(f"trust act {act.op_id} was not recorded")
        return TrustReceipt(
            act=recorded,
            state=state,
            replayed=prior is not None and prior[0] is not None,
        )

    def trust_compat_report(self, *, conn: Connection | None = None) -> dict[str, Any]:
        """The compatibility-window measurement (slice 1 cutover step 3).

        ``mode`` / ``strict_at`` from trust_write_state, ``expanded_at`` = when
        0214 was applied;
        per table the stamp histogram, the version-0 rows created since
        expansion (unconverted writers, by ref kind / link relation and
        set_by) and the pre-expansion legacy count. Unattributed rows stay
        listed as such — they are writers still to convert, not closure.
        """
        with self._review_conn(conn) as c:
            state = c.execute(
                "SELECT coalesce((SELECT mode FROM trust_write_state), 'compat'), "
                "       (SELECT min(applied_at) FROM _migrations "
                "         WHERE plugin = 'precis' AND version LIKE '0214\\_%%'), "
                "       (SELECT strict_at FROM trust_write_state)"
            ).fetchone()
            assert state is not None
            mode, expanded_at, strict_at = state
            since = expanded_at or datetime.min.replace(tzinfo=UTC)
            out: dict[str, Any] = {
                "mode": mode,
                "expanded_at": expanded_at,
                "strict_at": strict_at,
            }
            for target_kind, (table, _pk) in _TRUST_TABLE.items():
                group_col = "kind" if table == "refs" else "relation"
                ident = _sql.Identifier(table)
                hist = c.execute(
                    _sql.SQL(
                        "SELECT trust_write_version, trust_origin, trust_confidence, "
                        "       count(*) FROM {t} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
                    ).format(t=ident)
                ).fetchall()
                window = c.execute(
                    _sql.SQL(
                        "SELECT {g}, coalesce(set_by, '(none)'), count(*) FROM {t} "
                        " WHERE trust_write_version = 0 AND created_at >= %s "
                        " GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2"
                    ).format(g=_sql.Identifier(group_col), t=ident),
                    (since,),
                ).fetchall()
                legacy = c.execute(
                    _sql.SQL(
                        "SELECT count(*) FROM {t} "
                        " WHERE trust_write_version = 0 AND created_at < %s"
                    ).format(t=ident),
                    (since,),
                ).fetchone()
                out[target_kind] = {
                    "stamps": [
                        {
                            "version": int(v),
                            "origin": o,
                            "confidence": cf,
                            "count": int(n),
                        }
                        for v, o, cf, n in hist
                    ],
                    "compat_writes": [
                        {group_col: g, "set_by": s, "count": int(n)}
                        for g, s, n in window
                    ],
                    "legacy_rows": int(legacy[0]) if legacy else 0,
                }
        return out

    def activate_trust_strict(self, *, lock_timeout: str = "5s") -> datetime:
        """The one-way strict switch (``precis_trust_activate_strict``).

        Serializes with in-flight ref/link/chunk writers; a lock timeout
        aborts the switch and leaves compatibility mode in force."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT precis_trust_activate_strict(%s)", (lock_timeout,)
            ).fetchone()
        assert row is not None
        return row[0]


def trust_refusal(exc: BaseException) -> BadInput | None:
    """A 0214 trigger refusal as a BadInput (cause + the trigger's hint as
    the next step), or None when ``exc`` is not one."""
    if not isinstance(exc, psycopg.errors.CheckViolation):
        return None
    message = exc.diag.message_primary or ""
    if not message.startswith("trust: "):
        return None
    return BadInput(message, next=exc.diag.message_hint or None)
