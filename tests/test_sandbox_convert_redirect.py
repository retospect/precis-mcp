"""Converted handles cannot bypass the sandbox kind gate (gr521603)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from precis.errors import BadInput
from precis.ingest import kind_convert
from precis.runtime import PrecisRuntime, scope
from precis.store._refs_ops import RefsMixin
from precis.store.types import ResolvedHandle
from precis.workers.executors import _sandbox_read_mcp as read_mcp


class ConvertedStore:
    """Real handle resolution against one fake converted ref row; no pool."""

    def __init__(self, target: str) -> None:
        # Model a future conversion target without writing a ref or changing
        # the real conversion policy. The read gate must not trust that policy.
        assert target in kind_convert.CONVERT_TARGETS
        self.target = target

    def execute(self, sql: str, params: tuple[int]) -> SimpleNamespace:
        assert "FROM refs r" in sql and params == (1,)
        return SimpleNamespace(
            fetchone=lambda: (self.target, "converted-sheet", None, "datasheet")
        )

    def emit_hint(self, hint: Any) -> None:
        pass

    def resolve_handle(self, handle: str) -> ResolvedHandle | None:
        return RefsMixin.resolve_handle(cast(Any, self), handle, conn=cast(Any, self))


@pytest.mark.parametrize("target", ["web", "paper", "markdown"])
@pytest.mark.parametrize("read_only", [True, False], ids=["read", "write"])
def test_converted_datasheet_target_is_gated_before_rewrite(
    target: str, read_only: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        kind_convert, "CONVERT_TARGETS", kind_convert.CONVERT_TARGETS | {target}
    )
    monkeypatch.setattr(
        scope, "current_scope", lambda: "read" if read_only else "write"
    )
    runtime = object.__new__(PrecisRuntime)
    runtime.hub = cast(Any, SimpleNamespace(store=ConvertedStore(target)))
    args: dict[str, Any] = {"kind": "datasheet", "id": "da1~7"}

    # The serving gate accepted the caller's kind before dispatch resolves
    # the old handle. Exercise the real resolver's converted-kind redirect.
    assert read_mcp.kind_refusal("get", args["kind"]) is None
    blocked = read_only and target not in read_mcp.READ_KIND_ALLOWLIST
    if blocked:
        with pytest.raises(BadInput, match="sandbox"):
            runtime._maybe_infer_kind_from_handle(args, args["id"], verb="get")
        assert args == {"kind": "datasheet", "id": "da1~7"}
    else:
        assert runtime._maybe_infer_kind_from_handle(args, args["id"], verb="get")
        assert args == {"kind": target, "id": "converted-sheet~7"}
