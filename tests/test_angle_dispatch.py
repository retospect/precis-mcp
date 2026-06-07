"""``search`` angle-spray wiring through the runtime dispatcher.

The pure math (``test_angle.py``) and store engine (``test_angle_search.py``)
are pinned separately; here we exercise the dispatch interception:
routing on ``angle=``/``like=``, seed resolution (``q=`` embed vs
``like='kind:id'`` stored vector), the kind filter, and the guard/error
paths — end to end through ``runtime.dispatch``.
"""

from __future__ import annotations

import re

from precis.runtime import PrecisRuntime


def _put_memory(runtime: PrecisRuntime, text: str) -> int:
    out = runtime.dispatch("put", {"kind": "memory", "text": text})
    m = re.search(r"id=(\d+)", out)
    assert m, out
    return int(m.group(1))


def _embed_card(runtime: PrecisRuntime, ref_id: int, text: str) -> None:
    """Populate the memory's card_combined embedding (worker is lazy)."""
    store = runtime.hub.store
    assert store is not None
    vec = runtime.hub.embedder.embed_one(text)
    (cid,) = store.card_chunk_ids([ref_id])
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, 'bge-m3', %s, 'ok', 1) "
            "ON CONFLICT (chunk_id, embedder) DO UPDATE "
            "SET vector = EXCLUDED.vector, status = 'ok'",
            (cid, vec),
        )


# ── routing + happy paths ───────────────────────────────────────────


def test_like_seed_returns_other_neighbour(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    a = _put_memory(rt, "copper catalyses nitrate reduction")
    b = _put_memory(rt, "palladium absorbs hydrogen gas")
    _embed_card(rt, a, "copper catalyses nitrate reduction")
    _embed_card(rt, b, "palladium absorbs hydrogen gas")
    # angle=1 from a's card, a excluded → the only other item is b
    out = rt.dispatch("search", {"like": f"memory:{a}", "angle": 1.0, "n": 1})
    assert "neighbour" in out
    assert f"#{b}" in out
    assert f"#{a}" not in out


def test_q_seed_embeds_and_returns_neighbours(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    a = _put_memory(rt, "copper catalyses nitrate reduction")
    _embed_card(rt, a, "copper catalyses nitrate reduction")
    out = rt.dispatch("search", {"q": "copper nitrate", "angle": 1.0, "n": 3})
    assert "neighbour" in out
    assert f"#{a}" in out


def test_kind_filter_keeps_only_targets(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    a = _put_memory(rt, "alpha note")
    _embed_card(rt, a, "alpha note")
    # restrict to paper → the lone memory is filtered out → empty spray
    out = rt.dispatch("search", {"q": "alpha", "angle": 1.0, "n": 5, "kind": "paper"})
    assert "no neighbours" in out


# ── guards / errors ─────────────────────────────────────────────────


def test_angle_without_seed_errors(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch("search", {"angle": 0.5})
    assert "requires q= or like=" in out


def test_bad_angle_value_errors(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch("search", {"q": "x", "angle": 2.0})
    assert "angle must be in" in out


def test_like_unembedded_errors(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    a = _put_memory(rt, "not yet embedded")  # no _embed_card call
    out = rt.dispatch("search", {"like": f"memory:{a}", "angle": 1.0})
    assert "no embedding yet" in out


def test_like_takes_precedence_does_not_need_embedder(
    runtime_with_store: PrecisRuntime,
) -> None:
    # like= seeds from a stored vector, so it must work even with the
    # embedder absent. Simulate by dropping the hub embedder.
    rt = runtime_with_store
    a = _put_memory(rt, "seed item")
    b = _put_memory(rt, "other item")
    _embed_card(rt, a, "seed item")
    _embed_card(rt, b, "other item")
    rt.hub.embedder = None
    out = rt.dispatch("search", {"like": f"memory:{a}", "angle": 1.0, "n": 1})
    assert "neighbour" in out
    assert f"#{b}" in out
