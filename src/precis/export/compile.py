"""Compile an exported LaTeX project to PDF — the draft kind's Tier-B export.

A thin, deterministic wrapper over ``latexmk``: one command runs every
pass (pdflatex reruns + biber + makeglossaries, wired by the copied
``latexmkrc``). Mirrors the existing workspace ``compile_guard`` (same
flags, timeout, log-tail), but returns a result object instead of
raising — the export CLI decides what to do with a failure, and the
(future) LLM-repair loop consumes ``log_tail``.

Determinism rests on three things, in order of leverage:

1. **A pinned toolchain.** ``latexmk`` + ``biber`` + ``makeglossaries``
   + the packages the preamble loads (glossaries-extra, biblatex,
   cleveref, microtype) must be present and versioned. The honest way
   to guarantee this across the fleet is a containerised TeX Live; today
   we reuse the same host-mactex assumption as ``compile_guard`` and
   degrade cleanly (no binary → emit the .tex, skip the PDF).
2. **Compile-safe generation.** The renderer (``latex.py``) escapes
   LaTeX specials, translates non-ASCII to LaTeX commands (so pdflatex
   never hits a "missing character"), guarantees every ``\\gls`` has a
   ``\\newacronym`` and every ``\\cite`` a bib entry, and downgrades a
   dangling ``¶`` cross-ref instead of emitting a broken ``\\cref``.
   That shrinks the failure surface to genuinely malformed user math.
3. **Non-interactive, bounded invocation.** ``-interaction=nonstopmode
   -halt-on-error`` + a wall-clock cap → a deterministic exit code.

The residual (malformed math, an exotic macro) is what the LLM-repair
loop is for — but the deterministic path should succeed for any
well-formed draft.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from precis.utils.tex_hardening import (
    hardened_latex_env,
    latexmk_argv,
    trusted_latexmkrc,
)

log = logging.getLogger(__name__)


def _latexmk_bin() -> str:
    """The latexmk binary — overridable via ``PRECIS_LATEXMK_BIN`` (a
    stub binary in tests, like ``PRECIS_CLAUDE_BIN``)."""
    return os.environ.get("PRECIS_LATEXMK_BIN", "latexmk")


def have_latexmk() -> bool:
    """True when the latexmk binary is resolvable."""
    return shutil.which(_latexmk_bin()) is not None


@dataclass
class CompileResult:
    """Outcome of one compile attempt."""

    ok: bool
    pdf: Path | None
    returncode: int
    log_tail: str
    skipped: bool = False  # no toolchain → not attempted


def _log_tail(project_dir: Path, entrypoint: str, proc_out: str, n: int = 40) -> str:
    """Last ``n`` lines of the LaTeX log (or process output as fallback)
    — what a human or the repair loop reads to see what broke."""
    log_path = project_dir / (Path(entrypoint).stem + ".log")
    if log_path.exists():
        try:
            return "\n".join(log_path.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            pass
    return (proc_out or "")[-2000:]


def compile_pdf(
    project_dir: Path,
    *,
    entrypoint: str = "main.tex",
    timeout_s: int | None = None,
) -> CompileResult:
    """Run ``latexmk -pdf`` on an exported project. Returns a
    :class:`CompileResult`; never raises on a LaTeX error (that's a
    ``ok=False`` result with the log tail). ``skipped=True`` when no
    latexmk is installed."""
    project_dir = Path(project_dir)
    if not have_latexmk():
        log.warning("compile_pdf: latexmk not on PATH; skipping (install mactex)")
        return CompileResult(
            ok=False,
            pdf=None,
            returncode=-1,
            log_tail="latexmk not installed",
            skipped=True,
        )
    if timeout_s is None:
        timeout_s = int(os.environ.get("PRECIS_LATEXMK_TIMEOUT_S", "120"))
    try:
        with trusted_latexmkrc() as trusted_rc:
            # lualatex (not pdflatex): UTF-8 native, so arbitrary scientific
            # Unicode the encoder couldn't map degrades to a recoverable
            # missing-glyph warning instead of a fatal inputenc error (``-pdf``
            # would be pdflatex). ``-norc -r <trusted_rc>`` reads only the
            # packaged rc — never the agent's workspace ``.latexmkrc`` (arbitrary
            # Perl = RCE) — while still supplying $pdf_mode=4 + makeglossaries
            # (gr178973); the explicit ``-lualatex`` still wins the engine.
            cmd = latexmk_argv(_latexmk_bin(), "-lualatex", entrypoint, trusted_rc)
            log.info(
                "compile_pdf: %s in %s (timeout=%ds)",
                " ".join(cmd),
                project_dir,
                timeout_s,
            )
            proc = subprocess.run(
                cmd,
                cwd=project_dir,
                # Disable engine shell-escape on this agent-authored project —
                # same out-of-band ``\write18`` RCE channel as compile_guard.
                # See tex_hardening; zero-regression for the draft pipeline.
                env=hardened_latex_env(),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return CompileResult(
            ok=False,
            pdf=None,
            returncode=-1,
            log_tail=f"latexmk timed out after {timeout_s}s",
        )
    pdf = project_dir / (Path(entrypoint).stem + ".pdf")
    ok = proc.returncode == 0 and pdf.exists()
    return CompileResult(
        ok=ok,
        pdf=pdf if ok else None,
        returncode=proc.returncode,
        log_tail=_log_tail(project_dir, entrypoint, proc.stdout + proc.stderr),
    )


__all__ = ["CompileResult", "compile_pdf", "have_latexmk"]
