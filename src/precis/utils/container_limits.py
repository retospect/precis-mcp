"""Optional CPU-limit flags for spawned job containers (nice-all-jobs).

Batch compute (AlphaFold, DFT, TTS, sandbox agents) runs in ``docker``/``podman``
containers that do NOT inherit the host process's ``nice`` value — Docker gives
each container its own cgroup at default weight, so a heavy fold can starve
interactive/system work (e.g. sshd) on a shared node. These flags fence it off:

- ``PRECIS_JOB_CPUSET``     -> ``--cpuset-cpus <val>`` (e.g. "2-19": pin the
  container to cores 2-19, reserving 0-1 for the system; spark-only, set by ansible)
- ``PRECIS_JOB_CPU_SHARES`` -> ``--cpu-shares <val>`` (relative CPU weight;
  default is 1024, so "256" = a quarter share under contention)

Both flags are accepted by docker and podman. Absent/empty env -> empty list
(no change). Insert the returned flags right after the ``run`` subcommand.
"""

from __future__ import annotations

import os


def container_limit_flags() -> list[str]:
    """CPU-limit ``run`` flags from PRECIS_JOB_CPUSET / PRECIS_JOB_CPU_SHARES.

    Empty list when neither env var is set, so callers can unconditionally
    splice it into their container argv."""
    flags: list[str] = []
    cpuset = os.environ.get("PRECIS_JOB_CPUSET", "").strip()
    if cpuset:
        flags += ["--cpuset-cpus", cpuset]
    shares = os.environ.get("PRECIS_JOB_CPU_SHARES", "").strip()
    if shares:
        flags += ["--cpu-shares", shares]
    return flags


def container_user_flags() -> list[str]:
    """``--user <uid:gid>`` from PRECIS_CONTAINER_USER, for the science
    containers that write scratch on the cluster share (dft, fold,
    retrosynth).

    The share squashes root, so a container running as its image's default
    root writes files the NAS maps to ``guest``, which ``deploy`` cannot
    clean up (cluster-fileserver-move, Phase 0 step 3). The roles render
    ``deploy``'s uid:gid here; uids match fleet-wide. Empty or unset -> empty
    list (the image's own user, today's behaviour)."""
    user = os.environ.get("PRECIS_CONTAINER_USER", "").strip()
    return ["--user", user] if user else []


__all__ = ["container_limit_flags", "container_user_flags"]
