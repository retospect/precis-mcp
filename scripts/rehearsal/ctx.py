"""What a scenario is given: the world, the driver, the worker and the
declarations the invariant checker reads."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .driver import Driver, Plan, Timeout
from .invariants import Expect, SlotBaseline
from .worker import Worker
from .world import World

#: Seconds an ended job's folder may take to be settled by a worker pass (the
#: engine's tidy hook runs once per watch pass, throttled to ~30 s).
SETTLE_SECONDS = 150.0


@dataclass
class Ctx:
    """One run's shared state (the world outlives a scenario; ``expect`` is
    cumulative over the run)."""

    world: World
    driver: Driver
    #: Raises when the worker process died (a wait passes it as ``health``).
    health: Callable[[], None]
    #: Seconds a scenario may wait for one engine step.
    step_timeout: float = 240.0
    #: How long ``smoke`` follows the job after the shim launched.
    follow: float = 0.0
    worker: Worker | None = None
    #: The lane whose shim builds in this run.
    builder_lane: str = "claude-sub"
    expect: Expect = field(default_factory=Expect)
    #: Every plan a scenario registered (the failed ones are retired by the
    #: runner's quiesce, so they do not run into the next scenario).
    plans: list[Plan] = field(default_factory=list)
    #: The slot state when the running scenario began (rule 9's baseline).
    slot_baseline: SlotBaseline | None = None
    #: Facts a scenario wants in its result line or the report.
    facts: list[str] = field(default_factory=list)

    def wait(
        self,
        what: str,
        predicate: Callable[[], object],
        *,
        timeout: float | None = None,
        tick_every: float | None = 3.0,
        health: bool = True,
    ) -> object:
        return self.driver.wait_until(
            predicate,
            timeout=timeout or self.step_timeout,
            what=what,
            tick_every=tick_every,
            health=self.health if health else None,
        )

    def settle_folders(
        self,
        todo: int | None = None,
        *,
        keep: tuple[str, ...] = (),
        timeout: float = SETTLE_SECONDS,
    ) -> list[str]:
        """Wait until the attempt folders of ``todo`` (all of them when
        ``None``) are gone, except ``keep`` and the ones a scenario declared
        kept. The engine settles an ended job's folder from a worker pass
        within about a minute, not at the next launch: this is the outcome
        wait, with no ticks. Returns what is left (empty on success)."""
        spared = set(keep) | self.expect.kept

        def left() -> list[str]:
            if todo is not None:
                names = self.driver.folders(todo)
            else:
                names = [p.name for p in self.world.attempt_folders()]
            return sorted(set(names) - spared)

        try:
            self.driver.wait_until(
                lambda: not left(),
                timeout=timeout,
                what="the attempt folders to be settled",
                tick_every=None,
                health=self.health,
            )
        except Timeout:
            pass
        return left()

    def register(self, plan: Plan) -> Plan:
        """Declare a plan's build task: its files are checked (rule 7)."""
        self.plans.append(plan)
        self.expect.tasks.add(plan.build)
        self.expect.reviews[plan.build] = plan.review
        return plan

    def landed(self, plan: Plan) -> None:
        self.expect.landed.add(plan.build)

    def note(self, text: str) -> None:
        self.facts.append(text)
        self.driver.note(text)

    def kill_worker(self) -> None:
        """SIGKILL the worker (no shutdown, no release)."""
        assert self.worker is not None, "this run has no worker handle"
        self.note(self.worker.kill())

    def restart_worker(self) -> float:
        """Start a new worker after :meth:`kill_worker` and wait until its
        host advertises again; returns the seconds it took."""
        assert self.worker is not None, "this run has no worker handle"
        began = time.monotonic()
        pid = self.worker.restart()
        self.note(f"worker restarted as pid {pid}")
        self.driver.wait_until(
            lambda: {"repo_checkout", "git"} <= self.driver.advertised(),
            timeout=120,
            what="the restarted worker's host to advertise again",
            tick_every=None,
            health=self.health,
        )
        return time.monotonic() - began
