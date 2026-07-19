"""``precis heartbeat`` — report this host's liveness + sensors.

A one-shot reporter each machine runs on a timer (launchd /
systemd-timer / cron). It collects load average and a best-effort CPU
temperature and UPSERTs one row into ``host_heartbeat`` (migration
0017). The web Status tab reads the table to show "which machines are
alive and is any of them hot".

Identity matches the DB log handler: ``host`` is ``PRECIS_HOST_NAME``
or ``socket.gethostname()`` so heartbeat rows and ``worker_logs``
rows agree on the same host name.

Temperature is genuinely hard to read portably, so it is best-effort
in priority order:

1. ``PRECIS_TEMP_CMD`` — a shell command whose stdout's first float
   is parsed as °C. The escape hatch for any sensor (IPMI, a custom
   script) without baking platform logic here.
2. Linux ``/sys/class/thermal/thermal_zone*/temp`` (millidegrees),
   max across zones.
3. macOS — read the SoC thermal sensors through IOKit's HID event
   system (``ctypes``, unprivileged, no install); on the old Intel
   path fall back to the ``osx-cpu-temp`` brew binary. Apple Silicon
   exposes no sensor files and ``osx-cpu-temp`` reads Intel-only SMC
   keys (returns 0.0), so the IOKit read is the only numeric source
   short of ``sudo powermetrics`` (which itself gives only a
   qualitative thermal-pressure level on Apple Silicon).
4. ``None`` — the host still reports load + liveness.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import platform
import re
import subprocess
from typing import Any

from precis.cli._common import resolve_dsn

log = logging.getLogger(__name__)

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``heartbeat`` subcommand."""
    p = sub.add_parser(
        "heartbeat",
        help="Report this host's load + CPU temp to host_heartbeat.",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Override the reported host name (default PRECIS_HOST_NAME / hostname).",
    )
    p.set_defaults(func=run)
    return p


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


def run(args: argparse.Namespace) -> None:
    """Collect this host's snapshot and UPSERT it into ``host_heartbeat``."""
    from precis.store import Store

    host = resolve_host(getattr(args, "host", None))
    load1, load5, load15 = collect_loads()
    temp_c = read_temp_c()
    meta: dict[str, Any] = {
        "platform": platform.system(),
        "release": platform.release(),
        "top_cpu": collect_top_cpu(),
    }

    dsn = resolve_dsn(getattr(args, "database_url", None))
    store = Store.connect(dsn)
    try:
        store.record_heartbeat(
            host,
            temp_c=temp_c,
            load1=load1,
            load5=load5,
            load15=load15,
            meta=meta,
        )
        slots = _report_resource_slots(store, host)
    finally:
        store.close()

    temp_str = f"{temp_c:.1f}C" if temp_c is not None else "n/a"
    load_str = f"{load1:.2f}" if load1 is not None else "n/a"
    print(f"heartbeat: {host} temp={temp_str} load1={load_str} slots={slots}")


def _report_resource_slots(store: object, host: str) -> str:
    """Self-probe this host's capabilities and sync ``resource_slots``.

    Best-effort: the capability map (factory scheduler slice 6b, §5.5) is a
    scheduling optimisation, never the liveness signal — a probe or write
    failure must not fail the heartbeat, so this swallows and logs. Returns
    a short ``gpu=1,podman=2`` summary for the CLI line (``n/a`` on error).
    """
    from precis.workers.capability_probe import (
        mem_capacity,
        probe_host_resources,
        probe_soft_signals,
        resource_kind,
    )

    try:
        evaluated = probe_host_resources()
        kinds = {r: resource_kind(r) for r in evaluated}
        store.sync_host_resource_slots(host, evaluated, kinds=kinds)  # type: ignore[attr-defined]
        # Soft memory-pressure gauge (6d-deferred): written free-first, read as
        # a claim veto for heavy jobs. Best-effort, same as the hard sync.
        for resource, free in probe_soft_signals().items():
            store.sync_soft_signal(host, resource, free, mem_capacity())  # type: ignore[attr-defined]
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


__all__ = [
    "add_parser",
    "collect_loads",
    "collect_top_cpu",
    "read_temp_c",
    "resolve_host",
    "run",
]
