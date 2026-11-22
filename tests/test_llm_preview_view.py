"""``get(kind='llm', id='/preview')`` — the router-choice preview (gr681308).

An agent asks what the router would choose for a task kind; the view runs
the same filter-and-rank ``decide()`` the router runs, lists every
candidate with the chosen one marked, and places no model call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from precis.errors import BadInput
from precis.runtime import PrecisRuntime
from precis.utils.llm import decide, operations, recipes, router

KIND = "test.preview"
SOURCE = "preview:src"


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.llm import LlmHandler

    return LlmHandler(hub=Hub(store=store))


def _offering(key: str, **kwargs: Any) -> decide.Offering:
    return decide.Offering(
        key,
        key.split("@", 1)[0],
        adapter="api",
        tier_floor=kwargs.pop("tier_floor", "small"),
        **kwargs,
    )


@pytest.fixture
def routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A three-offering ledger, one recipe and one op; any dispatch fails."""
    ledger = decide.Ledger(
        (
            _offering("dear@api/none", price_in=3.0),
            _offering("cheap@api/none", price_in=0.1),
            _offering("gone@api/none", enabled=False),
            _offering("tiny@api/none", tier_floor=None),
        ),
        {},
        {},
        frozenset(),
        datetime.now(UTC),
    )
    monkeypatch.setattr(decide, "ledger_from_store", lambda *a, **kw: ledger)
    monkeypatch.setattr(decide, "mode_for", lambda _kind: "shadow")
    monkeypatch.setattr(
        recipes,
        "_rows",
        lambda: {SOURCE: recipes.Recipe(SOURCE, KIND, consequence="low")},
    )
    monkeypatch.setitem(
        operations.LLM_OPERATIONS,
        SOURCE,
        operations.OpDefault(router.Tier.MEDIUM, None, "preview", "preview test op"),
    )
    monkeypatch.setattr(operations, "resolve_op_chain", lambda *a, **kw: None)
    monkeypatch.setattr(router, "resolve_backend", lambda: router.Backend.ANTHROPIC)
    monkeypatch.setattr(router, "resolve_model", lambda *a, **kw: "legacy-model")
    monkeypatch.setattr(
        router,
        "resolve_chain",
        lambda *a, **kw: [router.Rung(router.Transport.LOCAL, "legacy-model", "local")],
    )

    def no_call(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("the preview placed a model call")

    monkeypatch.setattr(router, "route", no_call)
    monkeypatch.setattr(router, "dispatch_async", no_call)


def _log_rows(store: Any) -> int:
    with store.pool.connection() as conn:
        return int(conn.execute("SELECT count(*) FROM llm_call_log").fetchone()[0])


def test_preview_ranks_candidates_and_marks_the_choice(
    store: Any, routing: None
) -> None:
    before = _log_rows(store)
    handler = _handler(store)
    assert "preview" in handler._supported_list_views()
    body = handler.get(id="/preview", args={"task_kind": KIND, "tier": "small"}).body
    lines = body.splitlines()
    assert lines[0] == f"# llm route preview — task kind {KIND}"
    assert f"source={SOURCE} tier=small" in lines[1]
    assert "work_class=background" in lines[1]
    assert any(ln.startswith("decide mode: shadow") for ln in lines)
    assert "chosen: cheap@api/none" in lines
    assert "candidates (3 ranked, 1 rejected):" in lines
    ranked = [ln for ln in lines if ln.startswith(("  1.", "  2.", "  3."))]
    assert [ln.split()[1:3] for ln in ranked] == [
        ["*", "cheap@api/none"],
        ["dear@api/none", "—"],
        ["tiny@api/none", "—"],
    ]
    assert all("— healthy" in ln for ln in ranked)
    assert "price_in=0.1" in ranked[0]
    rejected = [ln for ln in lines if ln.startswith("  -")]
    assert rejected == [
        "  -   gone@api/none — entitlement: offering or account disabled"
    ]
    assert "legacy ladder: legacy-model@local/local" in lines
    assert "No LLM call made" in lines[-1]
    assert _log_rows(store) == before


def test_preview_takes_the_tier_from_the_kinds_operation(
    store: Any, routing: None
) -> None:
    """A task kind alone is enough: its recipe names the source, the
    source's registered operation the tier. Offerings below that tier are
    out of the candidate set and only counted."""
    body = _handler(store).get(id="/preview", args={"task_kind": KIND}).body
    assert f"source={SOURCE} tier=medium" in body
    assert "chosen: none" in body
    assert "candidates (0 ranked, 1 rejected):" in body
    assert "3 offerings below the medium tier floor not listed" in body
    assert "cheap@api/none" not in body


def test_preview_refuses_a_kind_with_no_derivable_tier(
    store: Any, routing: None
) -> None:
    with pytest.raises(BadInput) as exc:
        _handler(store).get(id="/preview", args={"task_kind": "no.such.kind"})
    assert "tier" in str(exc.value)
    assert exc.value.options == ["small", "medium", "big", "frontier"]
    with pytest.raises(BadInput, match="task_kind"):
        _handler(store).get(id="/preview")
    with pytest.raises(BadInput, match="/preview"):
        _handler(store).get(id="/decisions", args={"task_kind": KIND})


def test_preview_reaches_the_handler_through_dispatch(
    runtime_with_store: PrecisRuntime, routing: None
) -> None:
    out = runtime_with_store.dispatch(
        "get",
        {
            "kind": "llm",
            "id": "/preview",
            "__extras__": {"task_kind": KIND, "tier": "small", "tools": True},
        },
    )
    assert "[error:" not in out, out
    assert "chosen: cheap@api/none" in out
    assert "tools=yes" in out


def test_preview_reads_the_real_ledger_without_logging_a_call(store: Any) -> None:
    """No stubs: the catalog, pools and chain resolution of the test DB."""
    before = _log_rows(store)
    body = (
        _handler(store)
        .get(id="/preview", args={"task_kind": "no.such.kind", "tier": "small"})
        .body
    )
    assert body.startswith("# llm route preview — task kind no.such.kind")
    assert "\nchosen: " in body and "\nlegacy ladder: " in body
    assert _log_rows(store) == before


def test_llm_ops_skill_names_the_preview() -> None:
    from importlib import resources

    text = (resources.files("precis.data.skills") / "precis-llm-ops-help.md").read_text(
        encoding="utf-8"
    )
    assert "id='/preview'" in text
