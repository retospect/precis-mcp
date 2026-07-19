"""Tests for per-host local-LLM advertisement (:mod:`precis.workers.llm_serving`).

Runs against real PG (the ``store`` fixture) so the card upsert + the
``resource_slots`` reconcile exercise the true store paths. The HTTP probe is
monkeypatched — no llama-swap is contacted.
"""

from __future__ import annotations

from typing import Any

from precis.workers import llm_serving


def test_local_serve_url_env_override(monkeypatch: Any) -> None:
    monkeypatch.setenv("PRECIS_LOCAL_SERVE_URL", "http://127.0.0.1:11444/v1/")
    assert llm_serving.local_serve_url() == "http://127.0.0.1:11444/v1"


def test_local_serve_url_os_default(monkeypatch: Any) -> None:
    monkeypatch.delenv("PRECIS_LOCAL_SERVE_URL", raising=False)
    monkeypatch.setattr(llm_serving.platform, "system", lambda: "Linux")
    assert llm_serving.local_serve_url() == "http://127.0.0.1:11444/v1"
    monkeypatch.setattr(llm_serving.platform, "system", lambda: "Darwin")
    assert llm_serving.local_serve_url() == "http://127.0.0.1:11445/v1"


def test_discover_parses_ids_and_defaults_parallel(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        llm_serving,
        "_get_json",
        lambda url, timeout=6.0: {"data": [{"id": "m-a"}, {"id": "m-b"}]},
    )
    monkeypatch.setattr(llm_serving, "_parallel_by_model", lambda: {"m-a": 4})
    assert llm_serving.discover_local_models("http://x/v1") == {"m-a": 4, "m-b": 1}


def test_discover_returns_none_on_probe_failure(monkeypatch: Any) -> None:
    def boom(url: str, timeout: float = 6.0) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(llm_serving, "_get_json", boom)
    assert llm_serving.discover_local_models("http://x/v1") is None


def test_advertise_creates_card_and_seeds_slot(store: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        llm_serving, "discover_local_models", lambda base: {"qwen-235b-test": 2}
    )
    adv, pruned = llm_serving.advertise_local_llm(
        store, "spark", base_url="http://127.0.0.1:11444/v1"
    )
    assert (adv, pruned) == (1, 0)

    ref = store.find_ref_by_meta(kind="llm", key="model_id", value="qwen-235b-test")
    entry = store.get_ref(kind="llm", id=ref.id).meta["served_by"][0]
    assert entry == {
        "host": "spark",
        "endpoint": "http://127.0.0.1:11444/v1",
        "model": "qwen-235b-test",
        "max_parallel": 2,
    }
    slots = {s.resource: s.capacity for s in store.resource_slots_for_host("spark")}
    assert slots.get("llm:qwen-235b-test") == 2


def test_advertise_merges_two_hosts_on_one_card(store: Any, monkeypatch: Any) -> None:
    """melchior + spark both serve the shared 27B → one card, two served_by entries
    (each host owns only its own, no clobber)."""
    monkeypatch.setattr(
        llm_serving, "discover_local_models", lambda base: {"qwen-27b-test": 3}
    )
    llm_serving.advertise_local_llm(
        store, "melchior", base_url="http://127.0.0.1:11445/v1"
    )
    llm_serving.advertise_local_llm(
        store, "spark", base_url="http://127.0.0.1:11444/v1"
    )
    ref = store.find_ref_by_meta(kind="llm", key="model_id", value="qwen-27b-test")
    served = store.get_ref(kind="llm", id=ref.id).meta["served_by"]
    hosts = {e["host"]: e["endpoint"] for e in served}
    assert hosts == {
        "melchior": "http://127.0.0.1:11445/v1",
        "spark": "http://127.0.0.1:11444/v1",
    }


def test_advertise_prunes_a_gone_model(store: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        llm_serving, "discover_local_models", lambda base: {"m-gone-test": 1}
    )
    llm_serving.advertise_local_llm(store, "spark", base_url="http://x/v1")

    # Next cycle: the model vanished from this host.
    monkeypatch.setattr(llm_serving, "discover_local_models", lambda base: {})
    adv, pruned = llm_serving.advertise_local_llm(
        store, "spark", base_url="http://x/v1"
    )
    assert (adv, pruned) == (0, 1)

    ref = store.find_ref_by_meta(kind="llm", key="model_id", value="m-gone-test")
    assert store.get_ref(kind="llm", id=ref.id).meta.get("served_by") == []
    slots = {s.resource for s in store.resource_slots_for_host("spark")}
    assert "llm:m-gone-test" not in slots  # slot retracted with the entry


def test_advertise_noop_without_local_server(store: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(llm_serving, "local_serve_url", lambda: None)
    assert llm_serving.advertise_local_llm(store, "spark") == (0, 0)


def test_advertise_noop_on_probe_failure(store: Any, monkeypatch: Any) -> None:
    """A transient probe failure must NOT retract a real advertisement."""
    monkeypatch.setattr(llm_serving, "discover_local_models", lambda base: None)
    assert llm_serving.advertise_local_llm(store, "spark", base_url="http://x/v1") == (
        0,
        0,
    )
