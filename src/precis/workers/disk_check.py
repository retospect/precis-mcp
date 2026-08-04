"""Disk-check pass — raise a precis-native alert before a node fills.

Gripe 191008: caspar's DB SSD hit 100%, ``psycopg`` raised ``DiskFull``,
and ALL prod writes stalled ~1.5h before anyone noticed — Prometheus had
a 90% rule, but it reached no one. This pass is the precis-native
replacement: a per-node system-profile check that ``df``s the configured
watch paths every cycle and raises ``kind='alert'`` (warn / critical)
straight into the ``/alerts`` tab and, on a fresh critical, the ops
Discord push — the same surface every other detector (nursery,
quota_check) already pages through.

Runs on the **system** profile (every node), singleton per cycle — like
``quota_check`` there is nothing to batch, so ``limit`` is accepted for
the :class:`~precis.workers.runner.BatchResult` contract and ignored.
"""

from __future__ import annotations

import logging
import os
import shutil

from precis.alerts import (
    notify_critical_alert,
    open_alert_severity,
    raise_alert,
    resolve_stale_alerts,
)
from precis.store import Store
from precis.utils.db_log_handler import _resolve_host_name
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Alert source for a filling-disk condition.
_DISK_ALERT_SOURCE = "disk_check"

#: ``os.pathsep``-separated list of paths to ``df``. Default: just ``/`` —
#: on the data node (caspar) the PG data dir, /opt/homebrew, and
#: /opt/shared all live on the root volume, which is what filled.
_WATCH_PATHS_ENV = "PRECIS_DISK_WATCH_PATHS"
_DEFAULT_WATCH_PATHS = ("/",)

#: Percent-used thresholds. Crossing warn opens a ``warn`` alert; crossing
#: crit opens (or upgrades to) a ``critical`` alert and pages once.
_WARN_PCT_ENV = "PRECIS_DISK_WARN_PCT"
_CRIT_PCT_ENV = "PRECIS_DISK_CRIT_PCT"
_DEFAULT_WARN_PCT = 85.0
_DEFAULT_CRIT_PCT = 93.0


def _watch_paths() -> tuple[str, ...]:
    raw = os.environ.get(_WATCH_PATHS_ENV, "")
    if not raw:
        return _DEFAULT_WATCH_PATHS
    paths = tuple(p for p in raw.split(os.pathsep) if p)
    return paths or _DEFAULT_WATCH_PATHS


def _threshold(env_var: str, default: float) -> float:
    raw = os.environ.get(env_var, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.info("disk_check: bad %s=%r, falling back to %.1f", env_var, raw, default)
        return default


def _gib(nbytes: int) -> float:
    return nbytes / (1024**3)


def run_disk_check_pass(store: Store, *, limit: int = 1) -> BatchResult:
    """Check each watched path's disk usage; raise/resolve alerts on it.

    Counters (mirrors ``quota_check``'s singleton shape):

    * ``claimed`` = 1 always (one check cycle ran).
    * ``ok`` = 1 on a normal cycle (healthy or alerting, still measured).
    * ``failed`` = 1 only if the whole pass hit an unexpected exception —
      a ``df`` hiccup must never kill the worker cascade.
    """
    del limit  # singleton pass, nothing to batch — like quota_check

    try:
        host = _resolve_host_name()
        warn_pct = _threshold(_WARN_PCT_ENV, _DEFAULT_WARN_PCT)
        crit_pct = _threshold(_CRIT_PCT_ENV, _DEFAULT_CRIT_PCT)

        live_fingerprints: set[str] = set()
        for path in _watch_paths():
            try:
                usage = shutil.disk_usage(path)
            except (FileNotFoundError, OSError):
                log.info("disk_check: %s not mounted on %s, skipping", path, host)
                continue

            if usage.total <= 0:
                log.info(
                    "disk_check: %s on %s reports total=0 (pseudo-fs?), skipping",
                    path,
                    host,
                )
                continue

            used_pct = usage.used / usage.total * 100.0
            fingerprint = f"{host}:{path}"
            free_gib = _gib(usage.free)
            total_gib = _gib(usage.total)

            if used_pct >= crit_pct:
                live_fingerprints.add(fingerprint)
                title = f"[disk] {host}:{path} at {used_pct:.0f}% full"
                detail = (
                    f"{free_gib:.1f} GiB free of {total_gib:.1f} GiB total "
                    f"({used_pct:.1f}% used, crit threshold {crit_pct:.0f}%). "
                    "Writes can stall (DiskFull) at 100% — prune backups, "
                    "rotate/compress logs, or investigate large dirs under "
                    f"{path} now."
                )
                # Read the PRIOR severity before raise_alert: a gradual
                # fill typically opens a warn alert first, then a later
                # cycle crosses crit — raise_alert BUMPS that same open
                # row rather than inserting a fresh one, so is_new alone
                # is False on the escalation and the page would never
                # fire (the exact "reached no one" failure gripe 191008
                # is about). Paging on "is_new OR prior wasn't already
                # critical" covers fresh-crit and warn→crit alike, while
                # a repeat-crit cycle (prior == "critical") stays silent.
                prior_severity = open_alert_severity(
                    store, source=_DISK_ALERT_SOURCE, fingerprint=fingerprint
                )
                _ref_id, is_new = raise_alert(
                    store,
                    source=_DISK_ALERT_SOURCE,
                    fingerprint=fingerprint,
                    title=title,
                    detail=detail,
                    severity="critical",
                )
                if is_new or prior_severity != "critical":
                    notify_critical_alert(store, title, detail, fingerprint=fingerprint)
            elif used_pct >= warn_pct:
                live_fingerprints.add(fingerprint)
                title = f"[disk] {host}:{path} at {used_pct:.0f}% full"
                detail = (
                    f"{free_gib:.1f} GiB free of {total_gib:.1f} GiB total "
                    f"({used_pct:.1f}% used, warn threshold {warn_pct:.0f}%). "
                    "Prune backups, rotate/compress logs, or investigate "
                    f"large dirs under {path} before it reaches {crit_pct:.0f}%."
                )
                raise_alert(
                    store,
                    source=_DISK_ALERT_SOURCE,
                    fingerprint=fingerprint,
                    title=title,
                    detail=detail,
                    severity="warn",
                )
            # else: healthy — omitted from live_fingerprints so a
            # standing alert for this path resolves below.

        resolve_stale_alerts(
            store, source=_DISK_ALERT_SOURCE, live_fingerprints=live_fingerprints
        )
        return BatchResult(handler="disk_check", claimed=1, ok=1, failed=0)
    except Exception:
        log.warning("disk_check: pass failed", exc_info=True)
        return BatchResult(handler="disk_check", claimed=1, ok=0, failed=1)


__all__ = ["run_disk_check_pass"]
