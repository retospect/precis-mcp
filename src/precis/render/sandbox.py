"""Phase-1 render isolation: run untrusted figure code in a stripped subprocess.

ADR 0035 §3. Render code in a figure chunk is author-supplied Python, and an
LLM that ingests external content can be steered (indirect prompt injection)
into writing hostile code. The prohibited form is in-process ``exec`` on the
credential-bearing, every-node system worker — one poisoned render would be
cluster compromise. So we run it in a **child process** we can only crash:

* a **fresh, minimal environment** — built from an allowlist, never inherited;
  no DB creds, no ``SSH_AUTH_SOCK``, no ``PRECIS_*``, ``HOME`` redirected to a
  throwaway dir so nothing leaks to the real home;
* **rlimits** (CPU seconds, output file size, open files, address space where
  the OS honours it) applied in a ``preexec_fn``;
* a **wall-clock timeout** (the child is killed on expiry);
* a **throwaway CWD** and ``python -I`` (isolated: ignores ``PYTHON*`` env and
  the user site, keeps the corpus' import path off the child);
* **stdin closed**.

This is the cheap floor, not the ceiling. Phase 1 does **not** block network at
the OS level (no creds to abuse, but a determined exploit could still reach out)
nor fully jail the filesystem — those are the phase-2 Docker refinements on this
same subprocess seam. The contract and call sites do not change between phases.

Contract for the render code: it is handed two globals — ``data`` (the input
payload, e.g. ``{'table': {...}}``) and ``out`` (the absolute path to write the
PNG to). It must produce ``out``. As a convenience, if the code leaves an open
matplotlib figure without writing ``out``, the harness saves it for you.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Wall-clock ceiling for a single render (seconds).
DEFAULT_TIMEOUT_S = 30.0
#: Address-space cap (MB) — enforced on Linux; macOS ignores RLIMIT_AS, so the
#: real memory ceiling there only arrives with the phase-2 Docker jail.
DEFAULT_MEM_MB = 1024
#: Largest PNG we will accept back (MB) — also the child's RLIMIT_FSIZE.
DEFAULT_MAX_OUTPUT_MB = 64

#: Environment variables copied (by name) into the child's otherwise-empty env.
#: PATH is needed to locate shared libraries; the rest keep locale/encoding
#: sane. Everything else — creds, tokens, PRECIS_*, SSH — is dropped.
_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")

#: The child entrypoint, written to the throwaway dir per render. Pure stdlib
#: except the optional matplotlib convenience save. ``data`` and ``out`` are the
#: only globals the render code may rely on.
_HARNESS = """\
import json, os, sys

spec = json.load(open(sys.argv[1]))
out = spec["out"]
g = {"data": spec["inputs"], "out": out, "__name__": "__render__"}
try:
    exec(compile(spec["code"], "<render>", "exec"), g)
except Exception:
    import traceback
    traceback.print_exc()
    sys.exit(2)

if not os.path.exists(out):
    # Convenience: the code drew a matplotlib figure but didn't save it.
    try:
        import matplotlib
        import matplotlib.pyplot as plt

        if plt.get_fignums():
            plt.savefig(out, format="png", bbox_inches="tight")
    except Exception:
        pass

if not os.path.exists(out):
    sys.stderr.write("RENDER-NO-OUTPUT\\n")
    sys.exit(3)
"""


@dataclass(frozen=True)
class RenderResult:
    """Outcome of one sandboxed render.

    ``ok`` is True only when the child exited 0 and produced a PNG; ``png`` then
    holds the bytes. On failure ``error`` is a short tag — ``"timeout"``,
    ``"no-output"``, ``"oversize"``, or ``"exit:<n>"`` — and ``stderr`` carries
    the child's traceback for the failure-bubble.
    """

    ok: bool
    png: bytes | None
    error: str | None
    stdout: str
    stderr: str
    duration_s: float


def _preexec(mem_mb: int, cpu_s: int, fsize_mb: int):  # type: ignore[no-untyped-def]
    """Build the child ``preexec_fn`` that applies rlimits after fork.

    Each limit is best-effort: a platform that rejects one (macOS ignores
    RLIMIT_AS) must not break the others. Returns ``None`` on non-POSIX.
    """
    if os.name != "posix":
        return None
    import resource

    def _apply() -> None:
        # NB: the child's own session is created by start_new_session=True;
        # calling os.setsid() here would raise EPERM (already a leader).
        limits = [
            (resource.RLIMIT_CPU, (cpu_s, cpu_s)),
            (resource.RLIMIT_FSIZE, (fsize_mb << 20, fsize_mb << 20)),
            (resource.RLIMIT_NOFILE, (256, 256)),
            (resource.RLIMIT_CORE, (0, 0)),
        ]
        if hasattr(resource, "RLIMIT_AS"):
            limits.append((resource.RLIMIT_AS, (mem_mb << 20, mem_mb << 20)))
        for what, lim in limits:
            try:
                resource.setrlimit(what, lim)
            except (ValueError, OSError):
                pass

    return _apply


def _child_env(workdir: Path) -> dict[str, str]:
    """A fresh minimal environment for the child — allowlist only, plus a
    redirected HOME and a self-contained matplotlib config dir under the
    throwaway workdir (so the font cache never touches the real home)."""
    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
    env["HOME"] = str(workdir)
    env["TMPDIR"] = str(workdir)
    env["MPLBACKEND"] = "Agg"
    env["MPLCONFIGDIR"] = str(workdir / ".mpl")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env


def render_python(
    code: str,
    *,
    inputs: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    mem_mb: int = DEFAULT_MEM_MB,
    max_output_mb: int = DEFAULT_MAX_OUTPUT_MB,
) -> RenderResult:
    """Run ``code`` in a stripped subprocess and return the rendered PNG.

    ``code`` runs with globals ``data`` (= ``inputs``) and ``out`` (an absolute
    PNG path it must write). Isolation: fresh allowlist env, rlimits, wall-clock
    ``timeout_s``, throwaway CWD, ``python -I``, stdin closed (see module
    docstring). Never raises on render failure — inspect :class:`RenderResult`.
    """
    inputs = inputs or {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="precis-render-") as td:
        work = Path(td)
        (work / ".mpl").mkdir()
        harness = work / "harness.py"
        harness.write_text(_HARNESS)
        out = work / "figure.png"
        spec = work / "spec.json"
        spec.write_text(json.dumps({"code": code, "inputs": inputs, "out": str(out)}))

        argv = [sys.executable, "-I", str(harness), str(spec)]
        try:
            proc = subprocess.run(
                argv,
                cwd=str(work),
                env=_child_env(work),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                start_new_session=True,
                preexec_fn=_preexec(mem_mb, int(timeout_s) + 1, max_output_mb),
            )
        except subprocess.TimeoutExpired as exc:
            return RenderResult(
                ok=False,
                png=None,
                error="timeout",
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr),
                duration_s=time.monotonic() - started,
            )

        dur = time.monotonic() - started
        if proc.returncode != 0:
            tag = "no-output" if proc.returncode == 3 else f"exit:{proc.returncode}"
            return RenderResult(
                ok=False,
                png=None,
                error=tag,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=dur,
            )
        try:
            png = out.read_bytes()
        except OSError:
            return RenderResult(
                ok=False,
                png=None,
                error="no-output",
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=dur,
            )
        if len(png) > max_output_mb << 20:
            return RenderResult(
                ok=False,
                png=None,
                error="oversize",
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=dur,
            )
        return RenderResult(
            ok=True,
            png=png,
            error=None,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_s=dur,
        )


def _text(v: str | bytes | None) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else v.decode("utf-8", "replace")
