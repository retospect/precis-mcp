"""sandbox_run ``mode:script`` — the executor-side PyPI lock + wheel stage.

The script container runs ``--network none``, so its dependencies must
already be on disk when it starts. The executor (trusted, has network)
does the PyPI work, called by
:func:`precis.workers.executors.claude_docker._launch_script`:

1. ``uv pip compile --no-build --generate-hashes --python-version 3.12``
   the ``deps`` against PyPI. ``--no-build`` is the security line: without
   it uv may build an sdist to read its metadata, which runs an
   agent-chosen package's build backend on this host, outside the
   container. A lock pinning more than ``PRECIS_SANDBOX_MAX_PACKAGES``
   (default 50) packages is refused. ``--no-config`` + an explicit
   ``--default-index``, so a host ``uv.toml`` or ``UV_INDEX_URL`` cannot
   point it elsewhere; ``--no-header`` so the lock text, and therefore its
   sha, depends only on the resolution).
2. ``pip download --only-binary=:all: --no-deps -r lock`` into a wheel
   dir keyed by the lock's sha256 (uv has no ``pip download``). The lock
   carries hashes, so pip runs in hash-checking mode. Sdists are refused:
   the offline container cannot fetch build dependencies. Wheels totalling
   more than ``PRECIS_SANDBOX_MAX_WHEEL_MB`` (default 500 MB) are refused
   and discarded.

The wheel dir is reused across jobs with the same lock sha: a
``.complete`` marker is written only after a download finished, into a
temporary sibling that is renamed into place, so a crashed download
never leaves a half-filled dir that a later job would trust. The
container mounts it read-only.

Binaries: ``PRECIS_SANDBOX_UV`` (default ``uv``) and
``PRECIS_SANDBOX_PIP`` (a shell-split command, default
``python3 -m pip``) — the worker venv ships no pip, so the host's pip is
what runs; a missing one fails the job with a message naming the
override. Wheel cache root: ``PRECIS_SANDBOX_WHEEL_CACHE``, default
``<PRECIS_SANDBOX_WORK_DIR>/wheels``.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: The code-task image's interpreter; there is no ``python`` param.
SCRIPT_PYTHON_VERSION = "3.12"
PYPI_INDEX = "https://pypi.org/simple"
_COMPLETE_MARKER = ".complete"
_COMPILE_TIMEOUT_S = 300
_DOWNLOAD_TIMEOUT_S = 900
_DEFAULT_MAX_PACKAGES = 50
_DEFAULT_MAX_WHEEL_MB = 500


class ScriptStageError(RuntimeError):
    """The lock or the wheel download failed; the message is job-facing."""


@dataclass(frozen=True, slots=True)
class StagedDeps:
    """What :func:`stage_deps` produced for one job."""

    lock_text: str
    lock_sha: str
    wheels_dir: Path
    reused: bool


def _uv_bin() -> str:
    return os.environ.get("PRECIS_SANDBOX_UV") or "uv"


def _pip_cmd() -> list[str]:
    raw = os.environ.get("PRECIS_SANDBOX_PIP") or "python3 -m pip"
    return shlex.split(raw)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def max_packages() -> int:
    """Cap on pinned packages in one lock (``PRECIS_SANDBOX_MAX_PACKAGES``)."""
    return _env_int("PRECIS_SANDBOX_MAX_PACKAGES", _DEFAULT_MAX_PACKAGES)


def max_wheel_bytes() -> int:
    """Cap on one lock's total staged wheel size
    (``PRECIS_SANDBOX_MAX_WHEEL_MB``, megabytes)."""
    return _env_int("PRECIS_SANDBOX_MAX_WHEEL_MB", _DEFAULT_MAX_WHEEL_MB) * 1_000_000


def lock_packages(lock_text: str) -> list[str]:
    """The pinned requirement lines of a lock (hash continuation lines and
    comments excluded)."""
    return [
        ln.split()[0]
        for ln in lock_text.splitlines()
        if ln.strip() and not ln[0].isspace() and not ln.startswith("#")
    ]


def wheel_cache_root(work_root: Path) -> Path:
    raw = os.environ.get("PRECIS_SANDBOX_WHEEL_CACHE")
    return Path(raw) if raw else work_root / "wheels"


def _python_platform() -> str:
    """The uv ``--python-platform`` target: Linux on this host's arch (the
    container shares the host kernel, so the arch matches)."""
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("aarch64", "arm64") else "x86_64"
    return f"{arch}-unknown-linux-gnu"


def _run(argv: list[str], *, timeout: int, what: str) -> None:
    try:
        res = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ScriptStageError(
            f"sandbox_run: {what} is not available to the executor user "
            f"({exc.filename or argv[0]}); set PRECIS_SANDBOX_UV / "
            "PRECIS_SANDBOX_PIP on the sandbox host"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ScriptStageError(
            f"sandbox_run: {what} timed out after {timeout}s"
        ) from exc
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip()[-2000:]
        raise ScriptStageError(
            f"sandbox_run: {what} failed (exit {res.returncode}): {tail}"
        )


def compile_lock(deps: list[str]) -> str:
    """Resolve ``deps`` against PyPI to a hash-pinned lock text.

    No deps → an empty lock (no subprocess). Raises
    :class:`ScriptStageError` on a resolution failure.
    """
    reqs = [d.strip() for d in deps if d.strip()]
    if not reqs:
        return ""
    with tempfile.TemporaryDirectory(prefix="sandbox-lock-") as tmp:
        req_in = Path(tmp) / "requirements.in"
        req_in.write_text("\n".join(reqs) + "\n", encoding="utf-8")
        lock = Path(tmp) / "lock.txt"
        _run(
            [
                _uv_bin(),
                "pip",
                "compile",
                "--no-config",
                # Never build an sdist for metadata: that would run an
                # agent-chosen package's build backend on this host.
                "--no-build",
                "--generate-hashes",
                "--no-header",
                "--python-version",
                SCRIPT_PYTHON_VERSION,
                "--python-platform",
                _python_platform(),
                "--default-index",
                PYPI_INDEX,
                "-o",
                str(lock),
                str(req_in),
            ],
            timeout=_COMPILE_TIMEOUT_S,
            what="uv pip compile (dependency lock)",
        )
        return lock.read_text(encoding="utf-8")


def download_wheels(lock_text: str, dest: Path) -> None:
    """``pip download`` every pinned wheel of ``lock_text`` into ``dest``
    (binary-only, no dependency expansion: the lock is already closed)."""
    dest.mkdir(parents=True, exist_ok=True)
    lock = dest / "lock.txt"
    lock.write_text(lock_text, encoding="utf-8")
    try:
        _run(
            [
                *_pip_cmd(),
                "download",
                "--isolated",
                "--only-binary=:all:",
                "--no-deps",
                "--python-version",
                SCRIPT_PYTHON_VERSION,
                "--index-url",
                PYPI_INDEX,
                "-r",
                str(lock),
                "-d",
                str(dest),
            ],
            timeout=_DOWNLOAD_TIMEOUT_S,
            what="pip download (wheel stage; sdist-only packages are refused)",
        )
    finally:
        lock.unlink(missing_ok=True)


def stage_deps(deps: list[str], *, cache_root: Path) -> StagedDeps:
    """Lock ``deps`` and make sure their wheels sit in the shared cache.

    Returns the lock text, its sha256 and the wheel dir to mount. A dir
    with the ``.complete`` marker is reused without a download.
    """
    lock_text = compile_lock(deps)
    n_packages = len(lock_packages(lock_text))
    if n_packages > max_packages():
        raise ScriptStageError(
            f"sandbox_run: the dependency lock pins {n_packages} packages "
            f"(cap {max_packages()}, PRECIS_SANDBOX_MAX_PACKAGES); trim deps"
        )
    lock_sha = hashlib.sha256(lock_text.encode("utf-8")).hexdigest()
    wheels_dir = cache_root / lock_sha
    if (wheels_dir / _COMPLETE_MARKER).exists():
        return StagedDeps(lock_text, lock_sha, wheels_dir, reused=True)

    cache_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{lock_sha[:12]}-", dir=cache_root))
    try:
        if lock_text.strip():
            download_wheels(lock_text, tmp)
        total = sum(p.stat().st_size for p in tmp.iterdir() if p.is_file())
        if total > max_wheel_bytes():
            raise ScriptStageError(
                f"sandbox_run: the staged wheels total {total // 1_000_000} MB "
                f"(cap {max_wheel_bytes() // 1_000_000} MB, "
                "PRECIS_SANDBOX_MAX_WHEEL_MB); trim deps"
            )
        (tmp / _COMPLETE_MARKER).write_text(lock_sha + "\n", encoding="utf-8")
        # Readable by the container's mapped uid (mounted read-only).
        tmp.chmod(0o755)
        if wheels_dir.exists():
            # An incomplete leftover (no marker) or a concurrent winner.
            if (wheels_dir / _COMPLETE_MARKER).exists():
                return StagedDeps(lock_text, lock_sha, wheels_dir, reused=True)
            shutil.rmtree(wheels_dir, ignore_errors=True)
        try:
            tmp.rename(wheels_dir)
        except OSError:
            if not (wheels_dir / _COMPLETE_MARKER).exists():
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return StagedDeps(lock_text, lock_sha, wheels_dir, reused=False)


__all__ = [
    "PYPI_INDEX",
    "SCRIPT_PYTHON_VERSION",
    "ScriptStageError",
    "StagedDeps",
    "compile_lock",
    "download_wheels",
    "stage_deps",
    "wheel_cache_root",
]
