"""Render a :class:`~precis.errors.PrecisError` as the agent-facing string.

``ErrorMixin`` owns the terminal ``[error:ClassName] cause / options: /
next:`` envelope shape. Hint-*decoration* of the error object (the skill
breadcrumb appended before this runs) lives in :mod:`precis.runtime.hints`.
"""

from __future__ import annotations

from precis.errors import BadInput, PrecisError, Unsupported
from precis.runtime._shared import RuntimeShape

#: Standing-order gripe hint appended at render time to every
#: argument-shaped refusal (gr631696): a ``BadInput``/``Unsupported``
#: means the call shape was refused — and when a skill or doc showed
#: that shape the fix is a gripe against it, not a retry. Appended here
#: rather than raised so boundary-validation refusals that never reach
#: the dispatch ``_maybe_add_*`` hooks carry it too. A ``next=`` entry
#: already mentioning "gripe" suppresses it (a raise site's specific
#: gripe pointer wins over the generic line), and the create-only
#: ``put(kind='gripe'`` shape keeps it visible under read scope —
#: ``is_write_call``'s carve-out.
_ARG_SHAPE_GRIPE_HINT = (
    "if a skill or doc showed this call, file a gripe naming it — "
    "put(kind='gripe', text='<what it showed; what the tool did>')"
)


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
        nxt = [err.next] if isinstance(err.next, str) else list(err.next or [])
        if nxt:
            # F12: ``next`` may be a string (one hint) or a list of
            # strings (multiple hints). Render each on its own
            # ``next:`` line so the rendered envelope remains
            # backwards-compatible — a caller scanning for "next:"
            # finds every hint without needing to know the difference.
            # gr512233 slice 2: on a read-scope connection a ``next=``
            # pointing at a write recipe is unreachable — drop those
            # entries; when every hint drops, one pointer line names
            # why. One scope verdict per render (round-1 review); lazy
            # imports keep this module out of the runtime init cycle.
            from precis.runtime.scope import current_scope
            from precis.utils.skill_scope import GENERIC_POINTER, is_write_call

            if current_scope() == "read":
                nxt = [h for h in nxt if not is_write_call(h)] or [GENERIC_POINTER]
        # gr631696: the gripe standing order rides every argument-shaped
        # refusal, appended after the scope filter so the read-only
        # GENERIC_POINTER (itself gripe-shaped) dedups against it.
        if isinstance(err, (BadInput, Unsupported)) and not any(
            "gripe" in h for h in nxt
        ):
            nxt.append(_ARG_SHAPE_GRIPE_HINT)
        for hint in nxt:
            parts.append(f"  next: {hint}")
        return "\n".join(parts)

    # Backwards-compatible alias. Internal callers that pre-date the
    # promotion to a public method still reference ``_render_error``.
    _render_error = render_error
