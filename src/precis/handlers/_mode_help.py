"""Shared ``mode=`` validation for handlers that branch on ``put``/``edit``.

Before this module, each handler that restricted or interpreted ``mode=``
hand-rolled its own rejection message, quoting the accepted set as a
string literal independent of any machine-readable declaration
(``KindSpec.modes`` / ``KindSpec.edit_modes`` existed but were read
nowhere — gr292913). The two could drift: a handler's accepted set
changes, its error message doesn't, and the next agent that reads the
error gets stale advice.

:func:`require_mode` closes that gap by reading the declaration
straight off ``self.spec`` and building the error from it — the
declaration and the enforcement are now the same source, so they can't
disagree. Handlers that branch on ``mode=`` should declare the accepted
set on their ``KindSpec`` (``modes=`` for ``put``, ``edit_modes=`` for
``edit``) and call this instead of raising their own ``BadInput``.
"""

from __future__ import annotations

from typing import Literal

from precis.errors import BadInput
from precis.protocol import KindSpec

Verb = Literal["put", "edit"]


def require_mode(*, spec: KindSpec, verb: Verb, mode: str) -> None:
    """Raise ``BadInput`` if ``mode`` isn't declared for ``spec``/``verb``.

    Reads ``spec.edit_modes`` (``verb='edit'``) or ``spec.modes``
    (``verb='put'``) — the single source of truth for what this kind
    accepts — so the error text can never name a stale set. A no-op
    when ``mode`` is in the declared set.

    Callers with a ``mode=None`` default (``put``'s "omit to create"
    convention) should guard the call themselves — ``None`` is never
    passed here; only an explicit, non-``None`` ``mode`` value is
    checked.
    """
    allowed = spec.edit_modes if verb == "edit" else spec.modes
    if mode in allowed:
        return
    if allowed:
        if len(allowed) == 1:
            options_str = repr(allowed[0])
        else:
            options_str = " | ".join(repr(m) for m in allowed)
        raise BadInput(
            f"{verb}(kind={spec.kind!r}) only supports mode={options_str}, "
            f"got {mode!r}",
            options=list(allowed),
            next=f"{verb}(kind={spec.kind!r}, ..., mode={allowed[0]!r})",
        )
    raise BadInput(
        f"mode= is not accepted on {verb} for kind={spec.kind!r}, got {mode!r}",
        next=f"omit mode= — {verb}(kind={spec.kind!r}, ...) doesn't take one",
    )


__all__ = ["require_mode"]
