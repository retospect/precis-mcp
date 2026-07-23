"""Render a :class:`~precis.errors.PrecisError` as the agent-facing string.

``ErrorMixin`` owns the terminal ``[error:ClassName] cause / options: /
next:`` envelope shape. Hint-*decoration* of the error object (the skill
breadcrumb appended before this runs) lives in :mod:`precis.runtime.hints`.
"""

from __future__ import annotations

from precis.errors import PrecisError
from precis.runtime._shared import RuntimeShape


class ErrorMixin(RuntimeShape):
    """Renders a :class:`PrecisError` into the canonical envelope string.

    ``ErrorMixin`` itself doesn't reference any sibling-mixin attribute,
    but it still subclasses :class:`RuntimeShape` alongside every other
    mixin — that keeps ``RuntimeShape`` a common ancestor of *all* of
    them, which is what pushes it to the tail of ``PrecisRuntime``'s MRO
    (see :class:`RuntimeShape`'s docstring). Dropping this base would let
    C3 linearization slot ``RuntimeShape`` in *before* this class, and
    its ``render_error`` stub would then shadow this real one.
    """

    def render_error(self, err: PrecisError) -> str:
        """Render a :class:`PrecisError` as the canonical agent-facing string.

        Public surface so transport layers (``precis.server``) can format
        pre-dispatch validation errors with the same shape the runtime
        produces on raise. Was previously named ``_render_error`` and
        accessed via ``# type: ignore[attr-defined]`` from the MCP tool
        wrappers; the underscore-prefixed alias is kept for backwards
        compatibility with anything still calling the old name.
        """
        parts = [f"[error:{err.__class__.__name__}] {err.cause}"]
        if err.options:
            parts.append(f"  options: {', '.join(map(str, err.options))}")
        if err.next:
            # F12: ``next`` may be a string (one hint) or a list of
            # strings (multiple hints). Render each on its own
            # ``next:`` line so the rendered envelope remains
            # backwards-compatible — a caller scanning for "next:"
            # finds every hint without needing to know the difference.
            if isinstance(err.next, str):
                parts.append(f"  next: {err.next}")
            else:
                for hint in err.next:
                    parts.append(f"  next: {hint}")
        return "\n".join(parts)

    # Backwards-compatible alias. Internal callers that pre-date the
    # promotion to a public method still reference ``_render_error``.
    _render_error = render_error
