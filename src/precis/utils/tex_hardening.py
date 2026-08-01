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

The second RCE channel — a workspace ``.latexmkrc`` is itself arbitrary Perl,
and latexmk auto-reads ``./.latexmkrc`` from its ``cwd`` (the agent-assembled
workspace) — is closed by :func:`latexmk_argv` + :func:`trusted_latexmkrc`
(gr178973): every compile runs ``latexmk -norc -r <packaged-rc>`` so the
engine reads *only* the packaged, trusted rc (which supplies the same
``$pdf_mode=4`` + makeglossaries cus-dep the workspace copy did) and never the
agent's ``.latexmkrc``. ``-norc`` also drops the host's user/system rc — no
loss, the workspace copy was the only relied-upon one.

Still deferred (needs a real-compile check, not shipped here): the paranoid
``openin_any``/``openout_any=p`` file-scoping knobs would block
``\input{/etc/passwd}``-style exfil-to-PDF and dotfile writes, but risk
breaking lualatex's font-cache writes to an absolute ``TEXMFVAR`` — so they
need a live lualatex+glossary compile check before enabling.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

#: kpathsea shell-escape knob. ``f`` = fully disabled (not even the restricted
#: whitelist). See the module docstring for why fully-off is regression-free
#: for the draft pipeline.
_HARDENING: dict[str, str] = {"shell_escape": "f"}

#: The packaged, trusted latexmkrc — the SSOT the workspace copy is written
#: from (``precis.export.latex``). Read via ``-r`` so a compile never depends
#: on (or trusts) the agent's workspace ``.latexmkrc``.
_TRUSTED_RC_PKG = "precis.data.templates.draft"
_TRUSTED_RC_NAME = "latexmkrc"


def hardened_latex_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return ``base`` (default: a copy of ``os.environ``) with the LaTeX
    shell-escape hardening overlaid. Pass the result as ``subprocess.run``'s
    ``env=`` when invoking ``latexmk`` on an agent-authored workspace.
    """
    env = dict(os.environ if base is None else base)
    env.update(_HARDENING)
    return env


@contextmanager
def trusted_latexmkrc() -> Iterator[str]:
    """Yield a filesystem path to the packaged, trusted ``latexmkrc``.

    For ``latexmk -norc -r <path>`` so the agent-authored workspace
    ``.latexmkrc`` (arbitrary Perl = RCE) is never read — only this
    packaged rc, which supplies the ``$pdf_mode=4`` + makeglossaries cus-dep
    the pipeline relies on (gr178973). Uses :func:`importlib.resources.as_file`
    so the path is valid even when precis is installed from a wheel/zip; keep
    the ``latexmk`` subprocess inside the ``with`` block.
    """
    src = resources.files(_TRUSTED_RC_PKG).joinpath(_TRUSTED_RC_NAME)
    with resources.as_file(src) as path:
        yield str(path)


def latexmk_argv(
    binary: str, engine_flag: str, entrypoint: str, trusted_rc: str
) -> list[str]:
    """Build the ``latexmk`` argv with the trusted-rc injection (gr178973).

    ``-norc`` disables latexmk's automatic reading of the system / user /
    ``cwd`` rc files (the ``cwd`` one being the agent's workspace
    ``.latexmkrc``); ``-r <trusted_rc>`` then reads only the packaged rc.
    Both come *before* ``engine_flag`` so a command-line engine choice
    (``-pdf`` for the guard, ``-lualatex`` for export) still wins over the
    rc's ``$pdf_mode``. ``entrypoint`` is last (the IMAGE-of-tex positional).
    """
    return [
        binary,
        "-norc",
        "-r",
        trusted_rc,
        engine_flag,
        "-interaction=nonstopmode",
        "-halt-on-error",
        entrypoint,
    ]


__all__ = ["hardened_latex_env", "latexmk_argv", "trusted_latexmkrc"]
