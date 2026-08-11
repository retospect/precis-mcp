"""Shared derivation helpers over ``KindSpec`` — the single place a
downstream module turns a ``protocol.KindSpec`` fact (``is_numeric`` /
``corpus_role`` / ``role``) into a ``frozenset[str]`` of kinds, instead of
hand-restating the membership (the "KindSpec facts re-hardcoded downstream"
drift class — ``news``/``message``/``role3`` all slipped through manual
lists the same way ``handle_registry``'s docstring warns about).

Two shapes of caller:

* **A hub/runtime is reachable at call time** (a request handler with
  ``get_runtime(request).hub``, a booted CLI). Call :func:`specs_of` on the
  live hub and feed the result to :func:`numeric_kinds` /
  :func:`corpus_role_kinds` / :func:`role_kinds` — the set is always fresh,
  a new kind joins with no edit here.
* **An import-time module constant, no hub reachable** (a frozenset built
  once at module load, e.g. a fisheye/ring kind-grouping table). Booting a
  hub just to compute one import-time constant isn't worth the coupling, so
  these stay hand-maintained literals — but ``tests/test_kind_totality.py``
  boots a hub once and asserts every such constant against the derivation
  here, so a forgotten touch-point when a kind's ``KindSpec`` changes fails
  CI instead of drifting silently.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.dispatch import Hub
    from precis.protocol import KindSpec


def specs_of(hub: Hub) -> list[KindSpec]:
    """The live ``KindSpec`` roster for a booted hub.

    Thin alias over ``hub.kind_specs()`` so call sites read
    ``kind_facts.specs_of(hub)`` alongside the other derivations in this
    module rather than reaching into ``Hub`` directly. Reflects only kinds
    that actually *constructed* — a credential-gated kind (``patent``
    without an EPO OPS key, ``math`` without ``WOLFRAM_APP_ID``) is absent
    here even though its ``KindSpec`` exists; see :func:`all_declared_specs`
    for the credential-independent roster.
    """
    return hub.kind_specs()


def all_declared_specs() -> list[KindSpec]:
    """Every ``KindSpec`` declared under ``precis.handlers`` — the full
    static roster, independent of which kinds actually construct at boot.

    A booted :class:`~precis.dispatch.Hub` under-reports: a kind gated on
    ``requires_env``/``requires_secret`` (``patent`` needs an EPO OPS key,
    ``math`` needs ``WOLFRAM_APP_ID``) never constructs in an environment
    missing those — most test containers — so it's absent from
    :func:`specs_of`'s result even though its ``KindSpec`` is real. This
    walks every public submodule of ``precis.handlers`` (no store, no
    embedder, no env needed — handler modules defer any heavy/credentialed
    import into ``__init__``/methods, the same discipline
    ``precis.dispatch._try`` relies on to catch a missing optional dep at
    *construction* time rather than *import* time) and collects every
    class attribute named ``spec`` that is a ``KindSpec`` instance, deduped
    by kind. Scoped to ``precis.handlers`` on purpose — a plugin kind
    (``precis_bio``'s ``protein``, ``precis_pathway``'s ``route``, …) isn't
    part of this repo's ``KindSpec`` totality contract (mirrors
    ``tests/test_handle_registry.py``'s ``KIND_CODES`` totality, which is
    scoped the same way).

    Prefer :func:`specs_of` when a real hub is reachable — it's the
    ground truth for "what's actually enabled on THIS deployment"; this is
    for a hub-less or credential-starved context (tests) that still wants
    every kind's *declared* facts.
    """
    import precis.handlers as handlers_pkg
    from precis.protocol import KindSpec as _KindSpec

    out: dict[str, _KindSpec] = {}
    for info in pkgutil.iter_modules(handlers_pkg.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"precis.handlers.{info.name}")
        for obj in vars(module).values():
            spec = getattr(obj, "spec", None)
            if isinstance(spec, _KindSpec) and spec.kind not in out:
                out[spec.kind] = spec
    return sorted(out.values(), key=lambda s: s.kind)


def numeric_kinds(specs: Iterable[KindSpec]) -> frozenset[str]:
    """Kinds whose public id is numeric (``KindSpec.is_numeric``)."""
    return frozenset(s.kind for s in specs if s.is_numeric)


def corpus_role_kinds(specs: Iterable[KindSpec], *roles: str) -> frozenset[str]:
    """Kinds whose ``KindSpec.corpus_role`` is one of ``roles``.

    E.g. ``corpus_role_kinds(specs, "evidence", "spec")`` for every
    citable-or-spec document kind (the two non-``"none"`` corpus roles).
    """
    role_set = frozenset(roles)
    return frozenset(s.kind for s in specs if s.corpus_role in role_set)


def role_kinds(specs: Iterable[KindSpec], role: str) -> frozenset[str]:
    """Kinds whose organizational ``KindSpec.role`` equals ``role``.

    E.g. ``role_kinds(specs, "artifact")`` for the placeable-in-a-folder
    kinds.
    """
    return frozenset(s.kind for s in specs if s.role == role)


__all__ = [
    "all_declared_specs",
    "corpus_role_kinds",
    "numeric_kinds",
    "role_kinds",
    "specs_of",
]
