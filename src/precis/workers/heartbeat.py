"""Per-host liveness — the collection+upsert core behind ``precis heartbeat``.

Backs both the manual/cron-fired ``precis heartbeat`` CLI (launchd/systemd
timers still fire it on every node) and a ``heartbeat`` worker pass
(registry ``category="health"``, ``default_profiles=_SYS``) that runs it
once more per cycle.

**Not on** ``scheduler_leases`` (:mod:`precis.workers.scheduler`) —
heartbeat is the liveness signal the lease/claim machinery is judged by,
so it must never depend on that machinery. Self-throttles instead with a
plain in-process timestamp (module-level, not a DB round-trip) to at most
once per ``PRECIS_HEARTBEAT_INTERVAL_SECONDS`` (default 60); a double-fire
against the still-live launchd/systemd timer is a harmless idempotent
UPSERT.

Collects load average and a best-effort CPU temperature, UPSERTs one row
into ``host_heartbeat`` (migration 0017) — the web Status tab's "which
machines are alive and is any hot" source. ``host`` is
``PRECIS_HOST_NAME`` or ``socket.gethostname()``, matching the DB log
handler's identity so heartbeat and ``worker_logs`` rows agree.

Temperature is best-effort, priority order:

1. ``PRECIS_TEMP_CMD`` — shell command, first float in stdout parsed as
   °C (escape hatch for any sensor without baking platform logic here).
2. Linux ``/sys/class/thermal/thermal_zone*/temp`` (millidegrees), max
   across zones.
3. macOS — SoC thermal sensors via IOKit's HID event system (``ctypes``,
   unprivileged); old-Intel path falls back to the ``osx-cpu-temp`` brew
   binary. Apple Silicon exposes no sensor files and ``osx-cpu-temp``
   reads Intel-only SMC keys (returns 0.0), so IOKit is the only numeric
   source short of ``sudo powermetrics`` (qualitative-only there).
4. ``None`` — host still reports load + liveness.

**Per-process NAS attestation (gr248866).** macOS grants Full Disk Access per
binary, so the heartbeat's own ``_probe_nas`` only proves *its* interpreter
can read ``/opt/nas`` — another venv's python (web, MCP serve) can lose its
grant while ``meta.nas_ok`` stays true. A subprocess inherits its parent's
TCC attribution, so spawning other interpreters attests nothing: each
long-running process attests itself into ``meta.nas_ok_by_process[<process>]``
(``{ok, path, errno|err, exe, pid, ts, launched_by, grant_target}``) — the
worker via its heartbeat beat, web/serve via :func:`start_nas_attest_thread`
(UPDATE-only, never bumps ``ts``). Nursery's ``nas-denied`` detector alerts on
any fresh ``ok=false`` entry. Which grant fixes a denial depends on who
launched the process, because TCC charges access to the *responsible*
process, not to the interpreter: :func:`tcc_launch_class` reads that from
macOS's ``responsibility_get_pid_responsible_for_pid``. ``launchd`` (the
process is its own responsible process) → grant the python binary; ``ssh``
(sshd-session responsible — fleet tmux sessions started over SSH) → the
Remote Login "allow full disk access for remote users" setting; ``terminal``
→ grant the ``.app`` that launched the session. A container process does not
attest at all: the NAS reaches it through the VM's mount, and no grant on the
host binary applies. Known gaps: (i) one-shot
cron/launchd-timer interpreters are not long-running and do not attest; (ii)
a host with no ``host_heartbeat`` row (the database host, daemon-free by design) cannot
receive attestations — logged once per process; (iii) several stdio
``precis serve`` processes on one host without distinct ``PRECIS_PROCESS``
share one key, latest write wins, so one serve's denial can be overwritten by
another's success within the window.
"""

from __future__ import annotations

import glob
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from precis.store import Store

from precis.utils.hostname import CONTAINER_ID_RE
from precis.workers import activity
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")

# ── Worker boot epoch (the lease-epoch reclaim mechanism) ──────────
#
# One uuid4 minted the first time THIS process asks for it (module-global —
# NOT persisted; a fresh process gets a fresh id by construction, which is
# exactly the "this generation is provably gone" signal the epoch-reclaim
# arm in ``executors/_common.py`` needs). ``_boot_process`` is the
# ``worker_logs.process``-shaped name (``PRECIS_PROCESS`` env, e.g.
# ``precis-worker`` / ``precis-worker-agent``) this boot_id is advertised
# under — a host can run more than one profile (the agent host runs both), so
# the advertised value is keyed ``(host, process)``, not just ``host``.
_boot_id: str | None = None
_boot_process: str | None = None


def mint_boot_id(process: str | None = None) -> str:
    """Mint (once) and return this process's boot epoch.

    Idempotent per process lifetime — a second call (from a second pass
    registration site, a test, …) returns the SAME id rather than minting a
    fresh one; only a real process restart gets a new boot_id.

    ``process is None`` (no ``PRECIS_PROCESS`` env at mint time) means this
    boot_id is minted but will NEVER be advertised (:func:`_own_boot_ids_
    meta` requires both a boot_id AND a process name) — so
    ``executors/_common.py``'s epoch-reclaim arm can't ever prove this
    worker's claims are *replaced* (it correctly stamps no ``lease_boot_id``
    at all for such a worker, see ``_this_worker_lease_identity``, Finding
    3), and this worker's claims fall back to lease-expiry-only recovery.
    Logged once (this branch only runs once per process lifetime) so the
    degraded-recovery-latency tradeoff is visible in the worker's own log
    rather than silently discovered during an incident.
    """
    global _boot_id, _boot_process
    if _boot_id is None:
        _boot_id = uuid.uuid4().hex
        _boot_process = process
        if process is None:
            log.warning(
                "heartbeat: boot epoch minted without PRECIS_PROCESS — "
                "epoch reclaim disabled for this worker's claims (falls "
                "back to lease-expiry-only recovery)"
            )
    return _boot_id


def current_boot_id() -> str | None:
    """This process's minted boot_id, or ``None`` before :func:`mint_boot_id`
    has ever been called (e.g. a non-worker CLI invocation)."""
    return _boot_id


def _own_boot_ids_meta() -> dict[str, Any]:
    """This process's own ``{process: boot_id}`` contribution to
    ``host_heartbeat.meta.boot_ids`` — ``{}`` when no boot_id has been
    minted (e.g. the bare ``precis heartbeat`` CLI, which never mints one)."""
    if _boot_id and _boot_process:
        return {"boot_ids": {_boot_process: _boot_id}}
    return {}


def resolve_host(override: str | None = None) -> str:
    """Pick the reported host: flag > ``PRECIS_HOST_NAME`` > hostname."""
    if override:
        return override
    import socket

    return os.environ.get("PRECIS_HOST_NAME") or socket.gethostname()


#: The commit this process's install serves, frozen at first ask (same
#: semantics as handlers/skill.py's ``_SOURCE_GIT_INFO``: "which commit is
#: *this process* running" — a deploy bounces the worker, so a fresh
#: process is the honest moment to re-resolve). Reported as
#: ``host_heartbeat.meta.code_sha`` so the deploy-fleet-coverage condition
#: can flag a host still serving an older release (gr507870).
_code_sha: str | None = None
_code_sha_done = False


def _served_code_sha() -> str | None:
    """The commit the running install was deployed from, or ``None``.

    Lane order deliberately avoids ``handlers/skill.py``'s import weight
    and its traps: an installed wheel answers from PEP 610
    ``direct_url.json`` (the cluster's ``git+https://…@<sha>`` venvs record
    the resolved commit), a source checkout answers its HEAD sha (a venv
    inside a checkout is excluded so it can't pass the checkout's HEAD off
    as an installed wheel's identity, same as ``_dotgit_info``), and the
    image-baked ``PRECIS_GIT_SHA`` is the last resort — it describes the
    build, not whatever got installed or mounted over it since.
    """
    global _code_sha, _code_sha_done
    if _code_sha_done:
        return _code_sha
    _code_sha_done = True
    try:
        import precis

        src = Path(precis.__file__).resolve() if precis.__file__ else None
    except Exception:
        src = None
    if src is not None and "site-packages" in src.parts:
        try:
            import json
            from importlib.metadata import distribution

            raw = distribution("precis-mcp").read_text("direct_url.json")
            if raw:
                commit = (json.loads(raw).get("vcs_info") or {}).get("commit_id")
                if commit:
                    _code_sha = str(commit)
        except Exception:
            pass
    elif src is not None:
        try:
            from precis.install_watchdog import checkout_fingerprint

            for root in (src, *src.parents):
                if (root / ".git").exists():
                    sha = checkout_fingerprint(root)
                    if sha:
                        _code_sha = sha
                    break
        except Exception:
            pass
    if _code_sha is None:
        env_sha = (os.environ.get("PRECIS_GIT_SHA") or "").strip()
        if env_sha and env_sha != "unknown":
            _code_sha = env_sha
    return _code_sha


#: A Docker container booted with no ``--hostname`` gets the short (12
#: lowercase hex char) form of its container ID as ``socket.gethostname()``
#: — gr306275: a worker booted this way with no ``PRECIS_HOST_NAME``
#: advertises that throwaway ID as its fleet identity into
#: ``host_heartbeat``/``worker_logs``, and once the container is torn down,
#: nursery's host-dark detector would page critical forever for a "host" that
#: never existed — nobody is going to retire an identity they never knew about.
_CONTAINER_ID_RE = CONTAINER_ID_RE


def _resolve_host_ephemeral(override: str | None) -> tuple[str, bool]:
    """Resolve the host like :func:`resolve_host`, plus whether this
    identity is EPHEMERAL (gr306275): unset ``PRECIS_HOST_NAME`` (and no
    explicit ``override``/flag either — both are a deliberately-set
    identity, whatever shape they take) with a resolved hostname shaped
    like an unset Docker container ID.

    Logs one loud warning per call when ephemeral, so the operator sees
    it at resolve time (heartbeat cadence, not once ever) rather than only
    discovering it later via a bogus host-dark page.
    """
    resolved = resolve_host(override)
    ephemeral = (
        not override
        and not os.environ.get("PRECIS_HOST_NAME")
        and bool(_CONTAINER_ID_RE.match(resolved))
    )
    if ephemeral:
        log.warning(
            "worker hostname looks like an unset container ID — set "
            "PRECIS_HOST_NAME to join the fleet; heartbeat marked ephemeral"
        )
    return resolved, ephemeral


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


def collect_mem_used_pct() -> float | None:
    """Best-effort host memory utilisation percent (0–100), else ``None``.

    The memory half of the per-beat host utilisation sample (gr592932) —
    logged into ``host_heartbeat_log.mem_used_pct`` alongside the load
    averages. Never fails the report, same contract as
    :func:`collect_loads`.
    """
    try:
        from precis.workers.capability_probe import mem_used_pct

        return mem_used_pct()
    except Exception:
        log.warning("heartbeat: mem utilisation probe failed", exc_info=True)
        return None


def collect_mem_free_mb() -> int | None:
    """Best-effort currently-*admittable* memory in MiB, else ``None``.

    The RAM half of footprint admission's live headroom (td655172) — the
    ``mem_mb`` capacity row is static, so the claim cross-checks its RSS
    reservation against this measured number per beat. The probe nets the
    host's OS reserve (:func:`precis.workers.capability_probe.
    mem_reserve_mb`), the same subtraction the capacity row carries, so
    untracked consumers can't spend the reserve the ledger protects
    (td655171 review). Unmeasurable → the meta key stays absent and
    admission reads as dark (ledger only).
    """
    try:
        from precis.workers.capability_probe import mem_free_mb

        return mem_free_mb()
    except Exception:
        log.warning("heartbeat: mem free probe failed", exc_info=True)
        return None


def collect_vram_free_mb() -> int | None:
    """Best-effort *admittable* free VRAM of the freest single NVIDIA card
    in MiB, else ``None``.

    The VRAM half of footprint admission's live headroom (td655172) —
    untracked consumers (LLM serving, interactive use) don't decrement the
    static ``vram_mb`` row, so the claim cross-checks the reservation
    against this measured number per beat. It is the best SINGLE card's
    free (:func:`precis.workers.capability_probe.vram_free_mb`), never the
    cross-card sum — a model must fit one card — less the host-wide
    ``PRECIS_HOST_VRAM_RESERVE_MB`` reserve (``0`` by default). Absent (no
    cards / unmeasurable) → admission reads as dark on this dimension.
    """
    try:
        from precis.workers.capability_probe import vram_free_mb

        return vram_free_mb()
    except Exception:
        log.warning("heartbeat: vram free probe failed", exc_info=True)
        return None


def collect_vram_free_cards_mb() -> list[int] | None:
    """Best-effort per-card free VRAM in MiB (``nvidia-smi`` index order),
    else ``None``.

    The card-level half of live headroom (td655172 card assignment): the
    claim picks ONE card from this list — enforcing that card's own
    headroom and concurrency cap — and stamps ``meta.reserved.gpu_card``
    so the dispatch binds execution to the admitted card. Raw measured
    free per card (unlike the scalar, no reserve subtraction): the pick
    nets the reserve off the card under test itself, read from the
    ``vram_reserve_mb`` stamp
    :func:`collect_vram_reserve_mb` writes alongside this list
    (td655172 review). Absent (no
    cards / unmeasurable) → claims stay unbound, the pre-binding
    behaviour.
    """
    try:
        from precis.workers.capability_probe import vram_free_per_card_mb

        return vram_free_per_card_mb()
    except Exception:
        log.warning("heartbeat: per-card vram free probe failed", exc_info=True)
        return None


def collect_vram_reserve_mb() -> int | None:
    """This host's configured ``PRECIS_HOST_VRAM_RESERVE_MB`` reserve in
    MiB, else ``None``.

    Written alongside :func:`collect_vram_free_cards_mb` so card-bound
    admission nets the MEASURED host's reserve off the raw per-card
    free — a remote ``target_node`` claimer reading
    :func:`precis.workers.capability_probe.vram_reserve_mb` itself would
    subtract its own env's setting instead of the reservation host's
    (td655172 review). Unreadable → the key stays absent and a beat
    carrying the list without the stamp leaves claims on the unbound
    scalar path.
    """
    try:
        from precis.workers.capability_probe import vram_reserve_mb

        return vram_reserve_mb()
    except Exception:
        log.warning("heartbeat: vram reserve probe failed", exc_info=True)
        return None


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
            encoding="utf-8",
            errors="replace",
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


# ── Live admission census (gr647996, scheduler slice S7b) ─────────────
#
# The running-vs-admitted reconciliation the static ratchets (S7a) can't
# see: once per beat, count this host's real load — every process group or
# container above a threshold — and match each to an admitted holder: a
# live ``resource_slot_holds`` row (``holder`` ``host:pid`` with a matching
# holder-host), a running job leased to this host
# (``meta.compute_handle.pgid``/``pid``, ``meta.container``, or the
# deterministic ``--name`` a job launcher stamps), or a declared standing
# service — a group whose leader the *init system* manages (the systemd
# unit's MainPID on Linux, the launchd domain roster on macOS), and
# the census process's own ancestry. What matches nothing lands in
# ``host_heartbeat.meta.unadmitted``; an entry's ``beats`` counts
# consecutive beats seen, and nursery's ``unadmitted-load`` detector fires
# once it persists two beats. ``admitted_mem_mb``/``used_mem_mb`` frame the
# ledger against the machine, and ``meta.footprint_drift`` (measured RSS
# over the admitted mem_mb by more than ``_DRIFT_MARGIN``) feeds the
# predictor (S2).
#
# ``ppid == 1`` alone never declares a daemon: an orphaned workload
# reparents to init exactly the same way, and a leaderless group (leader
# reaped, members surviving) has no parentage to judge at all — both
# evade a bare-parentage test, so the init system itself must vouch
# (:func:`_declared_service_group`).
#
# ``docker``/``podman`` containers on a macOS gate host live inside the
# colima VM — host ``ps`` never sees them, so containers are their own
# census half via ``docker ps`` + ``docker stats``; on Linux a container's
# processes DO appear in ``ps``, so its host-side pids are collected too
# and any group containing one defers to the container entry (no double
# flag). A container admits through ``meta.container``, through any of its
# host-side pids matching a live hold or job handle, or through a
# deterministic job ``--name`` resolving to a running job on this host —
# a legitimately admitted container lacking ``meta.container`` is not
# flagged. ``docker run`` one-shots and ``compose run`` one-offs are
# workload and must match; long-running compose *service* containers are
# declared standing infra and skip. ``PRECIS_CENSUS_EXEMPT``
# (comma-separated regexes over comm/container names) names further
# declared residents — e.g. the LLM servers deploy declares as host
# reserves, which carry no holds by design.

#: A process group counts as real load past this RSS …
CENSUS_RSS_MB = 512.0
#: … or this summed CPU% sustained past this age.
CENSUS_CPU_PCT = 50.0
CENSUS_CPU_MIN_AGE_S = 60.0
#: Measured RSS beyond admitted × this counts as footprint drift.
_DRIFT_MARGIN = 1.25
#: A prior ``meta.unadmitted`` snapshot older than this isn't "last beat".
_PREV_FRESH_S = 300.0

_census_exempt_re: re.Pattern[str] | None = None
_census_exempt_loaded = False


def _census_exempt(name: str) -> bool:
    """``PRECIS_CENSUS_EXEMPT`` (comma-separated regexes) declares standing
    residents that carry no hold: LLM-server containers and other host
    reserves. Compiled once per process."""
    global _census_exempt_re, _census_exempt_loaded
    if not _census_exempt_loaded:
        _census_exempt_loaded = True
        raw = os.environ.get("PRECIS_CENSUS_EXEMPT", "").strip()
        _census_exempt_re = (
            re.compile("|".join(p.strip() for p in raw.split(",") if p.strip()))
            if raw
            else None
        )
    return bool(_census_exempt_re and _census_exempt_re.search(name))


def _parse_etime_seconds(text: str) -> float | None:
    """BSD ``etime`` (``[[dd-]hh:]mm:ss[.frac]``) or GNU ``etimes`` → seconds."""
    text = text.strip()
    if not text:
        return None
    days = 0.0
    if "-" in text:
        d, _, text = text.partition("-")
        try:
            days = float(d)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    secs = 0.0
    for n in nums:
        secs = secs * 60.0 + n
    return days * 86400.0 + secs


def _parse_container_mem_mb(text: str) -> float | None:
    """``docker stats`` ``MemUsage``'s used half (``"123.4MiB / 8GiB"``) → MiB."""
    used = text.split("/", 1)[0].strip()
    m = re.match(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]i?B)", used, re.IGNORECASE)
    if m is None:
        return None
    val = float(m.group(1))
    unit = m.group(2).upper()
    if unit.startswith("K"):
        return val / 1024.0
    if unit.startswith("G"):
        return val * 1024.0
    if unit.startswith("T"):
        return val * 1024.0 * 1024.0
    return val


#: Shared kwargs for the census probes. Every ``subprocess.run`` keeps a
#: literal ``ps``/``docker``/``podman`` argv head — these are the
#: read-only probes the launch-admission ratchet exempts by head+verb,
#: so no indirection may hide the argv (``tests/test_launch_admission_
#: ratchet.py`` counts an unresolvable argv as a launch site).
_PROBE_KW: dict[str, Any] = {
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
    "check": False,
}


def _collect_process_rows() -> list[dict[str, Any]] | None:
    """``ps`` census rows ``{pid, pgid, ppid, rss_mb, cpu, age_s, comm}``.

    ``etimes`` is GNU-only; macOS's BSD ``ps`` takes ``etime`` (the
    ``[[dd-]hh:]mm:ss`` form) instead — both normalise to seconds here.
    ``None`` when ``ps`` failed (the census then writes nothing rather
    than flagging from a partial table).
    """
    age_col = "etimes" if platform.system() == "Linux" else "etime"
    try:
        res = subprocess.run(
            ["ps", "-Ao", f"pid=,pgid=,ppid=,rss=,pcpu=,{age_col}=,comm="],
            timeout=5,
            **_PROBE_KW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    out = res.stdout
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        fields = line.split(None, 6)
        if len(fields) < 7:
            continue
        try:
            pid = int(fields[0])
            pgid = int(fields[1])
            ppid = int(fields[2])
            rss_kb = float(fields[3])
            cpu = float(fields[4])
        except ValueError:
            continue
        age_s = _parse_etime_seconds(fields[5])
        if age_s is None:
            age_s = 0.0
        comm = os.path.basename(fields[6].strip()) or fields[6].strip()
        rows.append(
            {
                "pid": pid,
                "pgid": pgid,
                "ppid": ppid,
                "rss_mb": rss_kb / 1024.0,
                "cpu": cpu,
                "age_s": age_s,
                "comm": comm,
            }
        )
    return rows


def _process_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold census rows into over-threshold process groups.

    A group counts when its summed RSS exceeds :data:`CENSUS_RSS_MB`, or
    its summed CPU exceeds :data:`CENSUS_CPU_PCT` sustained past
    :data:`CENSUS_CPU_MIN_AGE_S`. ``leader_pid``/``leader_ppid`` name the
    surviving leader (the member whose pid is the pgid) and its parentage;
    ``members`` maps each pid to its ppid. A group with no surviving
    leader still returns, using the oldest surviving member's age for
    the CPU threshold — its member roots carry the parentage
    :func:`_declared_service_group` judges, so an orphaned workload can't
    evade by outliving its leader.
    """
    groups: dict[int, dict[str, Any]] = {}
    for r in rows:
        g = groups.setdefault(
            r["pgid"],
            {
                "pgid": r["pgid"],
                "pids": set(),
                "members": {},
                "rss_mb": 0.0,
                "cpu": 0.0,
                "age_s": 0.0,
                "leader_pid": 0,
                "leader_ppid": 0,
                "comm": r["comm"],
            },
        )
        g["pids"].add(r["pid"])
        g["members"][r["pid"]] = r["ppid"]
        g["rss_mb"] += r["rss_mb"]
        g["cpu"] += r["cpu"]
        g["age_s"] = max(g["age_s"], r["age_s"])
        if r["pid"] == r["pgid"]:
            g["leader_pid"] = r["pid"]
            g["leader_ppid"] = r["ppid"]
            g["comm"] = r["comm"]
    return [
        g
        for g in groups.values()
        if g["pgid"] > 1
        and (
            g["rss_mb"] > CENSUS_RSS_MB
            or (g["cpu"] > CENSUS_CPU_PCT and g["age_s"] > CENSUS_CPU_MIN_AGE_S)
        )
    ]


def _launchd_service_pids() -> set[int] | None:
    """Pids of services the caller's launchd domain manages (Darwin).

    ``launchctl list`` is the unprivileged roster: a launchd-spawned
    daemon's main pid appears; a workload merely *reparented* to launchd
    never does (it is not a service). ``None`` when the query itself
    fails — the caller must treat every init-parented pid as
    indeterminate rather than flagging all of them at once.
    """
    try:
        res = subprocess.run(
            ["launchctl", "list"],
            timeout=5,
            **_PROBE_KW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    pids: set[int] = set()
    for line in res.stdout.splitlines():
        fields = line.split(None, 1)
        if not fields:
            continue
        try:
            pids.add(int(fields[0]))
        except ValueError:
            continue  # the header row and unloaded (``-``) entries
    return pids


def _init_service(pid: int, launchd: set[int] | None) -> bool | None:
    """``pid`` is itself managed by the host init system — a declared
    service, not a workload reparented to init.

    Linux uses ``/proc/<pid>/cgroup`` to identify the unit, then requires
    systemd's ``MainPID`` to equal ``pid``. Cgroup membership is inherited
    by worker children, including unadmitted orphans, so it proves no
    service identity on its own. User units query the user manager.
    macOS checks the launchd domain roster snapshot (``launchd`` from
    :func:`_launchd_service_pids`, the caller's ``gui/<uid>`` domain —
    system daemons other domains run are outside it and fall back to
    ``PRECIS_CENSUS_EXEMPT``). ``None`` = indeterminate (probe failed or
    unknown init); callers treat it as declared — a broken probe must
    not flag every standing daemon at once.
    """
    system = platform.system()
    if system == "Linux":
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return None
        units: list[str] = []
        for line in cgroup.splitlines():
            fields = line.split(":", 2)
            if len(fields) == 3 and fields[1] in {"", "name=systemd"}:
                units = [p for p in fields[2].split("/") if p.endswith(".service")]
                break
        if not units:
            return False
        user_manager = any(part.startswith("user@") for part in units[:-1])
        try:
            res = subprocess.run(
                [
                    "systemctl",
                    *(["--user"] if user_manager else []),
                    "show",
                    "--property=MainPID",
                    "--value",
                    "--",
                    units[-1],
                ],
                timeout=5,
                **_PROBE_KW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        try:
            return int(res.stdout.strip()) == pid
        except ValueError:
            return None
    if system == "Darwin":
        return None if launchd is None else pid in launchd
    return None


def _declared_service_group(g: dict[str, Any], launchd: set[int] | None) -> bool:
    """Init-managed standing service group, not a reparented workload.

    ``ppid == 1`` is necessary but never sufficient — an orphaned
    workload reparents to init identically. The init system must vouch
    for the pid (:func:`_init_service`). A group whose leader survives
    is declared when the leader is init-parented AND resolves to a
    service. A leaderless group (leader reaped, members alive) is
    declared only when every root member — a member whose parent is
    outside the group — is reparented to init AND resolves to a service;
    anything else is an orphaned workload and faces the admission match.
    An indeterminate verdict keeps the old pass rather than flagging
    every daemon on a probe outage.
    """
    if g["leader_ppid"]:
        if g["leader_ppid"] != 1:
            return False
        return _init_service(int(g["leader_pid"]), launchd) is not False
    roots = [(pid, ppid) for pid, ppid in g["members"].items() if ppid not in g["pids"]]
    return bool(roots) and all(
        ppid == 1 and _init_service(pid, launchd) is not False for pid, ppid in roots
    )


_CONTAINER_PS_FMT = (
    '{{.Names}}\t{{.ID}}\t{{.Label "com.docker.compose.project"}}'
    '\t{{.Label "com.docker.compose.oneoff"}}\t{{.Image}}'
)
_CONTAINER_STATS_FMT = "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"


def _collect_containers() -> list[dict[str, Any]]:
    """Running docker/podman containers: ``{name, project, oneoff, rss_mb,
    cpu, comm}`` — the Docker-VM half of the census (macOS ``ps`` can't see
    them). ``[]`` when no container runtime answers."""
    try:
        if shutil.which("docker"):
            res = subprocess.run(
                ["docker", "ps", "--format", _CONTAINER_PS_FMT],
                timeout=8,
                **_PROBE_KW,
            )
            st = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", _CONTAINER_STATS_FMT],
                timeout=8,
                **_PROBE_KW,
            )
        elif shutil.which("podman"):
            res = subprocess.run(
                ["podman", "ps", "--format", _CONTAINER_PS_FMT],
                timeout=8,
                **_PROBE_KW,
            )
            st = subprocess.run(
                ["podman", "stats", "--no-stream", "--format", _CONTAINER_STATS_FMT],
                timeout=8,
                **_PROBE_KW,
            )
        else:
            return []
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    out = res.stdout
    stats: dict[str, tuple[float, float]] = {}
    if st.returncode == 0:
        for line in st.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 3:
                continue
            cpu = _parse_first_float(fields[1]) or 0.0
            mem = _parse_container_mem_mb(fields[2])
            stats[fields[0].strip()] = (cpu, mem if mem is not None else 0.0)
    containers: list[dict[str, Any]] = []
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) < 2 or not fields[0].strip():
            continue
        name = fields[0].strip()
        project = fields[2].strip() if len(fields) > 2 else ""
        oneoff = len(fields) > 3 and fields[3].strip().lower() == "true"
        image = fields[4].strip() if len(fields) > 4 else name
        cpu, rss_mb = stats.get(name, (0.0, 0.0))
        containers.append(
            {
                "name": name,
                "project": project,
                "oneoff": oneoff,
                "rss_mb": rss_mb,
                "cpu": cpu,
                "comm": image,
            }
        )
    return containers


def _collect_container_pids() -> dict[int, str]:
    """Host pid → container name for every running container (Linux only).

    A container's processes appear in host ``ps`` — the init pid via
    ``inspect``, every member via ``top`` (a ``docker exec`` session gets
    its own group). Groups holding any of these pids defer to the
    container's own census entry (no double flag), and a live hold or
    job handle stamped by one of them attributes the container to an
    admission — the pid arm for admitted containers that never wrote
    ``meta.container``.
    """
    if platform.system() != "Linux":
        return {}
    try:
        if shutil.which("docker"):
            bin_ = "docker"
            ids_res = subprocess.run(["docker", "ps", "-q"], timeout=5, **_PROBE_KW)
        elif shutil.which("podman"):
            bin_ = "podman"
            ids_res = subprocess.run(["podman", "ps", "-q"], timeout=5, **_PROBE_KW)
        else:
            return {}
    except (OSError, subprocess.SubprocessError):
        return {}
    if ids_res.returncode != 0:
        return {}
    ids = [ln.strip() for ln in ids_res.stdout.splitlines() if ln.strip()]
    if not ids:
        return {}
    fmt = "{{.State.Pid}}\t{{.Name}}"
    try:
        res = (
            subprocess.run(
                ["docker", "inspect", "--format", fmt, *ids],
                timeout=8,
                **_PROBE_KW,
            )
            if bin_ == "docker"
            else subprocess.run(
                ["podman", "inspect", "--format", fmt, *ids],
                timeout=8,
                **_PROBE_KW,
            )
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if res.returncode != 0:
        return {}
    out: dict[int, str] = {}
    names: dict[str, str] = {}
    for cid, line in zip(ids, res.stdout.splitlines(), strict=False):
        pid_s, _, name = line.partition("\t")
        name = name.strip().lstrip("/")
        if name:
            names[cid] = name
        try:
            pid = int(pid_s.strip())
        except ValueError:
            continue
        if pid > 1 and name:
            out[pid] = name
    for cid, name in names.items():
        try:
            top = (
                subprocess.run(
                    ["docker", "top", cid, "-eo", "pid="], timeout=5, **_PROBE_KW
                )
                if bin_ == "docker"
                else subprocess.run(
                    ["podman", "top", cid, "-eo", "pid="], timeout=5, **_PROBE_KW
                )
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if top.returncode != 0:
            continue
        for ln in top.stdout.splitlines():
            try:
                pid = int(ln.strip())
            except ValueError:
                continue
            if pid > 1:
                out.setdefault(pid, name)
    return out


#: Deterministic ``--name`` prefixes job launchers stamp on containers,
#: → the ``meta.params`` key the numeric suffix carries. ``sandbox-`` is
#: the exception: claude_docker's ``container_name(job_id)`` names the
#: JOB row itself. Every compute producer names its container after the
#: *subject* ref it computes on — the name is that subject's per-node
#: mutex — so the suffix resolves through the running job's params, not
#: its ref_id (gr657246 review: ``struct_relax._dispatch`` stamps
#: ``precis-job-<structure_ref_id>`` — matching it against job ref_ids
#: missed every legitimate GPAW container, and could adopt a stray whose
#: number happened to equal an unrelated job's ref_id).
_JOB_CONTAINER_SUBJECT: dict[str, str] = {
    "precis-job": "structure_ref_id",  # struct_relax._dispatch
    "precis-route": "route_ref_id",  # precis_chem aizynth engine
    "precis-normalize": "route_ref_id",  # precis_chem LinChemIn normalizer
    "precis-fold": "protein_ref_id",  # precis_bio alphafold
}
_JOB_CONTAINER_RE = re.compile(
    r"(sandbox|precis-job|precis-route|precis-normalize|precis-fold)-(\d+)\Z"
)


def _own_group_chain(rows: list[dict[str, Any]]) -> set[int]:
    """Process groups of this process and its ancestors — the worker the
    heartbeat runs inside is the claimant, never a flag (same exemption
    ``leader_ppid == 1`` gives it under systemd/launchd, applied to a
    dev-terminal worker too)."""
    by_pid = {r["pid"]: r for r in rows}
    out: set[int] = set()
    pid: int | None = os.getpid()
    seen: set[int] = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        row = by_pid.get(pid)
        if row is None:
            break
        out.add(row["pgid"])
        pid = row["ppid"]
    return out


def _live_admissions(store: Any, host: str) -> dict[str, Any]:
    """Live holds covering this host + this host's running jobs.

    Holds admit a *local* pid only when the holder is this host's own —
    the stamped ``holder_host`` column when present, else the
    ``host:<pid>`` holder prefix. A row selected via ``host = this
    host`` whose holder names another host is a REMOTE client holding
    this host's slots (``local_serving._acquire_remote`` reserves
    against the serving host's row): its pid lives on the client, so
    attributing it here would let a same-numbered local pid hide
    unadmitted load. A remote hold's ``mem_mb`` units still count in
    ``admitted_mem_mb`` — this host's ledger owes them — but never in
    ``holder_mem``, the drift arm's local pid→holder map. Jobs match
    pid/pgid on ``meta.lease_host`` (the claimant minted them) and
    container identities on the execution host (``params.target_node``,
    else ``lease_host``) — ``meta.compute_handle`` ``pid``/``pgid``,
    ``meta.container``, ``ref_id`` (``sandbox-`` container ``--name``
    resolution) and ``meta.params`` subject refs (the other deterministic
    container ``--name`` prefixes — :data:`_JOB_CONTAINER_SUBJECT`).
    """
    hold_pids: set[int] = set()
    holder_mem: dict[str, int] = {}
    admitted_mem_mb = 0
    job_pgids: set[int] = set()
    job_pids: set[int] = set()
    job_containers: set[str] = set()
    job_ref_ids: set[int] = set()
    job_subjects: dict[str, set[int]] = {}
    with store.pool.connection() as conn:
        hold_rows = conn.execute(
            "SELECT holder, resource, units, host, holder_host "
            "  FROM resource_slot_holds "
            "WHERE expires_at > now() "
            "  AND (host = %(host)s OR holder LIKE %(prefix)s "
            "       OR holder_host = %(host)s)",
            {"host": host, "prefix": f"{host}:%"},
        ).fetchall()
        job_rows = conn.execute(
            "SELECT r.ref_id, "
            "       r.meta->'compute_handle' AS handle, "
            "       r.meta->>'container' AS container, "
            "       r.meta->'params' AS params, "
            "       r.meta->>'lease_host' AS lease_host "
            "  FROM refs r "
            "  JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "  JOIN tags t ON t.tag_id = rt.tag_id "
            " WHERE r.kind = 'job' AND r.retired_at IS NULL "
            "   AND t.namespace = 'STATUS' AND t.value = 'running' "
            "   AND (r.meta->>'lease_host' = %(host)s "
            "        OR r.meta->'params'->>'target_node' = %(host)s) "
            "   AND r.meta->>'compute_handle_released' IS DISTINCT FROM 'true'",
            {"host": host},
        ).fetchall()
    for holder, resource, units, row_host, holder_host in hold_rows:
        holder = str(holder)
        units = int(units)
        local = (
            str(holder_host) == host if holder_host else holder.startswith(f"{host}:")
        )
        if local:
            pid_str = holder.rsplit(":", 1)[-1]
            if pid_str.isdigit():
                hold_pids.add(int(pid_str))
            if str(resource) == "mem_mb" and str(row_host) == host:
                holder_mem[holder] = holder_mem.get(holder, 0) + units
        if str(resource) == "mem_mb" and str(row_host) == host:
            admitted_mem_mb += units
    for ref_id, handle, container, params, lease_host in job_rows:
        # The claimant (``lease_host``) minted any pid/pgid in the handle;
        # the container runs where ``params.target_node`` launched it
        # (struct_relax ssh's there), else on the claimant itself.
        target = params.get("target_node") if isinstance(params, dict) else None
        exec_host = str(target) if target else str(lease_host or "")
        local_pids = str(lease_host or "") == host
        if exec_host != host:
            container = None
            params = None
        else:
            job_ref_ids.add(int(ref_id))
        if isinstance(handle, dict) and local_pids:
            for key in ("pid", "pgid"):
                try:
                    pid = int(handle.get(key) or 0)
                except (TypeError, ValueError):
                    pid = 0
                if pid > 1:
                    job_pids.add(pid)
                    if key == "pgid":
                        job_pgids.add(pid)
        if container:
            job_containers.add(str(container))
        if isinstance(params, dict):
            for param_key in _JOB_CONTAINER_SUBJECT.values():
                try:
                    sid = int(params.get(param_key) or 0)
                except (TypeError, ValueError):
                    sid = 0
                if sid > 0:
                    job_subjects.setdefault(param_key, set()).add(sid)
    return {
        "hold_pids": hold_pids,
        "holder_mem": holder_mem,
        "admitted_mem_mb": admitted_mem_mb,
        "job_pgids": job_pgids,
        "job_pids": job_pids,
        "job_containers": job_containers,
        "job_ref_ids": job_ref_ids,
        "job_subjects": job_subjects,
    }


def _prev_unadmitted(store: Any, host: str) -> dict[str, dict[str, Any]]:
    """Last beat's ``meta.unadmitted`` keyed by entry key — the
    consecutive-beats memory for ``unadmitted-load``'s two-beat rule.
    A snapshot older than :data:`_PREV_FRESH_S` (heartbeat outage) is
    ignored, so the first beat after a gap restarts the count at one."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'unadmitted' FROM host_heartbeat "
            " WHERE host = %s "
            "   AND ts > now() - make_interval(secs => %s)",
            (host, _PREV_FRESH_S),
        ).fetchone()
    entries = row[0] if row and isinstance(row[0], list) else []
    out: dict[str, dict[str, Any]] = {}
    for e in entries:
        if isinstance(e, dict) and e.get("key"):
            out[str(e["key"])] = e
    return out


def _admission_census(store: Any, host: str) -> dict[str, Any]:
    """One beat's running-vs-admitted reconciliation → ``meta`` keys.

    ``{}`` when the ``ps`` half fails (a partial census must not flag).
    Container/ps failures degrade their own half; the DB reads degrade
    to "nothing admitted" only never — a failed read returns ``{}`` so a
    DB hiccup can't flag every daemon.
    """
    rows = _collect_process_rows()
    if rows is None:
        return {}
    try:
        admitted = _live_admissions(store, host)
        prev = _prev_unadmitted(store, host)
    except Exception:
        log.warning("heartbeat: admission ledger read failed", exc_info=True)
        return {}
    own = _own_group_chain(rows)
    launchd = _launchd_service_pids() if platform.system() == "Darwin" else None
    container_pids = _collect_container_pids()
    container_member_pids = set(container_pids)
    container_pid_names: dict[str, set[int]] = {}
    for pid, cname in container_pids.items():
        container_pid_names.setdefault(cname, set()).add(pid)
    drift: list[dict[str, Any]] = []
    unadmitted: dict[str, dict[str, Any]] = {}

    def _entry(key: str, comm: str, rss_mb: float, cpu: float) -> dict[str, Any]:
        beats = int(prev.get(key, {}).get("beats") or 0) + 1
        return {
            "key": key,
            "comm": comm[:60],
            "rss_mb": round(rss_mb, 1),
            "cpu": round(cpu, 1),
            "beats": beats,
        }

    for g in _process_groups(rows):
        pgid = int(g["pgid"])
        if pgid in own:
            continue  # the census process's own claimant ancestry
        if g["pids"] & container_member_pids:
            continue  # a container member's group — the container entry decides
        if _declared_service_group(g, launchd):
            continue  # init-managed standing service, not a reparented orphan
        if g["pids"] & admitted["hold_pids"]:
            holder_mem = admitted["holder_mem"]
            # Drift: summed mem_mb held by any holder whose pid sits in
            # this group vs the group's measured RSS.
            admitted_mb = sum(
                holder_mem.get(h, 0)
                for h in holder_mem
                if h.rsplit(":", 1)[-1].isdigit()
                and int(h.rsplit(":", 1)[-1]) in g["pids"]
            )
            if admitted_mb > 0 and g["rss_mb"] > admitted_mb * _DRIFT_MARGIN:
                drift.append(
                    {
                        "key": f"pgid:{pgid}",
                        "comm": g["comm"][:60],
                        "admitted_mb": admitted_mb,
                        "measured_mb": round(g["rss_mb"], 1),
                    }
                )
            continue
        if pgid in admitted["job_pgids"] or g["pids"] & admitted["job_pids"]:
            continue  # a running job's detached tree
        if _census_exempt(str(g["comm"])):
            continue
        key = f"pgid:{pgid}"
        unadmitted[key] = _entry(key, str(g["comm"]), g["rss_mb"], g["cpu"])
        unadmitted[key]["pgid"] = pgid

    for c in _collect_containers():
        name = str(c["name"])
        if name in admitted["job_containers"]:
            continue
        member_pids = container_pid_names.get(name)
        if member_pids and member_pids & (
            admitted["hold_pids"] | admitted["job_pids"] | admitted["job_pgids"]
        ):
            continue  # a live hold or job handle names a pid inside it
        m = _JOB_CONTAINER_RE.search(name)
        if m:
            prefix, num = m.group(1), int(m.group(2))
            subject = _JOB_CONTAINER_SUBJECT.get(prefix)
            admitted_ids = (
                admitted["job_ref_ids"]
                if subject is None
                else admitted["job_subjects"].get(subject, ())
            )
            if num in admitted_ids:
                continue  # deterministic job --name resolves to a running job
        if not c["oneoff"] and c["project"]:
            continue  # a declared compose *service* — standing infra
        if c["rss_mb"] < CENSUS_RSS_MB and c["cpu"] < CENSUS_CPU_PCT:
            continue  # below the load threshold (or stats unavailable)
        if _census_exempt(name) or _census_exempt(str(c["comm"])):
            continue
        key = f"container:{name}"
        unadmitted[key] = _entry(key, str(c["comm"]), c["rss_mb"], c["cpu"])
        unadmitted[key]["container"] = name

    used_mem_mb: int | None = None
    from precis.workers.capability_probe import _total_mem_mb, mem_used_pct

    total = _total_mem_mb()
    pct = mem_used_pct()
    if total is not None and pct is not None:
        used_mem_mb = round(total * pct / 100.0)
    meta: dict[str, Any] = {
        "unadmitted": sorted(unadmitted.values(), key=lambda e: -e["rss_mb"]),
        "admitted_mem_mb": int(admitted["admitted_mem_mb"]),
    }
    if used_mem_mb is not None:
        meta["used_mem_mb"] = used_mem_mb
    if drift:
        meta["footprint_drift"] = drift
    return meta


def _nas_probe_timeout_s() -> float:
    """``PRECIS_NAS_PROBE_TIMEOUT_SECONDS`` (default 5s) — how long
    :func:`_probe_nas` waits on its worker thread before giving up."""
    raw = os.environ.get("PRECIS_NAS_PROBE_TIMEOUT_SECONDS")
    if raw:
        try:
            val = float(raw)
        except ValueError:
            val = 0.0
        if val > 0:
            return val
    return 5.0


# ── gr270434: bounding the NAS probe against an NFS hang ───────────────────
#
# A stale NFS handle / unresponsive server makes os.scandir() block in an
# uninterruptible kernel syscall FOREVER — no Python exception, no signal,
# no thread.join(timeout=...) can preempt it from the inside. The only lever
# is to run it on a throwaway worker thread and have the CALLER stop
# waiting once the timeout elapses; the worker thread itself is abandoned
# (it cannot be killed) and may never return. That's accepted: the point is
# that _collect_and_upsert's heartbeat UPSERT proceeds regardless, so the
# process's liveness signal is never wedged behind one hung mount.
#
# ``_nas_probe_thread`` + ``_NAS_PROBE_LOCK`` guard against pile-up: if a
# previous probe is still stuck when the next tick fires (in-rotation pass
# and the dedicated heartbeat thread both call this, so overlap is real),
# we do NOT spawn a second abandoned thread per tick — we report the
# already-stuck state and move on.
_NAS_PROBE_LOCK = threading.Lock()
_nas_probe_thread: threading.Thread | None = None


def _probe_nas_body(path: str) -> dict[str, Any]:
    """The actual (blocking) scandir probe — run on a worker thread by
    :func:`_probe_nas` so a hang there can't wedge the heartbeat.

    Returns meta fields merged into the heartbeat row:
      - readable NAS      -> {'nas_ok': True,  'nas_path': <p>}
      - EPERM (the break) -> {'nas_ok': False, 'nas_path': <p>, 'nas_errno': <n>}
      - path absent       -> {}  (host doesn't mount the NAS -> no signal, never alerts)
      - other OSError     -> {'nas_probe_err': <str>, 'nas_path': <p>}  (transient; recorded, not alerted)
    """
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


def _probe_nas() -> dict[str, Any]:
    """Probe NAS readability from THIS process's context, bounded so an
    NFS hang can't wedge the caller (see the gr270434 note above).

    The heartbeat runs as ``deploy`` under launchd — the same context the
    watch/worker daemons use — so an EPERM here means every launchd/cron
    process on this host is locked out of the NAS. That happens when the
    venv python's Full Disk Access grant breaks after ``brew upgrade
    python`` changes its cdhash (OPEN-ITEMS: 'gateway daemon NAS lockout').

    Runs :func:`_probe_nas_body` on a daemon worker thread and joins with a
    ``PRECIS_NAS_PROBE_TIMEOUT_SECONDS`` bound (default 5s). Additional
    return shapes beyond :func:`_probe_nas_body`'s:
      - timed out         -> {'nas_probe_err': 'timeout', 'nas_path': <p>}
      - prior probe stuck -> {'nas_probe_err': 'previous probe still stuck', 'nas_path': <p>}
        (skips launching a second abandoned thread rather than piling one
        up per tick)
    Never raises — heartbeat liveness must not depend on the NAS.
    """
    global _nas_probe_thread
    path = os.environ.get("PRECIS_NAS_PROBE_PATH", "/opt/nas/botshome")

    with _NAS_PROBE_LOCK:
        if _nas_probe_thread is not None and _nas_probe_thread.is_alive():
            return {
                "nas_probe_err": "previous probe still stuck",
                "nas_path": path,
            }

        result: dict[str, Any] = {}

        def _run() -> None:
            result.update(_probe_nas_body(path))

        thread = threading.Thread(target=_run, name="nas-probe", daemon=True)
        _nas_probe_thread = thread
        thread.start()

    thread.join(timeout=_nas_probe_timeout_s())

    if thread.is_alive():
        # Still stuck (likely an uninterruptible NFS syscall) — stop
        # waiting and report it as a non-fatal, recorded-not-alerted
        # marker, same grain as the "other OSError" branch. Leave
        # ``_nas_probe_thread`` pointed at this thread so the NEXT call
        # sees "still stuck" above instead of leaking another thread.
        return {"nas_probe_err": "timeout", "nas_path": path}

    with _NAS_PROBE_LOCK:
        if _nas_probe_thread is thread:
            _nas_probe_thread = None
    return result


def _in_container() -> bool:
    """True inside a Docker/Podman container — no host TCC grant applies there."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


_WORKER_UNIT_RE = re.compile(
    r"precis-worker(?:-[A-Za-z0-9][A-Za-z0-9_.@-]*)?\.service\Z"
)


def collect_worker_units() -> dict[str, Any]:
    """Return a bounded snapshot of installed precis worker systemd units.

    Unit state is useful only when observed from the host, so containers and
    non-Linux systems report no snapshot. Any unavailable or malformed
    systemctl result also returns ``{}``; callers must treat that as unknown.
    """
    if platform.system() != "Linux" or _in_container():
        return {}
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {}

    observed_at = datetime.now(UTC).isoformat()
    try:
        listed = subprocess.run(
            [
                systemctl,
                "list-unit-files",
                "precis-worker*.service",
                "--no-legend",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
        if listed.returncode != 0:
            return {}

        names: list[str] = []
        for line in listed.stdout.splitlines():
            fields = line.split()
            if not fields:
                continue
            name = fields[0]
            if name.startswith("precis-worker"):
                if not _WORKER_UNIT_RE.fullmatch(name):
                    return {}
                names.append(name)
        names = sorted(set(names))
        if not names:
            return {"observed_at": observed_at, "units": {}}

        shown = subprocess.run(
            [
                systemctl,
                "show",
                *names,
                "--property=Id,ActiveState,UnitFileState",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
        if shown.returncode != 0:
            return {}
        units: dict[str, dict[str, str]] = {}
        current: dict[str, str] = {}
        blocks: list[dict[str, str]] = []
        for line in shown.stdout.splitlines() + [""]:
            if not line.strip():
                if current:
                    blocks.append(current)
                    current = {}
                continue
            key, sep, value = line.partition("=")
            if not sep or key not in {"Id", "ActiveState", "UnitFileState"}:
                return {}
            current[key] = value
        for block in blocks:
            name = block.get("Id", "")
            if (
                not _WORKER_UNIT_RE.fullmatch(name)
                or name not in names
                or name.removesuffix(".service") in units
                or not block.get("ActiveState")
                or not block.get("UnitFileState")
            ):
                return {}
            units[name.removesuffix(".service")] = {
                "active_state": block["ActiveState"],
                "unit_file_state": block["UnitFileState"],
            }
        if len(units) != len(names):
            return {}
        return {"observed_at": observed_at, "units": units}
    except (OSError, subprocess.SubprocessError, UnicodeError):
        log.warning("heartbeat: worker-unit probe failed", exc_info=True)
        return {}


def _responsible_process() -> tuple[int, str] | None:
    """``(pid, exe)`` of the process macOS TCC holds responsible for this one,
    via libSystem's (private, long-stable) ``responsibility_get_pid_responsible_for_pid``
    and ``proc_pidpath``. ``None`` off macOS or on any lookup failure."""
    if sys.platform != "darwin":
        return None
    import ctypes

    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        resp = lib.responsibility_get_pid_responsible_for_pid
        resp.restype = ctypes.c_int
        resp.argtypes = [ctypes.c_int]
        pidpath = lib.proc_pidpath
        pidpath.restype = ctypes.c_int
        pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        rpid = int(resp(os.getpid()))
        if rpid <= 0:
            return None
        buf = ctypes.create_string_buffer(4096)
        if pidpath(rpid, buf, 4096) <= 0:
            return None
        return rpid, buf.value.decode("utf-8", "replace")
    except (OSError, AttributeError):
        return None


_launch_class: dict[str, Any] | None = None


def tcc_launch_class() -> dict[str, Any]:
    """Who macOS charges this process's file access to, and so what to grant
    when it is denied — ``{'launched_by': ..., 'grant_target': ...}``.

    - ``launchd`` — its own responsible process (a launchd daemon); grant
      the interpreter, ``grant_target`` = the resolved ``sys.executable``.
    - ``ssh`` — sshd-session is responsible; the fix is the Remote Login
      "allow full disk access for remote users" setting, no per-binary grant.
    - ``terminal`` — a ``.app`` is responsible; ``grant_target`` = that app.
    - ``other`` — some other binary is responsible; ``grant_target`` = it.
    - ``unknown`` — not macOS, or the lookup failed.

    Computed once per process (the responsible process never changes).
    """
    global _launch_class
    if _launch_class is not None:
        return _launch_class
    resp = _responsible_process()
    if resp is None:
        out: dict[str, Any] = {"launched_by": "unknown", "grant_target": None}
    else:
        rpid, rexe = resp
        if rpid == os.getpid():
            out = {
                "launched_by": "launchd",
                "grant_target": os.path.realpath(sys.executable),
            }
        elif os.path.basename(rexe).startswith("sshd"):
            out = {"launched_by": "ssh", "grant_target": None}
        elif ".app/" in rexe:
            out = {
                "launched_by": "terminal",
                "grant_target": rexe[: rexe.index(".app/") + len(".app")],
            }
        else:
            out = {"launched_by": "other", "grant_target": rexe}
    _launch_class = out
    return out


def nas_attestation_entry(probe: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a :func:`_probe_nas` result into this process's
    ``nas_ok_by_process`` entry, or ``None`` when the probe returned ``{}``
    (path absent: host doesn't mount the NAS, so no signal) or when running
    in a container (the NAS comes through the VM's mount; no host grant
    applies, so a denial there has no TCC remedy to name).

    ``ok`` is ``True`` (readable), ``False`` (EPERM; carries ``errno``) or
    ``None`` (transient probe error/timeout; carries ``err``, recorded but
    never alerted). ``exe`` is the real interpreter path — FDA is granted
    per binary, so the alert must name what to re-grant.
    """
    if not probe or _in_container():
        return None
    ok: bool | None = bool(probe["nas_ok"]) if "nas_ok" in probe else None
    entry: dict[str, Any] = {"ok": ok, "path": probe.get("nas_path")}
    if ok is False and probe.get("nas_errno") is not None:
        entry["errno"] = probe["nas_errno"]
    if ok is None:
        entry["err"] = probe.get("nas_probe_err", "unknown")
    entry["exe"] = os.path.realpath(sys.executable)
    entry["pid"] = os.getpid()
    entry.update(tcc_launch_class())
    entry["ts"] = datetime.now(UTC).isoformat()
    return entry


def nas_process_key(default_process: str) -> str:
    """``PRECIS_PROCESS`` when set, else the caller's default role name."""
    return _resolve_process_name_for_boot() or default_process


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
        # shell=True is safe here: cmd is PRECIS_TEMP_CMD, an operator-set
        # deploy-time env var (launchd/systemd-timer plist) — never derived
        # from agent/request input.
        res = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        val = _parse_first_float(raw)
        if val is None:
            continue
        # /sys reports millidegrees; values >= 1000 are mC, else already C.
        readings.append(val / 1000.0 if abs(val) >= 1000 else val)
    return max(readings) if readings else None


def _pick_hottest_real_sensor(
    readings: Sequence[tuple[str | None, float]],
) -> float | None:
    """Max °C over *thermometer* readings, dropping calibration references.

    The AppleVendor temperature HID page includes ``PMU tcal`` /
    ``PMU2 tcal`` services — calibration constants pinned at ~51.8°C,
    not thermometers. Including them in the max pins the reported
    "CPU temp" at 51.8 whenever every real die sensor idles below it
    (real idle is ~37-41°C), hiding the whole idle-to-warm band. Filter
    by service name; a service whose name could not be read stays in
    (back-compat: better a possibly-bogus reading than a silent drop).
    """
    real = [v for name, v in readings if not (name and "tcal" in name.lower())]
    return max(real) if real else None


def _temp_from_macos_iokit() -> float | None:
    """Read the Apple Silicon SoC temp via IOKit's HID event system.

    macOS exposes no thermal sensor files; the SoC die sensors live
    behind the IOKit HID event system, which an unprivileged process
    can read (no sudo, no install — ``osx-cpu-temp`` reads Intel-only
    SMC keys and returns 0.0 here). We match HID services on the
    Apple-vendor temperature usage page and copy a temperature event
    from each, returning the hottest *real* reading in °C — the
    ``tcal`` calibration references on the same usage page are
    excluded (see :func:`_pick_hottest_real_sensor`).

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
        iokit.IOHIDServiceClientCopyProperty.restype = ctypes.c_void_p
        iokit.IOHIDServiceClientCopyProperty.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        iokit.IOHIDEventGetFloatValue.restype = ctypes.c_double
        iokit.IOHIDEventGetFloatValue.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]

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

        product_key = _cfstr("Product")
        name_buf = ctypes.create_string_buffer(256)

        def _svc_name(svc: ctypes.c_void_p) -> str | None:
            prop = iokit.IOHIDServiceClientCopyProperty(svc, product_key)
            if prop and cf.CFStringGetCString(
                prop, name_buf, len(name_buf), kCFStringEncodingUTF8
            ):
                return name_buf.value.decode(errors="replace")
            return None

        readings: list[tuple[str | None, float]] = []
        for i in range(cf.CFArrayGetCount(services)):
            svc = cf.CFArrayGetValueAtIndex(services, i)
            event = iokit.IOHIDServiceClientCopyEvent(
                svc, kIOHIDEventTypeTemperature, 0, 0
            )
            if not event:
                continue
            val = iokit.IOHIDEventGetFloatValue(event, temperature_field)
            if val and val > 0:
                readings.append((_svc_name(svc), val))
    except (OSError, AttributeError, ValueError):
        log.warning("heartbeat: IOKit temperature read failed", exc_info=True)
        return None
    return _pick_hottest_real_sensor(readings)


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
                encoding="utf-8",
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


@runtime_checkable
class _CapacitySink(Protocol):
    """The one store method the host-capacity publish needs
    (``Store.sync_host_capacity``). Narrow on purpose, like the ``object``
    annotation below: a test fake without it skips the publish."""

    def sync_host_capacity(
        self, host: str, capacity: dict[str, int | None]
    ) -> None: ...


def _report_resource_slots(store: object, host: str) -> str:
    """Self-probe this host's capabilities and sync ``resource_slots``.

    Best-effort: the capability map (factory scheduler slice 6b, §5.5) is a
    scheduling optimisation, never the liveness signal — a probe or write
    failure must not fail the heartbeat, so this swallows and logs. Returns
    a short ``gpu=1,podman=2`` summary for the CLI line (``n/a`` on error).

    ``store: object`` (not ``Store``) is deliberate:
    ``tests/cli/test_heartbeat.py``'s ``_RecordingStore`` covers only the
    sync/delete methods this function calls directly, narrower than
    ``Store`` — every call rides a ``try/except`` anyway (best-effort), so
    the missing methods (e.g. ``reclaim_expired_slot_holds``) degrade
    gracefully rather than needing the fake to implement them all.
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
    # Admittable host capacity (gr647996): measured mem/cpu/vram minus
    # reserves, the multi-unit hard rows admission.admit() reserves against.
    # Separate try so a capacity hiccup never blocks the capability map.
    try:
        from precis.workers.capability_probe import probe_host_capacity

        if isinstance(store, _CapacitySink):
            store.sync_host_capacity(host, probe_host_capacity())
    except Exception:
        log.warning("heartbeat: host-capacity probe/sync failed", exc_info=True)
    # Crash-safe reclaim (0118): sweep any resource_slot_holds a killed process
    # left expired, refunding the leaked units — the 2026-08-10 fleet-wide
    # llm:* outage's fix. Separate try so a sweep hiccup never blocks the
    # probe/sync above or the advertise below.
    try:
        n = store.reclaim_expired_slot_holds()  # type: ignore[attr-defined]
        if n:
            log.warning(
                "heartbeat: reclaimed %d expired resource-slot hold(s) "
                "(leaked reservations)",
                n,
            )
    except Exception:
        log.warning("heartbeat: expired-hold reclaim failed", exc_info=True)
    # Job-owned spawn holds (gr762788) naming no child pgid, of a job gone or
    # terminal for longer than the orphan reaper's grace — whether the poll
    # saw the transition or not (sweeper, cancel, a direct STATUS tag): one
    # statement, fleet-wide. A hold naming a pgid is left to the host-local
    # pass below or the TTL (td764135): its child may outlive the grace.
    # Separate try, same reasoning as above.
    try:
        from precis.workers.admission import reclaim_settled_job_holds

        n = reclaim_settled_job_holds(store)
        if n:
            log.warning("heartbeat: reclaimed %d settled job's spawn hold(s)", n)
    except Exception:
        log.warning("heartbeat: settled-job hold reclaim failed", exc_info=True)
    # Job-owned spawn holds on THIS host whose child process group is gone,
    # whatever the job's status (gr763200): a launcher SIGKILLed with its
    # child, the job reclaimed to queued and claimed elsewhere, leaves a
    # hold no poll tends; a terminal job's named child is refunded only
    # here, once its group exits (td764135). Host-local by nature
    # (a pgid means nothing on another host), so `local_host()`, never the
    # heartbeat's `host` override. Separate try, same reasoning as above.
    try:
        from precis.workers.admission import reap_gone_job_child_holds

        n = reap_gone_job_child_holds(store)
        if n:
            log.warning("heartbeat: reclaimed %d gone child's spawn hold(s)", n)
    except Exception:
        log.warning("heartbeat: gone-child hold reclaim failed", exc_info=True)
    # Advertise this host's local llama-swap models as served_by cards + llm: slots
    # so the router routes to them directly (self-gating: no local server ⇒ no-op).
    # Best-effort + separate try so a catalog blip never fails the heartbeat.
    try:
        from precis.workers.llm_serving import advertise_local_llm

        advertise_local_llm(store, host)  # type: ignore[arg-type]
    except Exception:
        log.warning("heartbeat: local-llm advertise failed", exc_info=True)
    present = {r: c for r, c in evaluated.items() if c}
    return ",".join(f"{r}={c}" for r, c in sorted(present.items())) or "none"


def _collect_and_upsert(
    store: Store, host: str, *, ephemeral: bool = False
) -> tuple[float | None, float | None, str]:
    """Collect this host's snapshot and UPSERT it into ``host_heartbeat`` +
    ``resource_slots``. Returns ``(temp_c, load1, slots_summary)`` for a
    caller to report.

    ``ephemeral`` (gr306275): stamps ``meta.ephemeral = True`` when the
    caller resolved an unset-container-ID identity — nursery's host-dark
    detector (``workers/nursery.py::_detect_host_dark``) excludes these
    rows from its symptom list, since the "host" is a torn-down container
    that will never advertise again, not a machine that went dark.
    """
    load1, load5, load15 = collect_loads()
    mem_used_pct = collect_mem_used_pct()
    temp_c = read_temp_c()
    meta: dict[str, Any] = {
        "platform": platform.system(),
        "release": platform.release(),
        "top_cpu": collect_top_cpu(),
        # Empty snapshots clear stale unit state after a failed/unsupported
        # probe; consumers can distinguish unknown from a fresh observation.
        "worker_units": collect_worker_units(),
    }
    if ephemeral:
        meta["ephemeral"] = True
    nas_probe = _probe_nas()
    meta.update(nas_probe)
    # gr248866: also attest per process, so a different interpreter losing
    # its Full Disk Access grant can't hide behind this one's top-level
    # ``nas_ok``. Same probe result (no second probe).
    nas_entry = nas_attestation_entry(nas_probe)
    if nas_entry is not None:
        meta["nas_ok_by_process"] = {nas_process_key("precis-heartbeat"): nas_entry}
    # §H boot epoch: this process's own {process: boot_id} entry, merged
    # (never clobbered) with any other process's entry on the same host row
    # by record_heartbeat's nested-merge SQL — see _heartbeat_ops.py.
    meta.update(_own_boot_ids_meta())
    # Live activity (2026-08-09 fetch_oa monopolization postmortem): this
    # process's own {process: snapshot} entry, keyed by the SAME
    # PRECIS_PROCESS source _own_boot_ids_meta uses above — so a host
    # running more than one profile (the agent host: system + agent) nested-
    # merges per-process, same as boot_ids (see record_heartbeat's UPSERT).
    # Skipped entirely pre-first-pass (empty snapshot) to avoid writing
    # noise, and when there's no process name to key it under.
    activity_snapshot = activity.snapshot()
    process = _resolve_process_name_for_boot()
    if activity_snapshot and process:
        meta["activity"] = {process: activity_snapshot}

    # db-resident-settings.md slice 4: self-report which registered env vars
    # are still set locally, so the condition registry can flag a host still
    # carrying a value now shadowed by a DB row (ansible-diet visibility).
    from precis.settings import advertised_env_presence

    env_present = advertised_env_presence()
    if env_present:
        meta["settings_env_present"] = env_present

    # gr507870: the served commit, so the deploy-fleet-coverage condition
    # can flag a host deploys stopped reaching (still heartbeating, still
    # running the old release).
    if code_sha := _served_code_sha():
        meta["code_sha"] = code_sha
    # Per-process boot version (td649547): meta.code_sha is host-wide and
    # last-writer-wins — a second worker or the bare heartbeat CLI on the
    # same host overwrites it and hides a mixed-version host. Each named
    # process also advertises its own {"sha", "at"} under a merge-
    # protected key (record_heartbeat's UPSERT, same as boot_ids), so a
    # reader sees every process's boot version and own last-beat stamp —
    # the freshness the surviving keys alone can't carry.
    if process:
        meta["code_sha_by_process"] = {
            process: {"sha": code_sha or None, "at": datetime.now(UTC).isoformat()}
        }
    # Host memory utilisation (gr592932): the latest-snapshot view of the
    # series host_heartbeat_log.mem_used_pct keeps per beat.
    if mem_used_pct is not None:
        meta["mem_used_pct"] = mem_used_pct

    # Footprint admission's live headroom (td655172): the measured
    # admittable free numbers the claim cross-checks footprint
    # reservations against — available minus the host's reserve, the same
    # subtraction the static capacity rows carry (td655171) — so
    # untracked consumers (LLM serving, interactive use) can't over-admit
    # past the static capacity rows or spend the OS reserve they protect.
    # Absent when unmeasurable — admission then reads as dark on that
    # dimension and the ledger stands alone.
    mem_free_mb = collect_mem_free_mb()
    if mem_free_mb is not None:
        meta["mem_free_mb"] = mem_free_mb
    vram_free_mb = collect_vram_free_mb()
    if vram_free_mb is not None:
        meta["vram_free_mb"] = vram_free_mb
    # Per-card list the claim binds GPU work to (td655172): absent →
    # admission keeps the unbound scalar fallback above. The reserve
    # stamp travels with it so the card pick nets THIS host's
    # PRECIS_HOST_VRAM_RESERVE_MB — a remote target_node claimer must
    # not read its own env (td655172 review).
    vram_free_cards_mb = collect_vram_free_cards_mb()
    if vram_free_cards_mb is not None:
        meta["vram_free_cards_mb"] = vram_free_cards_mb
        vram_reserve_mb = collect_vram_reserve_mb()
        if vram_reserve_mb is not None:
            meta["vram_reserve_mb"] = vram_reserve_mb

    # llm-lane-scheduler S0: on a serving host, the SGLang /metrics scrape
    # (running/queued/KV/retractions) plus host swap-in; self-gates to {}
    # everywhere else. Mirror into meta for the Now snapshot, and into
    # host_heartbeat_log below for the time-series consumers
    # (local-serving-pressure, S3's capacity controller).
    from precis.workers.serving_metrics import collect_serving_snapshot

    serving = collect_serving_snapshot()
    if serving:
        meta["serving"] = dict(serving)

    # gr647996 S7b: the running-vs-admitted census — ``meta.unadmitted``
    # (what burns without a hold/job/daemon match), ``admitted_mem_mb`` /
    # ``used_mem_mb``, ``meta.footprint_drift``. Best-effort: the census
    # is a report, never a liveness dependency.
    try:
        meta.update(_admission_census(store, host))
    except Exception:
        log.warning("heartbeat: admission census failed", exc_info=True)

    store.record_heartbeat(
        host,
        temp_c=temp_c,
        load1=load1,
        load5=load5,
        load15=load15,
        meta=meta,
    )
    # Append-only utilization history (host_heartbeat_log, migration 0113)
    # — the time-series behind ``precis stats --utilization``. Best-effort:
    # a history hiccup must never fail the liveness UPSERT above.
    try:
        store.record_heartbeat_history(
            host,
            temp_c=temp_c,
            load1=load1,
            load5=load5,
            load15=load15,
            mem_used_pct=mem_used_pct,
            retention_days=_history_retention_days(),
            **serving,
        )
    except Exception:
        log.warning("heartbeat: history append failed", exc_info=True)
    slots = _report_resource_slots(store, host)
    return temp_c, load1, slots


def _history_retention_days() -> float:
    """``PRECIS_HEARTBEAT_HISTORY_DAYS`` (default 14; <= 0 disables the
    ``host_heartbeat_log`` time-series entirely)."""
    raw = os.environ.get("PRECIS_HEARTBEAT_HISTORY_DAYS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 14.0


def collect_and_report(store: Store, host: str | None = None) -> str:
    """Unconditional collect+upsert — always fires (this is what the CLI
    ``precis heartbeat`` invokes, and the still-live launchd/systemd timers
    keep calling it directly too, until §L). Returns the CLI's summary line."""
    resolved, ephemeral = _resolve_host_ephemeral(host)
    temp_c, load1, slots = _collect_and_upsert(store, resolved, ephemeral=ephemeral)
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


def run_heartbeat_pass(
    # forwards into _collect_and_upsert (Store-typed); test_heartbeat_thread.py
    # calls this directly with a minimal _FakeStore narrower than Store.
    store: Any,
    *,
    host: str | None = None,
) -> BatchResult:
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

    resolved, ephemeral = _resolve_host_ephemeral(host)
    try:
        _collect_and_upsert(store, resolved, ephemeral=ephemeral)
    except Exception:
        log.exception("heartbeat: pass failed")
        return BatchResult(handler="heartbeat", claimed=1, ok=0, failed=1)
    _last_beat_monotonic = now
    return BatchResult(handler="heartbeat", claimed=1, ok=1, failed=0)


#: How long the thread sleeps between attempts (short — the 60s pacing
#: comes from the shared throttle above, NOT from this sleep, so a beat
#: lands promptly once the interval elapses and an env-tuned shorter
#: interval works with no thread change). A module global (not a local
#: constant) so tests can monkeypatch it down for a fast-running loop.
_THREAD_TICK_SECONDS = 5.0


def _sleep_with_stop_check(duration: float, should_stop: Callable[[], bool]) -> None:
    """Sleep ``duration`` seconds in ~1s increments, returning early the
    moment ``should_stop()`` flips — so a shutdown request is noticed
    promptly rather than sleeping through it."""
    remaining = duration
    while remaining > 0 and not should_stop():
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step


def _heartbeat_thread_body(
    dsn: str,
    host: str | None,
    should_stop: Callable[[], bool],
) -> None:
    """The ``start_heartbeat_thread`` loop body — see that function's
    docstring for the design rationale."""
    from precis.store import Store

    store: Store | None = None
    while not should_stop():
        # gr262888: name a pass stuck past the threshold (never raises).
        # A hang in this thread's own connect/pass below also loses the warning.
        activity.warn_if_stuck()
        try:
            if store is None:
                try:
                    store = Store.connect(dsn)
                except Exception:
                    log.warning(
                        "heartbeat thread: failed to connect; retrying",
                        exc_info=True,
                    )
                    store = None
            if store is not None:
                result = run_heartbeat_pass(store, host=host)
                # run_heartbeat_pass swallows its own exceptions and
                # reports them as failed=1 — that's the ONLY failure
                # signal available here, so treat it as "this store's
                # connection is suspect" and reconnect next tick.
                if result.failed > 0:
                    try:
                        store.close()
                    except Exception:
                        pass
                    store = None
        except Exception:
            # Belt-and-suspenders: a probe bug in this loop must never
            # take the whole worker down with it.
            log.warning("heartbeat thread: tick failed", exc_info=True)

        _sleep_with_stop_check(_THREAD_TICK_SECONDS, should_stop)

    if store is not None:
        try:
            store.close()
        except Exception:
            pass


def start_heartbeat_thread(
    dsn: str,
    *,
    host: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> threading.Thread:
    """Start a dedicated daemon thread that beats independently of the
    serial worker rotation (gr191264).

    ``workers/runner.py``'s ``run_loop`` runs handlers then ref_passes
    strictly serially — a single long pass (observed: an ~8-min fetch_oa
    markup-ingest batch) starves the in-rotation ``heartbeat`` pass, and the
    ``host_heartbeat`` row goes stale toward nursery's 10-minute host-dark
    threshold even though the worker is demonstrably alive. This thread
    bounds staleness to roughly ``PRECIS_HEARTBEAT_INTERVAL_SECONDS``
    regardless of how long any single rotation pass runs.

    Owns a SEPARATE ``Store.connect(dsn)`` from the main loop's store —
    psycopg connections aren't usable concurrently across threads. Calls
    the same :func:`run_heartbeat_pass` used by the in-rotation pass, so
    the module-global throttle (``_last_beat_monotonic``) is SHARED:
    whichever of thread/rotation fires most recently wins the interval and
    the other no-ops (``claimed=0``) — this is normal, not a bug, and keeps
    a double-fire against the still-live launchd/systemd timer harmless.

    Like :func:`run_heartbeat_pass` (§A), this deliberately touches no
    ``scheduler_leases`` / claim machinery — heartbeat is the liveness
    signal that machinery is judged by, so it must never depend on it. The
    thread never crashes the worker: a connect failure or an in-pass
    exception is caught and logged, and the loop just retries on the next
    tick.

    Returns the started (daemon, so it won't block process exit) thread;
    the caller usually just fires-and-forgets it.
    """
    stop = should_stop or (lambda: False)
    thread = threading.Thread(
        target=_heartbeat_thread_body,
        args=(dsn, host, stop),
        name="heartbeat",
        daemon=True,
    )
    thread.start()
    return thread


#: Seconds between a process's self-attestations (module global so tests can
#: monkeypatch it; ``PRECIS_NAS_ATTEST_INTERVAL_SECONDS`` overrides, default 600).
_NAS_ATTEST_INTERVAL_SECONDS = 600.0


def _nas_attest_interval_s() -> float:
    raw = os.environ.get("PRECIS_NAS_ATTEST_INTERVAL_SECONDS")
    if raw:
        try:
            val = float(raw)
        except ValueError:
            val = 0.0
        if val > 0:
            return val
    return _NAS_ATTEST_INTERVAL_SECONDS


def _nas_attest_thread_body(
    store: Any,
    default_process: str,
    should_stop: Callable[[], bool],
) -> None:
    warned = False
    while not should_stop():
        try:
            entry = nas_attestation_entry(_probe_nas())
            if entry is not None:
                host = resolve_host()
                key = nas_process_key(default_process)
                landed = store.record_nas_attestation(host, key, entry)
                if not landed and not warned:
                    warned = True
                    log.warning(
                        "nas attestation: no host_heartbeat row for host %r — "
                        "%s's NAS attestation has nowhere to land (host runs "
                        "no heartbeat)",
                        host,
                        key,
                    )
        except Exception:
            log.warning("nas attestation: tick failed", exc_info=True)
        _sleep_with_stop_check(_nas_attest_interval_s(), should_stop)


def start_nas_attest_thread(
    store: Any,
    *,
    default_process: str,
    should_stop: Callable[[], bool] | None = None,
) -> threading.Thread:
    """Start a daemon thread that attests THIS process's NAS readability
    on start and every ``PRECIS_NAS_ATTEST_INTERVAL_SECONDS`` (default 600)
    into ``host_heartbeat.meta.nas_ok_by_process`` (see module docstring).

    Uses the passed ``store`` (its pool is thread-safe). Never raises out of
    the thread. UPDATE-only: a host without a heartbeat row logs one warning
    per process lifetime and the loop continues silently.
    """
    stop = should_stop or (lambda: False)
    thread = threading.Thread(
        target=_nas_attest_thread_body,
        args=(store, default_process, stop),
        name="nas-attest",
        daemon=True,
    )
    thread.start()
    return thread


def advertise_boot_id_now(store: Store, *, host: str | None = None) -> str:
    """Mint (once) and immediately advertise this process's boot epoch.

    Called once at worker startup — BEFORE the first throttled
    :func:`run_heartbeat_pass` fires (see ``cli/worker.py``'s ``run()``,
    right after :func:`precis.cli.worker._record_boot_event`, the one site
    both the ``system`` and ``agent`` profiles pass through) — so a claim
    happening seconds after boot already sees this generation's boot_id
    advertised (the epoch-reclaim protocol; ``executors/_common.py``).
    Without this, the 60s heartbeat throttle would leave a stale/absent
    boot_id in ``host_heartbeat`` for up to a minute post-boot.

    Bypasses the throttle for this one call (a boot event is a one-off,
    not a hot loop) but resets the throttle clock afterwards so the next
    regular pass doesn't immediately re-fire.

    **Self-heal window (Finding 7).** ``_collect_and_upsert`` here is
    best-effort — a failed write (network blip, DB hiccup) is caught and
    logged, not raised, so a boot event never fails worker startup over a
    heartbeat write. If THIS call's advertisement fails, the boot_id stays
    minted (:func:`current_boot_id`) but unadvertised until the next
    regular :func:`run_heartbeat_pass` succeeds — at most one
    ``PRECIS_HEARTBEAT_INTERVAL_SECONDS`` (default 60s) later. Any row this
    worker claims in that window IS epoch-stealable (its stamped
    ``lease_boot_id`` has no matching advertisement — see the epoch arm's
    COALESCE-sentinel in ``executors/_common.py``), but only for that long:
    once the next heartbeat lands, the advertisement catches up and the
    epoch arm no longer treats this generation as gone. Contrast Finding
    3's fix (no ``PRECIS_PROCESS`` at all): that case never advertises and
    is handled by never stamping a ``lease_boot_id`` in the first place;
    this is the narrower, self-correcting gap of a *transient* advertise
    failure for a worker that DOES have ``PRECIS_PROCESS`` set.
    """
    global _last_beat_monotonic
    process = _resolve_process_name_for_boot()
    boot_id = mint_boot_id(process)
    resolved, ephemeral = _resolve_host_ephemeral(host)
    try:
        _collect_and_upsert(store, resolved, ephemeral=ephemeral)
    except Exception:
        log.warning("heartbeat: boot-time boot_id advertise failed", exc_info=True)
    _last_beat_monotonic = time.monotonic()
    return boot_id


def _resolve_process_name_for_boot() -> str | None:
    """``PRECIS_PROCESS`` env — the same identity ``worker_logs.process`` /
    the DB log handler use. Local import to dodge a module-load cycle
    (``utils.db_log_handler`` is a leaf module, but keeping the import
    lazy here matches this module's other lazy-import conventions)."""
    from precis.utils.db_log_handler import _resolve_process_name

    return _resolve_process_name()


__all__ = [
    "advertise_boot_id_now",
    "collect_and_report",
    "collect_loads",
    "collect_top_cpu",
    "current_boot_id",
    "mint_boot_id",
    "nas_attestation_entry",
    "read_temp_c",
    "resolve_host",
    "run_heartbeat_pass",
    "start_heartbeat_thread",
    "start_nas_attest_thread",
]
