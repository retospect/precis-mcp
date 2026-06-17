"""Discover and load ref-pass plugins via ``precis.ref_passes``.

Third-party packages can advertise background workers (RefPass
callables) without needing to fork ``cli/worker.py``. The first
real consumer is precis-dft's ``view_worker`` — a SQL scan that
materializes annotation chunks on ``structure_draft`` rows.

Entry-point shape::

    [project.entry-points."precis.ref_passes"]
    view_worker = "precis_dft.workers.view_worker:factory"

Each entry resolves to a **factory**, not the pass itself. The
factory receives the runtime store + the profile the worker is
running under + the parsed CLI args and returns either:

- ``(pass_name, callable, profiles)`` — the pass is registered.
  ``pass_name`` is the name ``_pass_enabled`` checks; ``callable``
  matches :class:`precis.workers.runner.RefPass`; ``profiles``
  is the set of ``--profile`` values this pass belongs to (e.g.
  ``frozenset({'system'})``).
- ``None`` — the plugin opts out of this worker invocation
  (e.g. the node lacks a GPU and the plugin's worker requires
  one).

Failure semantics mirror :func:`precis.dispatch._load_plugins`:
every ``Exception`` at every step (EP load, factory call, return-
shape validation) is caught and logged. One broken plugin must
not brick the worker process.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


#: Entry-point group third-party packages use to advertise their
#: ref passes. Discovered by :func:`discover_plugin_ref_passes`.
REF_PASS_PLUGIN_GROUP = "precis.ref_passes"


#: Type alias for the factory's return value when it opts in.
PluginPass = tuple[str, Callable[[int], BatchResult], frozenset[str]]


def _entry_points(group: str) -> list[Any]:
    """Indirection around ``importlib.metadata.entry_points``.

    Mirrors the pattern in :mod:`precis.workers.job_types` and
    :mod:`precis.store.migrate` so tests can patch this single
    function to inject fake plugin factories without setting up
    a real wheel install.
    """
    from importlib.metadata import entry_points

    return list(entry_points(group=group))


def discover_plugin_ref_passes(
    store: Any,
    *,
    profile: str,
    args: Any,
) -> list[PluginPass]:
    """Walk the ``precis.ref_passes`` group and return registered passes.

    Returns the list of ``(pass_name, callable, profiles)`` tuples
    for every factory that opted in. Broken plugins are logged and
    skipped — the caller continues with whatever did register.

    ``profile`` and ``args`` are forwarded to each factory so a
    plugin can decide at build time whether it belongs on this
    invocation (e.g. ``view_worker`` only registers under
    ``profile='system'``).
    """
    out: list[PluginPass] = []

    try:
        eps = _entry_points(REF_PASS_PLUGIN_GROUP)
    except Exception as exc:  # defensive — importlib surface is stable
        log.warning("precis.ref_passes discovery failed: %s", exc)
        return out

    for ep in eps:
        name = getattr(ep, "name", "<unknown>")
        try:
            factory = ep.load()
        except Exception as exc:
            log.warning(
                "precis.ref_passes plugin %r failed to load (%s): %s",
                name,
                type(exc).__name__,
                exc,
            )
            continue

        if not callable(factory):
            log.warning(
                "precis.ref_passes plugin %r resolved to a non-callable; skipping",
                name,
            )
            continue

        try:
            result = factory(store, profile=profile, args=args)
        except Exception as exc:
            log.warning(
                "precis.ref_passes plugin %r factory raised %s: %s",
                name,
                type(exc).__name__,
                exc,
            )
            continue

        if result is None:
            log.info(
                "precis.ref_passes plugin %r opted out of profile=%s",
                name,
                profile,
            )
            continue

        try:
            pass_name, pass_callable, profiles = result
        except (TypeError, ValueError) as exc:
            log.warning(
                "precis.ref_passes plugin %r factory returned bad shape (%s); skipping",
                name,
                exc,
            )
            continue

        if not isinstance(pass_name, str):
            log.warning(
                "precis.ref_passes plugin %r returned non-str pass_name; skipping",
                name,
            )
            continue
        if not callable(pass_callable):
            log.warning(
                "precis.ref_passes plugin %r returned non-callable pass; skipping",
                name,
            )
            continue
        if not isinstance(profiles, frozenset):
            log.warning(
                "precis.ref_passes plugin %r returned profiles as %s, not frozenset; "
                "coercing",
                name,
                type(profiles).__name__,
            )
            try:
                profiles = frozenset(profiles)
            except TypeError:
                log.warning(
                    "precis.ref_passes plugin %r profiles is not iterable; skipping",
                    name,
                )
                continue

        out.append((pass_name, pass_callable, profiles))

    return out


__all__ = [
    "REF_PASS_PLUGIN_GROUP",
    "PluginPass",
    "discover_plugin_ref_passes",
]
