"""Memory budget for xdist workers (gr788515).

The gate container carries a 24 GiB ``mem_limit``; ``scripts/ship`` fans out
``-n 6`` and a worker was seen at 5.0-5.4 GB anon-rss, so six of them OOM the
container's cgroup and xdist reports ``worker 'gwN' crashed`` on unrelated
tests. :func:`max_workers` turns the cgroup limit into a worker ceiling that
``tests/conftest.py`` applies through xdist's ``--maxprocesses``;
:func:`rss_mb` and :func:`rss_alarm` name the test that grows a worker so the
hog can be hunted down.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Per-worker budget the ceiling divides the cgroup limit by (MiB). Sized for
#: the largest observed worker (~5.4 GB); ``PRECIS_GATE_WORKER_MB`` overrides.
WORKER_BUDGET_ENV = "PRECIS_GATE_WORKER_MB"
DEFAULT_WORKER_MB = 5120
#: A worker whose RSS passes this (MiB) is reported after the test that
#: pushed it there; ``PRECIS_TEST_RSS_ALARM_MB`` overrides, 0 disables.
RSS_ALARM_ENV = "PRECIS_TEST_RSS_ALARM_MB"
DEFAULT_RSS_ALARM_MB = 3072

_CGROUP_LIMIT_FILES = (
    Path("/sys/fs/cgroup/memory.max"),  # cgroup v2
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),  # cgroup v1
)
#: cgroup v1 reports "no limit" as a huge page-rounded number, not "max".
_UNLIMITED_BYTES = 1 << 60


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def cgroup_limit_bytes() -> int | None:
    """The memory limit of this container, or None when unlimited/unknown."""
    for path in _CGROUP_LIMIT_FILES:
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not raw.isdigit():
            return None  # "max"
        limit = int(raw)
        return limit if limit < _UNLIMITED_BYTES else None
    return None


def max_workers(
    limit_bytes: int | None, per_worker_mb: int | None = None
) -> int | None:
    """Workers that fit ``limit_bytes`` (min 1); None when there is no limit."""
    if not limit_bytes:
        return None
    budget = per_worker_mb or _env_int(WORKER_BUDGET_ENV, DEFAULT_WORKER_MB)
    return max(1, limit_bytes // (max(budget, 1) * 1024 * 1024))


def rss_mb() -> float | None:
    """This process's resident set in MiB (Linux), else None."""
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)


def rss_alarm(last_alarm_mb: float | None) -> float | None:
    """The RSS to report now, or None.

    The first report fires at the alarm line. Later reports require at least
    1 GiB of growth since the last report.
    """
    line = _env_int(RSS_ALARM_ENV, DEFAULT_RSS_ALARM_MB)
    if line <= 0:
        return None
    now = rss_mb()
    if now is None or now < line:
        return None
    if last_alarm_mb is not None and now < last_alarm_mb + 1024:
        return None
    return now
