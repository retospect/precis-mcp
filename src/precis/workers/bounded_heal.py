"""bounded_heal — the ONE "self-heal with human handoff" primitive
(self-healing-spine Layer 2, slice 3).

The shape extracted from the sweeper's unpark phase (the repo's proven
inline original — attempts counter + exponential cooldown + hard cap +
terminal latch + one escalation for the capped case), made reusable for
heal actions that have no ref to hang state off (restart-once targets a
``(host, process)``, not a todo). State lives in ``app_settings`` under
``bounded_heal:<key>`` as a small JSON blob; the attempt bump is a
compare-and-swap on that row, so two hosts evaluating the same condition
in the same window can't both fire the action — the loser sees
``"raced"`` and stands down.

Semantics per :func:`run_bounded_heal` call:

* state older than ``reset_after_s`` (default 24 h) is treated as fresh —
  a *new incident* gets its own attempt budget, but a condition that
  flaps green↔red inside the window keeps burning the same budget, so a
  heal that "works" for twenty minutes at a time can never turn into an
  unbounded kick loop (the embedder-watchdog lesson: a restart watchdog
  that keeps kicking a wedge must escalate, not keep kicking).
* at ``cap`` attempts the key latches terminal and files ONE gripe
  (``origin:bounded-heal``) — the human handoff; further calls return
  ``"latched"`` without acting until the state ages out.
* inside the per-attempt cooldown (``base_cooldown_s · 2ᴺ``) → ``"cooldown"``.
* the attempt is burned (CAS) *before* the action runs — deliberately,
  matching the unpark discipline: an action that crashes its own process
  (restarting the local worker unit) must not be able to retry forever
  because it never lived to record the attempt.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from precis.store import Store

log = logging.getLogger(__name__)

_STATE_PREFIX = "bounded_heal:"

#: A green-again condition only resets a heal budget once the last attempt
#: is this old — bounds autonomous kicks to ``cap`` per window even when
#: the condition flaps.
_RESET_AFTER_S_DEFAULT = 24 * 3600.0


@dataclass(frozen=True)
class HealSpec:
    """One bounded heal action's identity + bounds.

    ``key`` is the durable state identity (e.g.
    ``restart-worker:melchior:precis-worker``); ``cap`` the autonomous
    attempt budget before the terminal latch; ``base_cooldown_s`` the
    first inter-attempt cooldown (doubles per attempt). ``title`` /
    ``detail`` seed the cap-escalation gripe.
    """

    key: str
    cap: int
    base_cooldown_s: float
    title: str
    detail: str = ""
    reset_after_s: float = _RESET_AFTER_S_DEFAULT


def _state_key(key: str) -> str:
    return _STATE_PREFIX + key


def _load_state(store: Store, key: str) -> tuple[dict, str | None]:
    """Return ``(state, raw)`` — ``raw`` is the exact stored string for the
    CAS (``None`` when no row exists yet).

    Direct SQL on ``app_settings`` (the 0070 generic K/V store, the one
    the CAS below writes) — NOT ``store.get_setting``, which reads the
    older ``app_state`` table.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = %s", (_state_key(key),)
        ).fetchone()
    raw = None if row is None else str(row[0])
    if raw is None:
        return {}, None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return {}, raw
    return (state if isinstance(state, dict) else {}), raw


def _cas_state(store: Store, key: str, old_raw: str | None, new: dict) -> bool:
    """Atomically replace the state row iff it still holds ``old_raw``.

    INSERT-if-absent when ``old_raw`` is None; UPDATE-guarded-by-old-value
    otherwise. Returns False when another process moved the row first —
    the caller lost the race and must not act.
    """
    new_raw = json.dumps(new, sort_keys=True)
    with store.tx() as conn:
        if old_raw is None:
            row = conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO NOTHING RETURNING key",
                (_state_key(key), new_raw),
            ).fetchone()
            return row is not None
        row = conn.execute(
            "UPDATE app_settings SET value = %s, updated_at = now() "
            " WHERE key = %s AND value = %s RETURNING key",
            (new_raw, _state_key(key), old_raw),
        ).fetchone()
        return row is not None


def _age_s(state: dict) -> float | None:
    last = state.get("last_at")
    if not isinstance(last, str):
        return None
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return None
    return (datetime.now(UTC) - then).total_seconds()


def _file_cap_gripe(store: Store, spec: HealSpec, attempts: int) -> int | None:
    """One human-handoff gripe when the cap latches (mirrors the
    health_digest router's direct insert-ref shape). Best-effort."""
    from precis.store.types import BlockInsert, Tag

    body = (
        f"bounded_heal: {spec.key}\n"
        f"{spec.title}\n{spec.detail}\n"
        f"Autonomous heal cap reached ({attempts}/{spec.cap} attempts) — "
        "latched terminal; human intervention required. The latch ages out "
        f"after {spec.reset_after_s / 3600:.0f}h of quiet.\n"
        "Design: docs/backlog/self-healing-spine.md Layer 2."
    )
    title = f"[bounded-heal] {spec.key} capped: {spec.title}"
    if len(title) > 200:
        title = title[:197].rstrip() + "…"
    try:
        with store.tx() as conn:
            ref = store.insert_ref(
                kind="gripe", slug=None, title=title, meta={}, conn=conn
            )
            store.insert_blocks(
                ref.id,
                [BlockInsert(pos=0, text=body, meta={"chunk_kind": "gripe_body"})],
                conn=conn,
            )
            store.add_tag(
                ref.id,
                Tag.closed("STATUS", "open"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
            store.add_tag(
                ref.id, Tag.open("origin:bounded-heal"), set_by="system", conn=conn
            )
        return int(ref.id)
    except Exception:
        log.warning("bounded_heal: cap gripe for %s failed", spec.key, exc_info=True)
        return None


def run_bounded_heal(store: Store, spec: HealSpec, action: Callable[[], bool]) -> str:
    """Run one bounded attempt of ``action`` under ``spec``'s budget.

    Returns ``"healed"`` (action ran and reported success), ``"failed"``
    (action ran and reported failure — the attempt is burned either way),
    ``"cooldown"``, ``"capped"`` (this call latched the cap + filed the
    gripe), ``"latched"`` (already latched), or ``"raced"`` (another
    process moved the state first). Never raises.
    """
    try:
        state, raw = _load_state(store, spec.key)
        age = _age_s(state)
        if age is not None and age > spec.reset_after_s:
            state = {}  # a new incident — fresh budget (raw kept for the CAS)
            age = None
        attempts = int(state.get("attempts", 0) or 0)

        if state.get("latched"):
            return "latched"
        if attempts >= spec.cap:
            new = dict(state)
            new["latched"] = True
            new["last_at"] = datetime.now(UTC).isoformat()
            if not _cas_state(store, spec.key, raw, new):
                return "raced"
            gripe_id = _file_cap_gripe(store, spec, attempts)
            log.warning(
                "bounded_heal: %s capped at %d attempts — latched (gripe %s)",
                spec.key,
                attempts,
                gripe_id,
            )
            return "capped"
        if age is not None and age < spec.base_cooldown_s * (2 ** max(attempts - 1, 0)):
            return "cooldown"

        # Burn the attempt BEFORE acting (see module docstring).
        new = {"attempts": attempts + 1, "last_at": datetime.now(UTC).isoformat()}
        if not _cas_state(store, spec.key, raw, new):
            return "raced"
        ok = False
        try:
            ok = bool(action())
        except Exception:
            log.warning("bounded_heal: action %s raised", spec.key, exc_info=True)
        log.warning(
            "bounded_heal: %s attempt %d/%d -> %s",
            spec.key,
            attempts + 1,
            spec.cap,
            "healed" if ok else "failed",
        )
        return "healed" if ok else "failed"
    except Exception:
        log.warning("bounded_heal: %s errored", spec.key, exc_info=True)
        return "failed"


__all__ = ["HealSpec", "run_bounded_heal"]
