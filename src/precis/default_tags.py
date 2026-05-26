"""Resolve ``PRECIS_DEFAULT_TAGS`` and apply the policy at dispatch.

Phase 5 of the cold-start token budget design
(``docs/design/mcp-cold-start-token-budget.md``).

The runtime parses the env value once at boot and caches a tuple of
default tags on :class:`precis.runtime.PrecisRuntime`. At dispatch
time, :func:`apply_to_put_args` merges them into the ``tags=``
payload for ``put`` calls on note-like kinds, and
:func:`suggest_missing_for_tag_args` returns the missing defaults
so the dispatcher can emit a suggestion hint on ``tag`` calls
(without mutating, since ``tag`` is operator-explicit).

Note-like kinds are determined by
:attr:`precis.protocol.KindSpec.note_like`. The flip-list lives
in the design doc and is enforced via per-handler edits to each
``KindSpec``; this module reads only the boolean flag and never
hard-codes a kind list.

Zero-overhead default: with the env var unset, parsing returns the
empty tuple, and both ``apply_to_put_args`` and
``suggest_missing_for_tag_args`` short-circuit to a no-op. The
performance budget for an unconfigured server is therefore the cost
of one tuple-truthiness check per ``put`` / ``tag`` dispatch.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def parse(value: str | None) -> tuple[str, ...]:
    """Parse a comma-separated env value into a deduped ordered tuple.

    Whitespace around commas is tolerated. Empty entries (``a,,b``)
    are dropped. Duplicates after the first occurrence are dropped
    so the operator's stated order survives — the order matters
    only for the suggestion-hint message rendering, not the actual
    tag set, but stability across boots is friendlier for log diffs.

    Returned tuple is hashable / immutable so it flows through frozen
    contexts (config caches, request scopes) without surprises.
    """
    if not value:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for raw in value.split(","):
        tag = raw.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return tuple(out)


def merge(explicit: list[str] | None, defaults: tuple[str, ...]) -> list[str]:
    """Set-union of ``explicit`` and ``defaults`` preserving order.

    The explicit caller-supplied tags come first (so the agent's
    stated order is honoured); defaults that are already present
    are skipped, missing ones are appended in the operator-stated
    order. Returns a new list — never mutates ``explicit``.
    """
    explicit_list = list(explicit or ())
    if not defaults:
        return explicit_list
    seen = set(explicit_list)
    out = list(explicit_list)
    for tag in defaults:
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def suggest_missing(
    explicit: list[str] | None, defaults: tuple[str, ...]
) -> tuple[str, ...]:
    """Return the defaults missing from ``explicit``, in stated order.

    The dispatcher uses this to compose a suggestion hint on ``tag``
    calls so the operator-stated session-context tags surface for
    the agent to add explicitly. The dispatcher never mutates the
    ``add=`` set — ``tag`` is the agent's deliberate op, and the
    design wants visibility (suggestion hint) over silent mutation.
    """
    if not defaults:
        return ()
    present = set(explicit or ())
    return tuple(t for t in defaults if t not in present)


def apply_to_put_args(args: dict, defaults: tuple[str, ...]) -> tuple[str, ...]:
    """Mutate ``args['tags']`` to include the merged default-tag set.

    Returns the tuple of newly-added tags (empty when nothing
    changed). The runtime emits a hint when the result is non-empty.

    No-op when ``defaults`` is empty or every default is already
    present in ``args['tags']``. Designed to be called only on
    note-like kinds — the runtime gate is responsible for the
    note-like check.

    Mutates in-place because ``args`` is a per-call dict; making a
    copy here would force the caller to swap it back, complicating
    the dispatch flow for no benefit.
    """
    if not defaults:
        return ()
    explicit = args.get("tags") or []
    merged = merge(explicit, defaults)
    if len(merged) == len(explicit):
        return ()
    args["tags"] = merged
    explicit_set = set(explicit)
    return tuple(t for t in defaults if t not in explicit_set)
