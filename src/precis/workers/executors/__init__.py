"""Executors — runner classes for `job` work.

A job_type declares what *needs* to happen (`fix_gripe` prepares a
candidate fix branch); an executor declares *how* it can happen
(`claude_inproc` spawns `claude -p` as a subprocess of the precis
worker; future `claude_docker` would spawn a per-job container;
future `slurm` would `sbatch` to a cluster).

The dispatcher matches the two at submit time: a `job_type`
declares `COMPATIBLE_EXECUTORS` (which executors can run it) and
`REQUIRES` (what the host must provide); each executor declares
`PROVIDES`. A put is rejected if executor ∉ COMPATIBLE_EXECUTORS
or REQUIRES \\ PROVIDES is non-empty.

v1 ships one executor (`claude_inproc`); the table below grows as
new runner classes land.
"""

from __future__ import annotations

#: Capability set each executor provides. The dispatcher checks
#: a job_type's REQUIRES against the chosen executor's PROVIDES;
#: any missing capability is reason to reject the submit.
EXECUTOR_PROVIDES: dict[str, frozenset[str]] = {
    "claude_inproc": frozenset(
        {
            "claude_bin",
            "git",
            "clones_dir",
            "claude_config_mount",
            # Planner-coroutine slice: claude_inproc forwards the
            # ``$PRECIS_MCP_CONFIG`` env into the claude subprocess
            # (--mcp-config) so the planner can call back via MCP.
            # See workers/job_types/plan_tick.py for the wiring.
            "mcp_config",
        }
    ),
}


#: Default executor when a `put(kind='job', ...)` call omits the
#: `executor=` field. v1 has only one option, so defaulting is the
#: kind thing to do; once `claude_docker` / `slurm` land, this
#: stays as the safest default and callers opt in explicitly.
DEFAULT_EXECUTOR = "claude_inproc"


def is_known_executor(name: str) -> bool:
    return name in EXECUTOR_PROVIDES


__all__ = ["DEFAULT_EXECUTOR", "EXECUTOR_PROVIDES", "is_known_executor"]
