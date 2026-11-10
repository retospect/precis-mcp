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

from precis_web.routes.status import (
    _active_routing_ctx,
    _llm_card_view,
    _llm_op_pins_ctx,
    _models_ctx,
)


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
        tier_floor="frontier",
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
    v = _llm_card_view(_ref(1, "claude-opus-4-8", tier_floor="frontier"))
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
    assert v["is_cloud"] is False  # bare local model id (no slash, not claude-*)
    assert [h["host"] for h in v["hosts"]] == ["balthazar", "no-host-key-skipped"]
    assert v["hosts"][0]["slots"] == 1


def test_tier_anchor_without_served_by_is_local_with_no_hosts() -> None:
    # ``qwen-heavy`` / ``summarizer`` are bare local model ids (the local rungs
    # backing BIG/SMALL): local, but no concrete host until dispatch resolves.
    v = _llm_card_view(_ref(162071, "qwen-heavy", tier_floor="big"))
    assert v["is_cloud"] is False
    assert v["hosts"] == []


def test_served_by_hosts_dominate_a_provider_slugged_model_id() -> None:
    # A fleet-served model whose model_id happens to be provider-slugged must
    # still land in the Local grid — served_by (where it's sourced) wins over
    # the id-sniff, which is only the fallback classifier for un-served cards.
    ref = _ref(
        165999,
        "some-org/some-model",
        tier_floor="big",
        served_by=[
            {
                "host": "spark",
                "endpoint": "http://127.0.0.1:8080/v1",
                "max_parallel": 2,
                "model": "some-org/some-model",
            }
        ],
    )
    v = _llm_card_view(ref)
    assert v["is_cloud"] is False
    assert [h["host"] for h in v["hosts"]] == ["spark"]


def test_card_view_lists_offerings_local_first_with_hosts() -> None:
    """gr502698 — a fleet-served + cloud-offered card shows BOTH operating
    points, the derived local one first, and its serving host — while the
    headline price stays the cloud rate."""
    ref = _ref(
        162513,
        "openai/gpt-oss-120b",
        tier_floor="medium",
        served_by=[
            {
                "host": "castor",
                "endpoint": "http://x:8000/v1",
                "max_parallel": 32,
                "model": "gpt-oss-120b",
            }
        ],
        offerings=[
            {
                "transport": "local",
                "served_by": [{"host": "castor", "max_parallel": 32}],
            },
            {"transport": "openai_compat", "max_input": 131072, "price_in": 0.037},
        ],
    )
    v = _llm_card_view(ref)
    assert v["is_cloud"] is False  # served → Local grid despite the slug
    assert [o["transport"] for o in v["offerings"]] == ["local", "openai_compat"]
    assert v["offerings"][0]["local"] is True
    assert v["offerings"][0]["hosts"] == ["castor"]
    assert v["offerings"][1]["local"] is False
    # headline keeps the cloud rate (the first non-local offering)
    assert v["price_in"] == 0.037 and v["window"] == 131072
    # the card-level + nested served_by dedupe to one host chip
    assert [h["host"] for h in v["hosts"]] == ["castor"]


def test_models_ctx_groups_sorts_and_lists_serving_hosts() -> None:
    refs = [
        _ref(
            "medium",
            "z-ai/glm-4.7-flash",
            tier_floor="medium",
            offerings=[{"price_in": 0.1}],
        ),
        _ref(
            "super",
            "z-ai/glm-5.2",
            tier_floor="frontier",
            offerings=[{"price_in": 0.9}],
        ),
        _ref(
            "mid",
            "qwen/qwen3.7-max",
            tier_floor="big",
            offerings=[{"price_in": 0.5}],
        ),
        # SMALL is a cloud-routed tier post-Phase-C — must sort after MEDIUM,
        # not fall into the unranked bucket alongside "unknown".
        _ref(
            "small",
            "z-ai/glm-4.7-flash-mini",
            tier_floor="small",
            offerings=[{"price_in": 0.05}],
        ),
        _ref(
            "unranked",
            "z-ai/glm-mystery",
            tier_floor="unknown",
            offerings=[{"price_in": 0.02}],
        ),
        _ref("anchor", "qwen-heavy", tier_floor="big"),
        _ref("served", "qwen3.6-35b", served_by=[{"host": "spark", "max_parallel": 1}]),
    ]
    ctx = _models_ctx(_FakeStore(refs))
    # Cloud sorted strongest tier first; small after medium; unknown-tier
    # (unranked, .get(..., 9) fallback) last.
    assert [c["tier"] for c in ctx["cloud_cards"]] == [
        "frontier",
        "big",
        "medium",
        "small",
        "unknown",
    ]
    # Local: host-backed models before the abstract tier anchors.
    assert [c["model_id"] for c in ctx["local_cards"]] == ["qwen3.6-35b", "qwen-heavy"]
    assert ctx["serving_hosts"] == ["spark"]
    # The active-routing header rides along (independent of the card query).
    assert [r["tier"] for r in ctx["active_routing"]] == [
        "frontier",
        "big",
        "medium",
        "small",
    ]


def test_models_ctx_degrades_cards_but_keeps_active_routing_on_store_error() -> None:
    # A ``list_refs`` boom empties the catalog grids, but the active-routing
    # header reads the live chains (not ``list_refs``), so it still renders.
    ctx = _models_ctx(_FakeStore([], boom=True))
    assert ctx["cloud_cards"] == []
    assert ctx["local_cards"] == []
    assert ctx["serving_hosts"] == []
    assert [r["tier"] for r in ctx["active_routing"]] == [
        "frontier",
        "big",
        "medium",
        "small",
    ]


def test_active_routing_ctx_reflects_an_operator_chain(monkeypatch) -> None:
    # The prod-shaped path: SMALL carries an operator chain
    # ``[cloud glm-4.7-flash → local summarizer]``. The header must advertise
    # rung-0 (glm-4.7-flash, cloud) as active, mark the source "operator chain",
    # and list the full failover order.
    from precis.utils.llm import live_config
    from precis.utils.llm.router import Tier

    small_chain = [
        {
            "placement": "cloud",
            "model": "z-ai/glm-4.7-flash",
            "transport": "openai_compat",
        },
        {"placement": "local", "model": "summarizer", "transport": "local"},
    ]
    monkeypatch.setattr(
        live_config,
        "chain_override",
        lambda tier: small_chain if tier is Tier.SMALL else None,
    )
    # The cloud rung is openai_compat — needs a real base url or
    # resolve_chain's PRECIS_LLM_BASE_URL guard (gr259631) degrades the
    # whole chain back to the compiled default instead of honoring it.
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    rows = {r["tier"]: r for r in _active_routing_ctx(_FakeStore([]))["active_routing"]}
    small = rows["small"]
    assert small["source"] == "operator chain"
    assert small["active_model"] == "z-ai/glm-4.7-flash"
    assert small["active_placement"] == "cloud"
    assert [(g["model"], g["placement"]) for g in small["rungs"]] == [
        ("z-ai/glm-4.7-flash", "cloud"),
        ("summarizer", "local"),
    ]
    # A tier without an override still shows its compiled default.
    assert rows["frontier"]["source"] == "default"


def test_active_routing_ctx_resolves_default_chain_per_tier() -> None:
    # With no operator ``llm.chain.*`` override reachable (the fake store has no
    # app_settings), every tier resolves its compiled DEFAULT chain — one
    # primary rung, ``source`` "default", a concrete model, a placement.
    ctx = _active_routing_ctx(_FakeStore([]))
    rows = ctx["active_routing"]
    assert [r["tier"] for r in rows] == ["frontier", "big", "medium", "small"]
    assert ctx["active_backend"] in ("anthropic", "openai")
    for r in rows:
        assert r["source"] == "default"  # no override reachable
        assert r["active_model"] and r["active_model"] != "—"
        assert r["active_placement"] in ("cloud", "local")
        assert len(r["rungs"]) >= 1
        # rung-0 IS the advertised active model.
        assert r["rungs"][0]["model"] == r["active_model"]
        assert r["rungs"][0]["placement"] == r["active_placement"]


def test_active_routing_ctx_marks_a_chain_with_no_local_rung(
    monkeypatch,
) -> None:
    """gr623033 — the card flags ``has_local`` so a cloud-only chain can
    say so. SMALL carries an all-cloud operator chain here."""
    from precis.utils.llm import live_config
    from precis.utils.llm.router import Tier

    monkeypatch.setattr(
        live_config,
        "chain_override",
        lambda tier: (
            [
                {
                    "placement": "cloud",
                    "model": "z-ai/glm-4.7-flash",
                    "transport": "openai_compat",
                }
            ]
            if tier is Tier.SMALL
            else None
        ),
    )
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    rows = {r["tier"]: r for r in _active_routing_ctx(_FakeStore([]))["active_routing"]}
    assert rows["small"]["has_local"] is False
    for r in rows.values():
        assert r["has_local"] == any(g["placement"] == "local" for g in r["rungs"])


def test_active_routing_ctx_shows_a_big_operator_chain_unfiltered(
    monkeypatch,
) -> None:
    """td624226 review — a BIG chain's local completion rung must render:
    the tools filter is per-call (a tier serves agentic AND completion
    traffic), so resolving the tier with ``tools_needed=True`` dropped the
    LOCAL rung and the card then lied "no local rung"."""
    from precis.utils.llm import live_config
    from precis.utils.llm.router import Tier

    big_chain = [
        {"placement": "local", "model": "gpt-oss-120b", "transport": "local"},
        {
            "placement": "cloud",
            "model": "claude-sonnet-5",
            "transport": "claude_agent",
        },
    ]
    monkeypatch.setattr(
        live_config,
        "chain_override",
        lambda tier: big_chain if tier is Tier.BIG else None,
    )
    rows = {r["tier"]: r for r in _active_routing_ctx(_FakeStore([]))["active_routing"]}
    big = rows["big"]
    assert big["source"] == "operator chain"
    # The configured rung-0 IS the advertised active model — a completion
    # call walks it; a tool-using call skips it (the ``tools`` flag).
    assert big["active_model"] == "gpt-oss-120b"
    assert big["active_placement"] == "local"
    assert big["has_local"] is True
    assert [(g["model"], g["placement"], g["tools"]) for g in big["rungs"]] == [
        ("gpt-oss-120b", "local", False),
        ("claude-sonnet-5", "cloud", True),
    ]


def test_active_routing_ctx_shows_an_all_local_big_chain(monkeypatch) -> None:
    """The sharpened td624226 case: a BIG chain of ONLY tool-less rungs —
    the per-call filter emptied it outright, so the card rendered the
    *default* chain under an "operator chain" badge. The configured rung
    must list, flagged as a completion wire."""
    from precis.utils.llm import live_config
    from precis.utils.llm.router import Tier

    monkeypatch.setattr(
        live_config,
        "chain_override",
        lambda tier: (
            [{"placement": "local", "model": "gpt-oss-120b", "transport": "local"}]
            if tier is Tier.BIG
            else None
        ),
    )
    big = {r["tier"]: r for r in _active_routing_ctx(_FakeStore([]))["active_routing"]}[
        "big"
    ]
    assert big["has_local"] is True
    assert [(g["model"], g["placement"], g["tools"]) for g in big["rungs"]] == [
        ("gpt-oss-120b", "local", False)
    ]


def test_active_routing_ctx_malformed_big_chain_keeps_the_tools_default(
    monkeypatch,
) -> None:
    """A malformed override still degrades to the default chain — resolved
    with the tier's representative ``tools_needed``, so a BIG card never
    advertises the ``claude_p`` completion wire its agentic traffic would
    not take."""
    from precis.utils.llm import live_config
    from precis.utils.llm.router import Tier

    monkeypatch.setattr(
        live_config,
        "chain_override",
        lambda tier: [{"placement": "local"}] if tier is Tier.BIG else None,
    )
    big = {r["tier"]: r for r in _active_routing_ctx(_FakeStore([]))["active_routing"]}[
        "big"
    ]
    assert big["source"] == "operator chain"  # the override exists; it is broken
    assert big["rungs"]  # the default chain, not the malformed rung
    assert all(g["tools"] for g in big["rungs"])


def test_llm_op_pins_ctx_empty_without_routing_overrides() -> None:
    # No ``llm.op.*`` rows reachable → nothing to list (the fake store has
    # no app_settings; ``quota_fallback``-only rows would be skipped too).
    assert _llm_op_pins_ctx(_FakeStore([])) == {"op_pins": []}


def test_llm_op_pins_ctx_lists_a_local_op_chain(monkeypatch) -> None:
    """gr623033 — an ``llm.op.<source>`` chain carrying a local rung must
    surface even when no ``llm.chain.<tier>`` has one: this is how the
    fleet's local gpt-oss-120b serves ``chase:verify``."""
    from precis.utils.llm import live_config

    chain = [
        {"placement": "local", "model": "gpt-oss-120b", "transport": "local"},
        {
            "placement": "cloud",
            "model": "z-ai/glm-4.7",
            "transport": "openai_compat",
            "profile": "oss",
        },
    ]
    monkeypatch.setattr(
        live_config,
        "op_override",
        lambda s: {"chain": chain} if s == "chase:verify" else None,
    )
    pins = {p["source"]: p for p in _llm_op_pins_ctx(_FakeStore([]))["op_pins"]}
    pin = pins["chase:verify"]
    assert pin["chain_name"] == "op chain"
    assert pin["has_local"] is True
    # chase:verify is a BIG-tier op — the local completion rung must still
    # list (the tools filter is per-call, not per-op).
    assert [(g["model"], g["placement"]) for g in pin["rungs"]] == [
        ("gpt-oss-120b", "local"),
        ("z-ai/glm-4.7", "cloud"),
    ]
    # Local-carrying pins sort ahead of cloud-only ones.
    assert _llm_op_pins_ctx(_FakeStore([]))["op_pins"][0]["has_local"] is True


def test_llm_op_pins_ctx_local_first_keeps_only_local_rungs(monkeypatch) -> None:
    """A ``<op>:local`` local-first arm walks only LOCAL-transport rungs
    (router._local_first_chain) — the page shows that arm, not the raw op
    chain."""
    from precis.utils.llm import live_config

    source = "survey_extract:table_recipe:local"
    chain = [
        {"placement": "local", "model": "gpt-oss-120b", "transport": "local"},
        {
            "placement": "cloud",
            "model": "claude-haiku-4-5",
            "transport": "claude_p",
        },
    ]
    monkeypatch.setattr(
        live_config,
        "op_override",
        lambda s: {"chain": chain} if s == source else None,
    )
    pins = {p["source"]: p for p in _llm_op_pins_ctx(_FakeStore([]))["op_pins"]}
    pin = pins[source]
    assert pin["local_first"] is True
    assert pin["chain_name"] == "local-first chain"
    assert [(g["model"], g["placement"], g["transport"]) for g in pin["rungs"]] == [
        ("gpt-oss-120b", "local", "local")
    ]


def test_llm_op_pins_ctx_local_first_with_no_local_rung_is_a_refusal(
    monkeypatch,
) -> None:
    """A local-first arm whose op chain has no LOCAL rung refuses at
    dispatch — the page must show the empty arm, not the cloud rungs the
    chain configured."""
    from precis.utils.llm import live_config

    source = "survey_extract:table_recipe:local"
    chain = [
        {
            "placement": "cloud",
            "model": "claude-haiku-4-5",
            "transport": "claude_p",
        }
    ]
    monkeypatch.setattr(
        live_config,
        "op_override",
        lambda s: {"chain": chain} if s == source else None,
    )
    pin = {p["source"]: p for p in _llm_op_pins_ctx(_FakeStore([]))["op_pins"]}[source]
    assert pin["rungs"] == []
    assert pin["has_local"] is False


def test_llm_op_pins_ctx_model_pin_rides_the_tier_chain(monkeypatch) -> None:
    """An ``llm.op.<source>`` model pin (no chain) rides the op's tier
    chain — listed so the pin's resolved ladder is visible, with the
    pinned model filling rungs that carry none of their own."""
    from precis.utils.llm import live_config

    monkeypatch.setattr(
        live_config,
        "op_override",
        lambda s: {"model": "gpt-oss-120b"} if s == "classify" else None,
    )
    pin = {p["source"]: p for p in _llm_op_pins_ctx(_FakeStore([]))["op_pins"]}[
        "classify"
    ]
    assert pin["tier"] == "small"  # registry default, un-remapped
    assert pin["chain_name"] == "small chain"
    assert pin["model_pin"] == "gpt-oss-120b"
    assert pin["rungs"]  # the resolved SMALL ladder, verbatim


def test_llm_op_pins_ctx_skips_quota_fallback_only_rows(monkeypatch) -> None:
    """A ``quota_fallback``-only override doesn't touch the ladder — not a
    routing pin, not listed."""
    from precis.utils.llm import live_config

    monkeypatch.setattr(
        live_config,
        "op_override",
        lambda s: {"quota_fallback": False} if s == "classify" else None,
    )
    assert _llm_op_pins_ctx(_FakeStore([])) == {"op_pins": []}


def test_models_tab_renders_cards_end_to_end(client, runtime) -> None:
    """Render smoke: the Jinja template compiles, the tab is wired, and a
    cloud card's price + a local card's serving host both reach the HTML."""
    runtime.store.list_refs = lambda **_kw: [
        _ref(
            162503,
            "z-ai/glm-5.2",
            title="GLM-5.2 — strongest open-weight model.",
            tier_floor="frontier",
            offerings=[{"price_in": 0.969, "price_out": 3.045, "max_input": 1048576}],
        ),
        _ref(
            165574,
            "qwen3.6-35b-a3b",
            served_by=[{"host": "balthazar", "max_parallel": 1}],
            offerings=[
                {
                    "transport": "local",
                    "served_by": [{"host": "balthazar", "max_parallel": 1}],
                },
                {"transport": "openai_compat", "price_in": 0.4},
            ],
        ),
    ]
    html = client.get("/status?tab=models").text
    assert "z-ai/glm-5.2" in html  # cloud card name
    assert "0.969" in html  # headline list price
    assert "qwen3.6-35b-a3b" in html  # local card name
    assert "balthazar" in html  # served-by host chip
    assert "local:balthazar" in html  # the local offering names its host
    assert "openai_compat" in html  # and the cloud fallback renders too
    # The active-routing header renders with a row per capability tier.
    assert "Active routing" in html
    for tier in ("frontier", "big", "medium", "small"):
        assert tier in html
