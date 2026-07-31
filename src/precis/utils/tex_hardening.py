r"""Hardened environment for compiling agent-authored LaTeX.

Two compile paths run ``latexmk`` with ``cwd`` set to a workspace the agent
assembled: the STATUS:done compile guard (:mod:`precis.utils.compile_guard`)
and the draft export compile (:mod:`precis.export.compile`). That puts the TeX
engine's ``\write18`` shell-escape in reach of a prompt-injected agent as an
out-of-band code-execution channel *outside* the claude-agent tool boundary /
envelope — a malicious paper/web body could steer an agent into emitting
``\immediate\write18{...}`` in its tex, and the worker would run it at compile
time.

:func:`hardened_latex_env` overlays the kpathsea knob that closes that, with
ZERO regression to today's compiles: ``shell_escape=f`` fully disables engine
shell-escape, and nothing in the draft pipeline needs it —

* ``makeglossaries`` / ``biber`` run via latexmk's *own* Perl orchestration
  (the shipped draft ``latexmkrc``'s ``system "makeglossaries"`` cus-dep), which
  the engine-level knob does not touch; and
* figures are PNG/PDF (``\includegraphics`` / ``\includepdf``), so there is no
  ``epstopdf`` EPS→PDF auto-conversion that would need *restricted* shell-escape.

kpathsea reads ``shell_escape`` from the environment, overriding whatever the
host ``texmf.cnf`` defaults to (a dev box may ship it enabled) — so this pins
the safe value regardless of host config.

Residual NOT closed here (tracked as a separate design item): a workspace
``.latexmkrc`` is itself arbitrary Perl and reading it is a *relied-upon*
feature (the shipped draft rc sets ``$pdf_mode=4`` + the makeglossaries
cus-dep), so a malicious workspace ``.latexmkrc`` — or the paranoid
``openin_any``/``openout_any`` file-scoping knobs, which risk breaking
lualatex's font-cache writes to an absolute ``TEXMFVAR`` — need a deliberate
decision (allowlist rc directives, or compile inside the §13 container),
not a drive-by env tweak.
"""

from __future__ import annotations

import os

#: kpathsea shell-escape knob. ``f`` = fully disabled (not even the restricted
#: whitelist). See the module docstring for why fully-off is regression-free
#: for the draft pipeline.
_HARDENING: dict[str, str] = {"shell_escape": "f"}


def hardened_latex_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return ``base`` (default: a copy of ``os.environ``) with the LaTeX
    shell-escape hardening overlaid. Pass the result as ``subprocess.run``'s
    ``env=`` when invoking ``latexmk`` on an agent-authored workspace.
    """
    env = dict(os.environ if base is None else base)
    env.update(_HARDENING)
    return env


__all__ = ["hardened_latex_env"]
