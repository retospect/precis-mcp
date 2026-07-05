"""Oracle lens sampling policy (``utils/oracle_lens.py``) + the handler
``get(lens=...)`` surface.

The policy tests use a tiny fake store and an injected ``random.Random``
so the mixture is deterministic — no DB, no embedder. One integration
test ingests the bundled persona traditions and exercises the real
handler path.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import cast

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.oracle import OracleHandler
from precis.jobs.ingest_oracles import bundled_oracle_dir, ingest_paper
from precis.store import Store
from precis.utils.oracle_lens import (
    LENS_REGISTRY,
    LensDraw,
    draw_lens_entry,
    render_lens_block_from_draw,
    resolve_lens_traditions,
)


class FakeStore:
    """Minimal store exposing just the two methods ``draw_lens_entry`` calls."""

    def __init__(self, traditions: dict[str, list[str]]) -> None:
        self._refs: list[SimpleNamespace] = []
        self._blocks: dict[int, list[SimpleNamespace]] = {}
        for i, (slug, titles) in enumerate(traditions.items(), start=1):
            self._refs.append(
                SimpleNamespace(id=i, kind="oracle", slug=slug, title=slug.title())
            )
            self._blocks[i] = [
                SimpleNamespace(
                    id=i * 100 + pos,
                    pos=pos,
                    text=f"{title} body",
                    meta={"section_path": [title]},
                )
                for pos, title in enumerate(titles, start=1)
            ]

    def list_refs(self, *, kind: str, limit: int = 50, **_kw: object) -> list:
        return [r for r in self._refs if r.kind == kind][:limit]

    def list_blocks_for_ref(self, ref_id: int) -> list:
        return list(self._blocks.get(ref_id, []))


def _fake(traditions: dict[str, list[str]]) -> Store:
    """FakeStore typed as a Store for the draw_lens_entry call boundary."""
    return cast(Store, FakeStore(traditions))


# ── resolve_lens_traditions ─────────────────────────────────────────


def test_resolve_unions_favoured_sets() -> None:
    assert resolve_lens_traditions(["sci"]) == {"scientists"}
    assert resolve_lens_traditions(["sci", "art"]) == {"scientists", "artists"}
    assert resolve_lens_traditions(["people"]) == {
        "scientists",
        "leadership",
        "artists",
    }


def test_resolve_is_case_insensitive() -> None:
    assert resolve_lens_traditions(["SCI"]) == {"scientists"}


def test_resolve_unknown_raises() -> None:
    with pytest.raises(BadInput):
        resolve_lens_traditions(["bogus"])


def test_resolve_empty_raises() -> None:
    with pytest.raises(BadInput):
        resolve_lens_traditions([])


def test_every_registered_lens_resolves() -> None:
    for name in LENS_REGISTRY:
        assert resolve_lens_traditions([name])


# ── draw_lens_entry: mixture policy ─────────────────────────────────


def _store() -> Store:
    # favoured 'scientists' (6) + three other traditions of very different
    # sizes so the "even across traditions, not entries" contract is
    # testable.
    return _fake(
        {
            "scientists": [f"sci{i}" for i in range(6)],
            "stoic": [f"st{i}" for i in range(40)],  # big
            "zen": ["z0"],  # tiny
            "proverbs": ["p0", "p1"],
        }
    )


def test_bias_is_roughly_half_favoured() -> None:
    store = _store()
    rng = random.Random(0)
    fav = sum(
        draw_lens_entry(store, ["sci"], bias=0.5, rng=rng).from_favoured  # type: ignore[union-attr]
        for _ in range(2000)
    )
    # 50/50 split — generous tolerance, but nowhere near 0 or all.
    assert 0.4 < fav / 2000 < 0.6


def test_bias_one_is_favoured_only() -> None:
    store = _store()
    rng = random.Random(1)
    for _ in range(200):
        draw = draw_lens_entry(store, ["sci"], bias=1.0, rng=rng)
        assert draw is not None and draw.ref.slug == "scientists"


def test_bias_zero_never_favoured() -> None:
    store = _store()
    rng = random.Random(2)
    for _ in range(200):
        draw = draw_lens_entry(store, ["sci"], bias=0.0, rng=rng)
        assert draw is not None and draw.ref.slug != "scientists"


def test_rest_is_even_across_traditions_not_entries() -> None:
    """In the non-favoured bucket, each tradition is ~equally likely
    regardless of how many entries it holds — the tiny 1-entry 'zen'
    must not be ~40× rarer than the 40-entry 'stoic'."""
    store = _store()
    rng = random.Random(3)
    counts: dict[str, int] = {}
    for _ in range(3000):
        draw = draw_lens_entry(store, ["sci"], bias=0.0, rng=rng)
        assert draw is not None
        slug = draw.ref.slug
        assert slug is not None
        counts[slug] = counts.get(slug, 0) + 1
    # Three non-favoured traditions → each ~1/3 despite 40:2:1 entry counts.
    for slug in ("stoic", "zen", "proverbs"):
        assert 0.22 < counts.get(slug, 0) / 3000 < 0.44, counts


def test_favoured_absent_falls_back_to_rest() -> None:
    # lens favours 'scientists' but the store has none loaded yet.
    store = _fake({"stoic": ["a", "b"], "zen": ["c"]})
    rng = random.Random(4)
    draw = draw_lens_entry(store, ["sci"], rng=rng)
    assert draw is not None
    assert draw.from_favoured is False
    assert draw.ref.slug in ("stoic", "zen")


def test_empty_store_returns_none() -> None:
    assert draw_lens_entry(_fake({}), ["sci"]) is None


def test_lens_covering_everything_still_draws() -> None:
    # 'people' favours all three persona traditions; with only those
    # loaded the 'rest' bucket is empty and it must still return a draw.
    store = _fake({"scientists": ["s"], "leadership": ["l"], "artists": ["a"]})
    rng = random.Random(5)
    draw = draw_lens_entry(store, ["people"], rng=rng)
    assert draw is not None and draw.from_favoured is True


def test_empty_tradition_is_skipped() -> None:
    store = _fake({"scientists": [], "stoic": ["only"]})
    draw = draw_lens_entry(store, ["sci"], bias=1.0, rng=random.Random(6))
    # favoured is empty (no blocks) → falls back to stoic.
    assert draw is not None and draw.ref.slug == "stoic"


def test_render_lens_block_from_draw() -> None:
    store = _fake({"scientists": ["Feynman"]})
    draw = draw_lens_entry(store, ["sci"], bias=1.0, rng=random.Random(7))
    assert draw is not None
    block = render_lens_block_from_draw(draw)
    assert block.startswith("## This cycle's lens: Feynman")
    assert "Feynman body" in block


def test_lensdraw_is_frozen() -> None:
    store = _fake({"scientists": ["x"]})
    draw = draw_lens_entry(store, ["sci"], bias=1.0, rng=random.Random(8))
    assert isinstance(draw, LensDraw)


# ── handler get(lens=...) over a real store ─────────────────────────


@pytest.fixture
def oracle(hub: Hub) -> OracleHandler:
    return OracleHandler(hub=hub)


def _ingest_bundled(store: Store, name: str) -> None:
    bundled = bundled_oracle_dir()
    assert bundled is not None
    ingest_paper(bundled / name, store=store, embedder=None, overwrite=True)


def test_handler_lens_consult_draws_a_scientist(
    store: Store, oracle: OracleHandler
) -> None:
    _ingest_bundled(store, "scientists.yaml")
    # Only scientists loaded → sci lens must draw from it.
    resp = oracle.get(lens="sci")
    assert "(lens: sci)" in resp.body
    assert "oracle scientists~" in resp.body


def test_handler_lens_list_form(store: Store, oracle: OracleHandler) -> None:
    _ingest_bundled(store, "scientists.yaml")
    resp = oracle.get(lens=["sci"])
    assert "(lens: sci)" in resp.body


def test_handler_unknown_lens_raises(store: Store, oracle: OracleHandler) -> None:
    _ingest_bundled(store, "scientists.yaml")
    with pytest.raises(BadInput):
        oracle.get(lens="bogus")
