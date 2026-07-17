"""Per-todo permission envelope — the *permission* side of a work spec.

Slice 8 of the factory design (``docs/design/factory-console-and-scheduling.md``
§4). Capability (§5 ``requires``) says *what a job consumes*; the envelope
says *what a job is allowed to do* — its least-privilege box, honored by the
executor **regardless of which host claims it**. A no-write / no-egress task
is therefore safe to run anywhere, which is what lets us universalize read
kinds without caring which box picks them up.

A job/todo declares its box in ``meta.envelope``::

    meta.envelope = { egress: none|api-only|open,   # network reach
                      write:  none|scoped|full,      # DB mutation
                      return: output-only|full }     # persist side effects?

Three enforcement *tiers*, chosen by how hard the guarantee must be — each
consumes one resolver in this module:

* **Tool-level** (cheap, cooperative) — :func:`disallowed_tools` drops the
  precis write verbs and/or the fetch tools from ``claude -p`` via its
  ``--settings permissions.deny`` channel. Enforced *now*, at the
  :func:`precis.utils.claude_agent.call_claude_agent` chokepoint.
* **Process-level** (real) — :func:`db_role` hands the task a read-only
  Postgres role (``agent_ro`` vs ``agent_rw``) so a write is refused by the
  database even if attempted. Consumed by the per-call ``precis serve`` the
  container executor spawns (§13).
* **Network-level** (hard) — :func:`network_mode` runs the task in a
  container with **no network namespace** (``--network none``): the only
  true egress denial. Consumed by the ``claude_docker`` / ``sandbox_run``
  container executor (§13).

Rollout is **dark**: an absent or unparseable ``meta.envelope`` yields
:data:`DEFAULT` (``open``/``full``/``full``) — byte-identical to today's
behavior — so the substrate ships without changing any job until one opts in
by declaring an envelope.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# ── The three axes (closed vocabularies) ──────────────────────────

#: ``egress`` — how far the task may reach on the network.
#: ``open`` = anywhere (today's default); ``api-only`` = the LLM/API
#: allowlist only (no arbitrary fetch); ``none`` = no network at all.
EGRESS_VALUES = ("none", "api-only", "open")

#: ``write`` — what the task may mutate in the DB.
#: ``full`` = read+write (today's default); ``scoped`` = write, but only
#: within a narrowed surface (still ``agent_rw`` at the DB, tool-narrowed);
#: ``none`` = read-only (``agent_ro`` role, refused writes).
WRITE_VALUES = ("none", "scoped", "full")

#: ``return`` — disposition of the task's work.
#: ``full`` = side effects persist (today's default); ``output-only`` =
#: "just bring back the output", persisted side effects are dropped.
RETURN_VALUES = ("output-only", "full")


@dataclass(frozen=True, slots=True)
class Envelope:
    """A resolved permission box. See the module docstring for the axes.

    Defaults are the **permissive** end of every axis so an unspecified
    envelope is exactly today's behavior (dark rollout).
    """

    egress: str = "open"
    write: str = "full"
    return_: str = "full"


#: The dark default — everything permissive, identical to pre-slice-8.
DEFAULT = Envelope()


# ── The precis MCP write verbs + fetch tools (tier-1 deny targets) ─
#
# The agentic jobs reach the DB through the precis MCP server, so the
# write boundary at the *tool* layer is the precis mutate verbs. The
# fetch/search tools are the egress boundary at the tool layer. (These are
# the cooperative tier; the DB role and no-net container are the real ones.)
_PRECIS_WRITE_VERBS: tuple[str, ...] = (
    "mcp__precis__put",
    "mcp__precis__edit",
    "mcp__precis__delete",
    "mcp__precis__tag",
    "mcp__precis__link",
)
#: Built-in tools that can write the local filesystem — denied alongside the
#: MCP verbs for a ``write:none`` box (a read-only task shouldn't edit files).
_FS_WRITE_TOOLS: tuple[str, ...] = ("Write", "Edit", "NotebookEdit")
#: Built-in tools that reach the network — denied for a ``egress:none`` box.
_FETCH_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")


def parse_envelope(meta: dict[str, Any] | None) -> Envelope:
    """Resolve ``meta.envelope`` into an :class:`Envelope` (never raises).

    An absent envelope, a non-dict envelope, or any axis carrying an
    out-of-vocabulary value falls back to that axis's permissive
    :data:`DEFAULT` — the dark path. A bad value is *logged* (an operator
    typo shouldn't silently tighten *or* loosen a box unexpectedly) but the
    job still runs, at the safe-for-compat default.
    """
    if not meta:
        return DEFAULT
    raw = meta.get("envelope")
    if not isinstance(raw, dict):
        return DEFAULT
    return Envelope(
        egress=_axis(raw, "egress", EGRESS_VALUES, DEFAULT.egress),
        write=_axis(raw, "write", WRITE_VALUES, DEFAULT.write),
        return_=_axis(raw, "return", RETURN_VALUES, DEFAULT.return_),
    )


def _axis(raw: dict[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    val = raw.get(key)
    if val is None:
        return default
    if isinstance(val, str) and val in allowed:
        return val
    log.warning(
        "envelope: ignoring out-of-vocabulary %s=%r (allowed: %s); falling back to %r",
        key,
        val,
        ", ".join(allowed),
        default,
    )
    return default


# ── Tier resolvers ────────────────────────────────────────────────


def disallowed_tools(env: Envelope) -> tuple[str, ...]:
    """Tier-1 (tool-level) deny list for an envelope.

    ``write != full`` drops the precis mutate verbs (and, for ``none``, the
    filesystem-write tools); ``egress == none`` drops the fetch/search
    tools. The permissive default yields ``()`` — nothing denied.

    The caller merges this into ``claude -p``'s ``--settings
    permissions.deny`` (see :func:`precis.utils.claude_agent.call_claude_agent`).
    Cooperative: a determined agent that ignored the deny would still hit
    the DB role (:func:`db_role`) or the no-net container
    (:func:`network_mode`) — the real boundaries.
    """
    deny: list[str] = []
    if env.write == "none":
        deny.extend(_PRECIS_WRITE_VERBS)
        deny.extend(_FS_WRITE_TOOLS)
    elif env.write == "scoped":
        # Scoped still writes (agent_rw at the DB) but not the destructive
        # verbs — a scoped task may put/edit/tag but not delete.
        deny.append("mcp__precis__delete")
    if env.egress == "none":
        deny.extend(_FETCH_TOOLS)
    return tuple(deny)


def db_role(env: Envelope) -> str:
    """Tier-2 (process-level) Postgres role for an envelope.

    ``write:none`` → ``agent_ro`` (writes refused by the database itself);
    everything else → ``agent_rw``. Consumed by the per-call ``precis
    serve`` the container executor spawns (§13): it swaps its DSN's role to
    the resolved one so the enforcement is at the database, not cooperative.
    """
    return "agent_ro" if env.write == "none" else "agent_rw"


def network_mode(env: Envelope) -> str | None:
    """Tier-3 (network-level) container network mode for an envelope.

    ``egress:none`` → ``"none"`` (``docker run --network none`` — the only
    true egress denial); ``api-only`` → ``"api-only"`` (the container
    executor resolves this to the 2-entry LLM/pgbouncer allowlist, §13);
    ``open`` → ``None`` (default networking). Consumed by the
    ``claude_docker`` / ``sandbox_run`` executor.
    """
    if env.egress == "none":
        return "none"
    if env.egress == "api-only":
        return "api-only"
    return None


def drops_side_effects(env: Envelope) -> bool:
    """Tier-agnostic: does this envelope ask for output only (no persist)?

    ``return:output-only`` means the executor should bring back the task's
    output but **not** persist its side effects (a dry-run / preview box).
    """
    return env.return_ == "output-only"


# ── Executor-scoped active envelope ───────────────────────────────
#
# The executor claims a job, reads its ``meta.envelope``, and wraps the run
# in :func:`envelope_scope` so the envelope is honored "by the executor
# regardless of host" without threading the box through every job_type and
# LLM call site. The ``call_claude_agent`` chokepoint reads the active
# envelope when no explicit one is passed. Dark: no scope set → ``None`` →
# today's behavior.

_active: ContextVar[Envelope | None] = ContextVar("precis_envelope", default=None)


@contextmanager
def envelope_scope(env: Envelope | None) -> Iterator[None]:
    """Bind ``env`` as the active envelope for the duration of the block.

    The executor wraps a job run in this; agentic calls made inside pick the
    envelope up via :func:`active_envelope`. ``None`` is a no-op scope (dark).
    """
    token = _active.set(env)
    try:
        yield
    finally:
        _active.reset(token)


def active_envelope() -> Envelope | None:
    """The envelope bound by the enclosing :func:`envelope_scope`, if any."""
    return _active.get()


__all__ = [
    "DEFAULT",
    "EGRESS_VALUES",
    "RETURN_VALUES",
    "WRITE_VALUES",
    "Envelope",
    "active_envelope",
    "db_role",
    "disallowed_tools",
    "drops_side_effects",
    "envelope_scope",
    "network_mode",
    "parse_envelope",
]
