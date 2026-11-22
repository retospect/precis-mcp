"""``get(view='read_region')`` — a server-side reading of a page region
with its extractor provenance (gr719555). The vision call and the page
render are stubbed; the view's checks and the reply shape are real."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import Store

_SHA = "e" * 64


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


@pytest.fixture
def seeded(store: Store) -> str:
    ref_id = store.insert_ref(
        kind="paper", slug="rr2020", title="T", provider="manual", meta={}
    ).id
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 12, 1, 'x.pdf') "
            "ON CONFLICT DO NOTHING",
            (_SHA, _SHA),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (_SHA, ref_id)
        )
    return "rr2020"


def _stub(monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any]) -> None:
    def png(self: Any, ref: Any, sha: str, page: int, clip: Any = None, dpi: Any = 0):
        seen["clip"], seen["page"] = clip, page
        return b"\x89PNG\r\n\x1a\nx", ""

    monkeypatch.setattr(PaperHandler, "_local_page_png", png)

    class _Res:
        error = None
        text = "| a | b |\n| 1.5 | 2.0 |"
        model = "claude-test"
        tier = None

    def fake_route(req: Any) -> _Res:
        seen["prompt"] = req.prompt
        return _Res()

    monkeypatch.setattr("precis.utils.llm.router.route", fake_route)


def test_reading_carries_text_and_provenance(
    handler: PaperHandler, seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    _stub(monkeypatch, seen)
    out = handler.get(
        id=seeded,
        view="read_region",
        args={"page": 11, "bbox": [0.1, 0.2, 0.9, 0.6], "question": "Table 3 row 1"},
    )
    assert "| 1.5 | 2.0 |" in out.body
    assert "extractor: paper.read_region v1" in out.body
    assert "model: claude-test" in out.body
    assert "dpi=300" in out.body
    assert seen["clip"] == (0.1, 0.2, 0.9, 0.6)
    assert seen["page"] == 11
    assert "Table 3 row 1" in seen["prompt"]
    assert len(out.images) == 1


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"page": 1, "ord": 2},
        {"page": -1},
        {"page": 1, "bbox": [0.5, 0.5, 0.4, 0.9]},
        {"page": 1, "bbox": [0, 0, 2, 1]},
        {"page": 1, "bbox": "nope"},
    ],
)
def test_bad_args_refuse(handler: PaperHandler, seeded: str, args: dict) -> None:
    with pytest.raises(BadInput):
        handler.get(id=seeded, view="read_region", args=args)


def test_get_args_declare_region_keys() -> None:
    """dispatch's args gate refuses undeclared keys: bbox/question must be declared."""
    from precis.handlers.paper import PaperHandler

    assert {"bbox", "question"} <= PaperHandler.ARGS_KEYS["get"]
