"""Datasheet reads stay local and do not enqueue paper work (gr521603)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from precis.errors import BadInput, Unsupported
from precis.handlers import paper
from precis.handlers.datasheet import DatasheetHandler
from precis.response import Response
from precis.runtime import scope
from precis.store.types import Ref


@pytest.fixture
def datasheet_ref() -> Ref:
    now = datetime.now(UTC)
    return Ref(
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


@pytest.fixture
def handler(datasheet_ref: Ref, monkeypatch: pytest.MonkeyPatch) -> DatasheetHandler:
    instance = object.__new__(DatasheetHandler)
    # An accidental DB read or write fails: this fixture has no pool.
    instance.store = cast(Any, object())
    instance.embedder = None
    monkeypatch.setattr(paper, "_suggest_paper_slugs", lambda *a, **kw: [])
    monkeypatch.setattr(paper, "resolve_live_slug_ref", lambda *a, **kw: datasheet_ref)
    return instance


@pytest.mark.parametrize(
    ("id", "view", "error"),
    [
        ("vendor-sheet", "health", Unsupported),
        ("vendor-sheet/health", None, BadInput),
    ],
)
def test_sandbox_health_never_reaches_external_checker(
    handler: DatasheetHandler,
    id: str,
    view: str | None,
    error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope.note_serve_read_only()

    def external_health(*a: Any, **kw: Any) -> Response:
        pytest.fail("sandbox delegated to the external DOI health checker")

    monkeypatch.setattr(paper.PaperHandler, "_render_health", external_health)
    with pytest.raises(error):
        handler.get(id=id, view=view)


@pytest.mark.parametrize("view", ["page_image", "pdf_link"])
@pytest.mark.parametrize("entrypoint", ["get", "renderer"])
def test_sandbox_signed_media_views_refuse_before_delegating(
    handler: DatasheetHandler,
    datasheet_ref: Ref,
    view: str,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope.note_serve_read_only()

    def render(*a: Any, **kw: Any) -> Response:
        pytest.fail("sandbox delegated to a view that needs a signing secret")

    monkeypatch.setattr(paper.PaperHandler, "_render_view", render)
    with pytest.raises(Unsupported) as exc:
        if entrypoint == "get":
            handler.get(id="vendor-sheet", view=view, args={"page": 0})
        else:
            handler._render_view(datasheet_ref, view, args={"page": 0})
    assert exc.value.next


@pytest.mark.parametrize("view", ["page_image", "pdf_link"])
def test_signed_media_views_outside_sandbox_keep_existing_renderer(
    handler: DatasheetHandler,
    datasheet_ref: Ref,
    view: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scope, "current_scope", lambda: "write")
    rendered: list[tuple[Ref, str, dict[str, Any] | None]] = []

    def render(
        self: Any, ref: Ref, view: str, *, args: dict[str, Any] | None = None
    ) -> Response:
        rendered.append((ref, view, args))
        return Response(body="signed media link")

    monkeypatch.setattr(paper.PaperHandler, "_render_view", render)
    args = {"page": 0}
    result = handler._render_view(datasheet_ref, view, args=args)
    assert result.body == "signed media link"
    assert rendered == [(datasheet_ref, view, args)]


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
