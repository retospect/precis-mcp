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
        "source": "auto",
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


def test_advertise_skips_noop_write_but_writes_on_change(
    store: Any, monkeypatch: Any
) -> None:
    """The refs UPDATE only fires when served_by actually changes — a repeat
    cycle with the same discovery is a byte-identical served_by and must not
    re-issue ``update_ref``; a genuinely changed served_by (new max_parallel)
    must."""
    monkeypatch.setattr(
        llm_serving, "discover_local_models", lambda base: {"qwen-dirty-test": 2}
    )
    llm_serving.advertise_local_llm(store, "spark", base_url="http://x/v1")

    calls = []
    orig_update_ref = store.update_ref

    def counting_update_ref(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return orig_update_ref(*args, **kwargs)

    monkeypatch.setattr(store, "update_ref", counting_update_ref)

    # Same discovery again — served_by rebuilds to the identical value.
    llm_serving.advertise_local_llm(store, "spark", base_url="http://x/v1")
    assert calls == []

    # A genuinely different served_by (max_parallel changed) must still write.
    monkeypatch.setattr(
        llm_serving, "discover_local_models", lambda base: {"qwen-dirty-test": 4}
    )
    llm_serving.advertise_local_llm(store, "spark", base_url="http://x/v1")
    assert len(calls) == 1


def test_advertise_noop_without_local_server(store: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(llm_serving, "local_serve_url", lambda: None)
    assert llm_serving.advertise_local_llm(store, "spark") == (0, 0)


def test_advertise_does_not_prune_a_static_served_by_entry(
    store: Any, monkeypatch: Any
) -> None:
    """A static card (e.g. a seeded remote-tunnel entry, ``source="static"``)
    must survive a heartbeat ``advertise_local_llm`` pass on the SAME host that
    discovers a DIFFERENT model via llama-swap — the static entry is never
    discoverable there (it routes to a remote tunnel), so the prune loop must
    not treat its absence-from-discovery as "gone"."""
    from precis import llm_catalog

    llm_catalog.seed_slullama_card(
        store,
        endpoint="http://127.0.0.1:11500/v1",
        model="qwen3-235b-a22b",
        host="melchior",
        max_parallel=3,
        model_id="qwen-heavy-static-test",
    )

    # This heartbeat pass on "melchior" discovers an unrelated local model —
    # the static card's model is NOT in the discovered set.
    monkeypatch.setattr(
        llm_serving, "discover_local_models", lambda base: {"other-local-model": 1}
    )
    adv, pruned = llm_serving.advertise_local_llm(
        store, "melchior", base_url="http://127.0.0.1:11445/v1"
    )
    assert pruned == 0

    ref = store.find_ref_by_meta(
        kind="llm", key="model_id", value="qwen-heavy-static-test"
    )
    served = store.get_ref(kind="llm", id=ref.id).meta["served_by"]
    assert served == [
        {
            "host": "melchior",
            "endpoint": "http://127.0.0.1:11500/v1",
            "model": "qwen3-235b-a22b",
            "max_parallel": 3,
            "source": "static",
        }
    ]
    assert adv == 1  # the other-local-model got its own (auto) card + entry


def test_advertise_keeps_static_entry_when_rebuilding_same_model(
    store: Any, monkeypatch: Any
) -> None:
    """The static-entry guard must hold in the REBUILD branch too, not only the
    prune branch. If a static card's ``model_id`` is (also) discovered locally on
    the same host, ``advertise_local_llm`` walks the per-model served_by rebuild
    (``served = [e for e in current if not _is_auto_here(e)]; served.append(...)``)
    — that filter must KEEP the ``source="static"`` entry and append the fresh
    auto one, never clobber the static. A regression that filtered on ``host``
    alone (dropping the static melchior entry) would be invisible to the
    prune-only test above, so this exercises the other call site explicitly."""
    from precis import llm_catalog

    llm_catalog.seed_slullama_card(
        store,
        endpoint="http://127.0.0.1:11500/v1",
        model="qwen3-235b-a22b",
        host="melchior",
        max_parallel=3,
        model_id="qwen-collide-test",
    )

    # Heartbeat on "melchior" DISCOVERS the same model_id locally → the rebuild
    # branch (card exists) runs for this card, not the prune branch.
    monkeypatch.setattr(
        llm_serving, "discover_local_models", lambda base: {"qwen-collide-test": 2}
    )
    llm_serving.advertise_local_llm(
        store, "melchior", base_url="http://127.0.0.1:11445/v1"
    )

    ref = store.find_ref_by_meta(kind="llm", key="model_id", value="qwen-collide-test")
    served = store.get_ref(kind="llm", id=ref.id).meta["served_by"]
    by_source = {e["source"]: e for e in served}
    assert by_source.keys() == {"static", "auto"}  # static NOT clobbered
    assert by_source["static"] == {
        "host": "melchior",
        "endpoint": "http://127.0.0.1:11500/v1",
        "model": "qwen3-235b-a22b",
        "max_parallel": 3,
        "source": "static",
    }
    assert by_source["auto"] == {
        "host": "melchior",
        "endpoint": "http://127.0.0.1:11445/v1",
        "model": "qwen-collide-test",
        "max_parallel": 2,
        "source": "auto",
    }


def test_advertise_noop_on_probe_failure(store: Any, monkeypatch: Any) -> None:
    """A transient probe failure must NOT retract a real advertisement."""
    monkeypatch.setattr(llm_serving, "discover_local_models", lambda base: None)
    assert llm_serving.advertise_local_llm(store, "spark", base_url="http://x/v1") == (
        0,
        0,
    )
