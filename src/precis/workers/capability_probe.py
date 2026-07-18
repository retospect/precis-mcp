"""Per-host capability + slot self-probe for the factory scheduler.

Slice 6b of ``docs/design/factory-console-and-scheduling.md`` (§5.5). Each
host discovers what it can do and how many parallel slots it offers; the
``heartbeat`` reporter writes the result into ``resource_slots`` every
cycle, and the scheduler (slice 6c) + the ``/factory`` console read the
same table.

**Probe for presence, not correctness.** The cheapest launchable signal —
a ``which``, a ``find_spec``, ``nvidia-smi -L`` — never a full exercise.
Three outcomes per capability, and the sync discipline turns on the
distinction:

* **present** — a positive capacity (``> 0``); the row is UPSERTed.
* **absent** — ``0``; the host definitively can't (no ``nvidia-smi`` binary,
  no ``podman``); the row is deleted so the capability stops advertising.
* **unknown** — ``None``; the probe's tool errored/timed out and we can't
  tell. The row is left *untouched* — a transient ``nvidia-smi`` hiccup must
  not retract a GPU the host really has (and, once 6c lands, drop its live
  reservations).

The capability vocabulary is *derived from the registry* — the union of
every ``ServiceSpec.requires`` token — so adding a ``requires={"foo"}``
service and a ``_PROBES["foo"]`` entry is the whole change; nothing here
hard-codes the service list.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from importlib.util import find_spec

from precis.workers.registry import SERVICES

log = logging.getLogger(__name__)

#: Reservation discipline per resource (mirrors ``resource_slots.kind``).
#: The 6b capability probes are ``hard`` (counted); the memory-pressure signal
#: (6d-deferred) is ``soft`` — advisory, over-commitable, a claim *veto* rather
#: than a counted slot.
_HARD = "hard"
_SOFT = "soft"

#: The soft memory-pressure resource + its nominal capacity. ``free`` is a
#: coarse headroom bucket (``_MEM_CAP`` = plenty … ``0`` = under pressure); the
#: claim vetoes requires-bearing (heavy) jobs on a host whose ``mem`` free is 0.
_MEM_RESOURCE = "mem"
_MEM_CAP = 2


def capability_vocabulary() -> frozenset[str]:
    """Every capability token any service declares via ``requires``.

    The self-probe evaluates exactly this set, so a capability no host can
    provide still gets a definitive absent (deleted) row rather than being
    silently ignored.
    """
    tokens: set[str] = set()
    for spec in SERVICES:
        tokens |= set(spec.requires)
    return frozenset(tokens)


def _env_slots(name: str) -> int | None:
    """Parse a positive-int slot override from ``os.environ[name]``.

    Returns the int (clamped ``>= 0``) when set and parseable, else ``None``
    (fall through to the real probe / default).
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("capability_probe: %s=%r is not an int", name, raw)
        return None


def _probe_gpu() -> int | None:
    """GPU slot count: ``PRECIS_GPU_COUNT`` override, else ``nvidia-smi -L``.

    No ``nvidia-smi`` on PATH → ``0`` (definitively no CUDA GPU — every Mac
    node). The binary present but erroring/timing out → ``None`` (unknown,
    keep whatever row exists). One row per physical GPU is the slot count,
    so GPU work auto-serialises at capacity.
    """
    override = _env_slots("PRECIS_GPU_COUNT")
    if override is not None:
        return override
    if shutil.which("nvidia-smi") is None:
        return 0
    try:
        res = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("capability_probe: nvidia-smi failed", exc_info=True)
        return None
    if res.returncode != 0:
        log.warning("capability_probe: nvidia-smi -L exited %d", res.returncode)
        return None
    return sum(1 for line in res.stdout.splitlines() if line.strip().startswith("GPU "))


def container_runtime() -> str | None:
    """The container CLI this host can actually run (``podman``/``docker``), or
    ``None``. Resolution: an explicit ``PRECIS_CONTAINER_BIN`` /
    ``PRECIS_PODMAN_BIN`` (a name on PATH or an absolute path — OrbStack's
    ``docker`` often isn't on a launchd daemon's PATH, so a full path is the
    escape hatch), else ``podman`` then ``docker`` on PATH. One detector shared
    by the capability probe and :func:`agent_container._container_bin`, so "each
    host has its own container capability" is uniform across podman (spark/Linux)
    and docker (OrbStack on the Macs)."""
    explicit = os.environ.get("PRECIS_CONTAINER_BIN") or os.environ.get(
        "PRECIS_PODMAN_BIN"
    )
    if explicit:
        return (
            explicit if (shutil.which(explicit) or os.path.exists(explicit)) else None
        )
    if shutil.which("podman"):
        return "podman"
    if shutil.which("docker"):
        return "docker"
    return None


def _probe_podman() -> int:
    """Container-agent slots: ``PRECIS_PODMAN_SLOTS`` override, else 2 if a
    container runtime (podman OR docker/OrbStack) is reachable, else ``0``.

    Presence is a hard runtime check (:func:`container_runtime`); the default
    concurrency (2) is a provisioning choice slice 6c/the console can retune per
    host — bounded because container agent jobs are heavy.
    """
    override = _env_slots("PRECIS_PODMAN_SLOTS")
    if override is not None:
        return override
    return 2 if container_runtime() else 0


def _probe_tts() -> int:
    """TTS render slots: ``PRECIS_TTS_SLOTS`` override, else 1 if this host
    can render — the container path (``PRECIS_TTS_IMAGE`` + ``podman``) or
    the local ``[tts]`` extra (``kokoro_onnx`` importable) — else ``0``.

    Default concurrency 1: episode rendering is serial-ish and RAM-heavy.
    """
    override = _env_slots("PRECIS_TTS_SLOTS")
    if override is not None:
        return override
    if os.environ.get("PRECIS_TTS_IMAGE") and container_runtime():
        return 1
    try:
        if find_spec("kokoro_onnx") is not None:
            return 1
    except (ImportError, ValueError):  # pragma: no cover - defensive
        log.warning("capability_probe: kokoro_onnx find_spec failed", exc_info=True)
    return 0


#: Capability token → its presence probe. A probe returns a capacity
#: (``> 0`` present, ``0`` absent) or ``None`` (unknown — leave the row).
_PROBES: dict[str, Callable[[], int | None]] = {
    "gpu": _probe_gpu,
    "podman": _probe_podman,
    "tts": _probe_tts,
}


def probe_host_resources() -> dict[str, int | None]:
    """Evaluate every capability in the vocabulary on THIS host.

    Returns ``{resource: capacity|None}`` for each vocabulary token: a
    positive capacity to advertise, ``0`` to retract, or ``None`` to leave
    the existing row alone (the probe couldn't tell). A vocabulary token
    with no registered probe is treated as unknown (``None``) and logged
    once — it neither advertises nor retracts until someone adds a probe.
    A probe that raises is caught and downgraded to unknown so a broken
    probe never breaks the (liveness-critical) heartbeat.
    """
    out: dict[str, int | None] = {}
    for token in sorted(capability_vocabulary()):
        probe = _PROBES.get(token)
        if probe is None:
            log.warning("capability_probe: no probe for required capability %r", token)
            out[token] = None
            continue
        try:
            out[token] = probe()
        except Exception:
            # A broken probe must never break the (liveness-critical) heartbeat.
            log.warning("capability_probe: probe %r raised", token, exc_info=True)
            out[token] = None
    return out


def resource_kind(resource: str) -> str:
    """Reservation discipline for a resource (``hard``/``soft``).

    The 6b capability probes are ``hard`` (counted, refuse-past-0); the
    memory-pressure signal is ``soft`` (advisory — a claim veto, not a slot).
    """
    return _SOFT if resource == _MEM_RESOURCE else _HARD


def _mem_free_bucket() -> int | None:
    """Coarse free-memory headroom on THIS host: ``_MEM_CAP`` (plenty) … ``0``
    (under pressure), or ``None`` when it can't be measured (leave the row).

    Precedence: the ``PRECIS_MEM_PRESSURE_FREE`` override (tests / manual pin),
    then Linux ``/proc/meminfo`` (``MemAvailable / MemTotal``), then a best-
    effort macOS ``memory_pressure`` parse. Thresholds: ``< 10%`` free → 0
    (critical, veto heavy claims), ``< 25%`` → 1 (warn), else ``_MEM_CAP``.
    """
    override = _env_slots("PRECIS_MEM_PRESSURE_FREE")
    if override is not None:
        return min(_MEM_CAP, override)
    pct = _linux_mem_avail_pct()
    if pct is None:
        pct = _macos_mem_free_pct()
    if pct is None:
        return None
    if pct < 10.0:
        return 0
    if pct < 25.0:
        return 1
    return _MEM_CAP


def _linux_mem_avail_pct() -> float | None:
    """``MemAvailable / MemTotal`` as a percent from ``/proc/meminfo`` (Linux)."""
    try:
        with open("/proc/meminfo") as fh:
            fields: dict[str, float] = {}
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    fields[key] = float(rest.strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    total = fields.get("MemTotal")
    avail = fields.get("MemAvailable")
    if not total or avail is None:
        return None
    return 100.0 * avail / total


def _macos_mem_free_pct() -> float | None:
    """Best-effort free-memory percent from macOS ``memory_pressure`` output."""
    if shutil.which("memory_pressure") is None:
        return None
    try:
        res = subprocess.run(
            ["memory_pressure", "-Q"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    import re

    m = re.search(r"free percentage:\s*([0-9]+)%", res.stdout)
    return float(m.group(1)) if m else None


def probe_soft_signals() -> dict[str, int | None]:
    """This host's soft (advisory) signals — currently memory pressure.

    Returns ``{resource: free|None}`` (``None`` = unmeasurable → leave the row).
    Separate from :func:`probe_host_resources` because soft signals are a gauge
    the heartbeat writes with :meth:`Store.sync_soft_signal` (free set directly,
    not the hard-capability delta path), and they're read as a claim veto, not
    reserved. A raising probe is swallowed to ``None`` (heartbeat is
    liveness-critical).
    """
    try:
        return {_MEM_RESOURCE: _mem_free_bucket()}
    except Exception:
        log.warning("capability_probe: soft-signal probe raised", exc_info=True)
        return {_MEM_RESOURCE: None}


def mem_capacity() -> int:
    """Nominal capacity of the soft ``mem`` gauge (for the heartbeat writer)."""
    return _MEM_CAP


__all__ = [
    "capability_vocabulary",
    "mem_capacity",
    "probe_host_resources",
    "probe_soft_signals",
    "resource_kind",
]
