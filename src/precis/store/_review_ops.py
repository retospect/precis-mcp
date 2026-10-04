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
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args

from psycopg import Connection

from precis.errors import BadInput, NotFound

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
                "       model, reason, prev_sha, new_sha, prev_state "
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
