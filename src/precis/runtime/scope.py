"""Connection scope for recipe surfaces — ``read`` vs ``write``.

gr512233 (``docs/backlog/read-scope-skill-fencing.md``; Reto's option-B
ruling 2026-10-25): a read-only *connection* must not be shown write
recipes — skill sections, ``Next:`` rows, ``next=`` hints. Scope is a
property of the process/connection, never a per-request flag an agent
could forget to pass.

:func:`current_scope` is the single function every surface asks. The
process signals that make a run ``read``:

- ``precis serve --read-only`` (the sandbox sidecar) —
  ``server._restrict_to_read_verbs`` records it through
  :func:`note_serve_read_only` once the command-profile check has
  passed (a refused startup must not leave the flag set);
- ``PRECIS_MCP_DENY_VERBS`` naming ``put`` — a ``write:none`` envelope
  (the literal mirrors ``runtime.dispatch._DENY_VERBS_ENV`` /
  ``workers.envelope.DENY_VERBS_ENV``; ``tests/test_envelope_verb_deny``
  pins them equal — this copy stays a private literal because
  ``runtime.dispatch`` itself imports this module). ``write:scoped``
  (only ``delete`` denied) stays ``write``;
- ``PRECIS_MCP_READ_ONLY`` on — the operator-declared override
  (``dispatch._read_only_env_override``: doctor, guide-web);
- the boot probe — ``dispatch._boot_is_read_only`` (``agent_ro`` grants
  or a ``transaction_read_only`` DSN) reports once per ``boot()``
  through :func:`note_boot_read_only`.

The env signals are read *live*, not cached: tests flip them
mid-process (``monkeypatch.setenv`` in ``test_envelope_verb_deny`` and
friends), and an env read is free. The serve and probe flags are
module state — the probe verdict is what ``note_boot_read_only``
caches, which is the spec's "computed once at boot", and
``boot_writes=False`` boots record ``False`` (no probe ran, so nothing
is claimed). A ``serve --read-only`` stamp would previously write the
env var itself, which leaked read scope across a pytest process's
later tests — the module flag plus the conftest reset keeps the
signal in-process where it belongs (round-2 review).

Slice 4 of the spec inserts the per-request source *above* the process
ones: a read bearer token on the shared streamable-http server, read
from the MCP request context — not a middleware contextvar, because
streamable-http runs tool calls in the session's task.
"""

from __future__ import annotations

import os
from typing import Literal

Scope = Literal["read", "write"]

#: ``precis serve --read-only`` keeps only ``get``/``search``/``more``.
#: The serving process records that fact through
#: :func:`note_serve_read_only`; the env var is still honoured for an
#: operator who exports it directly.
SERVE_READ_ONLY_ENV = "PRECIS_MCP_SERVE_READ_ONLY"

#: The worker's per-run verb deny (see module docstring for why this is
#: a private copy of ``runtime.dispatch._DENY_VERBS_ENV``).
_DENY_VERBS_ENV = "PRECIS_MCP_DENY_VERBS"

#: Operator-declared read-only process (``dispatch._read_only_env_override``
#: accepts the same on/off vocabulary).
_READ_ONLY_ENV = "PRECIS_MCP_READ_ONLY"

_ON_VALUES = ("1", "true", "yes", "on")

_boot_read_only = False
_serve_read_only = False


def note_boot_read_only(detected: bool) -> None:
    """Record the boot probe's read-only verdict — once per ``boot()``.

    Called unconditionally so a second boot on a writable store (tests
    boot repeatedly in one process) doesn't inherit a stale ``read``.
    """
    global _boot_read_only
    _boot_read_only = detected


def note_serve_read_only() -> None:
    """Record that this process serves the read-only tool set —
    ``server._restrict_to_read_verbs`` calls it once the command-profile
    check has passed, so a refused startup leaves no stale ``read``.
    """
    global _serve_read_only
    _serve_read_only = True


def _on_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _ON_VALUES


def _env_denies_put() -> bool:
    """``PRECIS_MCP_DENY_VERBS`` names ``put`` — the ``write:none`` run."""
    raw = os.environ.get(_DENY_VERBS_ENV)
    if not raw:
        return False
    return "put" in {v.strip() for v in raw.split(",")}


def current_scope() -> Scope:
    """``'read'`` when this connection may not write; ``'write'`` otherwise.

    Every surface that prints a recipe consults this — skill bodies
    (``handlers/skill.py``), and in later slices ``Next:`` rows, hint-bus
    entries and ``next=`` refusal lines.
    """
    if (
        _on_env(SERVE_READ_ONLY_ENV)
        or _serve_read_only
        or _env_denies_put()
        or _on_env(_READ_ONLY_ENV)
        or _boot_read_only
    ):
        return "read"
    return "write"
