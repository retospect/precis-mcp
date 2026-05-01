"""Handler registration + flat dispatch table (seven-verb surface).

Replaces the v1 ``precis.registry`` module. The old module stays
in-tree during phase 1 of the seven-verb migration (see
``docs/seven-verb-surface-migration.md``) so the live server keeps
working while handlers are ported; it is deleted once nothing imports
it.

## Handler-author contract

Handler ``__init__`` must do all validation before its first
``register_ability`` / ``register_skill`` / ``register_overview``
call. If any validation fails, raise :class:`InitError` with a short
actionable message. :func:`boot` catches ``InitError``, logs WARN,
and leaves the kind absent from the dispatch surface. Any other
exception propagates — it's a bug, not a missing dep, and should
crash boot so it gets noticed. Register as the last block of
``__init__``; once any ``register_*`` call has run, the instance is
committed to the registry.

## Failure-mode semantics

A handler's ``__init__`` has exactly two exit paths:

- *Returns normally.* Instance exists, fully wired, every ability
  in the dispatch table points at a working bound method. Kind is
  live on the LLM surface.
- *Raises ``InitError``.* No instance created (Python never binds a
  name on a raising ``__init__``). Registry state untouched because
  registration is the last block. Kind invisible: absent from
  ``tools/list`` dispatch, from ``precis-help``, from search
  suggestions. Operator sees one WARN line naming the reason.

See D7 in the migration doc for the rationale and rejected
alternatives.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Dispatch key: (kind, verb, mode). ``mode`` is ``None`` when the verb
# takes a single shape for the kind (e.g. ``delete``, ``tag``, ``link``).
AbilityKey = tuple[str, str, str | None]

# An ability is any callable; handler methods bind self via ``self.method``,
# so the signature is homogenised to ``**kwargs`` at the dispatch boundary.
# Each tool function in server.py knows the concrete call shape for its verb.
Ability = Callable[..., Any]


class InitError(RuntimeError):
    """Raised by a handler's ``__init__`` when it can't be usefully constructed.

    The boot loop catches this, logs WARN, and leaves the kind
    unregistered. See module docstring for the full contract.
    """


class DuplicateRegistration(RuntimeError):
    """Two handlers tried to register the same ``(kind, verb, mode)``.

    This is always a programming error — one handler is stepping on
    another's namespace. Fail loud at boot so the operator notices,
    rather than silently shadowing abilities at dispatch time.
    """


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class Registry:
    """Flat dispatch table: ``(kind, verb, mode) -> callable``.

    Owned by :func:`boot`. Handlers mutate it via the ``register_*``
    methods during their own ``__init__``. After boot it is read-only
    from the server's perspective; the MCP tool functions in
    ``server.py`` consult ``self.abilities`` on every call.

    No decorator magic, no reflection on method names — every entry
    is placed here by an explicit ``register_ability(...)`` call, which
    makes ``rg register_ability`` the authoritative "who handles X?"
    query.
    """

    abilities: dict[AbilityKey, Ability] = field(default_factory=dict)
    skills:    dict[str, str]            = field(default_factory=dict)
    overview:  dict[str, str]            = field(default_factory=dict)

    # ----- registration primitives (called from handler __init__) -----

    def register_ability(
        self,
        kind: str,
        verb: str,
        mode: str | None,
        fn: Ability,
    ) -> None:
        """Record one dispatch entry. Raises on duplicate key.

        ``mode`` is ``None`` when the verb is single-shaped for the
        kind. ``put`` is the only verb with multiple modes after the
        seven-verb migration (``create`` and ``replace``).
        """
        key: AbilityKey = (kind, verb, mode)
        if key in self.abilities:
            raise DuplicateRegistration(f"duplicate ability: {key!r}")
        self.abilities[key] = fn

    def register_skill(self, slug: str, text: str) -> None:
        """Publish a skill document under ``slug``. Raises on duplicate."""
        if slug in self.skills:
            raise DuplicateRegistration(f"duplicate skill: {slug!r}")
        self.skills[slug] = text

    def register_overview(self, kind: str, blurb: str) -> None:
        """Publish a one-line overview of ``kind``.

        Later-registered blurbs for the same kind overwrite silently —
        this is intentional: a composite handler that hosts multiple
        kinds can set an aggregate blurb after its per-kind calls.
        """
        self.overview[kind] = blurb

    # ----- read views (for the server and for internal introspection) -----

    @property
    def kinds(self) -> set[str]:
        """All kinds with at least one registered ability."""
        return {k for (k, _v, _m) in self.abilities}

    def verbs_for(self, kind: str) -> set[str]:
        """Set of verbs the given kind supports. Empty if kind unknown."""
        return {v for (k, v, _m) in self.abilities if k == kind}

    def modes_for(self, kind: str, verb: str) -> set[str | None]:
        """Modes registered for ``(kind, verb)``. Empty if none."""
        return {m for (k, v, m) in self.abilities if k == kind and v == verb}

    def kinds_supporting(self, verb: str) -> set[str]:
        """All kinds that have at least one ``(kind, verb, *)`` entry."""
        return {k for (k, v, _m) in self.abilities if v == verb}

    def get(self, kind: str, verb: str, mode: str | None = None) -> Ability | None:
        """Look up one ability. Returns ``None`` on miss.

        The server's tool functions use this; miss-handling (building
        the typed error + hint from D8) lives in the dispatch layer,
        not here.
        """
        return self.abilities.get((kind, verb, mode))


# ---------------------------------------------------------------------------
# Boot helpers
# ---------------------------------------------------------------------------


def _try(cls: Callable[..., Any], *args: Any, **kw: Any) -> Any | None:
    """Construct a handler; swallow :class:`InitError` into ``None``.

    Only ``InitError`` is caught — a stray ``KeyError`` /
    ``AttributeError`` is a programmer bug, not a missing dep, and
    should crash boot so it gets noticed.

    Logs at WARN when an ``InitError`` is swallowed, including the
    handler class name so operators can grep for "<ClassName> init
    failed" in the server log.
    """
    try:
        return cls(*args, **kw)
    except InitError as exc:
        log.warning("%s init failed: %s", getattr(cls, "__name__", cls), exc)
        return None


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def boot(env: dict[str, Any] | None = None) -> Registry:
    """Build and return a fully-populated :class:`Registry`.

    The composition root. Hand-ordered by dependency: construct
    infrastructure kinds first (filereader, embedder, store), then
    the kinds that consume them. Each step goes through :func:`_try`
    so any :class:`InitError` is logged and the kind silently drops
    off the LLM surface.

    ``env`` is the same config bag the v1 ``builtins()`` took —
    a plain dict for now. During phase 1 of the seven-verb migration
    this function is a stub; real handler wiring is added as each
    handler is ported to the constructor-registers shape.

    See ``docs/seven-verb-surface-migration.md`` D7/D8 for the full
    design and rejected alternatives.
    """
    env = env or {}
    r = Registry()

    # TODO(seven-verb-phase1): wire handlers here, ordered by dep.
    # Each line is ``_try(HandlerCls, r, env, *deps)``. Handlers whose
    # required deps returned None are skipped entirely (no attempt,
    # no WARN spam).

    log.info(
        "precis dispatch boot: %d kinds live: %s",
        len(r.kinds),
        sorted(r.kinds),
    )
    return r


__all__ = [
    "Ability",
    "AbilityKey",
    "DuplicateRegistration",
    "InitError",
    "Registry",
    "boot",
]
