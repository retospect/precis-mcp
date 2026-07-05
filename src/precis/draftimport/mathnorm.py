"""Normalise a raw LaTeX equation body into KaTeX-safe ``$$…$$`` display math.

The draft model has no dedicated ``equation`` chunk kind: mathematics is
LaTeX inside prose — inline ``$…$``, display ``$$…$$`` — rendered by KaTeX on
read (the reader's ``renderMathInElement`` delimiters are ``$$`` display /
``$`` inline). This module is the one place that turns a legacy/imported
equation body (bare LaTeX, or an ``equation``/``align``/``\\[..\\]`` block)
into a single ``$$ … $$`` string KaTeX will render.

Why a dedicated normaliser rather than "wrap in ``$$``":

* **KaTeX chokes on ``\\label``** — it must be stripped. (The draft's own
  ``\\label``→handle cross-ref system, ``demacro.labels_in`` /
  ``resolve_deferred``, captures the label from the *raw* body first, so
  stripping it from the rendered text loses nothing.)
* **Outer environments differ from KaTeX's.** Inside ``$$`` (already display
  mode) KaTeX rejects the "outer" display environments ``equation`` / ``align``
  / ``gather``. Map them to the nestable forms KaTeX accepts:
  ``equation``/``displaymath`` → unwrap (bare), ``align*`` → ``aligned``,
  ``gather*`` → ``gathered``, ``multline``/``split``/``eqnarray`` → ``aligned``.
  ``aligned`` / ``matrix`` / ``cases`` / ``array`` (already KaTeX-safe) pass
  through untouched.
* **Bare aligned rows need a wrapper.** The LaTeX importer stores the *inner*
  body of an ``align`` env (the rows, carrying ``&`` and ``\\``) with the outer
  wrapper already stripped — bare, those fail in ``$$``. If a body carries
  alignment tokens (``&`` or a ``\\`` row break) and no math environment of its
  own, we wrap it in ``\\begin{aligned}…\\end{aligned}``.

Output is deliberately **single-line** (``$$ … $$`` with interior whitespace
collapsed). Every ``\\$\\$.+?\\$\\$`` matcher in the codebase — the reader's
KaTeX auto-render, ``export/latex._MATH``, the DOCX OMML path — then matches it
without depending on ``re.DOTALL``. Newlines are insignificant in LaTeX math
(``\\`` is the real row separator), so collapsing them is loss-free.

Pure functions; no store, no I/O. Gold-set tests in ``tests/test_mathnorm.py``.
"""

from __future__ import annotations

import re

__all__ = ["normalize_math"]

# ``\label{…}`` — KaTeX cannot parse it; the draft handle system captures the
# label from the raw body before we strip it here.
_LABEL_RE = re.compile(r"\\label\s*\{[^}]*\}")

# ``\begin{env}`` / ``\end{env}`` — env name may carry a trailing ``*``.
_BEGIN_RE = re.compile(r"\\begin\s*\{([A-Za-z]+\*?)\}")
_END_RE = re.compile(r"\\end\s*\{([A-Za-z]+\*?)\}")

# Outer display environments that must not appear inside ``$$`` — unwrap them
# (their content becomes bare display math).
_UNWRAP_ENVS = frozenset(
    {"equation", "equation*", "displaymath", "math", "displaymath*"}
)

# Outer environments mapped onto the nestable KaTeX form with the same shape.
_ENV_MAP = {
    "align": "aligned",
    "align*": "aligned",
    "alignat": "aligned",
    "alignat*": "aligned",
    "flalign": "aligned",
    "flalign*": "aligned",
    "multline": "aligned",
    "multline*": "aligned",
    "eqnarray": "aligned",
    "eqnarray*": "aligned",
    "split": "aligned",
    "gather": "gathered",
    "gather*": "gathered",
}

# A math environment already present in the body → don't add an ``aligned``
# wrapper around alignment tokens (they belong to this env).
_MATH_ENV_PRESENT_RE = re.compile(
    r"\\begin\s*\{(?:aligned|gathered|alignedat|array|cases|dcases|rcases|"
    r"matrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|smallmatrix|split|"
    r"subarray|CD)\*?\}"
)


def _strip_outer_delims(s: str) -> str:
    """Peel a single layer of display delimiters (``$$…$$`` / ``\\[…\\]`` /
    ``\\(…\\)``) so the caller re-emits exactly one ``$$ … $$``."""
    s = s.strip()
    if len(s) >= 4 and s.startswith("$$") and s.endswith("$$"):
        return s[2:-2].strip()
    if len(s) >= 4 and s.startswith("\\[") and s.endswith("\\]"):
        return s[2:-2].strip()
    if len(s) >= 4 and s.startswith("\\(") and s.endswith("\\)"):
        return s[2:-2].strip()
    return s


def _remap_envs(s: str) -> str:
    """Rewrite outer environments to their KaTeX-nestable equivalents.

    Unmatched/unknown environments (``aligned``, ``matrix``, …) pass through.
    Applied to both ``\\begin`` and ``\\end`` so pairs stay balanced.
    """

    def _b(m: re.Match[str]) -> str:
        env = m.group(1)
        if env in _UNWRAP_ENVS:
            return ""
        mapped = _ENV_MAP.get(env)
        return f"\\begin{{{mapped}}}" if mapped else m.group(0)

    def _e(m: re.Match[str]) -> str:
        env = m.group(1)
        if env in _UNWRAP_ENVS:
            return ""
        mapped = _ENV_MAP.get(env)
        return f"\\end{{{mapped}}}" if mapped else m.group(0)

    s = _BEGIN_RE.sub(_b, s)
    s = _END_RE.sub(_e, s)
    return s


def _needs_aligned(s: str) -> bool:
    """True when the body carries alignment tokens (``&`` or a ``\\`` row
    break) but no math environment of its own — bare such content fails inside
    ``$$`` and must be wrapped in ``aligned``."""
    if _MATH_ENV_PRESENT_RE.search(s):
        return False
    return ("&" in s) or bool(re.search(r"\\\\", s))


def normalize_math(raw: str) -> str:
    """Turn a raw equation body into a single-line ``$$ … $$`` string.

    Returns ``""`` for a body that reduces to nothing (e.g. a ``\\label``-only
    block) — the caller decides whether to drop such a chunk.
    """
    s = _strip_outer_delims(raw or "")
    s = _LABEL_RE.sub("", s)
    s = _remap_envs(s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    if _needs_aligned(s):
        s = r"\begin{aligned} " + s + r" \end{aligned}"
    return f"$$ {s} $$"
