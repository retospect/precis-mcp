"""Detect a shared session MCP that is up, listening and wedged.

The shared server's recovery covers a process that *stops*: the checkout
watchdog exits cleanly, a crash exits, and something starts a new one. The
third state — running, bound, useless — is what ``install_watchdog``'s
docstring says a swapped install turns into ("desyncs at the protocol level
... and then hangs until the client's 1800 s idle timeout"). Nothing exits,
so nothing restarts it, and on a shared server every session hangs at once
(docs/backlog/mcp-shared-server-liveness.md).

This module is the detector, not the restarter. It is meant to run inside
the process supervisor that owns the listening socket and runs
``precis serve`` as a child (gr459481): on a wedge verdict the supervisor
kills the child and starts a fresh one on the same socket, so no container
restart is involved. Splitting it this way keeps the decision testable
without processes or sockets:

- :func:`probe` — one honest round-trip over the real MCP path:
  ``initialize`` then ``tools/list``. A bound port or a ``/healthz`` proves
  nothing here (``embedder_wedged_warming``: healthz OK, model never warm).
  ``tools/list`` does not enter tool dispatch, so a server busy with a burst
  of slow tool calls still answers it — load is not mistaken for a wedge.
- :class:`WedgeDetector` — pure state: consecutive failures, a startup grace
  and a cooldown between kills. Feed it probe outcomes, it says when to act.
- :func:`run_liveness_loop` — the thread body a supervisor starts.

Tolerances, and where the numbers come from:

- ``timeout_s`` 15 s per probe: a 12-session burst finished in 4.78 s wall
  (session-mcp-http-server.md), so 15 s is three times the measured worst
  load and still far below the client's 1800 s hang.
- ``failures_to_wedge`` 3 at ``interval_s`` 20 s: one slow probe is noise;
  three in a row is about a minute of nothing answering.
- ``startup_grace_s`` 60 s: a respawned child takes ~13 s from exit to
  serving (measured on the 8766 rig, 2026-10-01T12:05Z), and under the
  supervisor connections queue during that gap rather than being refused.
  Failures inside the grace window are not counted.
- ``kill_cooldown_s`` 300 s, doubling: a server that wedges again right
  after a restart is not fixed by restarting faster, and a restart loop
  under load is worse than a briefly slow server.

Every wedge verdict is visible: :func:`record_wedge_kill` writes the same
exit breadcrumb ``precis-status`` already shows after a watchdog exit, with
the probe failures and how many kills happened in the last hour.

Imports: standard library and the venv's third-party packages only, all at
module top, and **no** ``precis.*`` — not even lazily. The supervisor that
runs this is PID 1 for the container's life while every child start wipes
and re-copies the source tree ``precis`` resolves to, so a ``precis``
import from the supervisor can load the wrong generation or a half-copied
one. The supervisor runs this file as a sibling copied to a stable path.
That is why the breadcrumb writer below duplicates
``install_watchdog._write_exit_breadcrumb``'s path and shape rather than
calling it; a test pins the two together.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LivenessPolicy:
    """Knobs for :class:`WedgeDetector`; module docstring says why each."""

    interval_s: float = 20.0
    timeout_s: float = 15.0
    failures_to_wedge: int = 3
    startup_grace_s: float = 60.0
    kill_cooldown_s: float = 300.0
    kill_cooldown_cap_s: float = 3600.0


@dataclass
class WedgeDetector:
    """Decides, from a stream of probe outcomes, when a child is wedged.

    ``observe`` returns ``True`` exactly when the caller should kill the
    child. The caller then reports the new child's start through
    ``child_started`` so the grace window and failure count reset.
    """

    policy: LivenessPolicy = field(default_factory=LivenessPolicy)
    child_started_at: float | None = None
    failures: list[str] = field(default_factory=list)
    kills: list[float] = field(default_factory=list)

    def child_started(self, now: float) -> None:
        self.child_started_at = now
        self.failures.clear()

    def _cooldown_s(self) -> float:
        # Doubles with each kill inside the last hour, so a server that
        # keeps wedging is restarted less and less often rather than looped.
        recent = len(self.recent_kills(self.kills[-1])) if self.kills else 0
        cooldown = self.policy.kill_cooldown_s * 2 ** max(recent - 1, 0)
        return min(cooldown, self.policy.kill_cooldown_cap_s)

    def recent_kills(self, now: float, window_s: float = 3600.0) -> list[float]:
        return [t for t in self.kills if now - t <= window_s]

    def observe(self, failure: str | None, now: float) -> bool:
        """Record one probe outcome (``None`` = healthy); ``True`` = kill now."""
        if failure is None:
            self.failures.clear()
            return False
        if (
            self.child_started_at is not None
            and now - self.child_started_at < self.policy.startup_grace_s
        ):
            return False
        self.failures.append(failure)
        if len(self.failures) < self.policy.failures_to_wedge:
            return False
        if self.kills and now - self.kills[-1] < self._cooldown_s():
            log.warning(
                "mcp liveness: wedged (%d failed probes) but inside the %.0fs "
                "kill cooldown — not restarting yet",
                len(self.failures),
                self._cooldown_s(),
            )
            return False
        self.kills.append(now)
        return True


async def _probe_async(url: str, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.list_tools()


#: Extra time a timed-out probe gets to unwind its client before the caller
#: stops waiting for it.
_UNWIND_S = 5.0


def probe(url: str, token: str, timeout_s: float) -> str | None:
    """One MCP round-trip; ``None`` when healthy, else a one-line reason.

    Runs on its own daemon thread with a hard deadline. Cancelling a client
    mid-request against a peer that never answers has to unwind the HTTP
    stream; if that unwind itself blocked, a probe run inline would freeze
    the detector on exactly the server it exists to catch.
    """
    started = time.monotonic()
    outcome: list[str | None] = []

    def _run() -> None:
        try:
            asyncio.run(asyncio.wait_for(_probe_async(url, token), timeout_s))
        except TimeoutError:
            outcome.append(
                f"no answer to initialize+tools/list within {timeout_s:.0f}s"
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            outcome.append(f"{type(exc).__name__}: {exc} after {elapsed:.1f}s")
        else:
            outcome.append(None)

    worker = threading.Thread(target=_run, name="mcp-liveness-probe", daemon=True)
    worker.start()
    worker.join(timeout_s + _UNWIND_S)
    if not outcome:
        return f"no answer within {timeout_s:.0f}s and the probe did not unwind"
    return outcome[0]


def breadcrumb_path() -> Path:
    """Where ``install_watchdog`` keeps its last-exit note — same file.

    Mirrors ``config.cache_root("server-state") / "last-exit.json"``; see
    the module docstring for why it is not imported.
    """
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base).expanduser() / "precis" / "server-state" / "last-exit.json"


def record_wedge_kill(detector: WedgeDetector, *, pid: int | None, now: float) -> None:
    """Leave the breadcrumb the next server's ``precis-status`` shows.

    Call before ``child_started`` — that clears the failures this reports.
    """
    detail = (
        f"supervisor killed pid {pid} after {len(detector.failures)} failed "
        f"liveness probes ({'; '.join(detector.failures)}); "
        f"{len(detector.recent_kills(now))} wedge kill(s) in the last hour"
    )
    log.error("mcp liveness: %s", detail)
    payload = {
        "written_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "reason": "wedged",
        "detail": detail,
        "pid": pid,
    }
    try:
        path = breadcrumb_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        # The kill matters more than the note about it.
        log.warning("mcp liveness: breadcrumb write failed", exc_info=True)


def run_liveness_loop(
    *,
    url: str,
    token: str,
    on_wedge: Callable[[], None],
    stop: threading.Event,
    detector: WedgeDetector | None = None,
    probe_fn: Callable[[str, str, float], str | None] = probe,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Probe every ``interval_s`` until ``stop``; call ``on_wedge`` on a verdict.

    ``on_wedge`` must kill the child and report the replacement's start via
    ``detector.child_started`` — the supervisor owns the process, so it
    owns both halves.
    """
    detector = detector or WedgeDetector()
    policy = detector.policy
    while not stop.wait(policy.interval_s):
        failure = probe_fn(url, token, policy.timeout_s)
        if failure is not None:
            log.warning("mcp liveness: probe failed — %s", failure)
        if detector.observe(failure, clock()):
            on_wedge()
