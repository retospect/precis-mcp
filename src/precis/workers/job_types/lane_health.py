"""``lane_health`` job_type — probe every enabled lane's credential.

gr588221 — the 2026-11-08 03:13Z incident: claude-sub's ``claude -p``
answered "Not logged in" and every session on the lane failed until a
human flipped ``enabled`` by hand. The hourly ``lane_health`` scheduler
cadence mints one of these jobs (:func:`mint_health_job`); each enabled
account on a launchable adapter gets a cheap auth probe — the claude
ping is a single one-word ``claude -p`` turn (``--max-turns 1``, a 20 s
timeout) and every other probe is a read-only status call or one HTTPS
GET (gr592265).

The probe runs WHERE the sessions run (review B1): a detached
submit/poll pair under ``ssh_node`` (the ``ci_red_digest`` shape), so
the child inherits the slice worker's HOME, PATH and ``PRECIS_*_BIN``.
``submit`` launches ``python -m … lane_health probe <adapter>…`` —
the ``_main`` entry prints one ``probe: adapter=<a> outcome=<o>
key=<k>`` line per adapter — and ``poll`` applies the outcomes through
:func:`run_lane_health_pass`. The ``claude_p`` line adds
``token=present|absent`` and the token's class (``oauth-setup`` for an
``sk-ant-oat`` setup token, else ``other`` — the log never carries the
value or more than the 7-char vendor prefix), the CLI's raw
``read=outcome:key``, and a ``result:`` line with its ``result``
wording verbatim (gr601062): an ``auth`` read on an absent token is a
host/vault miss, not the lane's verdict, so it applies
``unprobed``/``vault-miss`` and the summary attributes ``auth (token
absent)`` vs ``auth (token rejected)``. Host affinity is the registry
``placement`` row (``repo_checkout``, ``claude_bin``, ``git`` — the
same tokens a slice needs), so no worker without the lane CLIs can
claim the job:

* red (``auth``/``outage``) on a healthy lane parks it —
  ``parked_reason = "<outcome>:<key>"`` — held out of dispatch exactly
  like ``enabled=False``;
* green on a parked lane unparks it, resolves its ``lane-parked:<name>``
  alert and notes the event — the lane self-heals, no human toggle.
  Review S2: a probe only verifies the class it can see, so a
  phrase/``outage`` park clears on one green while a structural
  ``auth:cli-missing``/``auth:http-*`` park needs two consecutive
  greens (the first only stamps ``ok``);
* red on a parked lane only moves the health stamp (no second park —
  the first-sighting ``parked_at`` keeps the >6h alert clock);
* ``unprobed`` (a CLI absent on the probing host, a probe that timed
  out or could not judge — and a ``claude_p`` ``auth`` read whose env
  held no token, ``vault-miss``) moves the stamp only — never parks,
  never unparks;
* a lane still parked past :data:`PARKED_ALERT_AFTER` raises one
  deduped ``lane-parked:<name>`` alert.

``enabled`` is the human switch and nothing here writes it. Probes
return the class key, never raw output: no credential value, session
text or log line reaches logs, meta or chunks. The child binds no
store, so ``submit`` resolves the vault credentials on the worker —
read-only, never a login/refresh/update (review R2-1/R2-5) — and hands
them down through the child's env alone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.alert_ids import RULES_BY_ID
from precis.alerts import raise_alert, resolve_alert_by_fingerprint
from precis.plans import lanes as _lanes
from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types import release_cut as _rc
from precis.workers.job_types import run_slice as _rs

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

NAME = "lane_health"

ALERT_SOURCE = RULES_BY_ID["lane_health/lane-parked:{lane}"].rule

#: A lane still parked past this raises the lane-parked alert — long
#: enough that a transient red doesn't page, short enough that a dead
#: login can't hold a lane out of dispatch unnoticed for a day.
PARKED_ALERT_AFTER = timedelta(hours=6)

#: The ``lane_health`` probe's per-call subprocess budget (the claude
#: ping); the devin auth-status helper carries its own 15 s.
_PROBE_TIMEOUT_S = 20

#: Serial CLI pings plus the kimi endpoint check — well under the
#: ssh_node lease this wall caps.
_WALL_S = 300.0

#: A pending lane_health job older than this is wedged, not queued —
#: same non-terminal semantics and fresh bound as secret_check's.
_PENDING_FRESH_S = 30 * 60

#: pg_advisory_xact_lock key for mint_health_job's check-then-insert.
_MINT_LOCK_KEY = 0x6C616E655F6874  # "lane_ht"

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: The detached child's output contract — one classified line per
#: adapter, the class key only (``-`` when the outcome carries none).
#: A ``claude_p`` line adds ``token=``/``class=``/``read=`` fields: the
#: token's presence and class label in the env the CLI was handed, and
#: the CLI's raw ``outcome:key`` before the vault-miss rewrite.
_PROBE_LINE_RE = re.compile(
    r"^probe: adapter=(\S+) outcome=(\S+) key=(\S+)"
    r"(?: token=(\S+) class=(\S+)(?: read=(\S+))?)?\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class _ProbeReading:
    """One probe's report: the APPLIED ``outcome``/``key`` plus, for the
    claude ping, the attribution extras the child's log line and the job
    summary carry — ``token`` (``present``|``absent`` in the env the CLI
    was handed), ``token_class`` (``oauth-setup`` for an ``sk-ant-oat``
    setup token, ``other``, ``-`` when absent — never the value or more
    than the 7-char vendor prefix), ``read`` (the CLI's raw
    ``outcome:key`` before the vault-miss rewrite) and ``result`` (its
    ``result`` wording verbatim, whitespace-collapsed — no secret)."""

    outcome: str
    key: str | None
    token: str | None = None
    token_class: str | None = None
    read: str | None = None
    result: str | None = None


#: The ``claude setup-token`` vendor prefix an ``oauth-setup`` class
#: names — the log carries the class label, never the prefix itself.
_OAUTH_SETUP_PREFIX = "sk-ant-oat"
_OAUTH_SETUP_CLASS = "oauth-setup"

#: The ``result:`` line's cap — the auth verdict's phrases are far
#: shorter; a longer answer is truncated, never paraphrased.
_PROBE_RESULT_CAP = 240


def _fingerprint(name: str) -> str:
    return f"lane-parked:{name}"


def _claude_ping_env() -> dict[str, str]:
    """The ping's env: the allowlisted host surface plus the OAuth token
    :func:`submit` resolved on the worker — a read-only vault value,
    never a login or a refresh (review R2-5). The shared
    ``PRECIS_MCP_TOKEN`` stays out — the ping needs no precis reach —
    and ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`` keeps the probe
    from triggering the CLI's self-update traffic (``scripts/claude-local``
    arms the same switch)."""
    from precis.utils.claude_oauth import ensure_oauth_token

    env = {
        k: os.environ[k]
        for k in _rs.ENV_ALLOWLIST
        if os.environ.get(k) and k != "PRECIS_MCP_TOKEN"
    }
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    ensure_oauth_token(env)
    return env


def _probe_claude_p(env: dict[str, str] | None = None) -> _ProbeReading:
    """One ``claude -p`` ping under :func:`_claude_ping_env` (the vault
    OAuth the worker handed down), classified by the same outage
    classifier the poll applies. Only a positive ``auth`` classification
    may park the lane (COORDINATOR ruling, review R2-2): a probe-seen
    ``quota``/``rate`` window, a crash, an unparseable answer or any
    other ``is_error`` — and everything the probe cannot judge, a
    missing binary or a timed-out ping (review S3) — is ``unprobed``.

    gr601062: the reading carries the token's presence and class label
    in ``env`` so one log line attributes an ``auth`` verdict — and an
    ``auth`` read on an env with NO token applies ``unprobed``/
    ``vault-miss`` (a host/vault condition the ping cannot judge), never
    ``auth:not-logged-in``'s park. ``env`` defaults to
    :func:`_claude_ping_env`; the ``secret_status`` probe group passes
    the token it resolved so /secrets runs the same ping."""
    from precis.utils import claude_oauth
    from precis.workers.job_types._slice_adapters import (
        claude_bin,
        parse_claude_result,
    )

    env = _claude_ping_env() if env is None else env
    token = env.get(claude_oauth.ENV_VAR) or ""
    token_state = "present" if token else "absent"
    token_class = (
        _OAUTH_SETUP_CLASS
        if token.startswith(_OAUTH_SETUP_PREFIX)
        else ("other" if token else "-")
    )
    argv = [
        claude_bin(),
        "-p",
        "ping",
        "--output-format",
        "json",
        "--max-turns",
        "1",
    ]
    try:
        proc = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _ProbeReading(
            "unprobed", "timeout", token=token_state, token_class=token_class
        )
    except OSError:
        return _ProbeReading(
            "unprobed", "cli-missing", token=token_state, token_class=token_class
        )
    result, is_error = parse_claude_result(proc.stdout)
    log_text = proc.stdout if proc.stdout.strip() else proc.stderr
    outage = _rs._lane_outage(
        result, log_text, adapter="claude_p", exit_code=proc.returncode
    )
    raw: tuple[str, str | None]
    if outage is not None:
        cls, _retry, raw_key = outage
        raw = ("auth", raw_key) if cls == "auth" else ("unprobed", cls)
    elif is_error:
        raw = ("unprobed", "error")
    else:
        raw = ("ok", None)
    outcome, key = raw
    if outcome == "auth" and not token:
        # gr601062: the CLI's auth read with no token in its env is the
        # host's vault miss, not a lane verdict — a probe that cannot
        # judge must not park (review R2).
        outcome, key = "unprobed", "vault-miss"
    return _ProbeReading(
        outcome,
        key,
        token=token_state,
        token_class=token_class,
        read=f"{raw[0]}:{raw[1] or '-'}",
        result=(" ".join(result.split())[:_PROBE_RESULT_CAP] if result else None),
    )


def _probe_devin_tree() -> tuple[str, str | None]:
    """``devin auth status`` — the same binary and login gates the
    submit preflight applies. Only the definitive not-logged-in wording
    is ``auth``; a missing binary, a timeout or an unreadable answer is
    ``unprobed`` — the probe could not judge (review S3)."""
    binary = _rs.devin_bin()
    if not _rs._devin_bin_ok(binary):
        return "unprobed", "cli-missing"
    status = _rs._devin_auth_status(binary)
    if status.returncode == 124:
        return "unprobed", "timeout"
    out = f"{status.stdout}\n{status.stderr}".lower()
    if "not logged in" in out or "logged out" in out:
        return "auth", "not-logged-in"
    if status.returncode == 0 and "logged in" in out:
        return "ok", None
    return "unprobed", "unverifiable"


def _probe_codex_exec() -> tuple[str, str | None]:
    """The codex lane's ``codex login status``, run here for the
    tri-state: ``codex_login_present`` flattens a timeout/error to
    False, which a probe must not read as a logged-out verdict (review
    S3). ``Logged in … ChatGPT`` is green — the subscription transport,
    matching the rung gate's rule; the definitive not-logged-in wording
    is ``auth``; anything else is ``unprobed``."""
    from precis.utils import codex_exec

    if not codex_exec.codex_bin_present():
        return "unprobed", "cli-missing"
    try:
        res = subprocess.run(
            [codex_exec.resolve_codex_binary(), "login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unprobed", "unverifiable"
    if res.returncode == 0 and "chatgpt" in (res.stdout or "").lower():
        return "ok", None
    out = f"{res.stdout}\n{res.stderr}".lower()
    if "not logged in" in out or "logged out" in out:
        return "auth", "not-logged-in"
    return "unprobed", "unverifiable"


def _probe_kimi_tree() -> tuple[str, str | None]:
    """The kimi membership endpoint's auth check — the same probe the
    /secrets weekly check runs, one HTTPS GET with the key; the CLI
    binary is never launched (it has no read-only mode a self-update
    guard could pin — review R2-5). A MISSING key is ``unprobed``: the
    host's secret state, not a positive auth failure the lane reported
    (review R2-1). A rejected key parks; anything unverifiable
    (transport, 5xx, refused base) is ``unprobed``: a probe that cannot
    judge must not park a lane."""
    from precis import secret_status, secrets
    from precis.utils.llm import kimi

    key = secrets.get_secret(kimi.API_KEY_REF)
    if not key:
        return "unprobed", "key-missing"

    async def _check() -> Any:
        async with secret_status._probe_client() as client:
            return await secret_status._probe_kimi(client, {"KIMI_CODE_API_KEY": key})

    try:
        res = asyncio.run(_check())
    except Exception:
        log.warning("lane_health: kimi probe raised", exc_info=True)
        return "unprobed", "unverifiable"
    if res.state == "ok":
        return "ok", None
    if res.state == "bad":
        return "auth", "key-rejected"
    return "unprobed", "unverifiable"


#: A probe reports a bare ``(outcome, key)`` or a full
#: :class:`_ProbeReading` (the claude ping's token attribution).
_PROBES: dict[str, Callable[[], tuple[str, str | None] | _ProbeReading]] = {
    "claude_p": _probe_claude_p,
    "devin_tree": _probe_devin_tree,
    "codex_exec": _probe_codex_exec,
    "kimi_tree": _probe_kimi_tree,
}


def _probe_reading(adapter: str) -> _ProbeReading:
    """One probe's full :class:`_ProbeReading` — the applied verdict plus
    the claude ping's attribution extras; a bare ``(outcome, key)``
    probe or a probe that cannot run fills the extras with ``None``."""
    probe = _PROBES.get(adapter)
    if probe is None:
        return _ProbeReading("unprobed", "no-probe")
    try:
        out = probe()
    except Exception:
        log.warning("lane_health: %s probe raised", adapter, exc_info=True)
        return _ProbeReading("unprobed", "probe-error")
    if isinstance(out, _ProbeReading):
        return out
    return _ProbeReading(out[0], out[1])


def _probe_account(adapter: str) -> tuple[str, str | None]:
    """``(outcome, key)`` for one lane's adapter — ``outcome`` is
    ``ok`` | ``auth`` | ``outage`` | ``unprobed``; ``key`` the short
    mark, never raw output."""
    reading = _probe_reading(adapter)
    return reading.outcome, reading.key


def _unparkable(parked_reason: str | None, health_outcome: str | None) -> bool:
    """Whether ONE green probe may clear this park (review S2).

    The probe only verifies the class it can see: a session-stamped
    ``auth:<phrase>`` or a probe-stamped ``outage:*`` park is the class
    the ping answers, so one green clears it. A structural
    ``auth:cli-missing``/``auth:http-*`` park is the process's or the
    API's report — possibly stamped by a DIFFERENT host's session — so
    the first green only stamps ``ok`` and the second consecutive green
    unparks."""
    if parked_reason is None:
        return True
    cls, _sep, key = parked_reason.partition(":")
    if cls == "outage":
        return True
    if cls == "auth" and key != "cli-missing" and not key.startswith("http-"):
        return True
    return health_outcome == "ok"


def run_lane_health_pass(
    store: Store,
    *,
    now: datetime | None = None,
    note: Callable[[str], None] | None = None,
    probe: Callable[[str], tuple[str, str | None]] | None = None,
) -> dict[str, str]:
    """One probe pass over the enabled lanes; ``{lane: outcome}``.

    Applies the park semantics to the account rows (never ``enabled``):
    green unparks a parked lane (:func:`_unparkable` decides how many
    greens the park class needs) and resolves its alert, red parks a
    healthy one, everything else only moves the health stamp. A lane
    still parked past :data:`PARKED_ALERT_AFTER` raises the deduped
    ``lane-parked:<name>`` alert. ``note`` receives the one job_event
    line an unpark earns (the dispatch wires it to the job's chunks).
    ``probe`` defaults to :func:`_probe_account` — the ssh_node poll
    hands it the detached child's parsed lines instead.
    """
    from precis.utils.llm import accounts

    probe = probe or _probe_account
    now = now or datetime.now(UTC)
    outcome_of: dict[str, str] = {}
    for a in accounts.list_accounts(store):
        if not a.enabled or a.adapter not in _lanes.LAUNCHABLE_ADAPTERS:
            continue
        outcome, key = probe(a.adapter)
        outcome_of[a.name] = outcome
        parked = a.parked_reason is not None
        if outcome == "ok":
            if parked and _unparkable(a.parked_reason, a.health_outcome):
                _lanes.unpark_lane(store, a.name, now)
                resolve_alert_by_fingerprint(
                    store,
                    source=ALERT_SOURCE,
                    fingerprint=_fingerprint(a.name),
                    resolved_by="lane_health",
                )
                if note is not None:
                    note(
                        f"lane_health: lane {a.name} probe green — "
                        f"unparked (was {a.parked_reason})"
                    )
            else:
                # A first green under a structural park: stamp ``ok``
                # but keep the lane parked — the next green clears it.
                _lanes.record_lane_health(store, a.name, "ok", now)
        elif outcome == "unprobed":
            _lanes.record_lane_health(store, a.name, "unprobed", now)
        elif parked:
            _lanes.record_lane_health(store, a.name, outcome, now)
        else:
            _lanes.park_lane(store, a.name, f"{outcome}:{key or 'unclassified'}", now)
        if (
            parked
            and outcome != "ok"
            and a.parked_at is not None
            and a.parked_at <= now - PARKED_ALERT_AFTER
        ):
            raise_alert(
                store,
                source=ALERT_SOURCE,
                fingerprint=_fingerprint(a.name),
                title=f"[lane_health] {a.name}: lane parked on failed health probe",
                detail=(
                    f"Parked as {a.parked_reason!r} since "
                    f"{a.parked_at:%Y-%m-%dT%H:%M:%SZ}; the latest probe "
                    f"read {outcome}. Re-login or repair the lane's "
                    "credential on its host — a green probe unparks the "
                    "lane and resolves this alert. `enabled` stays the "
                    "human switch; the probe never writes it."
                ),
            )
    return outcome_of


def _pending_fresh_health_job(conn: Any) -> bool:
    """A live ``lane_health`` job pending for less than the fresh bound —
    wedged pending rows stop counting so a dead job can't block mints."""
    row = conn.execute(
        """
        SELECT 1 FROM refs r
         WHERE r.kind = 'job' AND r.retired_at IS NULL
           AND r.meta->>'job_type' = %s
           AND r.created_at > now() - make_interval(secs => %s)
           AND COALESCE(
                 (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                 'open'
               ) NOT IN ('succeeded', 'failed', 'cancelled')
         LIMIT 1
        """,
        (NAME, _PENDING_FRESH_S),
    ).fetchone()
    return row is not None


def mint_health_job(store: Store, *, origin: str, title: str) -> int | None:
    """Queue one ``lane_health`` job unless one is already pending —
    the single mint path for the scheduler cadence. Returns the minted
    job's id, or ``None`` when a fresh pending job of the type exists.
    The check and the insert run under one advisory xact lock."""
    from precis.workers._mint import mint_background_job

    prio = 2 if origin == "human" else 8
    with store.pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MINT_LOCK_KEY,))
        if _pending_fresh_health_job(conn):
            conn.commit()
            return None
        ref = mint_background_job(
            store,
            conn=conn,
            job_type=NAME,
            executor="ssh_node",
            title=title,
            prio=prio,
            origin=origin,
        )
        conn.commit()
    return int(ref.id)


def wall_seconds() -> float:
    """Type cap for ssh_node's lease and kill deadline: serial CLI
    pings plus the endpoint check are seconds, not minutes."""
    return _WALL_S


def _probe_adapters(store: Store) -> list[str]:
    """The enabled, launchable adapters a probe pass covers — sorted so
    the child's argv is stable."""
    from precis.utils.llm import accounts

    return sorted(
        {
            a.adapter
            for a in accounts.list_accounts(store)
            if a.enabled and a.adapter in _lanes.LAUNCHABLE_ADAPTERS
        }
    )


def _probe_child_env(store: Store, adapters: list[str]) -> dict[str, str]:
    """The detached probe child's env: the worker's own (its HOME/PATH/
    logins ARE the probe surface — review B1) plus ONLY the credentials
    of the adapters the child actually probes — it binds no store
    (:func:`~precis.secrets.adopt_process_store` popped the DSN), so
    ``submit`` resolves them on the worker. The OAuth token resolves
    only for a ``claude_p`` pass and the kimi key only for a
    ``kimi_tree`` one: a devin/codex-only pass carries neither
    (gr592265). Both reads are read-only (review R2-5):
    :func:`claude_oauth.ensure_oauth_token` reveals the vault token, never mints or
    refreshes; the kimi key resolves against the worker's bound store
    the way :func:`run_slice._kimi_launch_env` does. The values go into
    the child's env on this host and nowhere else — never the handle,
    meta, a chunk or the log (review R2-1). Because the base is the
    worker's whole ``os.environ``, each credential is also REMOVED when
    its adapter is absent — an inherited ``CLAUDE_CODE_OAUTH_TOKEN`` or
    ``KIMI_CODE_API_KEY`` must not ride a devin/codex-only pass down to
    the child (td592904)."""
    from precis import secrets
    from precis.utils import claude_oauth
    from precis.utils.llm import kimi

    env = dict(os.environ)
    if "claude_p" in adapters:
        claude_oauth.ensure_oauth_token(env)
    else:
        env.pop(claude_oauth.ENV_VAR, None)
    if "kimi_tree" in adapters:
        try:
            key = secrets.get_secret(kimi.API_KEY_REF, store=store)
        except Exception:
            log.warning("%s: %s unreadable at submit", NAME, kimi.API_KEY_REF)
            key = None
        if key:
            env[kimi.API_KEY_REF] = key
    else:
        env.pop(kimi.API_KEY_REF, None)
    return env


def submit(ctx: Any, spec: Any) -> Any:
    """Detached submit half: launch ``python -m … lane_health probe``
    under :func:`_probe_child_env` (the worker's env plus only the
    credentials of the adapters probed). Files land under
    ``ops_root()``'s ``lane-health/`` so a reaped tree keeps the poll's
    reads."""
    del spec
    adapters = _probe_adapters(ctx.store)
    if not adapters:
        ctx.append_chunk("job_event", f"{NAME}: no enabled launchable lane to probe")
        return {"no_probe": True}
    run_dir = _rs.ops_root() / "lane-health"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"job-{ctx.ref_id}.log"
    exit_file = run_dir / f"job-{ctx.ref_id}.exit"
    exit_file.unlink(missing_ok=True)
    try:
        pid = _rs._launch(
            [sys.executable, "-m", __name__, "probe", *adapters],
            cwd=_rs.ops_root(),
            stdin=None,
            log=log_path,
            exit_file=exit_file,
            env=_probe_child_env(ctx.store, adapters),
        )
    except Exception as exc:
        log.warning("%s: probe launch failed", NAME, exc_info=True)
        ctx.record_failure(f"{NAME}: probe launch failed: {exc}")
        return None
    ctx.append_chunk(
        "job_event",
        f"{NAME}: launched lane probe pid {pid} for {', '.join(adapters)}",
    )
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "log": str(log_path),
        "exit_file": str(exit_file),
        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "adapters": adapters,
    }
    identity = _rs._process_identity(pid)
    if identity is not None:
        handle["boot_id"], handle["start_time"] = identity
    return handle


def _probe_lines(log_text: str) -> dict[str, _ProbeReading]:
    """``{adapter: reading}`` from the child's ``probe:`` lines — ``-``
    maps back to no key; a ``claude_p`` line's ``token=``/``class=``/
    ``read=`` fields ride along for the summary's attribution."""
    return {
        m.group(1): _ProbeReading(
            m.group(2),
            None if m.group(3) == "-" else m.group(3),
            token=m.group(4),
            token_class=m.group(5),
            read=m.group(6),
        )
        for m in _PROBE_LINE_RE.finditer(log_text)
    }


def _summary_outcome(outcome: str, reading: _ProbeReading | None) -> str:
    """The job-summary token for one lane: a ``claude_p`` pass whose CLI
    read ``auth`` is attributed — ``auth (token absent)`` when the env
    handed the ping no token (applied as ``unprobed``/``vault-miss``) or
    ``auth (token rejected)`` when it reached the CLI and failed — so
    one read of the card says which side to fix (gr601062)."""
    if reading is None:
        return outcome
    raw = (reading.read or "").partition(":")[0] or reading.outcome
    if raw != "auth":
        return outcome
    if reading.token == "absent":
        return "auth (token absent)"
    return "auth (token rejected)"


def poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half: apply the child's classified lines through
    :func:`run_lane_health_pass`. An adapter with no line reads
    ``unprobed/no-result`` — the probe could not judge it; a non-zero
    probe exit fails the job and applies nothing."""
    if isinstance(handle, dict) and handle.get("no_probe"):
        ctx.set_meta(status={})
        ctx.append_chunk("job_summary", f"{NAME}: no enabled lanes")
        ctx.set_status("succeeded")
        return True
    done = _rc.finish_run(ctx, handle, NAME)
    if done is None:
        return False
    exit_code, log_text = done
    if exit_code != 0:
        _rc.fail_with_tail(ctx, f"{NAME}: probe process exit {exit_code}", log_text)
        return True
    readings = _probe_lines(log_text)

    def applied(adapter: str) -> tuple[str, str | None]:
        r = readings.get(adapter)
        return (r.outcome, r.key) if r is not None else ("unprobed", "no-result")

    status = run_lane_health_pass(
        ctx.store,
        probe=applied,
        note=lambda line: ctx.append_chunk("job_event", line),
    )
    ctx.set_meta(status=status)
    from precis.utils.llm import accounts

    adapter_of = {a.name: a.adapter for a in accounts.list_accounts(ctx.store)}
    ctx.append_chunk(
        "job_summary",
        f"{NAME}: "
        + (
            ", ".join(
                f"{name} {_summary_outcome(outcome, readings.get(adapter_of.get(name, '')))}"
                for name, outcome in sorted(status.items())
            )
            or "no enabled lanes"
        ),
    )
    ctx.set_status("succeeded")
    return True


kill = _rc.kill


def _main(argv: list[str]) -> int:
    """The detached probe child: ``python -m … lane_health probe
    <adapter>…`` prints one classified line per adapter on stdout and
    exits 0 — the park/unpark decision stays with the claiming job's
    poll, the child only probes. A ``claude_p`` line adds the token's
    presence/class label and the CLI's raw read, then a ``result:`` line
    with the ``result`` wording verbatim (gr601062: one read attributes
    an ``auth`` verdict to the token or to the plumbing)."""
    if argv[:1] != ["probe"] or not argv[1:]:
        print(
            f"usage: python -m {__name__} probe <adapter>…",
            file=sys.stderr,
        )
        return 2
    for adapter in argv[1:]:
        r = _probe_reading(adapter)
        line = f"probe: adapter={adapter} outcome={r.outcome} key={r.key or '-'}"
        if r.token is not None:
            line += f" token={r.token} class={r.token_class or '-'}"
            if r.read:
                line += f" read={r.read}"
        print(line, flush=True)
        if r.result:
            print(f"result: adapter={adapter} text={r.result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"ssh_node"}),
    requires=frozenset(),  # the lane CLIs are the placement's, checked at claim
    description=(
        "Probe every enabled lane's credential on the slice host "
        "(claude ping, devin/codex login status, kimi key check): red "
        "parks the lane out of dispatch, green unparks it; enabled "
        "stays the human switch."
    ),
    submit=submit,
    poll=poll,
    kill=kill,
    wall_seconds=wall_seconds,
    # A queued second pass adds nothing — the pending run rewrites the
    # same health stamps. (Belt for dispatch-pass mints; mint_health_job
    # already refuses its own duplicates.)
    dedupe_pending=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "ALERT_SOURCE",
    "NAME",
    "PARKED_ALERT_AFTER",
    "SPEC",
    "kill",
    "load",
    "mint_health_job",
    "poll",
    "run_lane_health_pass",
    "submit",
    "wall_seconds",
]
