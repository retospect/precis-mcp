"""Kind-allowlist policy for Slack-originated agent turns.

Slack users may ask asa for research info (papers, patents, citations,
some Perplexity) and keep light notes on the people they talk to; they
must not be able to kick off compute (jobs, quests, cron) or touch
internal-ops surfaces — enforced with the existing boot-time kind gate
(``PRECIS_KINDS_DISABLED``, see ``precis.kind_gate`` + the skill
``precis-kinds-disabled-help``), not just prompt language. The env var is
threaded onto the spawned agent subprocess via ``LlmRequest.env_overlay``.

**Fail-closed allowlist.** :data:`ALLOWED_KINDS` is the *only* source of
truth — a kind is enabled for Slack iff it's in that set. There is no
parallel "every known kind" roster to hand-maintain: ``PRECIS_KINDS_DISABLED``
only understands a disabled *list*, not a positive allowlist, so
:func:`slack_kinds_disabled` computes the complement by *discovering* the
live kind registry at call time — every built-in handler under
``precis.handlers`` plus every third-party kind advertised via the
``precis.handlers`` entry-point group (see ``precis.dispatch._load_plugins``)
— rather than subtracting from a constant that can silently fall out of
date. A kind added to the registry and never added to ``ALLOWED_KINDS``
stays disabled by default, including one this module has never heard of by
name. ``tests/test_asa_slack_kind_policy.py`` diffs ``ALLOWED_KINDS``
against a fully-booted :class:`precis.dispatch.Hub` so a stale or renamed
entry (a kind removed from the live build) fails the build.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from functools import cache
from importlib.metadata import entry_points

log = logging.getLogger(__name__)

#: What a Slack turn's agent may touch. Deliberately narrow — this is a
#: research-lookup + light-memory surface, not a general precis client.
#: The complete allow/deny policy; see the module docstring for how the
#: disabled complement is derived.
ALLOWED_KINDS: frozenset[str] = frozenset(
    {
        "paper",
        "patent",
        "citation",
        "semanticscholar",
        "orcid",
        "edgar",
        "cfp",
        "web",
        "websearch",
        "wikipedia",
        "perplexity-research",
        "perplexity-reasoning",
        "memory",
        "skill",
    }
)

#: Entry-point group third-party kind plugins advertise under (mirrors
#: ``precis.dispatch.PLUGIN_GROUP``).
_PLUGIN_GROUP = "precis.handlers"


@cache
def _discover_live_kinds() -> frozenset[str]:
    """Enumerate every kind slug this build's ``precis`` package can
    register — built-in handlers plus entry-point plugins — without
    booting a :class:`precis.dispatch.Hub` (no store, no DB round trip;
    a plain sync call safe on the Slack event loop).

    Reads each handler class's ``spec: ClassVar[KindSpec]`` directly,
    the same attribute :func:`precis.dispatch._try` gates on.

    **Failure is fail-closed.** ``precis.kind_gate`` treats absence from
    ``PRECIS_KINDS_DISABLED`` as *enabled*, so a kind silently missing
    from this enumeration would silently un-block for Slack. Hence: a
    built-in handler module that fails to import raises — refusing to
    compute the policy beats running with a permissive one — and plugin
    kinds are taken from their entry-point *names* (``ep.name`` is the
    kind slug, mirroring ``precis.dispatch._load_plugins``), which need
    no import at all, so a broken plugin still lands in the disabled set.

    Cached for the life of the process (``@cache``): re-importing
    already-imported modules is cheap, but there's no reason to redo
    the walk on every Slack turn. Failures are not cached, so a
    transient error doesn't poison later calls.
    """
    from precis.protocol import KindSpec

    kinds: set[str] = set()

    import precis.handlers as _handlers_pkg

    for modinfo in pkgutil.iter_modules(_handlers_pkg.__path__):
        if modinfo.name.startswith("_"):
            continue  # private helper modules, not kind-bearing
        try:
            module = importlib.import_module(f"precis.handlers.{modinfo.name}")
        except Exception as exc:
            raise RuntimeError(
                "asa_slack.kind_policy: cannot compute the Slack kind gate — "
                f"precis.handlers.{modinfo.name} failed to import (its kinds "
                "would silently un-block)"
            ) from exc
        for value in vars(module).values():
            spec = getattr(value, "spec", None)
            if isinstance(spec, KindSpec):
                kinds.add(spec.kind)

    for ep in entry_points(group=_PLUGIN_GROUP):
        kinds.add(ep.name)  # slug without import; a broken plugin stays listed
        try:
            cls = ep.load()
        except Exception as exc:
            log.warning(
                "asa_slack.kind_policy: plugin %r failed to load "
                "(kind stays in the disabled enumeration): %s",
                ep.name,
                exc,
            )
            continue
        spec = getattr(cls, "spec", None)
        if isinstance(spec, KindSpec):
            kinds.add(spec.kind)

    return frozenset(kinds)


def slack_kinds_disabled() -> str:
    """``PRECIS_KINDS_DISABLED`` value for a Slack-originated agent turn.

    Every discovered kind not in :data:`ALLOWED_KINDS` — comma-separated,
    sorted for a stable/diffable env value. See the module docstring for
    how "every discovered kind" is computed.
    """
    disabled = _discover_live_kinds() - ALLOWED_KINDS
    return ",".join(sorted(disabled))
