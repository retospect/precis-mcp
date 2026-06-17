"""Layer-1 deterministic LaTeX fixes — no LLM in the loop.

Roughly half of compile-time LaTeX errors are mechanical: unbalanced
``\\begin{}/\\end{}`` pairs, raw unicode that needs escaping, missing
``\\usepackage{}`` for an obvious macro. These have a deterministic
correct answer. This module applies them silently as part of
``put(kind='tex', ...)`` so the planner LLM never sees the noise.

What this module does NOT do:

* Run ``pdflatex`` (that's Layer 3 / the workspace-level compile
  guard).
* Edit prose or heading text (the LLM owns content).
* Guess at bib-key resolutions (that's Layer 2's job).

Return shape from :func:`apply_mechanical_fixes`::

    @dataclass
    class MechanicalFixResult:
        text: str               # the (possibly modified) text
        fixes: list[str]        # human-readable summary lines

When ``fixes`` is empty, the input was already clean. When non-empty,
the caller surfaces it as the put response's "mechanical fixes" note.
Caller can ignore the result (the text always lands cleanly).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MechanicalFixResult:
    """Output of the mechanical fixer.

    Even if ``fixes`` is empty, ``text`` is returned (unchanged); the
    caller can write the result unconditionally without re-checking.
    """

    text: str
    fixes: tuple[str, ...]


_UNICODE_ESCAPES: dict[str, str] = {
    # Common scientific unicode → LaTeX text-mode escapes. Math-mode
    # callers will produce different output but the LLM's plain-prose
    # use of these characters is what we're cleaning up.
    "–": "--",  # en-dash
    "—": "---",  # em-dash
    "‘": "`",  # left single quote
    "’": "'",  # right single quote
    "“": "``",  # left double quote
    "”": "''",  # right double quote
    "×": r"$\times$",  # multiplication
    "°": r"$^\circ$",  # degree sign
    "μ": r"$\mu$",  # micro
    "±": r"$\pm$",  # plus-minus
    " ": "~",  # non-breaking space → tilde
}


_BEGIN_END_RE = re.compile(r"\\(begin|end)\{([^}]+)\}")


_MACRO_TO_PACKAGE: dict[str, str] = {
    # Common macros and the package they need.
    r"\toprule": "booktabs",
    r"\midrule": "booktabs",
    r"\bottomrule": "booktabs",
    r"\SI": "siunitx",
    r"\si": "siunitx",
    r"\num": "siunitx",
    r"\includegraphics": "graphicx",
    r"\url": "hyperref",
    r"\href": "hyperref",
    r"\text": "amsmath",
    r"\mathrm": "amsmath",
    r"\mathbb": "amssymb",
    r"\mathcal": "amssymb",
    r"\textmu": "textcomp",
    r"\textohm": "textcomp",
}


_USEPACKAGE_RE = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{([^}]+)\}")


def apply_mechanical_fixes(text: str) -> MechanicalFixResult:
    """Apply Layer-1 deterministic fixes; return the (possibly modified) text.

    The fixes are applied in a stable order so the result is
    reproducible for any input. Each fix that triggers contributes
    one entry to ``fixes`` describing what changed.
    """
    fixes: list[str] = []
    out = text

    # 1. Replace common unicode with LaTeX escapes.
    for char, replacement in _UNICODE_ESCAPES.items():
        if char in out:
            count = out.count(char)
            out = out.replace(char, replacement)
            fixes.append(
                f"escaped {count} occurrence(s) of U+{ord(char):04X} → {replacement!r}"
            )

    # 2. Detect unbalanced \begin{...}/\end{...} pairs. This is
    #    detection-only — we don't auto-balance because guessing where
    #    to insert the missing \end{} is semantic. We surface it as
    #    info for the LLM to inspect on the next tick if compile
    #    later fails.
    open_stack: list[str] = []
    for match in _BEGIN_END_RE.finditer(out):
        kind, env = match.group(1), match.group(2)
        if kind == "begin":
            open_stack.append(env)
        elif kind == "end":
            if open_stack and open_stack[-1] == env:
                open_stack.pop()
            else:
                # Mismatched / orphan \end{}. We don't try to fix —
                # surface in fixes for the result note.
                fixes.append(
                    f"DETECTED orphan \\end{{{env}}} (no matching \\begin); "
                    "left as-is — review at next compile"
                )
    for env in open_stack:
        fixes.append(
            f"DETECTED unclosed \\begin{{{env}}} (no matching \\end); "
            "left as-is — review at next compile"
        )

    # 3. Detect missing \usepackage{} for macros that are used.
    #    Only triggers for files that look like a *full* document
    #    (have \documentclass). Section files don't carry preamble.
    if r"\documentclass" in out:
        used_packages = set()
        for m in _USEPACKAGE_RE.finditer(out):
            for pkg in m.group(1).split(","):
                used_packages.add(pkg.strip())
        missing: set[str] = set()
        for macro, pkg in _MACRO_TO_PACKAGE.items():
            if macro in out and pkg not in used_packages:
                missing.add(pkg)
        if missing:
            # Inject \usepackage{} lines right after \documentclass.
            usepackage_block = "\n".join(
                f"\\usepackage{{{pkg}}}" for pkg in sorted(missing)
            )
            # Insert after the line containing \documentclass.
            lines = out.split("\n")
            for i, line in enumerate(lines):
                if r"\documentclass" in line:
                    lines.insert(i + 1, usepackage_block)
                    break
            out = "\n".join(lines)
            fixes.append(f"added missing \\usepackage{{{','.join(sorted(missing))}}}")

    return MechanicalFixResult(text=out, fixes=tuple(fixes))


def has_semantic_risk(text: str) -> bool:
    """Quick check whether the input has uncloseable issues Layer 1 can't fix.

    Returns True when there's evidence the file has structural problems
    (unbalanced environments, malformed cite keys) that need either
    Layer 2 LLM-fix or an author re-tick. Layer 1 doesn't fix these
    but the caller may want to short-circuit and return a hint without
    attempting to write.
    """
    open_stack: list[str] = []
    for match in _BEGIN_END_RE.finditer(text):
        kind, env = match.group(1), match.group(2)
        if kind == "begin":
            open_stack.append(env)
        else:
            if open_stack and open_stack[-1] == env:
                open_stack.pop()
            else:
                return True  # orphan \end{}
    return bool(open_stack)


__all__ = [
    "MechanicalFixResult",
    "apply_mechanical_fixes",
    "has_semantic_risk",
]
