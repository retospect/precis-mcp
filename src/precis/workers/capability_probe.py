"""Per-host capability + slot self-probe for the factory scheduler.

Slice 6b of `gr690514` (§5.5). Each
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

Claude and Codex lane tokens require login presence as well as a binary:
installing a CLI before logging in must not consume slice retry budgets.
Credential presence does not prove that a token is still valid upstream.

The capability vocabulary is *derived from the registry* — the union of
every ``ServiceSpec.requires`` token — so adding a ``requires={"foo"}``
service and a ``_PROBES["foo"]`` entry is the whole change; nothing here
hard-codes the service list.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path, PurePosixPath

from precis.embedder_wire import PATH_READY
from precis.plans import lanes
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

#: The soft ``container_agent`` gauge + its capacity. Unlike ``mem`` (a live
#: headroom bucket), this is a binary verified-capability readout: reported
#: ONLY on a host that opted into the container executor
#: (``PRECIS_AGENT_CONTAINER``), as ``1`` = the run-time/image/token probe
#: passes (verified) or ``0`` = opted-in-but-incapable (degraded — the selection
#: seam falls back in-proc). A host that never opted in reports nothing, so the
#: gauge means "you asked for containers here; here's whether they work", which
#: the ``/factory`` strip renders green/red instead of failing silently.
_CONTAINER_AGENT_RESOURCE = "container_agent"
_CONTAINER_AGENT_CAP = 1

#: The soft ``upload_staging`` gauge (gr537770): ``1`` on a host whose
#: ``PRECIS_UPLOAD_STAGING`` resolves (``pdf_upload.uploads_dir()`` — the
#: same predicate the ``paper_pull`` upload leg reads at run time), absent
#: otherwise. It's the capability the claim gate holds an upload-carrying
#: ``paper_pull`` for — presence, not correctness: a misconfigured share
#: still advertises and the run path surfaces the read failure.
_UPLOAD_STAGING_RESOURCE = "upload_staging"

#: The soft ``anki_sync`` gauge (gr747312): ``1`` on a host that can run the
#: AnkiWeb sync (enabled, mirror configured, and the ``anki`` wheel importable) —
#: the capability an on-demand ``anki_sync`` job is held queued for.
_ANKI_SYNC_RESOURCE = "anki_sync"

#: Nominal capacity per soft resource, for the heartbeat writer (each soft gauge
#: has its own — ``mem`` is a multi-bucket headroom, ``container_agent`` is 0/1).
_SOFT_CAPS = {_MEM_RESOURCE: _MEM_CAP, _CONTAINER_AGENT_RESOURCE: _CONTAINER_AGENT_CAP}

#: Soft gauges the heartbeat must DELETE when they drop out of
#: :func:`probe_soft_signals` (definitively absent), mirroring the hard-probe
#: delete-on-absent discipline. ``mem`` is NOT here: its absence is ``None`` =
#: "unmeasurable, leave the row". ``container_agent`` is reported only on an
#: opted-in host, so its absence means "opted out → retract the stale chip".
#: ``upload_staging`` the same: a host whose env went away stops advertising.
RETRACTABLE_SOFT_SIGNALS = frozenset(
    {_CONTAINER_AGENT_RESOURCE, _UPLOAD_STAGING_RESOURCE, _ANKI_SYNC_RESOURCE}
)


#: The per-adapter lane binaries run_slice's lane-aware placement asks
#: for (gr676054): no ServiceSpec declares them statically —
#: ``placement_requires`` substitutes the picked/pinned adapter's token
#: for the ``builder`` floor — but the heartbeat must still probe them
#: so each host advertises the lane subset it carries.
LANE_BIN_TOKENS = frozenset({"claude_bin", "codex_bin", "kimi_bin", "devin_bin"})

#: Lane-bin tokens whose probe folds a vendor login into the binary check
#: (gr782157): a host can carry the binary on PATH and still not advertise
#: the token — ``_probe_codex_bin`` also wants ``codex login status``,
#: ``_probe_devin_bin`` a ``devin auth`` login, ``_probe_claude_lane`` a
#: credential the CLI accepts. The sweeper's unschedulable alert annotates
#: these so "no host advertises codex_bin" reads as a missing login, not a
#: missing binary. ``kimi_bin`` is binary-only.
LOGIN_GATED_TOKENS: dict[str, str] = {
    "claude_bin": "a credential the claude CLI accepts (env token or login)",
    "codex_bin": "a `codex login` session",
    "devin_bin": "a `devin auth` login",
}


#: The per-repo checkout tokens a second-repo slice job asks for: no
#: ServiceSpec declares them statically — ``placement_requires`` adds one
#: per job from ``params.repo`` — but the heartbeat must still probe them
#: so each host advertises the repos it holds a clone of. Built from every
#: profile, with or without a lander.
SECOND_REPO_TOKENS = frozenset(
    lanes.repo_checkout_token(name) for name in lanes.REPO_PROFILES
)

#: The resource_slots token a host advertises when it can hand a slice
#: session a live precis bearer — the sources ``slice_env`` reads: a
#: mintable slice-token registry, the ``PRECIS_MCP_TOKEN`` env or the
#: mounted secret. Builder hosts deliberately render
#: ``PRECIS_MCP_SLICE_TOKEN_DIR=""`` and carry no shared token, so a
#: ``params.needs_mcp`` run_slice waits on a bearer host instead of
#: launching blind (gr793277).
PRECIS_MCP_RESOURCE = "precis_mcp"

#: The resource_slots token a host advertises when at least one lane
#: whose adapter carries the write precis MCP can exec on it — the lane
#: side of the ``needs_mcp`` claim floor (gr793277 review): the
#: ``builder`` floor passes a devin-only bearer host, whose submit
#: could only park the job it claimed, while a Claude bearer host that
#: could run it sits unclaimed.
MCP_LANE_RESOURCE = "mcp_lane"


def capability_vocabulary() -> frozenset[str]:
    """Every capability token any service declares via ``requires`` or
    ``placement`` (the check-only claim-gate tokens, td543917), plus the
    lane-bin tokens :data:`LANE_BIN_TOKENS` a lane-aware run_slice asks
    for per job, the per-repo checkout tokens :data:`SECOND_REPO_TOKENS`
    and the ``needs_mcp`` tokens :data:`PRECIS_MCP_RESOURCE` and
    :data:`MCP_LANE_RESOURCE`.

    The self-probe evaluates exactly this set, so a capability no host can
    provide still gets a definitive absent (deleted) row rather than being
    silently ignored.
    """
    tokens: set[str] = (
        set(LANE_BIN_TOKENS)
        | set(SECOND_REPO_TOKENS)
        | {PRECIS_MCP_RESOURCE, MCP_LANE_RESOURCE}
    )
    for spec in SERVICES:
        tokens |= set(spec.requires) | set(spec.placement)
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
            encoding="utf-8",
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
    host has its own container capability" is uniform across podman (GPU node/Linux)
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


#: Default embedder slot capacity when the readyz probe succeeds and no
#: ``PRECIS_EMBEDDER_SLOTS`` override is set. bge-m3 batches internally
#: (``EmbedHandler``/``embed_batch`` already forward-pass a whole claimed
#: batch at once), so 1 concurrent *reservation* is the safe default — a
#: host that can genuinely run more sets the override.
_EMBEDDER_DEFAULT_SLOTS = 1

#: Short — this probe is a live network round-trip to a service on THIS
#: host (loopback-ish), not a flaky remote call; a slow/hung answer should
#: read as absent quickly rather than stalling the heartbeat cycle.
_EMBEDDER_PROBE_TIMEOUT_S = 3.0


def _embedder_ready(url: str) -> bool:
    """``GET <url>/readyz`` → ``True`` on HTTP 200, ``False`` otherwise
    (non-200, connection refused, timeout — anything that isn't a clean
    ready answer). Extracted so tests can monkeypatch this one seam
    instead of mocking ``urllib`` internals (mirrors ``shutil.which`` /
    ``find_spec`` being the other probes' injectable seam)."""
    req = urllib.request.Request(url.rstrip("/") + PATH_READY, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_EMBEDDER_PROBE_TIMEOUT_S) as resp:
            return int(resp.status) == 200
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


def _probe_embedder() -> int | None:
    """Embedder slot count: ``PRECIS_EMBEDDER_SLOTS`` override, else 1 if
    this host's configured embedder (``PRECIS_EMBEDDER_URL``, the same
    endpoint :class:`precis.embedder.RemoteEmbedder` reads — the first
    endpoint when several are comma-listed) answers ``/readyz``.

    Mirrors the ``gpu``/``tts`` three-outcome discipline (module
    docstring), NOT a two-outcome present/absent probe: no
    ``PRECIS_EMBEDDER_URL`` configured at all is the definitive **absent**
    (``0``) — this host was never told to run an embedder, so there's
    nothing to advertise or retract. A URL IS configured but the probe
    ATTEMPT fails (timeout, connection refused, non-200 — anything
    :func:`_embedder_ready` returns ``False`` for) is **unknown**
    (``None``, leave the row) — a 3s ``/readyz`` timeout while the daemon
    is busy serving a large batch must not retract the slot: doing so
    deletes the ``resource_slots`` row, and the claim path's
    unadvertised-requires fallback (``_common.claim_executor_jobs``'s
    self-gating) then claims ``embed_batch`` jobs with NO reservation at
    all — the capacity gate silently disappears exactly when the host is
    busiest. The heartbeat re-advertises the instant ``/readyz`` answers
    again either way. This is bge-m3's slot (embedder-as-service's ``precis
    serve-embeddings`` daemon); the ``llm:`` rows for llama-swap-served
    models are advertised separately by :mod:`precis.workers.llm_serving`.
    """
    override = _env_slots("PRECIS_EMBEDDER_SLOTS")
    if override is not None:
        return override
    # PRECIS_EMBEDDER_URL is a Tier-1 PrecisConfig field (embedder_url) —
    # read via load_config(), not raw os.environ (docs/conventions/
    # env-vars.md; tests/test_env_config_policy.py guards this).
    from precis.config import load_config

    url = load_config().embedder_url
    if not url:
        return 0
    endpoint = url.split(",", 1)[0].strip()
    if not endpoint:
        return 0
    return _EMBEDDER_DEFAULT_SLOTS if _embedder_ready(endpoint) else None


# ── ADR-0048 sandbox-lane capabilities ──────────────────────────────────
# The four tokens the agent-fix lanes require (``diagnose_gripe``'s
# ServiceSpec: ``claude_bin`` / ``git`` / ``clones_dir`` /
# ``claude_config_mount``). Without probes these were permanently unknown
# (``None``) — never advertised, so capability-routed jobs could stall
# unroutable (docs/backlog was: capability-vocab-missing-probes). All are
# presence probes (capacity 1): the claude lane is serial per host anyway,
# so a counted slot of 1 matches how the work actually runs.


def _probe_git() -> int:
    """``git`` on PATH — 1/0, never unknown (``which`` can't error)."""
    return 1 if shutil.which("git") else 0


def _probe_claude_bin() -> int:
    """The ``claude`` CLI the agent lanes exec — ``PRECIS_CLAUDE_BIN`` (a
    name on PATH or an absolute path, the same override
    ``utils/claude_agent`` honors; a launchd daemon's PATH often misses
    it, so the full path is the escape hatch — mirrors
    :func:`container_runtime`), else ``claude`` on PATH. An explicit
    path must be an executable file — the same read
    :func:`_slice_adapters.adapter_bin_present` applies, so the
    advertised ``claude_bin`` token never outruns what a lane pick can
    exec (gr676054)."""
    configured = os.environ.get("PRECIS_CLAUDE_BIN") or "claude"
    if shutil.which(configured) or (
        os.path.isabs(configured)
        and os.path.isfile(configured)
        and os.access(configured, os.X_OK)
    ):
        return 1
    return 0


def _probe_clones_dir() -> int:
    """Clone scratch root for the fixer lanes — ``PRECIS_FIX_WORK_DIR``
    set is presence (the runner ``mkdir -p``'s under it, so the dir need
    not pre-exist); unset means this host was never configured for clone
    work. The repo allowlist (``PRECIS_FIX_REPO_DIR``/``PRECIS_FIX_REPOS``)
    stays a job-level config check, not a host capability."""
    return 1 if os.environ.get("PRECIS_FIX_WORK_DIR") else 0


def _stored_claude_auth() -> bool:
    """The non-env auth arms a daemon-spawned ``claude -p`` can resolve:
    the vaulted OAuth token (``ensure_oauth_token``'s env → ``vault.reveal``
    → bootstrap-file order, via ``get_secret``) or an interactive
    credentials file/macOS keychain item. Extracted as the test seam (mirrors
    :func:`_embedder_ready`). ``get_secret`` is best-effort and never
    raises in practice; a raised import-time error degrades to the
    local credential check rather than failing the heartbeat."""
    try:
        from precis import secrets as _secrets
        from precis.utils.claude_oauth import ENV_VAR as _oauth_var

        if _secrets.get_secret(_oauth_var):
            return True
    except Exception:
        log.warning("capability_probe: claude oauth vault lookup raised", exc_info=True)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    if os.path.isfile(os.path.join(config_dir, ".credentials.json")):
        return True
    if sys.platform != "darwin":
        return False
    # Query item metadata only: never extract the CLI's credential. A config
    # directory alone is created before login and proves nothing (gr701455).
    try:
        return (
            subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_claude_config() -> int:
    """Auth/config state for ``claude -p`` — the ``claude_config_mount``
    token: the ``CLAUDE_CODE_OAUTH_TOKEN`` env, the vault/bootstrap token,
    or an interactive credentials file/keychain item. Presence, not correctness: a
    stale keychain still advertises (the run path surfaces the 401)."""
    from precis.utils.claude_oauth import ENV_VAR

    if os.environ.get(ENV_VAR):
        return 1
    return 1 if _stored_claude_auth() else 0


def _probe_repo_checkout() -> int | None:
    """The repo checkout the dark-factory jobs work in (td543917):
    ``run_slice.slice_root()`` — ``PRECIS_SLICE_ROOT``, else the checkout
    around the installed package — resolving to a directory that holds a
    ``.git`` (a dir in a main checkout, a file in a worktree).

    ``1`` present; ``0`` when ``slice_root()`` raises (no checkout around
    the package, the case of a venv install — jo543902), the path is not a
    directory, it has no ``.git``, or ``git`` is not installed; ``None``
    (unknown, leave the row) when ``git`` timed out or failed to start for
    another reason."""
    from precis.workers.job_types import run_slice

    try:
        root = run_slice.slice_root()
    except RuntimeError:
        return 0
    except FileNotFoundError:
        return 0
    except (OSError, subprocess.SubprocessError):
        log.warning("capability_probe: repo_checkout git probe failed", exc_info=True)
        return None
    return 1 if root.is_dir() and (root / ".git").exists() else 0


#: The images ``scripts/ship`` / ``scripts/test`` need on the local docker
#: daemon: compose's ``additional_contexts: docker-image://precis-mcp:premodels``
#: dies on a host without the first, the gate container is the second
#: (gr701559).
GATE_IMAGES: tuple[str, ...] = ("precis-mcp:premodels", "precis-dev")
#: ``PRECIS_GATE_PREFLIGHT=off`` skips the gate-host checks (every gate
#: probe answers present): for a host that gates elsewhere, and the test
#: suite, which runs without docker.
GATE_PREFLIGHT_ENV = "PRECIS_GATE_PREFLIGHT"


def gate_preflight_off() -> bool:
    return os.environ.get(GATE_PREFLIGHT_ENV, "").strip().lower() in (
        "off",
        "0",
        "no",
        "false",
    )


#: ``docker inspect``'s image-not-found diagnostic, matched on a whole
#: stderr line: ``Error: No such image: <ref>`` /
#: ``Error response from daemon: No such image: <ref>`` (``object`` for
#: older daemons). Any other failure — daemon down, wrong ``DOCKER_HOST``,
#: a context this env cannot see — is "could not ask", not "absent".
#: Anchoring keeps a connect error that echoes a socket path like
#: ``unix:///tmp/no such image/docker.sock`` from reading as absent
#: (gr770035, td771780).
_IMAGE_ABSENT_RE = re.compile(
    r"^(?:error(?: response from daemon)?\s*:\s*)?no such (?:image|object)\s*:",
    re.IGNORECASE | re.MULTILINE,
)


def _image_present(image: str) -> bool | None:
    """``True``/``False`` whether the local daemon holds ``image``;
    ``None`` when the daemon could not be asked (timeout, spawn error, or
    a connection failure that is not "no such image")."""
    # ``docker`` itself: the gate is ``docker compose`` (scripts/ship), and a
    # podman-only host cannot run it.
    if shutil.which("docker") is None:
        return False
    try:
        done = subprocess.run(
            ["docker", "inspect", "--type", "image", image],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 0:
        return True
    stderr = done.stderr.decode("utf-8", "replace")
    return False if _IMAGE_ABSENT_RE.search(stderr) else None


def _probe_gate_image() -> int | None:
    """Both :data:`GATE_IMAGES` exist locally — the host can run
    ``scripts/ship`` and ``scripts/test``. ``0`` when either is missing
    (retract: a bare host must never be handed a land or a build);
    ``None`` when the daemon could not be asked (leave the row)."""
    if gate_preflight_off():
        return 1
    unknown = False
    for image in GATE_IMAGES:
        present = _image_present(image)
        if present is False:
            return 0
        unknown = unknown or present is None
    return None if unknown else 1


def _probe_uv_bin() -> int:
    """``uv`` on the PATH the slice jobs run with (user ``~/.local/bin``
    included, as ``run_slice.path_with_user_uv`` builds it)."""
    if gate_preflight_off():
        return 1
    from precis.workers.job_types import run_slice

    path = run_slice.path_with_user_uv(os.environ.get("PATH"))
    return 1 if shutil.which("uv", path=path) else 0


def gate_host_lack() -> str | None:
    """What this host lacks to run a gate — ``"image precis-dev"``,
    ``"tool uv"`` — or ``None`` when it has all of it (or could not tell:
    an unreadable daemon never refuses). The land/run submit preflight."""
    if gate_preflight_off():
        return None
    for image in GATE_IMAGES:
        if _image_present(image) is False:
            return f"image {image}"
    if _probe_uv_bin() == 0:
        return "tool uv"
    return None


def _probe_anki_sync() -> int:
    """``1`` when THIS process can run the AnkiWeb sync
    (enabled, mirror configured, and the ``anki`` wheel importable), else ``0``."""
    import importlib.util

    from precis.config import load_config

    cfg = load_config()
    return int(
        cfg.anki_enabled
        and bool(cfg.anki_mirror_dir and cfg.anki_mirror_dir.strip())
        and importlib.util.find_spec("anki") is not None
    )


#: Repos whose malformed-``PRECIS_SLICE_REPOS`` warning already fired in this
#: process (the probe runs every heartbeat; the log must not).
_SLICE_REPOS_WARNED: set[str] = set()


def _warn_once_if_slice_repos_malformed(repo: str, env_var: str) -> None:
    """Log one WARNING per repo per process when ``env_var`` is set but is not
    a JSON object (gr764103: a mangled unit env retracted the capability row
    every cycle with no trace). Names the variable and the repo, never the
    value, which may carry a path or secret. An unset variable is a normal
    unarmed host and stays quiet."""
    import json

    raw = os.environ.get(env_var, "")
    if repo == lanes.DEFAULT_REPO:
        return  # the default repo's root never reads the variable
    if not raw.strip() or repo in _SLICE_REPOS_WARNED:
        return
    try:
        malformed = not isinstance(json.loads(raw), dict)
    except ValueError:
        malformed = True
    if not malformed:
        return
    _SLICE_REPOS_WARNED.add(repo)
    log.warning(
        "capability_probe: repo_checkout_%s reads 0 because %s is set but is not "
        "a valid JSON object (value not logged); check how the service unit "
        "renders it",
        repo,
        env_var,
    )


def _make_repo_probe(name: str) -> Callable[[], int | None]:
    """The probe for ``repo_checkout_<name>``: ``run_slice.slice_root_for``
    resolving to a directory that holds a ``.git`` entry. ``1`` present; ``0``
    when it raises (``PRECIS_SLICE_REPOS`` unset, malformed or without an
    entry) or the path is not such a directory; ``None`` on an unexpected
    ``OSError`` (leave the row)."""

    def probe() -> int | None:
        from precis.workers.job_types import run_slice

        try:
            root = run_slice.slice_root_for(name)
        except (RuntimeError, FileNotFoundError):
            _warn_once_if_slice_repos_malformed(name, run_slice.SLICE_REPOS_ENV)
            return 0
        except (OSError, subprocess.SubprocessError):
            log.warning(
                "capability_probe: repo_checkout_%s probe failed", name, exc_info=True
            )
            return None
        return 1 if root.is_dir() and (root / ".git").exists() else 0

    return probe


def _probe_autocatpath() -> int:
    """The ``autocatpath`` engine importable in THIS venv (gr543909) — 1/0,
    never unknown. ``find_spec`` locates the package without importing it
    (the engine pulls numpy/scipy/ase), so a heartbeat stays cheap; a venv
    without the ``[catalyst]`` extra reads 0 and the claim gate leaves
    engine job types queued for a host that has it."""
    try:
        return 1 if find_spec("autocatpath") else 0
    except (ImportError, ValueError):
        return 0


def _probe_has_mace() -> int:
    """The ``mace`` MLIP backend importable in THIS venv — 1/0, never
    unknown. ``find_spec`` locates the package without importing it (mace
    pulls torch), so a heartbeat stays cheap; a venv carrying the engine
    but not the ``autocatpath[mace]`` extra reads 0 and the claim gate
    leaves backend job types queued for a host that has it. Distinct from
    ``autocatpath``: a host can import the engine and still produce
    garbage kinetics/modes output (gr548810: the gateway catch-all
    ``--profile all`` worker stole unpinned ``autocatpath_kinetics`` rows
    and re-solved them with no backend)."""
    try:
        return 1 if find_spec("mace") else 0
    except (ImportError, ValueError):
        return 0


def _probe_latexmk() -> int:
    """The TeX toolchain ``draft_export`` compiles through (gr592838) —
    ``PRECIS_LATEXMK_SLOTS`` override, else 1 when ``latexmk`` resolves.
    Presence, not correctness: the same ``have_latexmk()`` predicate the
    dispatch gate reads at run time; a partial TeX tree still advertises
    and the run path surfaces the compile failure."""
    override = _env_slots("PRECIS_LATEXMK_SLOTS")
    if override is not None:
        return override
    from precis.export.compile import have_latexmk

    return 1 if have_latexmk() else 0


def claude_bin_present() -> bool:
    """Binary-only seam: whether THIS host can exec ``claude``
    (``PRECIS_CLAUDE_BIN`` or ``claude`` on PATH). The LLM router's rung
    gate uses it to skip transports without an executable. The advertised
    ``claude_bin`` token uses :func:`_probe_claude_lane` to require login too."""
    return _probe_claude_bin() == 1


def claude_auth_present() -> bool:
    """Public read of the ``claude_config_mount`` probe — whether a
    non-``--bare`` ``claude -p`` can resolve a credential here (env token,
    vault/bootstrap token, or a ``.credentials.json`` file in the Claude
    config directory or a macOS keychain item). Presence,
    not correctness: a stale login still reports True (the run path surfaces
    the 401)."""
    return _probe_claude_config() == 1


def codex_bin_present() -> bool:
    """Public read of the ``codex_exec`` lane's binary probe — whether THIS
    host can exec ``codex`` (``PRECIS_CODEX_BIN`` or ``codex`` on PATH).
    Delegates to :mod:`precis.utils.codex_exec` (the transport owns the
    binary/env names); the LLM router's rung gate reads it so a codex-less
    host skips the rung at dispatch (gr543830)."""
    from precis.utils.codex_exec import codex_bin_present as _probe

    return _probe()


def codex_login_present() -> bool:
    """Public read of the ``codex_exec`` lane's login probe — a ChatGPT
    subscription login per ``codex login status`` (TTL-cached in
    :mod:`precis.utils.codex_exec`). Presence, not correctness: a stale
    login still reports True — the run path surfaces the auth failure.

    Used by the ``codex_bin`` lane token as well as the router gate."""
    from precis.utils.codex_exec import codex_login_present as _probe

    return _probe()


def devin_login_present() -> bool:
    """Public read of the ``devin_tree`` lane's login probe — the binary
    resolves and ``devin auth status`` reports a login (TTL-cached in
    :mod:`precis.workers.job_types._slice_adapters`, lazy import: the
    worker module is heavier than this probe file). Presence, not
    correctness: a stale login still reports True.

    Used by the ``devin_bin`` lane token and run_slice's pick filter."""
    from precis.workers.job_types._slice_adapters import devin_login_present as _probe

    return _probe()


def _probe_adapter_bin(adapter: str) -> int:
    """A lane adapter's binary token — resolved exactly as its argv does
    (:func:`_slice_adapters.adapter_bin_present`; lazy import: the
    worker module is heavier than this probe file)."""
    from precis.workers.job_types._slice_adapters import adapter_bin_present

    return 1 if adapter_bin_present(adapter) else 0


def _probe_codex_bin() -> int:
    """Advertise ``codex_bin`` only with a binary and subscription login."""
    return int(codex_bin_present() and codex_login_present())


def _probe_claude_lane() -> int:
    """Advertise ``claude_bin`` only with an executable and credentials.

    Installation precedes vendor login on new builders; advertising the
    executable alone spends slice retry budgets on unauthenticated launches.
    Keep the binary-only public seam for callers that check auth separately.
    """
    return int(claude_bin_present() and claude_auth_present())


def _probe_kimi_bin() -> int:
    """The ``kimi_tree`` lane's binary — the ``kimi_bin`` token."""
    return _probe_adapter_bin("kimi_tree")


def _probe_devin_bin() -> int:
    """Advertise ``devin_bin`` only with a binary and a Devin login.

    A logged-out host that advertised the lane would die in the submit
    preflight on every launch, and that refusal backs the lane off for
    the whole fleet. No binary answers 0 without running ``auth status``."""
    return int(devin_login_present())


def _probe_builder() -> int:
    """The ``builder`` token — at least one ready slice lane on this
    host: the unpinned ``run_slice`` placement floor (gr676054). The
    pick then filters to the adapters actually present.

    A process that holds ``run_slice`` (``PRECIS_SUSPENDED_JOB_TYPES``, the
    claim path's operator hold) will not claim one, so its host does not
    advertise the floor: the gateway's deploy-run units carry the hold once
    the ``<host>-coder`` builder unit takes the builds, and
    ``gripe_dispatch.build_queue_supply`` must not count their slots as
    capacity. The lane-bin tokens stay — other job types require them."""
    from precis.workers.executors import suspended_job_types

    if "run_slice" in suspended_job_types():
        return 0
    for probe in (
        _probe_claude_lane,
        _probe_codex_bin,
        _probe_kimi_bin,
        _probe_devin_bin,
    ):
        if probe() == 1:
            return 1
    return 0


def claude_agent_capable() -> bool:
    """Whether THIS host can run a ``call_claude_agent``-class workload —
    the ``claude`` binary (:func:`claude_bin_present`) OR the verified
    container lane (``PRECIS_AGENT_CONTAINER`` opted in AND
    :func:`~precis.workers.executors.agent_container.container_capability_ok`
    green — the image carries its own claude). Mirrors the rescue the LLM
    router's ``_claude_rung_unavailable`` applies to claude_agent rungs, so
    a host that can only serve them in-container is not wrongly gated off."""
    if claude_bin_present():
        return True
    from precis.workers.executors import agent_container

    return (
        agent_container.container_agent_enabled()
        and agent_container.container_capability_ok()
    )


def _registry_writable(directory: Path) -> bool:
    """``True`` when this worker could actually write a grant file into
    the slice-token registry ``directory`` — existence alone is no
    mint: a dir the mount or its mode refuses still serves every slice
    a 401 (gr793277 review). The probe file takes the mint's
    ``.td*.tmp`` name so the TTL sweep reaps it when the unlink is
    lost."""
    probe = directory / f".td-probe.{os.getpid()}.tmp"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def _probe_precis_mcp() -> int:
    """1 when a slice launched here would carry a precis bearer — the
    same sources ``slice_env`` reads: a writable slice-token registry
    the worker can mint into (:func:`_registry_writable`), the
    ``PRECIS_MCP_TOKEN`` env, or the mounted secret. Builder hosts
    render ``PRECIS_MCP_SLICE_TOKEN_DIR=""`` and hold no shared token
    by design — 0 — so a ``params.needs_mcp`` slice is routed only to a
    host that can serve the MCP (gr793277)."""
    from precis.slice_tokens import registry_dir

    directory = registry_dir()
    if directory is not None and _registry_writable(directory):
        return 1
    if os.environ.get("PRECIS_MCP_TOKEN"):
        return 1
    try:
        from precis.secrets import mounted_secret

        # Mirrors slice_env: an unreadable store is no bearer.
        if mounted_secret("PRECIS_MCP_TOKEN"):
            return 1
    except Exception:
        pass
    return 0


def _probe_mcp_lane() -> int:
    """The ``mcp_lane`` token (:data:`MCP_LANE_RESOURCE`) — 1 when at
    least one lane whose adapter carries the write precis MCP can exec
    here: the ``builder`` floor's shape, restricted to the
    ``_slice_adapters.ADAPTERS`` entries with ``mcp`` set (today
    ``claude_p`` alone; the probe map below is per-adapter, kept in sync
    the way :func:`_probe_builder`'s list is). A process holding
    ``run_slice`` does not claim one (the ``builder`` rule), so its host
    does not advertise it."""
    from precis.workers.executors import suspended_job_types
    from precis.workers.job_types._slice_adapters import ADAPTERS

    if "run_slice" in suspended_job_types():
        return 0
    probes: dict[str, Callable[[], int]] = {
        "claude_p": _probe_claude_lane,
        "codex_exec": _probe_codex_bin,
        "kimi_tree": _probe_kimi_bin,
        "devin_tree": _probe_devin_bin,
    }
    for adapter, probe in probes.items():
        if getattr(ADAPTERS.get(adapter), "mcp", False) and probe() == 1:
            return 1
    return 0


#: Capability token → its presence probe. A probe returns a capacity
#: (``> 0`` present, ``0`` absent) or ``None`` (unknown — leave the row).
_PROBES: dict[str, Callable[[], int | None]] = {
    "gpu": _probe_gpu,
    "podman": _probe_podman,
    "tts": _probe_tts,
    "embedder": _probe_embedder,
    "git": _probe_git,
    "claude_bin": _probe_claude_lane,
    "codex_bin": _probe_codex_bin,
    "kimi_bin": _probe_kimi_bin,
    "devin_bin": _probe_devin_bin,
    "builder": _probe_builder,
    "clones_dir": _probe_clones_dir,
    "claude_config_mount": _probe_claude_config,
    "repo_checkout": _probe_repo_checkout,
    "gate_image": _probe_gate_image,
    "uv_bin": _probe_uv_bin,
    "autocatpath": _probe_autocatpath,
    "has_mace": _probe_has_mace,
    "latexmk": _probe_latexmk,
    PRECIS_MCP_RESOURCE: _probe_precis_mcp,
    MCP_LANE_RESOURCE: _probe_mcp_lane,
    **{
        lanes.repo_checkout_token(_n): _make_repo_probe(_n)
        for _n in lanes.REPO_PROFILES
    },
}

#: Vocabulary tokens already warned about as probe-less — the
#: missing-probe warning fires once per process, not once per heartbeat
#: cycle (it used to repeat forever on every host).
_WARNED_MISSING_PROBES: set[str] = set()


def probe_host_resources() -> dict[str, int | None]:
    """Evaluate every capability in the vocabulary on THIS host.

    Returns ``{resource: capacity|None}`` for each vocabulary token: a
    positive capacity to advertise, ``0`` to retract, or ``None`` to leave
    the existing row alone (the probe couldn't tell). A vocabulary token
    with no registered probe is treated as unknown (``None``) and logged
    once per process — it neither advertises nor retracts until someone
    adds a probe (and the full-coverage test in
    ``tests/test_capability_probe.py`` reddens until they do).
    A probe that raises is caught and downgraded to unknown so a broken
    probe never breaks the (liveness-critical) heartbeat.
    """
    out: dict[str, int | None] = {}
    for token in sorted(capability_vocabulary()):
        probe = _PROBES.get(token)
        if probe is None:
            if token not in _WARNED_MISSING_PROBES:
                _WARNED_MISSING_PROBES.add(token)
                log.warning(
                    "capability_probe: no probe for required capability %r", token
                )
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
    return _SOFT if resource in _SOFT_CAPS else _HARD


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
        with open("/proc/meminfo", encoding="utf-8") as fh:
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
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    import re

    m = re.search(r"free percentage:\s*([0-9]+)%", res.stdout)
    return float(m.group(1)) if m else None


def _container_agent_signal() -> int | None:
    """This host's ``container_agent`` gauge, or ``None`` to omit the row.

    ``None`` on a host that did NOT opt into the container executor — the gauge
    is meaningless there, so no row is advertised. On an opted-in host it's the
    verified-capability probe reduced to a binary: ``1`` (can launch) /
    ``0`` (degraded — opted in but the run-time/image/token check fails, so the
    selection seam runs in-proc). Any probe error ⇒ ``0`` (fail-*visible*: an
    opted-in host we can't verify reads as degraded on the console, not green).
    """
    # Lazy import (intentional): ``agent_container._container_bin`` imports
    # ``capability_probe.container_runtime`` the same way, so the two modules
    # depend on each other in both directions. Both imports MUST stay
    # function-local — hoisting either to module level reintroduces a cycle.
    from precis.workers.executors.agent_container import (
        container_agent_enabled,
        container_capability_ok,
    )

    if not container_agent_enabled():
        return None
    try:
        return _CONTAINER_AGENT_CAP if container_capability_ok() else 0
    except Exception:
        log.warning("capability_probe: container_agent probe raised", exc_info=True)
        return 0


def probe_soft_signals() -> dict[str, int | None]:
    """This host's soft (advisory) signals — memory pressure + agent capability.

    Returns ``{resource: free|None}`` (``None`` = unmeasurable → leave the row).
    Separate from :func:`probe_host_resources` because soft signals are a gauge
    the heartbeat writes with :meth:`Store.sync_soft_signal` (free set directly,
    not the hard-capability delta path), and they're read as a claim veto, not
    reserved. A raising probe is swallowed to ``None`` (heartbeat is
    liveness-critical). ``container_agent`` is present only on an opted-in host
    (see :func:`_container_agent_signal`).
    """
    signals: dict[str, int | None] = {}
    try:
        signals[_MEM_RESOURCE] = _mem_free_bucket()
    except Exception:
        log.warning("capability_probe: soft-signal probe raised", exc_info=True)
        signals[_MEM_RESOURCE] = None
    container = _container_agent_signal()
    if container is not None:
        signals[_CONTAINER_AGENT_RESOURCE] = container
    try:
        from precis.ingest.pdf_upload import uploads_dir

        if uploads_dir() is not None:
            signals[_UPLOAD_STAGING_RESOURCE] = 1
    except Exception:
        # A probe error must not break the (liveness-critical) heartbeat;
        # not reporting leaves any stale row for the retractable sweep.
        log.warning("capability_probe: upload_staging probe raised", exc_info=True)
    try:
        if _probe_anki_sync() == 1:
            signals[_ANKI_SYNC_RESOURCE] = 1
    except Exception:
        log.warning("capability_probe: anki_sync probe raised", exc_info=True)
    return signals


def mem_used_pct() -> float | None:
    """This host's memory utilisation percent (0–100), or ``None``.

    The continuous complement of :func:`_mem_free_bucket`'s coarse gauge —
    the heartbeat logs it per beat into ``host_heartbeat_log.mem_used_pct``
    (gr592932's host utilisation sample; the footprint-admission headroom
    series). ``None`` on a platform the probe can't read. Deliberately
    ignores ``PRECIS_MEM_PRESSURE_FREE`` — that override pins the claim
    gauge, not the measurement.
    """
    pct = _linux_mem_avail_pct()
    if pct is None:
        pct = _macos_mem_free_pct()
    if pct is None:
        return None
    return round(max(0.0, 100.0 - pct), 1)


def _linux_mem_available_mb() -> int | None:
    """``MemAvailable`` from ``/proc/meminfo`` in MiB (Linux)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key == "MemAvailable":
                    return int(rest.strip().split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def mem_free_mb() -> int | None:
    """This host's currently *admittable* free memory in MiB, or ``None``.

    The live headroom footprint admission cross-checks an RSS reservation
    against (td655172): the ``mem_mb`` capacity row is a static total minus
    the OS reserve, so workloads that never reserved through the ledger
    (LLM serving, interactive use) don't shrink it and the claim alone
    over-admits. The measurement nets the SAME reserve —
    ``MemAvailable`` minus :func:`mem_reserve_mb` — so live headroom can't
    spend what the ledger protects (td655171 review): an untracked
    consumer that eats into the reserve vetoes admissions instead of
    silently borrowing it. A real ``0`` (available at or under the
    reserve) is not dark: it refuses memory admissions. Linux reads
    ``MemAvailable`` directly; macOS multiplies the sysctl total by
    ``memory_pressure``'s free percentage. Same override discipline as
    :func:`mem_used_pct` — ``PRECIS_MEM_PRESSURE_FREE`` pins the claim
    gauge, never this measurement.
    """
    free = _linux_mem_available_mb()
    if free is None:
        if sys.platform != "darwin":
            return None
        pct = _macos_mem_free_pct()
        total = _darwin_mem_total_mb()
        if pct is None or total is None:
            return None
        free = int(total * pct / 100.0)
    return max(0, free - mem_reserve_mb())


#: Memory the OS keeps for itself, off the admittable ``mem_mb`` capacity.
#: Code default mirroring deploy's ``host_memory_os_reserve: 10G``
#: (``roles/hardening/defaults/main.yml``), which reaches no running code;
#: ``PRECIS_HOST_MEM_RESERVE_MB`` overrides it per host.
DEFAULT_MEM_RESERVE_MB = 10240


def _reserve(name: str, default: int = 0) -> int:
    """A host reserve from ``os.environ[name]``, else ``default``."""
    value = _env_slots(name)
    return default if value is None else value


def mem_reserve_mb() -> int:
    """The OS memory reserve kept off admittable capacity —
    ``PRECIS_HOST_MEM_RESERVE_MB`` else :data:`DEFAULT_MEM_RESERVE_MB`.
    The one subtraction both the static ``mem_mb`` row
    (:func:`probe_host_capacity`) and the live headroom
    (:func:`mem_free_mb`) apply."""
    return _reserve("PRECIS_HOST_MEM_RESERVE_MB", DEFAULT_MEM_RESERVE_MB)


def vram_reserve_mb() -> int:
    """The host-wide VRAM reserve kept off admittable capacity —
    ``PRECIS_HOST_VRAM_RESERVE_MB`` else ``0``. Same pairing as
    :func:`mem_reserve_mb`: the ``vram_mb`` row, the live
    :func:`vram_free_mb` scalar, and footprint admission's per-card pick
    all subtract it — the pick reads it off the beat's
    ``vram_reserve_mb`` stamp (THIS host's own value, carried so a
    remote ``target_node`` claimer doesn't subtract its own env's —
    td655172 review), never a claimer-side call."""
    return _reserve("PRECIS_HOST_VRAM_RESERVE_MB")


def _linux_mem_total_mb() -> int | None:
    """``MemTotal`` from ``/proc/meminfo`` in MiB (Linux)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key == "MemTotal":
                    return int(rest.strip().split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _darwin_mem_total_mb() -> int | None:
    """Installed physical memory in MiB (macOS): ``sysctl -n hw.memsize``
    by absolute path, then by bare name, else ``os.sysconf`` page counts.

    A launchd unit's ``PATH`` may omit ``/usr/sbin``; the bare name is then
    not found, and an unknown total keeps the host's ``mem_mb`` row from
    ever being published. The two calls keep literal argv: they are the
    ``sysctl`` probe the launch-admission ratchet reads by its head."""
    for absolute in (True, False):
        try:
            res = (
                subprocess.run(
                    ["/usr/sbin/sysctl", "-n", "hw.memsize"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                    check=False,
                )
                if absolute
                else subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                    check=False,
                )
            )
            if res.returncode == 0:
                return int(res.stdout.strip()) // (1024 * 1024)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return pages * page_size // (1024 * 1024)


def _total_mem_mb() -> int | None:
    """This host's physical memory in MiB, or ``None`` if unreadable."""
    total = _linux_mem_total_mb()
    if total is None and sys.platform == "darwin":
        total = _darwin_mem_total_mb()
    return total


def _nvidia_smi_query(field: str) -> list[int] | None:
    """One ``nvidia-smi --query-gpu=<field>`` value per card, in MiB.

    ``None`` = the answer is unknown (call failed, unparseable — leave any
    existing row untouched); ``[]`` = definitively no NVIDIA cards (retract
    the row). The present/absent/unknown discipline the capacity rows sync
    with. One helper for both the static per-card totals and the live free
    probes below.
    """
    if shutil.which("nvidia-smi") is None:
        return []
    try:
        res = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={field}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning(
            "capability_probe: nvidia-smi %s query failed", field, exc_info=True
        )
        return None
    if res.returncode != 0:
        return None
    try:
        return [int(line.strip()) for line in res.stdout.splitlines() if line.strip()]
    except ValueError:
        return None


def _total_vram_mb() -> int | None:
    """Summed ``memory.total`` of every NVIDIA card in MiB.

    No ``nvidia-smi`` → ``0`` (no VRAM, retract the row). A failing call or
    an unparseable answer (``[N/A]`` on unified-memory cards) → ``None``
    (unknown, leave the row)."""
    cards = _nvidia_smi_query("memory.total")
    if cards is None:
        return None
    return sum(cards)


def _max_card_vram_mb() -> int | None:
    """The largest single card's ``memory.total`` in MiB — the per-card VRAM
    ceiling (:data:`precis.workers.admission.VRAM_CARD_MB`) a model's
    footprint must fit (td655172: the host-wide sum admits models no single
    card can hold). Same three-way verdict as :func:`_total_vram_mb`."""
    cards = _nvidia_smi_query("memory.total")
    if cards is None:
        return None
    return max(cards) if cards else 0


def vram_free_per_card_mb() -> list[int] | None:
    """Per-card ``memory.free`` in MiB, in ``nvidia-smi`` index order.

    The card-level view footprint admission binds a claim to (td655172 card
    assignment): the scalar :func:`vram_free_mb` says SOME card has
    headroom; this list is what picks WHICH one and enforces that card's
    own headroom and concurrency cap. Stays RAW measured free — the
    pick nets :func:`vram_reserve_mb` off the card under test, read
    from the ``vram_reserve_mb`` stamp the heartbeat writes alongside
    this list (td655172 review), and the truthful per-card number is
    what the beat
    should record. ``None`` = unmeasurable or no cards —
    the heartbeat then leaves the key off and admission stays unbound (the
    pre-binding behaviour). A ``[0, …]`` entry is a real reading: that card
    is full and must fail eligibility.
    """
    cards = _nvidia_smi_query("memory.free")
    return cards or None


def vram_free_mb() -> int | None:
    """The largest single card's *admittable* ``memory.free`` in MiB.

    The live headroom footprint admission cross-checks a VRAM reservation
    against (td655172): the ``vram_mb`` capacity row is static, so untracked
    consumers (an LLM server, an interactive session) don't shrink it and
    the ledger alone over-admits. A model must fit ONE card, so this is the
    best single card's free, never the cross-card sum (two cards with 12 GiB
    free each can't hold a 20 GiB model) — less :func:`vram_reserve_mb`,
    the same host-wide reserve the ``vram_mb`` row subtracts from the
    summed total. Applying a host-wide reserve to the best card is
    conservative (it can only under-state headroom) and is ``0`` by
    default; the per-card list :func:`vram_free_per_card_mb` reports stays
    raw, so the card pick applies the same subtraction to the card under
    test from the beat's own ``vram_reserve_mb`` stamp (td655172 review).
    ``None`` = unmeasurable or no cards — the
    heartbeat then leaves the key untouched and admission reads as dark.
    A real ``0`` means every card is fully used (or the reserve is
    exhausted) and must veto GPU claims.
    """
    cards = vram_free_per_card_mb()
    if not cards:
        return None
    return max(0, max(cards) - vram_reserve_mb())


#: Where this process's cgroup v2 membership and the unified hierarchy live
#: (module constants so tests can point them at a fixture tree).
_PROC_SELF_CGROUP = "/proc/self/cgroup"
_CGROUP_ROOT = "/sys/fs/cgroup"


def _read_cpu_max(path: str) -> int | None:
    """A cgroup v2 ``cpu.max`` (``"<quota> <period>"``) as millicores;
    ``None`` when unreadable or unlimited (``max``)."""
    try:
        with open(path, encoding="utf-8") as fh:
            quota, period = fh.read().split()[:2]
        if quota == "max":
            return None
        return int(quota) * 1000 // int(period)
    except (OSError, ValueError, ZeroDivisionError):
        return None


def _cgroup_cpu_quota_milli() -> int | None:
    """The tightest cgroup v2 CPU quota on this process's cgroup or any
    ancestor, in millicores; ``None`` on cgroup v1, no cgroup, or no quota."""
    try:
        with open(_PROC_SELF_CGROUP, encoding="utf-8") as fh:
            rel = next((ln.strip()[3:] for ln in fh if ln.startswith("0::")), None)
    except OSError:
        return None
    if rel is None:
        return None
    path = PurePosixPath(rel or "/")
    quotas: list[int] = []
    for d in (path, *path.parents):
        q = _read_cpu_max(os.path.join(_CGROUP_ROOT, str(d).lstrip("/"), "cpu.max"))
        if q is not None:
            quotas.append(q)
    return min(quotas) if quotas else None


def _available_cpu_milli() -> int | None:
    """CPUs this process may actually use, in millicores: its affinity set
    (``sched_getaffinity``, Linux) else ``os.cpu_count()``, capped by a
    readable cgroup v2 ``cpu.max`` quota."""
    affinity = getattr(os, "sched_getaffinity", None)
    try:
        n = len(affinity(0)) if affinity is not None else os.cpu_count()
    except OSError:
        n = os.cpu_count()
    cpus = None if n is None else n * 1000
    quota = _cgroup_cpu_quota_milli()
    if quota is None:
        return cpus
    return quota if cpus is None else min(cpus, quota)


def _minus(total: int | None, reserve: int) -> int | None:
    return None if total is None else max(0, total - reserve)


#: How long a capacity probe is reused. Total memory, CPUs and VRAM are
#: static between reboots, so the heartbeat re-probes (sysctl, nvidia-smi,
#: cgroup reads) at most this often instead of every beat.
_CAPACITY_TTL_S = 600.0
_capacity_cache: tuple[float, dict[str, int | None]] | None = None


def reset_capacity_cache() -> None:
    """Drop the cached :func:`probe_host_capacity` verdict (tests)."""
    global _capacity_cache
    _capacity_cache = None


def probe_host_capacity() -> dict[str, int | None]:
    """Admittable host capacity as hard multi-unit rows (gr647996).

    ``{mem_mb, cpu_milli, vram_mb, vram_card_mb}``: the measured total minus
    this host's reserve, in the units :class:`precis.workers.admission.Footprint`
    reserves — plus ``vram_card_mb``, the largest single card's total, which
    is checked by footprint admission rather than reserved (td655172).
    Same three-way verdict as :func:`probe_host_resources`
    (``> 0`` present, ``0`` retract, ``None`` leave). Reserves:
    ``PRECIS_HOST_MEM_RESERVE_MB`` (default :data:`DEFAULT_MEM_RESERVE_MB`),
    ``PRECIS_HOST_CPU_RESERVE_MILLI`` and ``PRECIS_HOST_VRAM_RESERVE_MB``
    (default 0). Cached for :data:`_CAPACITY_TTL_S`. Kept out of
    :data:`_PROBES`: these are not ``requires`` capability tokens."""
    global _capacity_cache
    now = time.monotonic()
    if _capacity_cache is not None and now - _capacity_cache[0] < _CAPACITY_TTL_S:
        return dict(_capacity_cache[1])
    from precis.workers.admission import CPU_MILLI, MEM_MB, VRAM_CARD_MB, VRAM_MB

    out = {
        MEM_MB: _minus(_total_mem_mb(), mem_reserve_mb()),
        CPU_MILLI: _minus(
            _available_cpu_milli(), _reserve("PRECIS_HOST_CPU_RESERVE_MILLI")
        ),
        VRAM_MB: _minus(_total_vram_mb(), vram_reserve_mb()),
        # Per-card ceiling, NOT a reserve target: the raw largest-card
        # total (the host-wide VRAM reserve above is about aggregate
        # headroom; per-card fit gets its own margin from the claim).
        VRAM_CARD_MB: _max_card_vram_mb(),
    }
    _capacity_cache = (now, out)
    return dict(out)


def mem_capacity() -> int:
    """Nominal capacity of the soft ``mem`` gauge (for the heartbeat writer)."""
    return _MEM_CAP


def soft_capacity(resource: str) -> int:
    """Nominal capacity of a soft gauge, for the heartbeat writer.

    Each soft resource carries its own capacity — ``mem`` is a multi-bucket
    headroom gauge, ``container_agent`` a 0/1 verified-capability flag — so the
    heartbeat must not stamp one capacity across every soft signal. Unknown
    resources default to ``1`` (a plain present/absent gauge)."""
    return _SOFT_CAPS.get(resource, 1)


__all__ = [
    "MCP_LANE_RESOURCE",
    "PRECIS_MCP_RESOURCE",
    "RETRACTABLE_SOFT_SIGNALS",
    "capability_vocabulary",
    "claude_auth_present",
    "claude_bin_present",
    "mem_capacity",
    "mem_free_mb",
    "mem_reserve_mb",
    "mem_used_pct",
    "probe_host_capacity",
    "probe_host_resources",
    "probe_soft_signals",
    "reset_capacity_cache",
    "resource_kind",
    "soft_capacity",
    "vram_free_mb",
    "vram_free_per_card_mb",
    "vram_reserve_mb",
]
