"""Web-editable runtime overrides for LLM backend + model selection.

The ``/factory`` console can write ``app_settings`` rows that override the
``PRECIS_LLM_BACKEND`` / ``PRECIS_MODEL_*`` env defaults **live — no redeploy**,
so an operator flips the whole fleet's LLM backend, or a per-tier model, from
the browser (or, until the console write side lands, a single SQL ``INSERT``).
This module is the **read** side: a small TTL-cached layer the router's
:func:`~precis.utils.llm.router.resolve_backend` /
:func:`~precis.utils.llm.router.resolve_model` consult *before* env.

Resolution order (per key): **app_settings DB row → env default → compiled
default**. Ships **dark**: with no store bound (tests, DB-free CLI) or no row
written, every read returns ``None`` and the router falls back to env — so with
nothing set, behavior is byte-identical to before.

Sourced from the same process-bound store the budget breaker uses
(:func:`precis.budget.meter.active_store`), read through the generic
``app_settings`` KV (:func:`precis.budget.settings.get_setting`, migration
0070). A short TTL cache (matching the meter's ~15s) bounds per-dispatch DB
overhead and, because ``resolve_model`` is on the dispatch hot path, keeps it
from querying every call; a flip is picked up within one TTL across every
process. The budget/meter imports are **lazy** (inside the reads) so the router
module's import graph stays free of the DB chain, exactly as it is today.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.utils.llm.router import Tier

log = logging.getLogger(__name__)

#: app_settings key for the backend family — ``"anthropic"`` | ``"openai"``.
BACKEND_KEY = "llm.backend"
#: app_settings key prefix for a per-tier model override: ``llm.model.<tier>``
#: (e.g. ``llm.model.cloud-super``). The suffix is the ``Tier`` string value,
#: so the console writes the same tier vocabulary the resolver keys on.
MODEL_KEY_PREFIX = "llm.model."

#: How long a read is reused before re-querying. Matches the budget meter's
#: cache window; short enough that a console flip is seen promptly.
_TTL_S = 15.0

_lock = threading.Lock()
#: ``key -> (expiry_monotonic, value_or_None)``. A ``None`` value is cached too
#: (negative cache) so an absent row doesn't hit the DB on every call.
_cache: dict[str, tuple[float, str | None]] = {}


def model_key(tier: Tier) -> str:
    """The ``app_settings`` key a per-tier model override lives under."""
    return f"{MODEL_KEY_PREFIX}{tier.value}"


def backend_override() -> str | None:
    """The web-set backend family (``"anthropic"`` / ``"openai"``), or ``None``.

    ``None`` = no row / no store / an unrecognized value → the router keeps its
    env default. An unknown string is dropped (a typo can't dark the fleet).
    """
    raw = _cached_setting(BACKEND_KEY)
    if raw is None:
        return None
    low = raw.lower()
    if low in ("anthropic", "openai"):
        return low
    log.warning("live_config: ignoring unknown %s=%r", BACKEND_KEY, raw)
    return None


def model_override(tier: Tier) -> str | None:
    """The web-set model id for ``tier``, or ``None`` (→ env / compiled)."""
    return _cached_setting(model_key(tier))


def bust_cache() -> None:
    """Drop the TTL cache so the *next* read re-queries the DB.

    The console write side calls this so its own process reflects a flip
    immediately; other processes pick it up within one TTL.
    """
    with _lock:
        _cache.clear()


def _cached_setting(key: str) -> str | None:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    value = _read_setting(key)
    with _lock:
        _cache[key] = (now + _TTL_S, value)
    return value


def _read_setting(key: str) -> str | None:
    # Lazy imports: the router calls the resolvers, and its module docstring
    # promises to stay free of the worker/DB import chain — so pull budget/meter
    # in only when a read actually runs (mirrors dispatch()'s lazy breaker
    # import). A missing table / query error returns None (get_setting swallows
    # it), so an un-migrated or unreachable DB is just "no override".
    from precis.budget import meter
    from precis.budget import settings as budget_settings

    store = meter.active_store()
    if store is None:
        return None
    raw = budget_settings.get_setting(store, key)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


__all__ = [
    "BACKEND_KEY",
    "MODEL_KEY_PREFIX",
    "backend_override",
    "bust_cache",
    "model_key",
    "model_override",
]
