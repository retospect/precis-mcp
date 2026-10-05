"""gr468293: a waiting queue hides terminal rows and labels actual lifecycle."""

from typing import Any

import pytest

from precis.store import Tag
from precis.tools import core


@pytest.mark.parametrize(
    "kind,live,terminal",
    [
        (
            "todo",
            ["open", "doing", "blocked", "paused"],
            ["done", "won't-do", "abandoned"],
        ),
        ("gripe", ["open", "triaged", "in_review"], ["done", "wontfix"]),
        ("quest", ["active", "dormant"], ["abandoned"]),
        ("alert", ["open"], ["resolved"]),
    ],
)
def test_browse_lifecycle_scope(
    runtime_with_store: Any,
    kind: str,
    live: list[str],
    terminal: list[str],
    monkeypatch: Any,
) -> None:
    hub = runtime_with_store.hub
    store = hub.live_store
    handler = hub.handler_for(kind)
    for state in live + terminal:
        with store.tx() as conn:
            ref = store.insert_ref(
                kind=kind, slug=None, title=f"fixture-state-{state}", conn=conn
            )
            lifecycle = (
                Tag.open(f"alert-state:{state}")
                if kind == "alert"
                else Tag.closed("STATUS", state)
            )
            store.add_tag(ref.id, lifecycle, conn=conn)
            store.add_tag(ref.id, Tag.open("waiting-for:fixture-r14"), conn=conn)
    args: dict[str, Any] = dict(tags=["waiting-for:fixture-r14"], page_size=100)
    default = handler.search(**args).body
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    native = core.search(kind=kind, tags=["waiting-for:fixture-r14"], page_size=100)
    assert isinstance(native, str)
    assert f"{len(terminal)} terminal entries hidden" in native
    assert "\tstatus\t" in default
    assert f"{len(terminal)} terminal entries hidden" in default
    assert "status='*'" in default
    for state in live:
        assert f"\t{state}\tfixture-state-{state}" in default
    for state in terminal:
        assert f"fixture-state-{state}" not in default
    if kind != "alert":
        first = handler.search(tags=args["tags"], page_size=1, sort="prio").body
        assert f"1 of {len(live)}" in first
        for state in terminal:
            assert f"fixture-state-{state}" not in first
    all_rows = handler.search(**args, status="*").body
    for state in live + terminal:
        assert f"\t{state}\tfixture-state-{state}" in all_rows
    explicit = handler.search(**args, status=terminal[0]).body
    assert f"fixture-state-{terminal[0]}" in explicit
    assert "terminal entries hidden" not in explicit
    tagged = handler.search(
        tags=[
            *args["tags"],
            f"alert-state:{terminal[0]}"
            if kind == "alert"
            else f"STATUS:{terminal[0]}",
        ],
        page_size=100,
    ).body
    assert f"fixture-state-{terminal[0]}" in tagged
    empty_page = handler.search(**args, page=2).body
    assert "no " in empty_page
    assert f"{len(terminal)} terminal entries hidden" in empty_page


def test_alert_closed_shorthand(runtime_with_store: Any) -> None:
    handler = runtime_with_store.hub.handler_for("alert")
    assert "no alert" in handler.search(status="closed").body
