"""halt_heal — a halted cadence gets one big-model repair attempt before a
human is alerted (self-healing-spine, the "heal" dial; spec `me691477`,
safety laws `me691598`).

A cadence that stops itself (today only the gripe triage tick, whose halt
flag is the ``app_state`` row :data:`~precis.workers.gripe_triage._BLOCKED_KEY`)
used to leave a log warning and a board line the installed worker cannot
post: nobody was told, and the halt stood for four days (2026-11-21 ruling).
Now the halt site calls :func:`request_heal` every time it arms or re-arms
the flag, and this module decides, per halt key:

1. **First halt in the 24 h window** — mint ONE ``halt_heal`` job
   (:mod:`precis.workers.job_types.halt_heal`): a FRONTIER session that
   reads the fleet with read verbs only and answers "what stopped it, and is
   that cause demonstrably gone". CODE acts on the answer: ``CLEAR`` empties
   this one halt key (the cadence then probes on its next pass); anything
   else — ``ESCALATE``, no parseable verdict, a session error, any exception
   — raises the alert. The model never writes an alert.
2. **The halt comes back inside the window** (the budget is one attempt per
   24 h, :mod:`precis.workers.bounded_heal`) — escalate at once, carrying the
   earlier attempt's stored diagnosis. A heal that keeps recurring is a
   human's problem. The capped call also makes ``bounded_heal`` file one
   ``[bounded-heal]`` gripe (``HealSpec`` has no way to suppress it).
3. **Escalation** is :func:`escalate`: one critical ``halt_heal`` alert
   (source registered in :mod:`precis.alert_ids`, fingerprint = the halt key)
   plus one push to the ops channel when the alert is new.
4. **One page per episode.** :func:`escalate` writes the marker
   ``halt_heal:alerted:<key>``; while it stands :func:`request_heal` returns
   ``already-escalated`` in every mode (no session, no ``bounded_heal``
   write, no repeat alert write) and a session that answers ``CLEAR`` clears
   nothing (a human was already alerted; its diagnosis rides the open alert).
5. **Overdue session.** A minted job that never reports must not wait for a
   failed probe: :func:`check_overdue`, called by the halted cadence on every
   tick that runs no batch, escalates once when the job has been ``minted``
   for over :data:`_OVERDUE_S` (2 h) and records ``outcome="overdue"``.
6. **Resume** is :func:`note_resumed`: the alert resolves when the cadence
   demonstrably runs again (the halt site calls it after a good batch), never
   on age. The marker keeps a healthy pass at one settings read.

Operator switch, ``app_state`` key ``halt_heal:mode`` (an operator setting,
set it with ``scripts/prod-psql``):

* absent / ``""`` / ``heal`` (default) — session, and a ``CLEAR`` clears the
  halt.
* ``report`` — the session still investigates and its diagnosis rides the
  alert, but a ``CLEAR`` never clears the halt: it escalates.
* ``off`` — no session; escalate at once (a job queued before the switch
  also ends in an escalation without a session).

An unknown value reads as ``report`` (the cautious one: no write by the
supervisor). Rights start narrow: the whole catalogue of what the supervisor
may do is "clear this one halt key" (:data:`HALTS`, first row the gripe
triage halt); a new row is a reviewed code change, widened case by case.

The halt note, log lines and everything the session reads are untrusted
data. Text that came from the model is clipped and passed on marked "model
diagnosis".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.alert_ids import RULES_BY_ID
from precis.alerts import (
    notify_critical_alert,
    raise_alert,
    resolve_alert_by_fingerprint,
)
from precis.utils.secret_scan import mask_secrets
from precis.utils.timeutil import as_utc
from precis.workers.bounded_heal import HealSpec, run_bounded_heal
from precis.workers.gripe_triage import _BLOCKED_KEY as _GRIPE_TRIAGE_BLOCKED_KEY

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

log = logging.getLogger(__name__)

#: The job_type this module mints (``job_types/halt_heal.py``).
JOB_TYPE = "halt_heal"
#: ``alert_ids`` source for the escalation; bound to the registry row so a
#: rename is a ``KeyError`` at import.
ALERT_SOURCE = RULES_BY_ID["halt_heal/{key}"].rule

_MODE_KEY = "halt_heal:mode"
_LAST_PREFIX = "halt_heal:last:"
_ALERTED_PREFIX = "halt_heal:alerted:"
MODES = ("heal", "report", "off")

#: One attempt per 24 h window per halt key (``bounded_heal`` default
#: window); the cooldown is moot at ``cap=1`` but ``HealSpec`` wants one.
_COOLDOWN_S = 3600.0
#: A minted session older than this with no report is overdue (check_overdue).
_OVERDUE_S = 2 * 3600
#: Prompt-side and alert-side length caps for text that is data.
_NOTE_CAP = 600
_MODEL_TEXT_CAP = 400
_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


@dataclass(frozen=True)
class HaltSpec:
    """One registered halt the supervisor may try to clear.

    ``key`` is the ``app_state`` halt flag; ``cadence`` the human name (the
    alert title is ``"<cadence> halted"``); ``what`` one sentence on what the
    cadence does, for the session's prompt; ``reset_hint`` the operator's
    manual reset, shown to the human when the session could not clear it.
    """

    key: str
    cadence: str
    what: str
    reset_hint: str


HALTS: dict[str, HaltSpec] = {
    _GRIPE_TRIAGE_BLOCKED_KEY: HaltSpec(
        key=_GRIPE_TRIAGE_BLOCKED_KEY,
        cadence="gripe triage",
        what=(
            "the 15-minute model pass that triages open gripes; it halts after "
            "three whole-batch failures in a row and probes once every six hours"
        ),
        reset_hint=(
            'scripts/prod-psql "DELETE FROM app_state WHERE key = '
            "'gripe_triage:blocked'\" (the tick resumes on its next pass)"
        ),
    ),
}


def clip(text: object, cap: int = _MODEL_TEXT_CAP) -> str:
    """One masked, whitespace-collapsed line, at most ``cap`` characters —
    for note and model text that goes into an alert or a push."""
    line = " ".join(mask_secrets(str(text)).split())
    return line if len(line) <= cap else line[: cap - 1].rstrip() + "…"


def heal_mode(store: Store) -> str:
    """The ``halt_heal:mode`` operator setting, normalised to one of
    :data:`MODES`; an unknown value reads as ``report``."""
    return _normalize_mode(store.get_setting(_MODE_KEY))


def _normalize_mode(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in ("", "heal"):
        return "heal"
    if raw in MODES:
        return raw
    log.warning("halt_heal: unknown %s=%r — treating as 'report'", _MODE_KEY, raw)
    return "report"


def _lock_setting(conn: Connection, key: str, *, create: bool = False) -> str:
    row = conn.execute(
        "SELECT value FROM app_state WHERE key = %s FOR UPDATE", (key,)
    ).fetchone()
    if row is None and create:
        # Materialize missing mode/marker rows so concurrent upserts contend
        # with the clear. If a writer inserted one first, lock its value.
        row = conn.execute(
            "INSERT INTO app_state (key, value) VALUES (%s, '') "
            "ON CONFLICT (key) DO UPDATE SET value = app_state.value "
            "RETURNING value",
            (key,),
        ).fetchone()
    return str(row[0]) if row else ""


def clear_if_current(
    store: Store, key: str, armed: str, *, job: int, cause: str
) -> tuple[str, str]:
    """Lock the clear preconditions through the write and its outcome record."""
    with store.atomic() as conn:
        if _lock_setting(conn, _ALERTED_PREFIX + key, create=True):
            return "alerted", armed
        mode = _normalize_mode(_lock_setting(conn, _MODE_KEY, create=True))
        if mode != "heal":
            return "mode", mode
        current = _lock_setting(conn, key)
        _lock_setting(conn, _LAST_PREFIX + key)
        last = load_last(store, key)
        if last.get("job") != job or last.get("outcome") != "minted":
            return "superseded", ""
        if not current:
            save_last(store, key, job=job, outcome="already-clear", cause=cause)
            return "already-clear", ""
        if current != armed:
            return "re-armed", current
        store.set_setting(key, "")
        save_last(
            store,
            key,
            job=job,
            outcome="cleared",
            cleared_at=datetime.now(UTC).isoformat(),
            cause=cause,
        )
        return "cleared", ""


def since_of(note: str) -> str:
    """The leading ISO timestamp of a halt note, or ``""``."""
    m = _TS_RE.match(note or "")
    return m.group(0) if m else ""


def load_last(store: Store, key: str) -> dict[str, Any]:
    """The stored record of the last supervisor attempt for ``key``."""
    raw = store.get_setting(_LAST_PREFIX + key)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_last(store: Store, key: str, *, fresh: bool = False, **fields: Any) -> None:
    """Merge ``fields`` into the ``halt_heal:last:<key>`` record; ``fresh``
    starts a new record (a new attempt must not inherit the last one's
    ``cleared_at`` or ``cause``)."""
    rec = {} if fresh else load_last(store, key)
    rec.update(fields)
    store.set_setting(_LAST_PREFIX + key, json.dumps(rec, sort_keys=True))


def escalate(
    store: Store,
    spec: HaltSpec,
    *,
    note: str,
    diagnosis: str,
    tried: str,
    human_action: str = "",
    since: str = "",
    job_ref_id: int | None = None,
    overdue_at: datetime | None = None,
) -> bool:
    """Raise the critical alert for a halt a human has to look at and push it
    to the ops channel when it is new. Returns ``True`` for a new alert or
    when an overdue attempt is recorded. The overdue path rechecks the
    attempt and halt under the same locks used for a clear.

    The detail reads: what halted and since when; the halt note; the model
    diagnosis if any (marked as such — it is model output, not fact); what
    the supervisor tried; the human action; the job handle. Writes the
    ``halt_heal:alerted:<key>`` marker :func:`note_resumed` keys on.
    """
    title = f"{spec.cadence} halted"
    lines = [
        f"{spec.cadence} halted since {since or since_of(note) or 'unknown'}: {spec.what}."
    ]
    lines.append(f"Halt note: {clip(note, _NOTE_CAP)}")
    if diagnosis:
        lines.append(f"Model diagnosis (untrusted model output): {clip(diagnosis)}")
    lines.append(f"Supervisor tried: {clip(tried)}")
    action = clip(human_action) if human_action else ""
    if not action or action.lower() == "none":
        action = spec.reset_hint
    lines.append(f"Human action: {action}")
    if job_ref_id is not None:
        lines.append(f"Job: jo{job_ref_id}")
    detail = mask_secrets("\n".join(lines))
    marker = _ALERTED_PREFIX + spec.key
    with store.atomic() as conn:
        if overdue_at is not None:
            if _lock_setting(conn, marker, create=True) or not _lock_setting(
                conn, spec.key
            ):
                return False
            _lock_setting(conn, _LAST_PREFIX + spec.key)
            last = load_last(store, spec.key)
            if last.get("outcome") != "minted" or last.get("job") != job_ref_id:
                return False
            save_last(
                store, spec.key, outcome="overdue", overdue_at=overdue_at.isoformat()
            )
        store.set_setting(marker, datetime.now(UTC).isoformat())
        _ref, is_new = raise_alert(
            store,
            source=ALERT_SOURCE,
            fingerprint=spec.key,
            title=title,
            detail=detail,
            severity="critical",
        )
    if is_new:
        try:
            notify_critical_alert(store, title, detail, fingerprint=spec.key)
        except Exception:
            log.warning("halt_heal: ops push for %s failed", spec.key, exc_info=True)
    return is_new or overdue_at is not None


def is_alerted(store: Store, key: str) -> bool:
    """A human was already alerted for ``key``'s current episode (the marker
    :func:`escalate` writes and :func:`note_resumed` clears)."""
    return bool(store.get_setting(_ALERTED_PREFIX + key))


def check_overdue(store: Store, key: str, *, when: datetime | None = None) -> bool:
    """A minted supervisor job that never reports must still reach a human.

    Called by the halted cadence on every tick that finds the halt armed and
    runs no batch: one settings read (the ``last`` record). When the record
    says ``minted`` for longer than :data:`_OVERDUE_S` and nobody was alerted
    yet, escalate and record ``outcome="overdue"`` so it fires once. Returns
    ``True`` when it escalated.
    """
    last = load_last(store, key)
    if last.get("outcome") != "minted":
        return False
    spec = HALTS.get(key)
    minted = as_utc(last.get("minted_at"))
    if spec is None or minted is None:
        return False
    now = when or datetime.now(UTC)
    if (now - minted).total_seconds() < _OVERDUE_S:
        return False
    job = f" (jo{last['job']})" if last.get("job") else ""
    return escalate(
        store,
        spec,
        note=store.get_setting(key) or "",
        diagnosis="",
        tried=(
            f"a supervisor session{job} was started at {last.get('minted_at')} "
            f"and has not reported after {_OVERDUE_S // 3600} h"
        ),
        since=str(last.get("halted_at") or ""),
        job_ref_id=int(last["job"]) if last.get("job") else None,
        overdue_at=now,
    )


def note_resumed(store: Store, key: str) -> bool:
    """The halted cadence ran a good batch: resolve its open alert.

    Cheap when nothing was ever escalated (one settings read on the marker
    :func:`escalate` writes). Returns ``True`` when a marker was cleared.
    """
    marker = _ALERTED_PREFIX + key
    if not store.get_setting(marker):
        return False
    resolved = resolve_alert_by_fingerprint(
        store, source=ALERT_SOURCE, fingerprint=key, resolved_by="halt_heal:resumed"
    )
    store.set_setting(marker, "")
    log.warning("halt_heal: %s resumed — alert resolved=%s", key, resolved)
    return True


def _tried(last: dict[str, Any], outcome: str) -> str:
    """What the supervisor already did in this window, for the alert."""
    job = f" (jo{last['job']})" if last.get("job") else ""
    if outcome == "failed":
        return "could not start a supervisor session (the job mint failed)"
    state = last.get("outcome")
    if state == "cleared" and last.get("cleared_at"):
        return f"cleared the halt once at {last['cleared_at']}{job}; it halted again"
    if state == "already-clear":
        return (
            f"a supervisor session{job} found the halt already cleared (a probe or "
            "an operator got there first; the supervisor cleared nothing); it "
            "halted again"
        )
    if state == "overdue":
        return (
            f"a supervisor session{job} did not report after {_OVERDUE_S // 3600} h "
            "and was escalated"
        )
    if state == "escalated":
        return f"a supervisor session{job} could not clear it and escalated"
    if state == "failed":
        return f"a supervisor session{job} failed: {clip(last.get('reason') or '')}"
    if state == "minted":
        return (
            f"a supervisor session{job} was started at {last.get('minted_at', '?')} "
            "and has not reported yet"
        )
    return "the supervisor's attempt budget for this 24 h window is spent"


def _mint(store: Store, spec: HaltSpec, note: str, now: datetime) -> bool:
    """The bounded-heal action: mint ONE ``halt_heal`` job. The halt note
    rides the job params as data."""
    from precis.workers._mint import mint_job_once

    idem = f"halt-heal:{spec.key}:{now:%Y-%m-%d}T{now:%H}"
    halted_at = since_of(note) or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with store.atomic() as conn:
        job_id, reused = mint_job_once(
            store,
            conn=conn,
            job_type=JOB_TYPE,
            executor="claude_inproc",
            title=f"halt_heal ({spec.key})",
            params={"key": spec.key, "note": note[:_NOTE_CAP], "halted_at": halted_at},
            idem_key=idem,
            prio=5,
        )
        if reused:
            return False
        save_last(
            store,
            spec.key,
            fresh=True,
            job=job_id,
            outcome="minted",
            minted_at=now.isoformat(),
            halted_at=halted_at,
        )
    return True


def request_heal(
    store: Store, key: str, note: str, *, when: datetime | None = None
) -> str:
    """The halt site's one call: ``key`` was just armed (or re-armed) with
    ``note``. Returns a short outcome for logs and tests:

    ``minted`` (a supervisor job was queued, no alert), ``cooldown`` /
    ``raced`` (nothing to do), ``unregistered``, ``already-escalated`` (a
    human was already alerted this episode), or ``escalated:<why>``
    (``off``, ``capped``, ``latched``, ``failed``) with the alert raised.
    """
    spec = HALTS.get(key)
    if spec is None:
        log.warning("halt_heal: no registered halt for %r — nothing to do", key)
        return "unregistered"
    if is_alerted(store, key):
        # One page per episode, in every mode: a human already knows, so no
        # new session, no bounded_heal write (and no cap gripe), no repeat
        # alert write. The episode ends in note_resumed.
        return "already-escalated"
    now = when or datetime.now(UTC)
    if heal_mode(store) == "off":
        escalate(
            store,
            spec,
            note=note,
            diagnosis="",
            tried=f"nothing: the supervisor is switched off ({_MODE_KEY}=off)",
        )
        return "escalated:off"
    heal = HealSpec(
        key=f"halt-heal:{key}",
        cap=1,
        base_cooldown_s=_COOLDOWN_S,
        title=f"{spec.cadence} halted",
        detail=spec.what,
    )
    outcome = run_bounded_heal(store, heal, lambda: _mint(store, spec, note, now))
    if outcome == "healed":
        return "minted"
    if outcome in ("cooldown", "raced"):
        return outcome
    # failed (the mint did not happen), capped, latched: the budget for this
    # window is used up — a human, with whatever the earlier attempt learned.
    last = load_last(store, key)
    escalate(
        store,
        spec,
        note=note,
        diagnosis=str(last.get("cause") or ""),
        tried=_tried(last, outcome),
        human_action=str(last.get("human_action") or ""),
        since=str(last.get("halted_at") or ""),
        job_ref_id=int(last["job"]) if last.get("job") else None,
    )
    return f"escalated:{outcome}"


__all__ = [
    "ALERT_SOURCE",
    "HALTS",
    "JOB_TYPE",
    "MODES",
    "HaltSpec",
    "check_overdue",
    "clear_if_current",
    "clip",
    "escalate",
    "heal_mode",
    "is_alerted",
    "load_last",
    "note_resumed",
    "request_heal",
    "save_last",
    "since_of",
]
