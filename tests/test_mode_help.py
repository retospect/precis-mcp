"""Unit coverage for :func:`precis.handlers._mode_help.require_mode`'s
three-way ``KindSpec.modes``/``edit_modes`` sentinel (gr343755): ``None``
(no ``mode=`` concept), ``()`` (recognised but rejects every value), and
a non-empty tuple (membership check).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from precis.errors import BadInput
from precis.handlers._mode_help import require_mode
from precis.protocol import KindSpec

_BASE_SPEC = KindSpec(kind="widget", title="Widget", description="test-only")


def test_require_mode_none_rejects_like_no_concept() -> None:
    """``modes=None`` (the default): calling ``require_mode`` with it
    anyway is a handler-authoring contradiction (a handler only calls
    this when it means to enforce *something*) — collapses to the same
    "mode= is not accepted" outcome as ``()``, not a membership check
    against an empty/absent set."""
    spec = replace(_BASE_SPEC, modes=None)
    with pytest.raises(BadInput, match="mode= is not accepted"):
        require_mode(spec=spec, verb="put", mode="anything")


def test_require_mode_empty_tuple_rejects_every_value() -> None:
    """``modes=()``: recognised but every value is rejected."""
    spec = replace(_BASE_SPEC, modes=())
    with pytest.raises(BadInput, match="mode= is not accepted"):
        require_mode(spec=spec, verb="put", mode="create")


def test_require_mode_nonempty_tuple_membership_check() -> None:
    """``modes=('create', 'import')``: a value in the set is a no-op; a
    value outside it raises, naming the allowed set."""
    spec = replace(_BASE_SPEC, modes=("create", "import"))
    require_mode(spec=spec, verb="put", mode="create")  # no-op, doesn't raise
    require_mode(spec=spec, verb="put", mode="import")  # no-op, doesn't raise
    with pytest.raises(BadInput, match="only supports mode="):
        require_mode(spec=spec, verb="put", mode="bogus")


def test_require_mode_reads_edit_modes_for_edit_verb() -> None:
    """``verb='edit'`` reads ``edit_modes``, independent of ``modes``."""
    spec = replace(_BASE_SPEC, modes=("create",), edit_modes=("replace",))
    require_mode(spec=spec, verb="edit", mode="replace")  # no-op
    with pytest.raises(BadInput, match="only supports mode="):
        # 'create' is a valid *put* mode, not an edit mode.
        require_mode(spec=spec, verb="edit", mode="create")


def test_require_mode_edit_modes_none_rejects() -> None:
    spec = replace(_BASE_SPEC, edit_modes=None)
    with pytest.raises(BadInput, match="mode= is not accepted"):
        require_mode(spec=spec, verb="edit", mode="replace")
