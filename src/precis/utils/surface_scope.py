"""Surface-scope masking for graph-neighbour rows (gr555998 round 3).

A scoped surface — the Slack bridge binds ``PRECIS_CONV_READ_SCOPE`` to
the turn's own conversation and fills ``PRECIS_KINDS_DISABLED`` with
every kind outside its allowlist — must not leak a masked neighbour's
handle, title, size or get-call through the Links table or an eye ring.
Two mask rules share the one predicate so every neighbour render
agrees on what a scoped surface may name:

- a ``conv`` ref outside the bound read scope
  (``PRECIS_CONV_READ_SCOPE``, enforced server-side by
  :mod:`precis.handlers.conversation`) → :data:`CONV_SCOPE_PLACEHOLDER`;
- a ref whose *kind* is gated off this surface →
  ``"<kind> (not available on this surface)"``. The kind name itself is
  already public — the ``Kinds unavailable:`` banner lists it; the
  ref's identity is not.

The masked row still proves the edge exists — that much the surfaced
ref's own neighbourhood already shows — without naming what it reaches.
"""

from __future__ import annotations

import os
from typing import Any

from precis.kind_gate import parse_disabled
from precis.utils import inproc_context

#: The env back-door a surface's kind allowlist arrives on
#: (``asa_slack.kind_policy.slack_kinds_disabled`` fills it for a Slack
#: turn). Read at render time — same as the conv scope — so the
#: spawned-MCP env and the in-process tick both answer for the right run.
_DISABLED_KINDS_ENV = "PRECIS_KINDS_DISABLED"

#: The opaque row a conv neighbour outside the run's read scope renders
#: (gr555998 follow-up, ``PRECIS_CONV_READ_SCOPE``). No handle, title,
#: keywords, excerpt or size; applied at every hop.
CONV_SCOPE_PLACEHOLDER = "conv (outside this session's scope)"


def surface_disabled_kinds() -> frozenset[str]:
    """The kinds gated off *this* surface right now.

    The union the verb gate honours: the env set (the spawned MCP
    server inherits the bridge's ``PRECIS_KINDS_DISABLED`` overlay, the
    same boot-time read ``precis.kind_gate`` makes) plus the in-process
    tick's ``disabled_kinds`` — the ContextVar twin for the OSS loop,
    where the worker's own env would answer for the wrong run.
    """
    disabled = parse_disabled(os.environ.get(_DISABLED_KINDS_ENV))
    ctx = inproc_context.current()
    if ctx is not None and ctx.disabled_kinds:
        disabled = disabled | frozenset(kind for kind, _hint in ctx.disabled_kinds)
    return disabled


def masked_ref_placeholder(ref: Any) -> str | None:
    """The opaque neighbour row ``ref`` renders on this surface, or
    ``None`` when it renders normally.

    Ring rows (``precis.utils.eye_render``, ``precis.utils.refeye``) and
    Links-table rows (``precis.handlers._links_render``,
    ``NumericRefHandler._format_link_line``) print this instead of the
    endpoint's handle/title/size whenever the endpoint is unreadable
    here. ``None`` for ``ref=None`` too — an unfetched endpoint keeps
    the existing ``<unknown ref N>`` fallback.
    """
    if ref is None:
        return None
    kind = getattr(ref, "kind", None)
    if kind == "conv":
        from precis.handlers.conversation import conv_ref_out_of_scope

        if conv_ref_out_of_scope(ref):
            return CONV_SCOPE_PLACEHOLDER
        return None
    if kind is not None and kind in surface_disabled_kinds():
        return f"{kind} (not available on this surface)"
    return None


__all__ = [
    "CONV_SCOPE_PLACEHOLDER",
    "masked_ref_placeholder",
    "surface_disabled_kinds",
]
