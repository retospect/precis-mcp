"""SVG cache retains geometry bytes while shown hover facts remain live."""

from __future__ import annotations

import asyncio
import collections
import json
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from starlette.requests import Request

from precis_web.routes import pcb


@pytest.fixture
def svg_route(monkeypatch):
    state = SimpleNamespace(
        token="snapshot",
        svg="<svg><path /></svg>",
        facts={"parts": {"R1": {"stock": 5, "price": 0.1}}},
        renders=[],
        hover_reads=[],
    )
    store = SimpleNamespace(pcb_view_tx=lambda: nullcontext(None))
    monkeypatch.setattr(pcb, "_FAB_SVG_CACHE", collections.OrderedDict())
    monkeypatch.setattr(pcb, "get_store", lambda _: store)
    monkeypatch.setattr(
        pcb,
        "resolve_live_slug_ref",
        lambda *_, **kw: SimpleNamespace(id=int(kw["id"]), slug=kw["id"]),
    )
    monkeypatch.setattr(
        pcb.pcb_design,
        "capture_view",
        lambda *_, **__: SimpleNamespace(token=state.token),
    )
    monkeypatch.setattr(pcb.pcb_design, "selection_manifest", lambda _: {"objects": []})

    def hover(*_):
        state.hover_reads.append(True)
        return {"facts": state.facts, "unrouted": ["SIG"], "failed": ["SIG"]}

    def render(*args, **kw):
        state.renders.append((args, kw))
        return SimpleNamespace(body=state.svg)

    monkeypatch.setattr(pcb.pcb_hover, "hover_facts", hover)
    monkeypatch.setattr(
        pcb, "_handler", lambda _: SimpleNamespace(get=render, _render_fab_svg=render)
    )

    def request(board=1, *, shown=None, flip=False):
        return asyncio.run(
            pcb.pcb_board_svg(
                Request({"type": "http"}), str(board), shown=shown, flip=flip
            )
        )

    state.request = request
    return state


def test_shown_hit_refreshes_escaped_hover_without_rendering(svg_route):
    state = svg_route
    first = state.request(shown=state.token)
    state.facts = {
        "parts": {
            "R1": {
                "stock": 7,
                "price": 0.2,
                "basic": True,
                "jlcpcb_assemblable": True,
                "label": '<&"</metadata>',
            }
        }
    }
    second = state.request(shown=state.token)
    assert first.status_code == second.status_code == 200
    assert len(state.renders) == 1
    assert len(state.hover_reads) == 2
    metadata = ET.fromstring(second.body).find("metadata")
    assert metadata is not None and metadata.text is not None
    assert metadata.attrib["id"] == "pcb-facts"
    assert json.loads(metadata.text) == state.facts
    assert b"&lt;" in second.body and b"&amp;" in second.body
    assert b"pcb-facts" not in next(iter(pcb._FAB_SVG_CACHE.values()))
    manifest = state.renders[0][1]["selection_manifest"]
    assert manifest["hover"] == {"unrouted": ["SIG"], "failed": ["SIG"]}


def test_bare_and_shown_and_flip_have_separate_geometry(svg_route):
    state = svg_route
    bare = state.request()
    state.request(shown=state.token)
    state.request(shown=state.token, flip=True)
    assert state.request().body == bare.body == state.svg.encode()
    assert b"pcb-facts" not in bare.body
    assert len(state.renders) == 3
    assert len(state.hover_reads) == 2
    assert len(pcb._FAB_SVG_CACHE) == 3


def test_stale_shown_refused_before_cache_or_hover(svg_route):
    state = svg_route
    state.request(shown=state.token)
    response = state.request(shown="stale")
    assert response.status_code == 409
    assert b"changed" in response.body
    assert len(state.renders) == len(state.hover_reads) == 1


def test_entry_limit_evicts_oldest_and_hit_refreshes_recency(svg_route):
    state = svg_route
    for board in range(1, 9):
        state.request(board)
    state.request(1)
    state.request(9)
    assert [key[0] for key in pcb._FAB_SVG_CACHE] == [3, 4, 5, 6, 7, 8, 1, 9]
    state.request(2)
    assert len(state.renders) == 10
    assert len(pcb._FAB_SVG_CACHE) == 8


def test_byte_limit_counts_utf8_and_evicts_by_recency(svg_route, monkeypatch):
    state = svg_route
    state.svg = "<svg>é</svg>"
    size = len(state.svg.encode("utf-8"))
    monkeypatch.setattr(pcb, "_FAB_SVG_CACHE_MAX_BYTES", 2 * size)
    state.request(1)
    state.request(2)
    state.request(1)
    state.request(3)
    assert [key[0] for key in pcb._FAB_SVG_CACHE] == [1, 3]
    assert sum(map(len, pcb._FAB_SVG_CACHE.values())) == 2 * size
    assert all(isinstance(body, bytes) for body in pcb._FAB_SVG_CACHE.values())


def test_oversized_utf8_served_without_retention_or_eviction(svg_route, monkeypatch):
    state = svg_route
    small = state.request(1).body
    monkeypatch.setattr(pcb, "_FAB_SVG_CACHE_MAX_BYTES", 20)
    state.svg = "<svg>" + "é" * 8 + "</svg>"
    assert len(state.svg) <= 20 < len(state.svg.encode("utf-8"))
    for _ in range(2):
        assert state.request(2).body == state.svg.encode("utf-8")
    assert [key[0] for key in pcb._FAB_SVG_CACHE] == [1]
    assert state.request(1).body == small
    assert len(state.renders) == 3
