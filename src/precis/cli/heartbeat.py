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
   is parsed as °C. The escape hatch for any sensor (macOS
   ``osx-cpu-temp``, IPMI, a custom script) without baking platform
   logic here.
2. Linux ``/sys/class/thermal/thermal_zone*/temp`` (millidegrees),
   max across zones.
3. ``None`` — the host still reports load + liveness.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import platform
import re
import subprocess

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


def _temp_from_macos_smc() -> float | None:
    """Probe macOS CPU temp via ``osx-cpu-temp`` (Homebrew, no sudo).

    macOS doesn't expose thermal sensors as files — userspace reads
    have to go through IOKit / SMC. ``osx-cpu-temp`` is a 1-binary
    Homebrew tool that does the SMC call and prints "47.5°C". On
    Apple Silicon it reads the package temp from the SoC. Returns
    ``None`` when the binary isn't installed (cluster nodes lacking
    the brew install still report load + host fine).

    The binary lives at ``/opt/homebrew/bin/osx-cpu-temp`` on Apple
    Silicon and ``/usr/local/bin/osx-cpu-temp`` on Intel. ``which``
    via ``/usr/bin/env`` walks PATH for either.
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
        if val is not None:
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
        return _temp_from_macos_smc()
    return None


def run(args: argparse.Namespace) -> None:
    """Collect this host's snapshot and UPSERT it into ``host_heartbeat``."""
    from precis.store import Store

    host = resolve_host(getattr(args, "host", None))
    load1, load5, load15 = collect_loads()
    temp_c = read_temp_c()
    meta = {"platform": platform.system(), "release": platform.release()}

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
    finally:
        store.close()

    temp_str = f"{temp_c:.1f}C" if temp_c is not None else "n/a"
    load_str = f"{load1:.2f}" if load1 is not None else "n/a"
    print(f"heartbeat: {host} temp={temp_str} load1={load_str}")


__all__ = [
    "add_parser",
    "collect_loads",
    "read_temp_c",
    "resolve_host",
    "run",
]
