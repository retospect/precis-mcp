"""Unit tests for the Models sub-tab context builders (``/status?tab=models``).

``_llm_card_view`` normalises one ``llm`` catalog card into the flat render
shape; ``_models_ctx`` groups the catalog by where a model is *sourced* (Cloud
tier vs fleet-served Local) and sorts each grid. Both are pure over a
``list_refs``-shaped store, so this is a fast fake-ref test — the live-PG
catalog + reconcile pass are covered elsewhere.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis_web.routes.status import _llm_card_view, _models_ctx


def _ref(
    ref_id: int | str, model_id: str, title: str = "", **meta: Any
) -> SimpleNamespace:
    return SimpleNamespace(id=ref_id, title=title, meta={"model_id": model_id, **meta})


class _FakeStore:
    def __init__(self, refs: list[Any], *, boom: bool = False) -> None:
        self._refs = refs
        self._boom = boom

    def list_refs(self, *, kind: str, limit: int) -> list[Any]:
        assert kind == "llm"
        if self._boom:
            raise RuntimeError("db down")
        return self._refs


def test_cloud_card_view_extracts_price_provider_and_caps() -> None:
    ref = _ref(
        162503,
        "z-ai/glm-5.2",
        title="GLM-5.2 — the strongest open-weight model.",
        tier_floor="cloud-super",
        offerings=[
            {
                "transport": "openai_compat",
                "max_input": 1048576,
                "price_in": 0.969,
                "price_out": 3.045,
            }
        ],
        capability={"code": 5, "tool-structured": {"score": 4}},
        provenance={"source": "seed-frontier"},
    )
    v = _llm_card_view(ref)
    assert v["is_cloud"] is True
    assert v["provider"] == "z-ai"  # split on the slug slash
    assert v["price_in"] == 0.969 and v["price_out"] == 3.045
    assert v["window"] == 1048576
    # Ordinal caps normalised whether given as int or {"score": N}.
    assert {"axis": "code", "score": 5} in v["caps"]
    assert {"axis": "tool-structured", "score": 4} in v["caps"]


def test_claude_slug_maps_to_anthropic_provider() -> None:
    v = _llm_card_view(_ref(1, "claude-opus-4-8", tier_floor="cloud-super"))
    assert v["provider"] == "anthropic"


def test_local_card_view_surfaces_served_by_hosts() -> None:
    ref = _ref(
        165574,
        "qwen3.6-35b-a3b-ud-q3_k_m",
        served_by=[
            {
                "host": "balthazar",
                "endpoint": "http://127.0.0.1:11445/v1",
                "max_parallel": 1,
                "model": "qwen3.6-35b-a3b-ud-q3_k_m",
            },
            {"host": "no-host-key-skipped"},  # missing host → dropped below
        ],
    )
    v = _llm_card_view(ref)
    assert v["is_cloud"] is False  # no cloud-* tier floor
    assert [h["host"] for h in v["hosts"]] == ["balthazar", "no-host-key-skipped"]
    assert v["hosts"][0]["slots"] == 1


def test_tier_anchor_without_served_by_is_local_with_no_hosts() -> None:
    # ``qwen-heavy`` / ``summarizer`` are local-* tier anchors: local, but no
    # concrete host until the router resolves them at dispatch.
    v = _llm_card_view(_ref(162071, "qwen-heavy", tier_floor="local-big"))
    assert v["is_cloud"] is False
    assert v["hosts"] == []


def test_models_ctx_groups_sorts_and_lists_serving_hosts() -> None:
    refs = [
        _ref(
            "small",
            "z-ai/glm-4.7-flash",
            tier_floor="cloud-small",
            offerings=[{"price_in": 0.1}],
        ),
        _ref(
            "super",
            "z-ai/glm-5.2",
            tier_floor="cloud-super",
            offerings=[{"price_in": 0.9}],
        ),
        _ref(
            "mid",
            "qwen/qwen3.7-max",
            tier_floor="cloud-mid",
            offerings=[{"price_in": 0.5}],
        ),
        _ref("anchor", "qwen-heavy", tier_floor="local-big"),
        _ref("served", "qwen3.6-35b", served_by=[{"host": "spark", "max_parallel": 1}]),
    ]
    ctx = _models_ctx(_FakeStore(refs))
    # Cloud sorted strongest tier first.
    assert [c["tier"] for c in ctx["cloud_cards"]] == [
        "cloud-super",
        "cloud-mid",
        "cloud-small",
    ]
    # Local: host-backed models before the abstract tier anchors.
    assert [c["model_id"] for c in ctx["local_cards"]] == ["qwen3.6-35b", "qwen-heavy"]
    assert ctx["serving_hosts"] == ["spark"]


def test_models_ctx_degrades_to_empty_on_store_error() -> None:
    ctx = _models_ctx(_FakeStore([], boom=True))
    assert ctx == {"cloud_cards": [], "local_cards": [], "serving_hosts": []}


def test_models_tab_renders_cards_end_to_end(client, runtime) -> None:
    """Render smoke: the Jinja template compiles, the tab is wired, and a
    cloud card's price + a local card's serving host both reach the HTML."""
    runtime.store.list_refs = lambda **_kw: [  # type: ignore[method-assign]
        _ref(
            162503,
            "z-ai/glm-5.2",
            title="GLM-5.2 — strongest open-weight model.",
            tier_floor="cloud-super",
            offerings=[{"price_in": 0.969, "price_out": 3.045, "max_input": 1048576}],
        ),
        _ref(
            165574,
            "qwen3.6-35b-a3b",
            served_by=[{"host": "balthazar", "max_parallel": 1}],
        ),
    ]
    html = client.get("/status?tab=models").text
    assert "z-ai/glm-5.2" in html  # cloud card name
    assert "0.969" in html  # headline list price
    assert "qwen3.6-35b-a3b" in html  # local card name
    assert "balthazar" in html  # served-by host chip
