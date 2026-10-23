"""sandbox_run ``mode:script`` — the async dependency stage job.

Locking and downloading a dependency set can take minutes. Done in the
run job's claim tick it blocked the worker's poll loop and ate the
script's lease (gr496426). Instead the run job asks for a *stage job*: a
``sandbox_run`` row with ``params.mode == "stage"`` (internal — the put
gate refuses that mode) that runs
:func:`~precis.workers.executors._sandbox_script.stage_deps` in a worker
thread under its own lease. The run job consumes the staged wheel dir and
only then launches, so ``wall_seconds`` and the run lease start at the
launching claim and staging time is never counted against the script.

Keying: ``meta.idem_key = sandbox_stage:<node>:<sha256 of the normalised
deps>``. The lock hash exists only after the compile, so the dependency
set keys the job and the lock hash keys the wheel dir the job fills
(two dep sets that resolve to one lock still share the dir). Wheels are
host-local, hence the node in the key. Concurrent requests for one key
share one job: :func:`find_or_mint` serialises on a transaction-level
advisory lock.

A stage that succeeded records ``meta.stage = {lock_text, lock_sha,
wheels_dir, reused}``; one that failed records the message in the job's
event chunk and ``meta.stage_error``. A succeeded stage whose wheel dir
has since vanished is not reused: the next request mints a new one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store

#: ``params.mode`` of a stage job. Not in ``sandbox_run._SUPPORTED_MODES``,
#: so no agent can submit one.
MODE = "stage"

#: Stage jobs hold a short lease that the running worker re-extends every
#: pass; a worker that died leaves it to expire and the row is reclaimed.
STAGE_LEASE_S = 180

#: How long a run job waits before it re-checks its stage.
RECHECK_S = 15

#: ``params.resources.wall_seconds`` of a stage job: the compile plus the
#: download timeouts of ``_sandbox_script``.
STAGE_WALL_S = 1200

_COMPLETE_MARKER = ".complete"


def normalise_deps(deps: list[str]) -> list[str]:
    """Stripped, de-duplicated, sorted requirement strings (resolution does
    not depend on their order)."""
    return sorted({d.strip() for d in deps if d.strip()})


def deps_key(deps: list[str]) -> str:
    return hashlib.sha256(json.dumps(normalise_deps(deps)).encode("utf-8")).hexdigest()


def idem_key(node: str | None, deps: list[str]) -> str:
    return f"sandbox_stage:{node or ''}:{deps_key(deps)}"


@dataclass(frozen=True, slots=True)
class StageState:
    """The newest stage job for one key."""

    ref_id: int
    status: str | None
    meta: dict[str, Any]

    @property
    def usable(self) -> bool:
        """Succeeded, and the wheel dir it staged is still complete."""
        if self.status != "succeeded":
            return False
        info = self.meta.get("stage") or {}
        wheels = info.get("wheels_dir")
        return bool(wheels) and (Path(str(wheels)) / _COMPLETE_MARKER).exists()

    @property
    def pending(self) -> bool:
        return self.status in ("queued", "running")


def _latest(conn: Any, key: str) -> StageState | None:
    row = conn.execute(
        "SELECT r.ref_id, r.meta, "
        "  (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
        "   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1) "
        "FROM refs r WHERE r.kind = 'job' AND r.retired_at IS NULL "
        "AND r.meta->>'idem_key' = %s ORDER BY r.ref_id DESC LIMIT 1",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return StageState(int(row[0]), row[2], dict(row[1] or {}))


def get_stage(store: Store, ref_id: int) -> StageState | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.meta, "
            "  (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1) "
            "FROM refs r WHERE r.ref_id = %s",
            (ref_id,),
        ).fetchone()
    if row is None:
        return None
    return StageState(ref_id, row[1], dict(row[0] or {}))


def find_or_mint(store: Store, node: str | None, deps: list[str]) -> StageState:
    """The stage job for ``(node, deps)``: the newest one when it is
    pending or usable, else a freshly minted queued one (a failed or
    vanished stage is not reused)."""
    from precis.workers._mint import mint_background_job

    key = idem_key(node, deps)
    with store.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        found = _latest(conn, key)
        if found is not None and (found.pending or found.usable):
            return found
        params: dict[str, Any] = {
            "mode": MODE,
            "deps": normalise_deps(deps),
            "resources": {"wall_seconds": STAGE_WALL_S},
        }
        if node:
            params["target_node"] = node
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="sandbox_run",
            executor="claude_docker",
            title=f"sandbox_run stage ({key[-12:]})",
            params=params,
            idem_key=key,
        )
        return StageState(int(ref.id), "queued", {})


__all__ = [
    "MODE",
    "RECHECK_S",
    "STAGE_LEASE_S",
    "STAGE_WALL_S",
    "StageState",
    "deps_key",
    "find_or_mint",
    "get_stage",
    "idem_key",
    "normalise_deps",
]
