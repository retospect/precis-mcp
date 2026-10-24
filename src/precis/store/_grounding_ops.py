"""Stored typed groundings and the term admit act (migration 0235;
docs/backlog/graph-node-trust-state.md slice 2b, Reto rulings 2026-10-21).
Mixin on :class:`precis.store.Store`.

A ``groundings`` row is a target's CURRENT typed grounding — ``defined``,
``sourced`` or ``verified`` (``motivated``/``none`` stay derived, see
:mod:`precis.taproot.eligibility`). Current state lives in the table, never
in meta; history is the ``ground``/``unground`` acts the row's AFTER
triggers append to the target's ``revisions.trust_acts``. A defined
grounding is never upgraded in place: a later sourced/verified grounding
is a sibling row; demotion is an explicit retire.

Doors:

- :meth:`record_grounding` — mint one row. ``sourced``/``verified`` need
  resolvable pins (addressable chunk/ref handles); ``verified`` also names
  a current approved review (the checker's); ``defined`` needs a current
  approved review by the owner (lax Q2: recorded as the caller stated it —
  ``model IS NULL`` and the actor is the configured owner identity), a
  ``considered`` target, and binds the target's trust-subject sha.
- :meth:`retire_grounding` — explicit demotion; nothing is deleted.
- :meth:`groundings_for` / :meth:`grounding_set` — reads for surfaces and
  the eligibility endpoint arguments.
- :meth:`admit_ref` — the Q6 term admit gate: body definition + stored
  sourced use whose pins resolve + current approved review + considered,
  checked in one transaction, each missing condition a named BadInput; a
  passing check records an ``admit`` act (stamp unchanged — admission
  changes no origin).
- :meth:`is_admitted` — the current fold: the latest admit act still binds
  the subject sha now (an edit restales it) and no demote followed it.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.errors import BadInput, NotFound
from precis.store.revision_context import (
    TrustAct,
    apply_revision_context,
    revision_context,
)
from precis.utils import handle_registry

GroundingType = Literal["defined", "sourced", "verified"]
STORED_GROUNDINGS: tuple[str, ...] = ("defined", "sourced", "verified")

#: Named gate rules — tests pin these strings.
RULE_ADMIT_NO_DEFINITION = "admit-no-definition"
RULE_ADMIT_NO_SOURCED = "admit-no-sourced-use"
RULE_ADMIT_NO_REVIEW = "admit-no-current-review"
RULE_ADMIT_NOT_CONSIDERED = "admit-not-considered"
RULE_PIN_UNRESOLVABLE = "grounding-pin-unresolvable"
RULE_DEFINED_NOT_OWNER = "defined-review-not-owner"
RULE_DEFINED_REVIEW = "defined-review-not-current"
RULE_DEFINED_NOT_CONSIDERED = "defined-not-considered"
RULE_VERIFIED_REVIEW = "verified-review-not-current"


@dataclass(frozen=True, slots=True)
class GroundingRow:
    """One ``groundings`` row."""

    grounding_id: int
    target_kind: str
    target_id: int
    grounding: str
    pins: list[dict[str, Any]] = field(default_factory=list)
    definition_sha: str | None = None
    review_id: int | None = None
    recorded_by: str = ""
    actor_type: str = ""
    at: datetime | None = None
    retired_at: datetime | None = None
    retired_reason: str | None = None


_GROUNDING_COLS = (
    "grounding_id, target_kind, target_id, grounding, pins, definition_sha, "
    "review_id, recorded_by, actor_type, at, retired_at, retired_reason"
)


def _owner_actors() -> frozenset[str]:
    """The actor spellings counted as "the owner" under lax Q2: the
    configured owner, its ``user:<owner>`` web form, and ``reto`` — the
    actor the 0185 mirrors already stamp Reto's rulings as. Recorded as
    the caller states it; the authenticated boundary is a later slice."""
    from precis.acting_user import actor_for
    from precis.config import load_config

    owner = load_config().owner
    return frozenset({owner, actor_for(owner), "reto"})


def _resolve_pin(pin: Any) -> dict[str, Any]:
    """One pin string → ``{'handle', 'kind' ('ref'|'chunk'), 'id'}``, or a
    BadInput when it is not an addressable refs-backed handle."""
    parsed = handle_registry.parse(pin) if isinstance(pin, str) else None
    if parsed is None:
        raise BadInput(
            f"{RULE_PIN_UNRESOLVABLE}: {pin!r} is not an addressable handle "
            "(a chunk or ref handle like pc42 / fi7)",
            next="a sourced grounding pins a passage or finding the reader "
            "can open — a bare URL or tag is not a pin",
        )
    kind, is_chunk, pk = parsed
    return {
        "handle": handle_registry.normalize(pin),
        "kind": "chunk" if is_chunk else "ref",
        "id": int(pk),
    }


def _pin_live(c: Connection, pin: dict[str, Any]) -> bool:
    """The pin's target exists and is not retired."""
    if pin["kind"] == "chunk":
        row = c.execute(
            "SELECT 1 FROM chunks WHERE chunk_id = %s AND retired_at IS NULL",
            (pin["id"],),
        ).fetchone()
    else:
        row = c.execute(
            "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL",
            (pin["id"],),
        ).fetchone()
    return row is not None


class GroundingsMixin:
    """``groundings`` access; needs ``self.pool`` and the review doors."""

    pool: Any
    # Provided by ReviewsMixin / Store on the composed Store.
    trust_state: Any
    trust_acts_for: Any
    record_trust_act: Any
    atomic: Any

    def _grounding_conn(self, conn: Connection | None) -> AbstractContextManager[Any]:
        return nullcontext(conn) if conn is not None else self.pool.connection()

    # ── reads ────────────────────────────────────────────────────────────

    def groundings_for(
        self,
        target_kind: str,
        target_id: int,
        *,
        include_retired: bool = False,
        conn: Connection | None = None,
    ) -> list[GroundingRow]:
        """The target's grounding rows, live first then retired, newest
        first within each."""
        if target_kind not in ("ref", "link"):
            raise BadInput(
                f"groundings cover refs and links, not {target_kind!r}",
                options=["ref", "link"],
            )
        with self._grounding_conn(conn) as c:
            rows = c.execute(
                f"SELECT {_GROUNDING_COLS} FROM groundings "
                " WHERE target_kind = %s AND target_id = %s "
                "   AND (%s OR retired_at IS NULL) "
                " ORDER BY retired_at IS NOT NULL, at DESC, grounding_id DESC",
                (target_kind, target_id, include_retired),
            ).fetchall()
        return [GroundingRow(*r) for r in rows]

    def grounding_set(
        self, target_kind: str, target_id: int, *, conn: Connection | None = None
    ) -> frozenset[str]:
        """The target's live stored grounding types (for eligibility's
        endpoint arguments)."""
        with self._grounding_conn(conn) as c:
            rows = c.execute(
                "SELECT grounding FROM groundings "
                " WHERE target_kind = %s AND target_id = %s AND retired_at IS NULL",
                (target_kind, target_id),
            ).fetchall()
        return frozenset(str(r[0]) for r in rows)

    def stored_groundings(
        self, targets: list[tuple[str, int]], *, conn: Connection | None = None
    ) -> dict[tuple[str, int], set[str]]:
        """Batch form of :meth:`grounding_set` — delegates to
        :func:`precis.taproot.eligibility.stored_groundings`, the one
        loader the endpoint consumers share."""
        from precis.taproot import eligibility

        rows = eligibility.stored_groundings(self, targets, conn=conn)
        return {k: {str(g) for g in v} for k, v in rows.items()}

    def definition_label(
        self, target_kind: str, target_id: int, *, conn: Connection | None = None
    ) -> str | None:
        """``definition (who, YYYY-MM-DD)`` for a target with a live
        defined grounding, else None."""
        with self._grounding_conn(conn) as c:
            row = c.execute(
                "SELECT coalesce("
                "  (SELECT r.actor FROM reviews r WHERE r.review_id = g.review_id),"
                "  g.recorded_by), g.at "
                "  FROM groundings g WHERE g.target_kind = %s AND g.target_id = %s "
                "   AND g.grounding = 'defined' AND g.retired_at IS NULL "
                " ORDER BY g.at LIMIT 1",
                (target_kind, target_id),
            ).fetchone()
        if row is None:
            return None
        actor, at = row
        day = at.astimezone().strftime("%Y-%m-%d") if at is not None else "?"
        return f"definition ({actor}, {day})"

    # ── writes ───────────────────────────────────────────────────────────

    def record_grounding(
        self,
        target_kind: str,
        target_id: int,
        *,
        grounding: GroundingType,
        pins: list[str] | None = None,
        review_id: int | None = None,
        actor: str,
        actor_type: str,
        reason: str,
        conn: Connection | None = None,
    ) -> GroundingRow:
        """Mint one grounding row. Never an upgrade: a second grounding of
        the same type is a sibling row (the earlier one stays until
        explicitly retired). Raises BadInput with a named rule on a
        contract violation, NotFound when the target is gone."""
        if target_kind not in ("ref", "link"):
            raise BadInput(
                f"groundings cover refs and links, not {target_kind!r}",
                options=["ref", "link"],
            )
        if grounding not in STORED_GROUNDINGS:
            raise BadInput(
                f"a stored grounding is one of {STORED_GROUNDINGS} "
                f"(motivated and none stay derived), got {grounding!r}",
                options=list(STORED_GROUNDINGS),
            )
        if not str(reason).strip():
            raise BadInput("a grounding needs a reason")
        if not str(actor).strip():
            raise BadInput("a grounding needs an actor")
        resolved = [_resolve_pin(p) for p in (pins or [])]

        def _do(c: Connection) -> GroundingRow:
            apply_revision_context(c)
            # target exists and is live (links are deleted, not retired —
            # existence is liveness)
            if target_kind == "ref":
                tgt = c.execute(
                    "SELECT ref_id FROM refs  WHERE ref_id = %s AND retired_at IS NULL",
                    (target_id,),
                ).fetchone()
            else:
                tgt = c.execute(
                    "SELECT link_id FROM links WHERE link_id = %s",
                    (target_id,),
                ).fetchone()
            if tgt is None:
                raise NotFound(f"no live {target_kind} {target_id} to ground")

            if grounding in ("sourced", "verified"):
                if not resolved:
                    raise BadInput(
                        f"a {grounding} grounding needs at least one pin "
                        "(an addressable passage/finding handle)",
                        next="pins=['pc…', 'fi…']",
                    )
                dead = [p["handle"] for p in resolved if not _pin_live(c, p)]
                if dead:
                    raise BadInput(
                        f"{RULE_PIN_UNRESOLVABLE}: pin(s) {dead} name no "
                        "live chunk or ref",
                        next="re-resolve the passage/finding and pin its "
                        "current handle",
                    )
            review_ok: Any = None
            if grounding in ("verified", "defined"):
                if review_id is None:
                    raise BadInput(
                        f"a {grounding} grounding names the review that "
                        "certifies it (review_id=)",
                        next="record the review first "
                        "(store.record_target_review), then ground by it",
                    )
                review_ok = c.execute(
                    "SELECT review_id, actor, model, verdict, "
                    "       coalesce(content_sha = "
                    "         CASE sha_basis WHEN 'trust-subject' "
                    "              THEN precis_trust_subject_sha(target_kind, target_id) "
                    "              ELSE precis_target_sha(target_kind, target_id) END,"
                    "         false) AS current "
                    "  FROM reviews WHERE review_id = %s",
                    (review_id,),
                ).fetchone()
                if review_ok is None:
                    raise BadInput(
                        f"{RULE_DEFINED_REVIEW}: no review {review_id}",
                        next="record the certifying review first",
                    )
                if review_ok[3] != "approved" or not review_ok[4]:
                    raise BadInput(
                        f"{RULE_DEFINED_REVIEW if grounding == 'defined' else RULE_VERIFIED_REVIEW}: "
                        f"review {review_id} is {review_ok[3]}"
                        + ("" if review_ok[4] else " and stale"),
                        next="approve a fresh review on the current sha, "
                        "then ground by it",
                    )
            definition_sha = None
            if grounding == "defined":
                if review_ok[2] is not None or review_ok[1] not in _owner_actors():
                    raise BadInput(
                        f"{RULE_DEFINED_NOT_OWNER}: 'defined' is minted only "
                        f"by the owner's human review (review {review_id} is "
                        f"actor={review_ok[1]!r} model={review_ok[2]!r})",
                        next="the owner records an approved review "
                        "(precis trust review --by <owner>), then this "
                        "grounding cites it — machine review mints at most "
                        "sourced/verified",
                    )
                state = self.trust_state(target_kind, target_id, conn=c)
                if state is None or state.confidence != "considered":
                    raise BadInput(
                        f"{RULE_DEFINED_NOT_CONSIDERED}: {target_kind} "
                        f"{target_id} is not considered — a definition is a "
                        "considered act, not a hunch",
                        next="record the consider act first (edit(mode='trust'))",
                    )
                sha_row = c.execute(
                    "SELECT precis_trust_subject_sha(%s, %s)",
                    (target_kind, target_id),
                ).fetchone()
                definition_sha = None if sha_row is None else sha_row[0]

            row = c.execute(
                f"INSERT INTO groundings (target_kind, target_id, grounding, "
                " pins, definition_sha, review_id, recorded_by, actor_type) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_GROUNDING_COLS}",
                (
                    target_kind,
                    target_id,
                    grounding,
                    Jsonb(resolved),
                    definition_sha,
                    review_id,
                    actor,
                    actor_type,
                ),
            ).fetchone()
            assert row is not None
            return GroundingRow(*row)

        with revision_context(reason, actor=actor, trust_actor_type=actor_type):
            if conn is not None:
                return _do(conn)
            with self.pool.connection() as c:
                return _do(c)

    def retire_grounding(
        self,
        grounding_id: int,
        *,
        reason: str,
        actor: str,
        actor_type: str = "agent",
        conn: Connection | None = None,
    ) -> GroundingRow:
        """Explicit demotion: stamp retired_at/reason; the row's trigger
        appends the 'unground' act. Nothing is deleted."""
        if not str(reason).strip():
            raise BadInput("retiring a grounding needs a reason")

        def _do(c: Connection) -> GroundingRow:
            apply_revision_context(c)
            row = c.execute(
                f"UPDATE groundings SET retired_at = now(), retired_reason = %s "
                f" WHERE grounding_id = %s AND retired_at IS NULL "
                f"RETURNING {_GROUNDING_COLS}",
                (reason, grounding_id),
            ).fetchone()
            if row is None:
                raise NotFound(
                    f"no live grounding {grounding_id} (missing or already retired)"
                )
            return GroundingRow(*row)

        with revision_context(reason, actor=actor, trust_actor_type=actor_type):
            if conn is not None:
                return _do(conn)
            with self.pool.connection() as c:
                return _do(c)

    # ── the admit gate (Q6) ──────────────────────────────────────────────

    def admit_ref(
        self,
        ref_id: int,
        *,
        reason: str,
        actor: str,
        actor_type: str = "agent",
    ) -> dict[str, Any]:
        """The term/concept admit act: body definition + a stored
        ``sourced`` grounding whose pins resolve + a current approved
        review + a consider act — checked in one transaction, each missing
        condition a named BadInput. Passing records an ``admit`` act;
        the stamp (origin · confidence) is unchanged."""
        with self.atomic() as c:
            row = c.execute(
                "SELECT kind, meta, trust_confidence, trust_write_version "
                "  FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"no live ref {ref_id} to admit")
            kind, meta, confidence, _version = row
            # 1 — a body definition: the term convention (meta.definition)
            #     or at least one live subject chunk carrying text.
            has_definition = bool(str((meta or {}).get("definition") or "").strip())
            if not has_definition:
                has_definition = (
                    c.execute(
                        "SELECT 1 FROM chunks WHERE ref_id = %s AND retired_at IS NULL "
                        "   AND precis_trust_subject_chunk(chunk_kind, ord) "
                        "   AND btrim(text) <> '' LIMIT 1",
                        (ref_id,),
                    ).fetchone()
                    is not None
                )
            if not has_definition:
                raise BadInput(
                    f"{RULE_ADMIT_NO_DEFINITION}: {kind} {ref_id} has no "
                    "definition (meta.definition or a live body chunk)",
                    next="write the definition first — the admit gate "
                    "certifies a defined term, it does not write one",
                )
            # 2 — a stored sourced grounding whose pins all still resolve.
            sourced_ok = False
            for g in self.groundings_for("ref", ref_id, conn=c):
                if g.grounding != "sourced" or not g.pins:
                    continue
                if all(_pin_live(c, p) for p in g.pins):
                    sourced_ok = True
                    break
            if not sourced_ok:
                raise BadInput(
                    f"{RULE_ADMIT_NO_SOURCED}: {kind} {ref_id} has no stored "
                    "sourced grounding with resolvable pins",
                    next="record_grounding(grounding='sourced', pins=[…]) — "
                    "a pin is an addressable passage/finding handle, not a "
                    "URL or a co-occurrence",
                )
            # 3 — a current approved review of the ref.
            review = c.execute(
                "SELECT review_id FROM reviews "
                " WHERE target_kind = 'ref' AND target_id = %s "
                "   AND verdict = 'approved' "
                "   AND content_sha = CASE sha_basis WHEN 'trust-subject' "
                "         THEN precis_trust_subject_sha(target_kind, target_id) "
                "         ELSE precis_target_sha(target_kind, target_id) END "
                " ORDER BY at DESC LIMIT 1",
                (ref_id,),
            ).fetchone()
            if review is None:
                raise BadInput(
                    f"{RULE_ADMIT_NO_REVIEW}: {kind} {ref_id} has no current "
                    "approved review",
                    next="record a review on the ref's current sha "
                    "(precis trust review ref ID --by …) — machine review "
                    "counts; it does not mint defined",
                )
            # 4 — considered (the explicit act).
            if confidence != "considered":
                raise BadInput(
                    f"{RULE_ADMIT_NOT_CONSIDERED}: {kind} {ref_id} is "
                    f"{confidence}, not considered",
                    next="record the consider act: edit(mode='trust', "
                    "args={'trust': {'confidence': 'considered', …}})",
                )
            sha = self.trust_state("ref", ref_id, conn=c)
            assert sha is not None and sha.sha is not None
            receipt = self.record_trust_act(
                TrustAct(
                    action="admit",
                    target_kind="ref",
                    target_id=ref_id,
                    expected_sha=sha.sha,
                    reason=reason,
                ),
                actor=actor,
                actor_type=actor_type,
            )
        return {"admitted": True, "act": receipt.act, "replayed": receipt.replayed}

    def is_admitted(self, ref_id: int, *, conn: Connection | None = None) -> bool:
        """Currently admitted: the latest admit act still binds the
        subject sha now (a semantic edit restales it) and no demote act
        followed it. Current confidence must be considered — a demote
        flips it, a revise restamps hunch; both un-admit."""
        state = self.trust_state("ref", ref_id, conn=conn)
        if state is None or state.confidence != "considered" or state.sha is None:
            return False
        admitted_at_sha: str | None = None
        for act in self.trust_acts_for("ref", ref_id, conn=conn):
            action = act.get("action")
            if action == "admit":
                admitted_at_sha = act.get("expected_sha")
            elif action in ("demote", "revise"):
                admitted_at_sha = None
        return admitted_at_sha is not None and admitted_at_sha == state.sha
