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

## Domains (gr572719)

A :class:`Domain` is a named partition of the surface — ``{name, kinds,
skill families, memory SPACE: tags}`` — that a connection bound to it
serves. The registry is the :data:`DOMAINS` code constant (coordinator
ruling gr604579, 2026-11-09; a DB-resident settings kind lands only if
operators need to edit it). :func:`current_domain` resolves, beside
:func:`current_scope`:

1. the connection token's bound domain — fencing slice 4 wires the
   shared streamable-http server's token gate to
   :data:`_request_domain`, same per-request seam the read scope takes;
2. ``PRECIS_DOMAIN`` — the process env overlay the envelope exports;
3. the default ``'all'``.

``'all'`` leaves every axis ``None`` (unrestricted), which is
byte-identical to today: the slice ships the model and resolution dark,
and nothing consumes a domain's axes until the fencing slices that
follow — the fan-out kinds fence first (gr572720), then the ``spaces``
axis (gr572723): ``search(kind='memory')`` on a bounded domain defaults
to the domain's ``SPACE:`` set, and a caller's ``SPACE:`` tag wins. An
env value naming no registry entry raises — a typo must fail loud,
never silently widen to ``'all'``. A ``Domain`` naming an unknown kind /
family / space raises ``ValueError`` from ``__post_init__``, so a bad
registry entry fails at *import* (the constant build), not at serve —
the lint is the construction itself.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from precis.store.types import _CLOSED_VOCAB
from precis.utils.handle_registry import is_known_kind
from precis.utils.skill_common import SKILL_FAMILIES

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


# ---------------------------------------------------------------------------
# Domains (gr572719)
# ---------------------------------------------------------------------------

#: The process env overlay — the envelope exports ``PRECIS_DOMAIN=<name>``
#: to pin a run's surface to one domain.
DOMAIN_ENV = "PRECIS_DOMAIN"

#: Families every domain carries whatever it declares — verbs and
#: addressing (``core``), work tracking (``work``), citations and
#: provenance (``evidence``), recall (``memory``) and the file corpus
#: (``files``). A domain widens beyond these; it can never drop one.
ALWAYS_IN_FAMILIES: frozenset[str] = frozenset(
    {"core", "work", "evidence", "memory", "files"}
)

_SPACE_VALUES: frozenset[str] = _CLOSED_VOCAB["SPACE"]


@dataclass(frozen=True)
class Domain:
    """One named surface partition: the kinds, skill families and memory
    ``SPACE:`` tags a connection bound to it may see.

    ``None`` on an axis leaves it unrestricted — the dark default
    ``'all'`` is ``None`` on all three, byte-identical to the pre-domain
    surface. ``spaces`` entries are the bare ``SPACE:`` values
    (``repo-dev``); a ``SPACE:``-prefixed spelling normalises to bare,
    mirroring ``utils.eye_render.RingFilter.parse``.

    ``__post_init__`` validates every named kind / family / space against
    the closed registries (``handle_registry.is_known_kind``,
    ``SKILL_FAMILIES``, ``store.types._CLOSED_VOCAB['SPACE']``) and
    unions :data:`ALWAYS_IN_FAMILIES` into a declared ``families`` set —
    so ``domain.families`` is always the *effective* serve set and a bad
    registry entry fails at import, not at serve.
    """

    name: str
    kinds: frozenset[str] | None = None
    families: frozenset[str] | None = None
    spaces: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.kinds is not None:
            kinds = frozenset(self.kinds)
            bad = sorted(k for k in kinds if not is_known_kind(k))
            if bad:
                raise ValueError(
                    f"domain {self.name!r}: unknown kind(s) {bad} "
                    "— not in the handle registry"
                )
            object.__setattr__(self, "kinds", kinds)
        if self.families is not None:
            declared = frozenset(self.families)
            bad = sorted(declared - set(SKILL_FAMILIES))
            if bad:
                raise ValueError(
                    f"domain {self.name!r}: unknown skill "
                    f"families {bad} — not in SKILL_FAMILIES"
                )
            object.__setattr__(self, "families", declared | ALWAYS_IN_FAMILIES)
        if self.spaces is not None:
            spaces = frozenset(
                s[len("SPACE:") :] if s.upper().startswith("SPACE:") else s
                for s in self.spaces
            )
            bad = sorted(spaces - _SPACE_VALUES)
            if bad:
                raise ValueError(
                    f"domain {self.name!r}: unknown SPACE "
                    f"values {bad} — not in the closed vocab"
                )
            object.__setattr__(self, "spaces", spaces)


#: The domain registry — a code constant (coordinator ruling gr604579,
#: 2026-11-09; a DB-resident settings kind lands if operators need to
#: edit it). ``'all'`` is the dark default: every axis open. Named
#: domains arrive with the slices that fence on them; adding one is
#: editing this dict, and an unknown kind / family / space in it fails
#: the import that builds it.
DOMAINS: dict[str, Domain] = {
    "all": Domain(name="all"),
    # The electronics surface (gr572720 — the first named domain, lands
    # with the fan-out kinds fence that consumes it). ``kinds`` is the
    # electronics corpus plus the shared commons
    # :data:`ALWAYS_IN_FAMILIES` encodes for skills — work, memory,
    # evidence, files, core — mirrored as kinds so a bound connection
    # still tracks and recalls its own work. The other specialist
    # clusters (se/protein/route/pathway/chem/rxn/structure/cad/
    # estimate) stay outside, and so do the literature, drafting and
    # external-tools families; an explicit kind= reaches any of them —
    # the fence narrows the *default* fan-out, never the door.
    "pcb": Domain(
        name="pcb",
        kinds=frozenset(
            {
                # the electronics corpus itself
                "pcb",
                "part",
                "datasheet",
                "component",
                "measure",
                "material",
                # work commons
                "todo",
                "quest",
                "job",
                "gripe",
                "fleet",
                "alert",
                "agentlog",
                "message",
                # memory commons
                "memory",
                "anki",
                "conv",
                "learning",
                # evidence commons
                "finding",
                "citation",
                "concept",
                "taxon",
                "oracle",
                "provenance",
                # files commons
                "markdown",
                "plaintext",
                "tex",
                "python",
                "md",
                "folder",
                # core commons + the generic tools a bounded session keeps
                "skill",
                "tag",
                "alias",
                "checklist",
                "profile",
                "machine",
                "llm",
                "calc",
                "time",
                "math",
                "random",
            }
        ),
        families=frozenset({"pcb"}),
    ),
    # gr572723: the repo-dev lane. Only the memory SPACE: axis fences
    # today — kinds/families stay open until their own slices declare
    # them.
    "repo-dev": Domain(name="repo-dev", spaces=frozenset({"repo-dev"})),
}

#: The connection token's bound domain (fencing slice 4): the shared
#: streamable-http server binds it per request through
#: :func:`request_domain`; ``None`` falls through to the process overlay.
_request_domain: ContextVar[Domain | None] = ContextVar(
    "precis_runtime_request_domain", default=None
)


@contextmanager
def request_domain(domain: Domain | None) -> Iterator[None]:
    """Bind ``domain`` for the current request's task — the per-request
    seam the shared server's token gate uses (fencing slice 4), same
    shape as the read scope's request source. ``None`` clears to the
    process overlay."""
    token = _request_domain.set(domain)
    try:
        yield
    finally:
        _request_domain.reset(token)


def current_domain() -> Domain:
    """The :class:`Domain` this connection serves; ``'all'`` by default.

    Resolution order: the request-bound connection token
    (:data:`_request_domain`) → the ``PRECIS_DOMAIN`` process overlay →
    ``'all'``. Read per request on the shared server; the registry is a
    per-process constant, so resolution is one dict lookup. An env value
    naming no registry entry raises ``ValueError`` — a typo must fail
    loud, never silently widen to ``'all'``.
    """
    bound = _request_domain.get()
    if bound is not None:
        return bound
    name = os.environ.get(DOMAIN_ENV, "").strip() or "all"
    try:
        return DOMAINS[name]
    except KeyError:
        raise ValueError(
            f"{DOMAIN_ENV}={name!r} names no registered domain "
            f"(known: {', '.join(sorted(DOMAINS))})"
        ) from None
