"""Datasheet reads stay local and do not enqueue paper work (gr521603)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from precis.errors import Unsupported
from precis.handlers import paper
from precis.handlers.datasheet import DatasheetHandler
from precis.response import Response
from precis.runtime import scope
from precis.store.types import Ref


@pytest.fixture
def handler(monkeypatch: pytest.MonkeyPatch) -> DatasheetHandler:
    now = datetime.now(UTC)
    ref = Ref(
        id=1,
        kind="datasheet",
        slug="vendor-sheet",
        title="Vendor sheet",
        provider=None,
        meta={},
        created_at=now,
        updated_at=now,
        retired_at=None,
    )
    instance = object.__new__(DatasheetHandler)
    # An accidental DB read or write fails: this fixture has no pool.
    instance.store = cast(Any, object())
    instance.embedder = None
    monkeypatch.setattr(paper, "_suggest_paper_slugs", lambda *a, **kw: [])
    monkeypatch.setattr(paper, "resolve_live_slug_ref", lambda *a, **kw: ref)
    return instance


@pytest.mark.parametrize(
    "args", [{"id": "vendor-sheet", "view": "health"}, {"id": "vendor-sheet/health"}]
)
def test_sandbox_health_never_reaches_external_checker(
    handler: DatasheetHandler,
    args: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope.note_serve_read_only()

    def external_health(*a: Any, **kw: Any) -> Response:
        pytest.fail("sandbox delegated to the external DOI health checker")

    monkeypatch.setattr(paper.PaperHandler, "_render_health", external_health)
    with pytest.raises(Unsupported):
        handler.get(**args)


def test_health_outside_sandbox_keeps_the_existing_checker(
    handler: DatasheetHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scope, "current_scope", lambda: "write")
    checked: list[Ref] = []

    def external_health(self: Any, ref: Ref) -> Response:
        checked.append(ref)
        return Response(body="health report")

    monkeypatch.setattr(paper.PaperHandler, "_render_health", external_health)
    assert handler.get(id="vendor-sheet", view="health").body == "health report"
    assert len(checked) == 1


def test_datasheet_overview_never_queues_paper_si_work(
    handler: DatasheetHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.store import si_links

    scope.note_serve_read_only()

    def queue(*a: Any, **kw: Any) -> None:
        pytest.fail("datasheet read attempted to queue paper SI work")

    monkeypatch.setattr(si_links, "queue_si_on_attention", queue)
    monkeypatch.setattr(handler, "_render_overview", lambda ref: Response(body="sheet"))
    assert handler.get(id="vendor-sheet").body == "sheet"
