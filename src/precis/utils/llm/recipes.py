"""Cached demand metadata, independent of routing and request hashes.

Recipes share the route logger's boot-time store binding and are cached for
``_TTL`` seconds (300); a missing database or table yields an empty cache so
telemetry cannot prevent an LLM call. A failed reload is not cached as a
hit: the previous rows keep serving (a different bound store keeps nothing)
and the next attempt comes ``_RETRY`` seconds (30) later, logging once per
failure window — one pool timeout must not stamp every source unknown for
the full TTL. A request's task kind, consequence and privacy ride along
without touching routing, blob policy or request hashes;
``route_log.record_call`` stamps every writer, including agent-cost records.
The scheduler's six-hour ``recipe_gap`` scan reports unknown sources with
daily deduplication instead of writing gripes from the router, skipping
sources a live recipe already covers (stale ``unknown.`` rows outlive the
registration that resolved their gripe by up to 24 h).
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, NamedTuple

from precis import route_log

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Recipe:
    """One source's declared demand; none of these fields steer routing yet."""

    source: str
    task_kind: str
    lane_class: str = "none"
    consequence: str = "medium"
    classify: bool = False
    requirement: dict[str, Any] = field(default_factory=dict)
    difficulty_prior: int | None = None
    validators: tuple[str, ...] = ()


class Resolution(NamedTuple):
    """Effective demand and task-kind provenance."""

    task_kind: str
    consequence: str
    privacy_class: str
    origin: str


_TTL = 300.0
#: Retry interval after a failed load — long enough to ride out a restart,
#: short enough that a transient pool error cannot blank recipes for _TTL.
_RETRY = 30.0
_cache: dict[str, Recipe] = {}
_expires = 0.0
_store_identity: object = None


def _rows() -> dict[str, Recipe]:
    global _cache, _expires, _store_identity
    store = route_log.bound_store()
    now = monotonic()
    if store is _store_identity and now < _expires:
        return _cache
    rows: dict[str, Recipe] = {}
    failed = False
    if store is not None:
        try:
            with store.pool.connection() as conn:
                for row in conn.execute(
                    "SELECT source, task_kind, lane_class, consequence, classify, "
                    "requirement, difficulty_prior, validators FROM task_recipes"
                ).fetchall():
                    recipe = Recipe(
                        source=row[0],
                        task_kind=row[1],
                        lane_class=row[2],
                        consequence=row[3],
                        classify=row[4],
                        requirement=row[5],
                        difficulty_prior=row[6],
                        validators=tuple(row[7]),
                    )
                    rows[recipe.source] = recipe
        except Exception:
            log.warning("task_recipes load failed", exc_info=True)
            failed = True
    if failed:
        _expires = now + _RETRY
        if store is _store_identity and _cache:
            return _cache
        _cache, _store_identity = rows, store
        return rows
    _cache, _expires, _store_identity = rows, now + _TTL, store
    return rows


def _covered_key(sources: Collection[str], source: str) -> str | None:
    if source in sources:
        return source
    base = source.removesuffix(":local")
    if base in sources:
        return base
    prefixes = [key for key in sources if key.endswith(":") and base.startswith(key)]
    return max(prefixes, key=len) if prefixes else None


def lookup(source: str) -> Recipe | None:
    """Exact, one trailing :local inheritance, then longest prefix family."""
    rows = _rows()
    key = _covered_key(rows, source)
    return rows[key] if key is not None else None


def covers(recipe_sources: Collection[str], source: str) -> bool:
    """True when ``recipe_sources`` — a ``task_recipes.source`` set — covers
    ``source`` under :func:`lookup`'s rules (exact, ``:local`` base, longest
    ``:``-terminated prefix family). Lets a pass that already holds a store
    check coverage against the table it just read, instead of the ambient
    process-bound cache :func:`_rows` serves the router's hot path."""
    return _covered_key(recipe_sources, source) is not None


def resolve(
    source: str,
    caller_task_kind: str | None = None,
    caller_consequence: str | None = None,
    caller_privacy_class: str | None = None,
) -> Resolution:
    """Caller values win independently; unknown sources default to medium."""
    recipe = lookup(source)
    return Resolution(
        caller_task_kind
        if caller_task_kind is not None
        else recipe.task_kind
        if recipe
        else f"unknown.{source}",
        caller_consequence
        if caller_consequence is not None
        else recipe.consequence
        if recipe
        else "medium",
        caller_privacy_class if caller_privacy_class is not None else "default",
        "caller" if caller_task_kind is not None else "recipe" if recipe else "unknown",
    )
