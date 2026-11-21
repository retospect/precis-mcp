"""The driver: plans and tasks through the store API, ticks, waits, reads.

Everything here talks to the scratch database through ``precis.store.Store``
(the product's own API: ``plans.templates.mint_spec_build`` mints the build
task with its pre-land review exactly as ``put(kind='todo', mode='plan',
args={'template': 'spec-build', ...})`` does) and to the world's repositories
through plain git. It does not run the engine itself: a tick is a
``plan_dispatch`` job the real worker claims through its ``job_inproc`` pass.
(An in-process ``run_pass`` was removed: it ran the engine in the harness's
own process with the operator's home and PATH and skipped the claim path.)

Readers return plain dicts and lists so a scenario's assertions read as
data.
"""

from __future__ import annotations

import json
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import procguard
from .safety import assert_scratch_dsn
from .world import World

TERMINAL = ("succeeded", "failed", "cancelled")


class Timeout(AssertionError):
    """A wait ran out; the message says for what and what was seen last."""


@dataclass(frozen=True)
class Plan:
    """What ``mint_spec_build`` made."""

    root: int
    build: int
    review: int


def kill_record(
    record: dict[str, Any], run_dir: Path, sig: int = signal.SIGKILL
) -> int:
    """Signal the process group a session record names, only when the process
    table shows it belongs to this run (:mod:`.procguard`). Raises
    :class:`AssertionError` when the record has no usable group or the group
    is not this run's."""
    pgid = int(record.get("pgid") or 0)
    if pgid <= 1:
        raise AssertionError(
            f"session record has no process group: {record.get('_path')}"
        )
    if not procguard.signal_group(pgid, sig, run_dir):
        raise AssertionError(
            f"process group {pgid} of {record.get('_path')} is gone or does not "
            "belong to this run; nothing was signalled"
        )
    return pgid


class Driver:
    """One connection to the scratch database plus the world it rehearses."""

    def __init__(self, world: World, dsn: str) -> None:
        assert_scratch_dsn(dsn)
        from precis.store import Store

        self.world = world
        self.dsn = dsn
        self.store = Store.connect(dsn)
        self._tick_n = 0
        self.events: list[str] = []

    # ── lanes ───────────────────────────────────────────────────────

    def configure_lanes(self, enabled: Mapping[str, int | None]) -> None:
        """Enable exactly the named ``llm_account`` lanes (``{name: cap}``,
        ``None`` keeps the seeded cap) and disable every other one."""
        with self.store.tx() as conn:
            conn.execute("UPDATE llm_account SET enabled = false")
            for name, cap in enabled.items():
                if cap is None:
                    conn.execute(
                        "UPDATE llm_account SET enabled = true WHERE name = %s", (name,)
                    )
                else:
                    conn.execute(
                        "UPDATE llm_account SET enabled = true, concurrency = %s WHERE name = %s",
                        (cap, name),
                    )

    # ── minting ─────────────────────────────────────────────────────

    def mint_build(self, name: str, *, lane: str | None = None) -> Plan:
        """A spec-build plan: root, build task and its pre-land reviewer.
        ``name`` only labels the spec address (``docs/rehearsal/<name>.md``)."""
        from precis.plans import templates

        mint = templates.mint_spec_build(
            self.store, section=f"docs/rehearsal/{name}.md~Probe", lane=lane
        )
        plan = Plan(
            root=mint.root_id, build=mint.ids["build"], review=mint.ids["review"]
        )
        self.note(
            f"minted plan td{plan.root}: build td{plan.build}, pre-land review td{plan.review}"
        )
        return plan

    # ── reads ───────────────────────────────────────────────────────

    def _q(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self.store.pool.connection() as conn:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]

    def meta(self, ref_id: int) -> dict[str, Any]:
        rows = self._q("SELECT meta FROM refs WHERE ref_id = %s", (ref_id,))
        return dict(rows[0][0] or {}) if rows else {}

    def tags(self, ref_id: int) -> list[str]:
        out: list[str] = []
        for t in self.store.tags_for(ref_id):
            out.append(f"{t.prefix}:{t.value}" if t.prefix else t.value)
        return out

    def status(self, ref_id: int) -> str:
        for t in self.tags(ref_id):
            if t.startswith("STATUS:"):
                return t.split(":", 1)[1]
        return ""

    def comments(self, ref_id: int) -> list[str]:
        rows = self._q(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND chunk_kind IN ('todo_comment','comment') ORDER BY ord",
            (ref_id,),
        )
        return [str(r[0]) for r in rows]

    def chunk_texts(self, ref_id: int) -> list[tuple[str, str]]:
        """``[(chunk_kind, text)]`` of a ref in order (job events, results)."""
        rows = self._q(
            "SELECT coalesce(chunk_kind, ''), text FROM chunks "
            "WHERE ref_id = %s ORDER BY ord",
            (ref_id,),
        )
        return [(str(a), str(b)) for a, b in rows]

    def jobs(
        self, todo: int | None = None, job_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Job rows (newest last): ``{id, type, status, meta, parent}``.
        ``todo`` selects jobs parented to that todo."""
        where = ["kind = 'job'"]
        params: list[Any] = []
        if todo is not None:
            where.append("parent_id = %s")
            params.append(todo)
        if job_type is not None:
            where.append("meta->>'job_type' = %s")
            params.append(job_type)
        rows = self._q(
            f"SELECT ref_id, meta, parent_id FROM refs WHERE {' AND '.join(where)} ORDER BY ref_id",
            tuple(params),
        )
        out = []
        for ref_id, meta, parent in rows:
            m = dict(meta or {})
            out.append(
                {
                    "id": int(ref_id),
                    "type": m.get("job_type"),
                    "status": self.status(int(ref_id)),
                    "meta": m,
                    "parent": parent,
                }
            )
        return out

    def open_asks(self) -> list[int]:
        """Todos carrying an ask (``meta.ask``) that is not closed."""
        rows = self._q(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND retired_at IS NULL "
            "AND jsonb_typeof(meta->'ask') = 'object' ORDER BY ref_id"
        )
        return [
            int(r[0])
            for r in rows
            if self.status(int(r[0])) not in ("done", "won't-do", "auto-timeout")
        ]

    def advertised(self) -> set[str]:
        rows = self._q(
            "SELECT resource FROM resource_slots WHERE capacity > 0 AND host = %s",
            (self.host,),
        )
        return {str(r[0]) for r in rows}

    host = "rehearsal-host"

    # ── sessions (written by the shim) ──────────────────────────────

    def sessions(self, todo: int) -> list[dict[str, Any]]:
        """The shim's records for a todo, attempt order."""
        recs = []
        for p in sorted(
            self.world.sessions.glob(f"td{todo}-n*.json"),
            key=lambda p: int(p.stem.rsplit("-n", 1)[1]),
        ):
            try:
                data = json.loads(p.read_text(encoding="utf-8") or "{}")
            except ValueError:
                data = {}
            if data:
                data["_path"] = str(p)
                recs.append(data)
        return recs

    def kill_session(
        self, todo: int, attempt: int | None = None, sig: int = signal.SIGKILL
    ) -> int:
        """Signal the process group of a session (the newest one of ``todo``
        unless ``attempt`` is given); returns the group id. The group is the
        one the worker's launcher made, so the shim and everything it
        started (its commit and the hook it runs) die together."""
        recs = self.sessions(todo)
        if not recs:
            raise AssertionError(f"no session record for td{todo}")
        rec = recs[-1] if attempt is None else recs[attempt - 1]
        return kill_record(rec, self.world.run_dir, sig)

    def brief(self, todo: int, attempt: int) -> str:
        p = self.world.run_dir / "briefs" / f"td{todo}-n{attempt}.md"
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def write_script(
        self, todo: int, attempts: list[dict[str, Any]], *, repeat_last: bool = False
    ) -> Path:
        """Write the per-task session script (see ``shim.py``)."""
        self.world.scripts.mkdir(parents=True, exist_ok=True)
        path = self.world.scripts / f"td{todo}.json"
        path.write_text(
            json.dumps({"attempts": attempts, "repeat_last": repeat_last}, indent=1),
            encoding="utf-8",
        )
        return path

    # ── ticks and waits ─────────────────────────────────────────────

    def note(self, text: str) -> None:
        self.events.append(f"{time.strftime('%H:%M:%S')} {text}")

    def tick(self, *, timeout: float = 120.0) -> dict[str, Any]:
        """One dispatcher pass. Returns ``{status, counts}``."""
        self._tick_n += 1
        from precis.workers._mint import mint_job_once

        key = f"rehearsal-tick-{int(time.time())}-{self._tick_n}"
        with self.store.tx() as conn:
            job_id, _ = mint_job_once(
                self.store,
                conn=conn,
                job_type="plan_dispatch",
                executor="job_inproc",
                title=f"plan_dispatch tick {self._tick_n} (rehearsal)",
                params={},
                idem_key=key,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.status(job_id)
            if st in TERMINAL:
                counts = self.meta(job_id).get("plan_dispatch") or {}
                return {"status": st, "counts": counts, "job": job_id}
            time.sleep(0.3)
        raise Timeout(
            f"plan_dispatch tick job {job_id} still {self.status(job_id)!r} after {timeout:.0f}s"
        )

    def wait_until(
        self,
        predicate: Callable[[], Any],
        *,
        timeout: float,
        what: str,
        tick_every: float | None = 3.0,
        poll: float = 0.5,
        health: Callable[[], None] | None = None,
    ) -> Any:
        """Poll ``predicate`` until truthy (its value is returned), ticking
        the dispatcher every ``tick_every`` seconds (``None``: never).
        ``health`` runs each round and raises when the world is broken (the
        worker died). Raises :class:`Timeout` naming ``what``."""
        deadline = time.monotonic() + timeout
        last_tick = 0.0
        while True:
            if health is not None:
                health()
            val = predicate()
            if val:
                return val
            now = time.monotonic()
            if now > deadline:
                raise Timeout(f"timed out after {timeout:.0f}s waiting for {what}")
            if tick_every is not None and now - last_tick >= tick_every:
                self.tick()
                last_tick = time.monotonic()
                continue
            time.sleep(poll)

    def describe(self, *todos: int) -> str:
        """A failure message body: each todo's status and build_ref, its jobs
        and their newest event lines, the hub's attempt refs."""
        lines: list[str] = []
        for todo in todos:
            meta = self.meta(todo)
            lines.append(
                f"td{todo}: {self.status(todo)} tags={self.tags(todo)} "
                f"build_ref={(meta.get('build_ref') or {}).get('ref')} "
                f"pending_land={'yes' if meta.get('pending_land') else 'no'} "
                f"conflict={'yes' if meta.get('conflict') else 'no'}"
            )
            for job in self.jobs(todo):
                lines.append(f"  jo{job['id']} {job['type']} {job['status']}")
                for kind, text in self.chunk_texts(job["id"])[-4:]:
                    lines.append(f"    [{kind or '-'}] {text[:240]}")
            lines.append(f"  hub refs: {sorted(self.hub_refs(todo))}")
        return "\n".join(lines)

    # ── world reads the assertions use ──────────────────────────────

    def hub_refs(self, todo: int) -> dict[str, str]:
        return self.world.hub_refs(f"refs/factory/build/{todo}/")

    def folders(self, todo: int) -> list[str]:
        return [
            p.name
            for p in self.world.attempt_folders()
            if p.name.startswith(f"slice-td{todo}-")
        ]

    def close(self) -> None:
        self.store.close()
