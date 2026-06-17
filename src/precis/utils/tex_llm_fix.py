"""Layer-2 LLM-fixer for tex puts that survive Layer-1 mechanical fixes.

When ``utils.tex_mechanical_fix.apply_mechanical_fixes`` runs but
leaves syntactic problems (unbalanced ``\\begin{}/\\end{}`` not
trivially mechanical, undefined macros that aren't typos, malformed
bib references), Layer 2 invokes ``claude -p`` with a tightly-bounded
prompt:

> Fix only the lines flagged by chktex errors. Do NOT modify
> ``\\section{}`` / ``\\subsection{}`` heading text, or body prose.
> If you can't fix without changing meaning, return UNFIXABLE.

The result is returned as a **hint verdict** from
``PlaintextHandler._put_create`` — the file is NOT written. The
caller (the planner LLM in the parent claude session) sees the
proposal and decides whether to resubmit it as their own put.

Cost: ~$0.003 per fix call (sonnet). Triggered only when chktex
flags real errors — most puts get verdict 'ok' from Layer 1 alone.
Bounded to one LLM call per put; chained fixes would loop without
adding value.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Layer2Result:
    """Output of the LLM-fixer attempt.

    Three terminal shapes:

    * ``verdict='ok'`` — chktex was already clean; no fix attempted.
      Caller writes the text unmodified.
    * ``verdict='hint'`` — Layer 2 produced a proposed correction.
      Caller surfaces it via the put-response hint shape; file NOT
      written.
    * ``verdict='failed'`` — Layer 2 refused (returned UNFIXABLE) or
      the call itself errored. Caller surfaces the error to the LLM
      so it can rewrite.
    """

    verdict: str
    proposed_text: str = ""
    errors: tuple[str, ...] = ()
    note: str = ""


def attempt_llm_fix(
    text: str,
    *,
    timeout_s: int = 60,
) -> Layer2Result:
    """Run chktex; if clean → 'ok'. Otherwise call sonnet for a fix.

    Idempotent at the spec level — the same input always queries
    chktex and (if errors) the LLM with the same prompt. Stochastic
    at the LLM-output level by definition.

    ``timeout_s`` caps both the chktex run and the claude -p call.
    """
    chktex_errors = _run_chktex(text)
    if not chktex_errors:
        return Layer2Result(verdict="ok")

    # Layer 1 already ran upstream; the remaining errors are by
    # definition not mechanically fixable. Ask sonnet.
    claude_bin = os.environ.get("PRECIS_CLAUDE_BIN", "claude")
    sonnet_model = os.environ.get("PRECIS_MODEL_SONNET", "claude-sonnet-4-6")
    prompt = _build_fixer_prompt(text=text, errors=chktex_errors)
    try:
        proc = subprocess.run(
            [
                claude_bin,
                "-p",
                prompt,
                "--model",
                sonnet_model,
                "--max-turns",
                "1",
                "--permission-mode",
                "default",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Layer2Result(
            verdict="failed",
            errors=chktex_errors,
            note=f"Layer-2 fixer timed out after {timeout_s}s",
        )
    except (OSError, FileNotFoundError) as exc:
        return Layer2Result(
            verdict="failed",
            errors=chktex_errors,
            note=f"claude binary not found: {exc}",
        )
    if proc.returncode != 0:
        return Layer2Result(
            verdict="failed",
            errors=chktex_errors,
            note=f"claude -p exit {proc.returncode}: {proc.stderr[:500]}",
        )
    proposed = _extract_proposal(proc.stdout)
    if not proposed or "UNFIXABLE" in proc.stdout.upper():
        return Layer2Result(
            verdict="failed",
            errors=chktex_errors,
            note="LLM declined the fix as semantically risky",
        )
    return Layer2Result(
        verdict="hint",
        proposed_text=proposed,
        errors=chktex_errors,
    )


def _run_chktex(text: str) -> tuple[str, ...]:
    """Pipe text to chktex; return error lines (empty when clean).

    Wraps a subprocess call with a short timeout. When chktex isn't
    installed, returns empty (we can't tell errors from absence;
    Layer-3 latexmk at STATUS:done catches what we miss).
    """
    import shutil

    chktex = shutil.which("chktex")
    if chktex is None:
        return ()
    try:
        proc = subprocess.run(
            [chktex, "-q", "-n", "all"],
            input=text,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ()
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    # Only keep error/warning prefixes; chktex emits "Warning N..." +
    # "Message N..." lines.
    keep = [ln for ln in lines if ln.startswith(("Warning", "Message", "Error"))]
    return tuple(keep[:30])  # Cap so the prompt stays small.


def _build_fixer_prompt(*, text: str, errors: tuple[str, ...]) -> str:
    """Render the constrained Layer-2 fixer prompt.

    Hard constraints baked in:
    * Edit only flagged spans.
    * Do not modify headings or body prose.
    * Return either the full corrected file or the literal token
      ``UNFIXABLE`` (no other commentary).
    """
    errors_block = "\n".join(f"- {e}" for e in errors)
    return (
        "You are a LaTeX-syntax fixer. Below is a .tex file body and a "
        "list of chktex-flagged errors. Your job is mechanical: fix ONLY "
        "the lines flagged. Strict constraints:\n"
        "\n"
        "1. Do NOT modify \\section{...} or \\subsection{...} heading text.\n"
        "2. Do NOT modify body prose — fix only macros / commands / "
        "environments.\n"
        "3. Do NOT touch \\cite{...} keys.\n"
        "4. If you can't fix without changing meaning, output the literal "
        "token UNFIXABLE on a line by itself — nothing else.\n"
        "5. Otherwise output the FULL corrected file content — no "
        "Markdown fences, no commentary, no explanations.\n"
        "\n"
        "Errors flagged by chktex:\n"
        f"{errors_block}\n"
        "\n"
        "File content:\n"
        "<<<TEX>>>\n"
        f"{text}\n"
        "<<<END>>>\n"
    )


def _extract_proposal(stdout: str) -> str:
    """Pull the fixer's proposed file content out of claude's stdout.

    The fixer is instructed to emit the full corrected file. claude's
    stdout may have trailing pleasantries; strip surrounding whitespace
    and any meta-commentary lines that snuck in.
    """
    text = stdout.strip()
    # Common LLM noise: leading "Here's the corrected file:" line.
    lines = text.splitlines()
    # Drop a leading prose line if it looks meta.
    while lines and lines[0].strip().lower().startswith(
        ("here", "here's", "below", "this is", "the corrected", "fixed:")
    ):
        lines.pop(0)
    return "\n".join(lines).strip()


__all__ = [
    "Layer2Result",
    "attempt_llm_fix",
]
