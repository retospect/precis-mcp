"""Per-host liveness — the collection+upsert core behind ``precis heartbeat``.

Refactored out of ``cli/heartbeat.py`` (§A) so the exact same collection
logic backs BOTH the manual/cron-fired ``precis heartbeat`` CLI (still live —
the launchd (Macs) / systemd-timer (spark) heartbeat timers on every node
keep firing until §L retires them) AND a new ``heartbeat`` **worker pass**
(registry ``category="health"``, ``default_profiles=_SYS`` — every node's
system worker) that runs it once more per cycle.

The pass is deliberately **NOT** on ``scheduler_leases`` (:mod:`precis.workers.
scheduler`) — heartbeat is the liveness signal that lease/claim machinery
(and every other health check) is judged by, so it must never depend on the
claim machinery it would be used to judge. Instead it self-throttles with a
plain in-process timestamp (module-level state, NOT a DB round-trip) to at
most once per ``PRECIS_HEARTBEAT_INTERVAL_SECONDS`` (default 60) — a
double-fire against the still-live launchd/systemd timer is a harmless
idempotent UPSERT, so no coordination between the two triggers is needed.

Collects load average and a best-effort CPU temperature and UPSERTs one row
into ``host_heartbeat`` (migration 0017). The web Status tab reads the table
to show "which machines are alive and is any of them hot". Identity matches
the DB log handler: ``host`` is ``PRECIS_HOST_NAME`` or
``socket.gethostname()`` so heartbeat rows and ``worker_logs`` rows agree on
the same host name.

Temperature is genuinely hard to read portably, so it is best-effort in
priority order:

1. ``PRECIS_TEMP_CMD`` — a shell command whose stdout's first float is parsed
   as °C. The escape hatch for any sensor (IPMI, a custom script) without
   baking platform logic here.
2. Linux ``/sys/class/thermal/thermal_zone*/temp`` (millidegrees), max across
   zones.
3. macOS — read the SoC thermal sensors through IOKit's HID event system
   (``ctypes``, unprivileged, no install); on the old Intel path fall back to
   the ``osx-cpu-temp`` brew binary. Apple Silicon exposes no sensor files
   and ``osx-cpu-temp`` reads Intel-only SMC keys (returns 0.0), so the IOKit
   read is the only numeric source short of ``sudo powermetrics`` (which
   itself gives only a qualitative thermal-pressure level on Apple Silicon).
4. ``None`` — the host still reports load + liveness.
"""

from __future__ import annotations

import glob
import logging
import os
import platform
import re
import subprocess
import time
from typing import Any

from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def resolve_host(override: str | None = None) -> str:
    """Pick the reported host: flag > ``PRECIS_HOST_NAME`` > hostname."""
    if override:
        return override
    import socket

    return os.environ.get("PRECIS_HOST_NAME") or socket.gethostname()


def collect_loads() -> tuple[float | None, float | None, float | None]:
    """Return the 1/5/15-minute load averages, or ``(None, None, None)``.

    ``os.getloadavg`` is available on the unix hosts in play; a
    platform without it (or a sandbox that denies it) degrades to
    ``None`` rather than failing the whole report.
    """
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):
        return (None, None, None)
    return (one, five, fifteen)


def collect_top_cpu(n: int = 3) -> list[dict[str, Any]]:
    """Best-effort top-``n`` processes by CPU %, for the factory host strip.

    So "why is this host's load high?" is answerable from the dashboard
    (postgres pegging three cores, a runaway worker, …) without SSH-ing in.
    A diagnostic nicety, never the liveness signal: any failure degrades to
    ``[]`` rather than failing the report, same grain as the temp probe.

    ``ps -Ao pcpu=,comm=`` is the portable slice across the Linux + macOS
    cluster hosts (``=`` suppresses headers on both BSD and GNU ``ps``). We
    sort in Python (don't rely on ``-r`` / ``--sort``), basename ``comm`` so
    an absolute path doesn't bloat the JSONB, and keep only cpu > 0. Note a
    postgres backend collapses to ``postgres`` here — enough to point at the
    DB; the exact query still needs ``pg_stat_activity``.
    """
    try:
        res = subprocess.run(
            ["ps", "-Ao", "pcpu=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("heartbeat: top-CPU probe failed to run", exc_info=True)
        return []
    if res.returncode != 0:
        return []
    procs: list[dict[str, Any]] = []
    for line in res.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        cpu = _parse_first_float(parts[0])
        if cpu is None or cpu <= 0.0:
            continue
        cmd = os.path.basename(parts[1].strip()) or parts[1].strip()
        procs.append({"cpu": round(cpu, 1), "cmd": cmd[:40]})
    procs.sort(key=lambda p: p["cpu"], reverse=True)
    return procs[:n]


def _probe_nas() -> dict[str, Any]:
    """Probe NAS readability from THIS process's context.

    The heartbeat runs as ``deploy`` under launchd — the same context the
    watch/worker daemons use — so an EPERM here means every launchd/cron
    process on this host is locked out of the NAS. That happens when the
    venv python's Full Disk Access grant breaks after ``brew upgrade
    python`` changes its cdhash (OPEN-ITEMS: 'melchior daemon NAS lockout').

    Returns meta fields merged into the heartbeat row:
      - readable NAS      -> {'nas_ok': True,  'nas_path': <p>}
      - EPERM (the break) -> {'nas_ok': False, 'nas_path': <p>, 'nas_errno': <n>}
      - path absent       -> {}  (host doesn't mount the NAS -> no signal, never alerts)
      - other OSError     -> {'nas_probe_err': <str>, 'nas_path': <p>}  (transient; recorded, not alerted)
    Never raises — heartbeat liveness must not depend on the NAS.
    """
    path = os.environ.get("PRECIS_NAS_PROBE_PATH", "/opt/nas/botshome")
    try:
        with os.scandir(path) as it:
            next(
                it, None
            )  # force an actual directory read (triggers the perm check / automount)
    except PermissionError as e:
        return {"nas_ok": False, "nas_path": path, "nas_errno": e.errno}
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except OSError as e:
        return {"nas_probe_err": str(e), "nas_path": path}
    return {"nas_ok": True, "nas_path": path}


def _parse_first_float(text: str) -> float | None:
    m = _FLOAT_RE.search(text)
    if m is None:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _temp_from_cmd(cmd: str) -> float | None:
    """Run ``cmd`` and parse the first float in stdout as °C."""
    try:
        res = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("heartbeat: PRECIS_TEMP_CMD failed to run", exc_info=True)
        return None
    if res.returncode != 0:
        log.warning(
            "heartbeat: PRECIS_TEMP_CMD exited %d: %s",
            res.returncode,
            (res.stderr or "").strip()[:200],
        )
        return None
    return _parse_first_float(res.stdout)


def _temp_from_linux_thermal() -> float | None:
    """Max over ``/sys/class/thermal/thermal_zone*/temp`` (millidegrees)."""
    readings: list[float] = []
    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        val = _parse_first_float(raw)
        if val is None:
            continue
        # /sys reports millidegrees; values >= 1000 are mC, else already C.
        readings.append(val / 1000.0 if abs(val) >= 1000 else val)
    return max(readings) if readings else None


def _temp_from_macos_iokit() -> float | None:
    """Read the Apple Silicon SoC temp via IOKit's HID event system.

    macOS exposes no thermal sensor files; the SoC die sensors live
    behind the IOKit HID event system, which an unprivileged process
    can read (no sudo, no install — ``osx-cpu-temp`` reads Intel-only
    SMC keys and returns 0.0 here). We match HID services on the
    Apple-vendor temperature usage page and copy a temperature event
    from each, returning the hottest reading in °C.

    Pure ``ctypes`` against system frameworks; any failure (missing
    framework, API shape change, no matching sensors) degrades to
    ``None`` so the host still reports load + liveness.
    """
    import ctypes
    import ctypes.util

    # kHIDPage_AppleVendor / kHIDUsage_AppleVendor_TemperatureSensor
    HID_PAGE, HID_USAGE = 0xFF00, 0x0005
    kIOHIDEventTypeTemperature = 15
    temperature_field = kIOHIDEventTypeTemperature << 16
    kCFNumberSInt32Type = 3
    kCFStringEncodingUTF8 = 0x08000100

    try:
        iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        cf.CFNumberCreate.restype = ctypes.c_void_p
        cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]

        iokit.IOHIDEventSystemClientCreate.restype = ctypes.c_void_p
        iokit.IOHIDEventSystemClientCreate.argtypes = [ctypes.c_void_p]
        iokit.IOHIDEventSystemClientSetMatching.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        iokit.IOHIDEventSystemClientCopyServices.restype = ctypes.c_void_p
        iokit.IOHIDEventSystemClientCopyServices.argtypes = [ctypes.c_void_p]
        iokit.IOHIDServiceClientCopyEvent.restype = ctypes.c_void_p
        iokit.IOHIDServiceClientCopyEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int64,
            ctypes.c_int32,
            ctypes.c_int64,
        ]
        iokit.IOHIDEventGetFloatValue.restype = ctypes.c_double
        iokit.IOHIDEventGetFloatValue.argtypes = [ctypes.c_void_p, ctypes.c_int32]

        def _cfstr(s: str) -> ctypes.c_void_p:
            return cf.CFStringCreateWithCString(None, s.encode(), kCFStringEncodingUTF8)

        def _cfnum(n: int) -> ctypes.c_void_p:
            v = ctypes.c_int32(n)
            return cf.CFNumberCreate(None, kCFNumberSInt32Type, ctypes.byref(v))

        keys = (ctypes.c_void_p * 2)(_cfstr("PrimaryUsagePage"), _cfstr("PrimaryUsage"))
        vals = (ctypes.c_void_p * 2)(_cfnum(HID_PAGE), _cfnum(HID_USAGE))
        matching = cf.CFDictionaryCreate(None, keys, vals, 2, None, None)

        client = iokit.IOHIDEventSystemClientCreate(None)
        if not client:
            return None
        iokit.IOHIDEventSystemClientSetMatching(client, matching)
        services = iokit.IOHIDEventSystemClientCopyServices(client)
        if not services:
            return None

        readings: list[float] = []
        for i in range(cf.CFArrayGetCount(services)):
            svc = cf.CFArrayGetValueAtIndex(services, i)
            event = iokit.IOHIDServiceClientCopyEvent(
                svc, kIOHIDEventTypeTemperature, 0, 0
            )
            if not event:
                continue
            val = iokit.IOHIDEventGetFloatValue(event, temperature_field)
            if val and val > 0:
                readings.append(val)
    except (OSError, AttributeError, ValueError):
        log.warning("heartbeat: IOKit temperature read failed", exc_info=True)
        return None
    return max(readings) if readings else None


def _temp_from_macos_smc() -> float | None:
    """Intel-Mac fallback: CPU temp via the ``osx-cpu-temp`` brew binary.

    ``osx-cpu-temp`` does the SMC call and prints "47.5°C". It reads
    Intel-only SMC keys, so on Apple Silicon it returns 0.0 — treated
    as no reading here (the IOKit path above covers Apple Silicon).
    Returns ``None`` when the binary isn't installed.

    The binary lives at ``/usr/local/bin/osx-cpu-temp`` on Intel and
    ``/opt/homebrew/bin/osx-cpu-temp`` on Apple Silicon.
    """
    for path in (
        "/opt/homebrew/bin/osx-cpu-temp",
        "/usr/local/bin/osx-cpu-temp",
    ):
        try:
            res = subprocess.run(
                [path],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode != 0:
            continue
        val = _parse_first_float(res.stdout)
        if val is not None and val > 0:
            return val
    return None


def read_temp_c() -> float | None:
    """Best-effort CPU temperature in °C (see module docstring order)."""
    cmd = os.environ.get("PRECIS_TEMP_CMD")
    if cmd:
        temp = _temp_from_cmd(cmd)
        if temp is not None:
            return temp
    if platform.system() == "Linux":
        return _temp_from_linux_thermal()
    if platform.system() == "Darwin":
        return _temp_from_macos_iokit() or _temp_from_macos_smc()
    return None


def _report_resource_slots(store: object, host: str) -> str:
    """Self-probe this host's capabilities and sync ``resource_slots``.

    Best-effort: the capability map (factory scheduler slice 6b, §5.5) is a
    scheduling optimisation, never the liveness signal — a probe or write
    failure must not fail the heartbeat, so this swallows and logs. Returns
    a short ``gpu=1,podman=2`` summary for the CLI line (``n/a`` on error).
    """
    from precis.workers.capability_probe import (
        RETRACTABLE_SOFT_SIGNALS,
        probe_host_resources,
        probe_soft_signals,
        resource_kind,
        soft_capacity,
    )

    try:
        evaluated = probe_host_resources()
        kinds = {r: resource_kind(r) for r in evaluated}
        store.sync_host_resource_slots(host, evaluated, kinds=kinds)  # type: ignore[attr-defined]
        # Soft gauges: memory-pressure headroom (6d) + container_agent capability.
        # Written free-first, read as a claim veto / rendered as a console health
        # chip. Each carries its own capacity (mem is multi-bucket, container_agent
        # 0/1) — hence per-resource ``soft_capacity``, not one stamp for all.
        soft = probe_soft_signals()
        for resource, free in soft.items():
            store.sync_soft_signal(host, resource, free, soft_capacity(resource))  # type: ignore[attr-defined]
        # Retract a retractable gauge that dropped out (e.g. container_agent once
        # a host opts out of PRECIS_AGENT_CONTAINER) so the console stops showing
        # a stale chip. mem is never retractable — its absence means "leave".
        for resource in RETRACTABLE_SOFT_SIGNALS - soft.keys():
            store.delete_soft_signal(host, resource)  # type: ignore[attr-defined]
    except Exception:
        log.warning("heartbeat: resource-slot probe/sync failed", exc_info=True)
        return "n/a"
    # Advertise this host's local llama-swap models as served_by cards + llm: slots
    # so the router routes to them directly (self-gating: no local server ⇒ no-op).
    # Best-effort + separate try so a catalog blip never fails the heartbeat.
    try:
        from precis.workers.llm_serving import advertise_local_llm

        advertise_local_llm(store, host)
    except Exception:
        log.warning("heartbeat: local-llm advertise failed", exc_info=True)
    present = {r: c for r, c in evaluated.items() if c}
    return ",".join(f"{r}={c}" for r, c in sorted(present.items())) or "none"


def _collect_and_upsert(
    store: Any, host: str
) -> tuple[float | None, float | None, str]:
    """Collect this host's snapshot and UPSERT it into ``host_heartbeat`` +
    ``resource_slots``. Returns ``(temp_c, load1, slots_summary)`` for a
    caller to report."""
    load1, load5, load15 = collect_loads()
    temp_c = read_temp_c()
    meta: dict[str, Any] = {
        "platform": platform.system(),
        "release": platform.release(),
        "top_cpu": collect_top_cpu(),
    }
    meta.update(_probe_nas())

    store.record_heartbeat(
        host,
        temp_c=temp_c,
        load1=load1,
        load5=load5,
        load15=load15,
        meta=meta,
    )
    slots = _report_resource_slots(store, host)
    return temp_c, load1, slots


def collect_and_report(store: Any, host: str | None = None) -> str:
    """Unconditional collect+upsert — always fires (this is what the CLI
    ``precis heartbeat`` invokes, and the still-live launchd/systemd timers
    keep calling it directly too, until §L). Returns the CLI's summary line."""
    resolved = resolve_host(host)
    temp_c, load1, slots = _collect_and_upsert(store, resolved)
    temp_str = f"{temp_c:.1f}C" if temp_c is not None else "n/a"
    load_str = f"{load1:.2f}" if load1 is not None else "n/a"
    return f"heartbeat: {resolved} temp={temp_str} load1={load_str} slots={slots}"


#: The last monotonic timestamp a heartbeat *actually fired* from this
#: process — in-process state (module-global), deliberately NOT persisted to
#: the DB: the DB row is the liveness signal this throttle protects, so the
#: throttle itself must never depend on a DB read/write to decide.
_last_beat_monotonic: float | None = None


def _interval_s() -> float:
    raw = os.environ.get("PRECIS_HEARTBEAT_INTERVAL_SECONDS")
    if raw:
        try:
            val = float(raw)
        except ValueError:
            val = 0.0
        if val > 0:
            return val
    return 60.0


def run_heartbeat_pass(store: Any, *, host: str | None = None) -> BatchResult:
    """The ``heartbeat`` worker pass (§A) — runs on EVERY node's system
    worker each cycle, self-throttled to at most once every
    ``PRECIS_HEARTBEAT_INTERVAL_SECONDS`` (default 60s) via the in-process
    timestamp above. Deliberately does **not** touch ``scheduler_leases`` or
    any other claim machinery — heartbeat is the liveness signal that
    machinery is judged by. A double-fire against the still-live
    launchd/systemd heartbeat timer (retired only in §L) is a harmless
    idempotent UPSERT, so the two triggers need no coordination.
    """
    global _last_beat_monotonic
    now = time.monotonic()
    if (
        _last_beat_monotonic is not None
        and (now - _last_beat_monotonic) < _interval_s()
    ):
        return BatchResult(handler="heartbeat", claimed=0, ok=0, failed=0)

    resolved = resolve_host(host)
    try:
        _collect_and_upsert(store, resolved)
    except Exception:
        log.exception("heartbeat: pass failed")
        return BatchResult(handler="heartbeat", claimed=1, ok=0, failed=1)
    _last_beat_monotonic = now
    return BatchResult(handler="heartbeat", claimed=1, ok=1, failed=0)


__all__ = [
    "collect_and_report",
    "collect_loads",
    "collect_top_cpu",
    "read_temp_c",
    "resolve_host",
    "run_heartbeat_pass",
]
