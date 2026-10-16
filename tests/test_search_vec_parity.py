"""Parity pin for chunk-search ``query_vec`` binding.

Pins exact ranked keys + scores of the semantic / fused / dispatcher /
multi search paths so a change in how ``query_vec`` is bound to SQL
(``list[float]`` -> ``pgvector.Vector``) is provably result-neutral.
Hits are keyed by ``(ref slug, chunk ord)``, never by database id.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pgvector import Vector

from precis.embedder import MockEmbedder
from precis.store import ChunkInsert, Store

TOL = 1e-6
_EMB = MockEmbedder(dim=1024)

_PAPERS: list[tuple[str, str, list[str]]] = [
    (
        "wang2020nitrate",
        "Wang 2020",
        [
            "Nitrate reduction on copper electrodes is fast and selective.",
            "Copper catalysts convert nitrate to ammonia at low potential.",
            "Carbon dioxide capture with amine solvents is unrelated here.",
            "Electrode surface roughness changes ammonia selectivity.",
        ],
    ),
    (
        "smith2019perovskite",
        "Smith 2019",
        [
            "Perovskite solar cells degrade under humidity and heat.",
            "Ammonia synthesis over ruthenium catalysts requires high pressure.",
            "Lead halide perovskite stability improves with encapsulation.",
        ],
    ),
    (
        "lee2021enzymes",
        "Lee 2021",
        [
            "Nitrogenase enzymes reduce nitrogen to ammonia under ambient conditions.",
            "Protein folding kinetics depend on temperature and solvent.",
            "Copper containing enzymes catalyse nitrite reduction in bacteria.",
            "Catalyst deactivation by sulfur poisoning in ammonia plants.",
            "Molecular dynamics of nitrate transporters in membranes.",
        ],
    ),
]

_Q_NITRATE = "nitrate reduction copper catalyst"
_Q_AMMONIA = "ammonia synthesis catalyst"
_Q_PEROV = "perovskite solar stability"


def _seed(store: Store) -> None:
    for slug, title, blocks in _PAPERS:
        ref = store.insert_ref(kind="paper", slug=slug, title=title)
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(ord=i, text=t, embedding=_EMB.embed_one(t))
                for i, t in enumerate(blocks)
            ],
        )
    mem = store.insert_ref(kind="memory", slug=None, title="Mem")
    t = "Copper nitrate memory note about ammonia catalyst reduction."
    store.chunks.insert_chunks(
        mem.id, [ChunkInsert(ord=0, text=t, embedding=_EMB.embed_one(t))]
    )


def _hits(rows: list[Any]) -> list[tuple[str, int, float]]:
    return [(ref.slug or f"<{ref.kind}>", blk.ord, float(s)) for blk, ref, s in rows]


def _bind(
    fn: Callable[[Store, str], list[Any]], q: str
) -> Callable[[Store], list[Any]]:
    return lambda s: fn(s, q)


# case name -> callable(store) -> rows
def _cases() -> dict[str, Callable[[Store], list[Any]]]:
    def vec(q: str) -> list[float]:
        return _EMB.embed_one(q)

    c: dict[str, Callable[[Store], list[Any]]] = {}
    for name, q in [
        ("nitrate", _Q_NITRATE),
        ("ammonia", _Q_AMMONIA),
        ("perov", _Q_PEROV),
    ]:
        c[f"semantic_{name}"] = _bind(
            lambda s, q: s.chunks.search_chunks_semantic(query_vec=vec(q), limit=20), q
        )
        c[f"fused_{name}"] = _bind(
            lambda s, q: s.chunks.search_chunks_fused(q=q, query_vec=vec(q), limit=20),
            q,
        )
        c[f"dispatch_semantic_{name}"] = _bind(
            lambda s, q: s.chunks.search_chunks(
                q=q, query_vec=vec(q), mode="semantic", limit=20
            ),
            q,
        )
        c[f"dispatch_hybrid_{name}"] = _bind(
            lambda s, q: s.chunks.search_chunks(
                q=q, query_vec=vec(q), mode="hybrid", limit=20
            ),
            q,
        )
    c["semantic_kind_paper"] = lambda s: s.chunks.search_chunks_semantic(
        query_vec=vec(_Q_NITRATE), kind="paper", limit=20
    )
    c["semantic_kind_memory"] = lambda s: s.chunks.search_chunks_semantic(
        query_vec=vec(_Q_NITRATE), kind="memory", limit=20
    )
    c["fused_kind_paper"] = lambda s: s.chunks.search_chunks_fused(
        q=_Q_NITRATE, query_vec=vec(_Q_NITRATE), kind="paper", limit=20
    )
    c["semantic_max_distance"] = lambda s: s.chunks.search_chunks_semantic(
        query_vec=vec(_Q_AMMONIA), max_distance=0.99, limit=20
    )
    c["fused_page_0"] = lambda s: s.chunks.search_chunks_fused(
        q=_Q_AMMONIA, query_vec=vec(_Q_AMMONIA), limit=3, offset=0
    )
    c["fused_page_1"] = lambda s: s.chunks.search_chunks_fused(
        q=_Q_AMMONIA, query_vec=vec(_Q_AMMONIA), limit=3, offset=3
    )
    c["multi_one_leg"] = lambda s: s.chunks.search_chunks_multi(
        q_texts=[_Q_NITRATE], query_vecs=[vec(_Q_NITRATE)], limit=20
    )
    return c


GOLDEN: dict[str, list[tuple[str, int, float]]] = {
    "semantic_nitrate": [
        ("smith2019perovskite", 0, 0.942743912),
        ("wang2020nitrate", 3, 0.967486654),
        ("<memory>", 0, 0.976477287),
        ("wang2020nitrate", 0, 0.981541461),
        ("lee2021enzymes", 1, 0.99238677),
        ("smith2019perovskite", 2, 0.99601075),
        ("lee2021enzymes", 2, 0.998038195),
        ("wang2020nitrate", 1, 1.011955504),
        ("wang2020nitrate", 2, 1.01219055),
        ("lee2021enzymes", 3, 1.013729748),
        ("smith2019perovskite", 1, 1.033741478),
        ("lee2021enzymes", 0, 1.040943769),
        ("lee2021enzymes", 4, 1.066104773),
    ],
    "fused_nitrate": [
        ("<memory>", 0, 0.032266458),
        ("smith2019perovskite", 0, 0.016393443),
        ("wang2020nitrate", 3, 0.016129032),
        ("wang2020nitrate", 0, 0.015625),
        ("lee2021enzymes", 1, 0.015384615),
        ("smith2019perovskite", 2, 0.015151515),
        ("lee2021enzymes", 2, 0.014925373),
        ("wang2020nitrate", 1, 0.014705882),
        ("wang2020nitrate", 2, 0.014492754),
        ("lee2021enzymes", 3, 0.014285714),
        ("smith2019perovskite", 1, 0.014084507),
        ("lee2021enzymes", 0, 0.013888889),
        ("lee2021enzymes", 4, 0.01369863),
    ],
    "dispatch_semantic_nitrate": [
        ("smith2019perovskite", 0, 0.942743912),
        ("wang2020nitrate", 3, 0.967486654),
        ("<memory>", 0, 0.976477287),
        ("wang2020nitrate", 0, 0.981541461),
        ("lee2021enzymes", 1, 0.99238677),
        ("smith2019perovskite", 2, 0.99601075),
        ("lee2021enzymes", 2, 0.998038195),
        ("wang2020nitrate", 1, 1.011955504),
        ("wang2020nitrate", 2, 1.01219055),
        ("lee2021enzymes", 3, 1.013729748),
        ("smith2019perovskite", 1, 1.033741478),
        ("lee2021enzymes", 0, 1.040943769),
        ("lee2021enzymes", 4, 1.066104773),
    ],
    "dispatch_hybrid_nitrate": [
        ("<memory>", 0, 0.032266458),
        ("smith2019perovskite", 0, 0.016393443),
        ("wang2020nitrate", 3, 0.016129032),
        ("wang2020nitrate", 0, 0.015625),
        ("lee2021enzymes", 1, 0.015384615),
        ("smith2019perovskite", 2, 0.015151515),
        ("lee2021enzymes", 2, 0.014925373),
        ("wang2020nitrate", 1, 0.014705882),
        ("wang2020nitrate", 2, 0.014492754),
        ("lee2021enzymes", 3, 0.014285714),
        ("smith2019perovskite", 1, 0.014084507),
        ("lee2021enzymes", 0, 0.013888889),
        ("lee2021enzymes", 4, 0.01369863),
    ],
    "semantic_ammonia": [
        ("lee2021enzymes", 3, 0.952645039),
        ("<memory>", 0, 0.953617598),
        ("smith2019perovskite", 0, 0.956609077),
        ("lee2021enzymes", 2, 0.978068475),
        ("wang2020nitrate", 3, 0.988052051),
        ("lee2021enzymes", 4, 0.999688681),
        ("smith2019perovskite", 2, 0.999840695),
        ("smith2019perovskite", 1, 1.003114106),
        ("wang2020nitrate", 2, 1.020446565),
        ("wang2020nitrate", 0, 1.027580586),
        ("wang2020nitrate", 1, 1.028031348),
        ("lee2021enzymes", 0, 1.033294201),
        ("lee2021enzymes", 1, 1.076496053),
    ],
    "fused_ammonia": [
        ("smith2019perovskite", 1, 0.031099325),
        ("lee2021enzymes", 3, 0.016393443),
        ("<memory>", 0, 0.016129032),
        ("smith2019perovskite", 0, 0.015873016),
        ("lee2021enzymes", 2, 0.015625),
        ("wang2020nitrate", 3, 0.015384615),
        ("lee2021enzymes", 4, 0.015151515),
        ("smith2019perovskite", 2, 0.014925373),
        ("wang2020nitrate", 2, 0.014492754),
        ("wang2020nitrate", 0, 0.014285714),
        ("wang2020nitrate", 1, 0.014084507),
        ("lee2021enzymes", 0, 0.013888889),
        ("lee2021enzymes", 1, 0.01369863),
    ],
    "dispatch_semantic_ammonia": [
        ("lee2021enzymes", 3, 0.952645039),
        ("<memory>", 0, 0.953617598),
        ("smith2019perovskite", 0, 0.956609077),
        ("lee2021enzymes", 2, 0.978068475),
        ("wang2020nitrate", 3, 0.988052051),
        ("lee2021enzymes", 4, 0.999688681),
        ("smith2019perovskite", 2, 0.999840695),
        ("smith2019perovskite", 1, 1.003114106),
        ("wang2020nitrate", 2, 1.020446565),
        ("wang2020nitrate", 0, 1.027580586),
        ("wang2020nitrate", 1, 1.028031348),
        ("lee2021enzymes", 0, 1.033294201),
        ("lee2021enzymes", 1, 1.076496053),
    ],
    "dispatch_hybrid_ammonia": [
        ("smith2019perovskite", 1, 0.031099325),
        ("lee2021enzymes", 3, 0.016393443),
        ("<memory>", 0, 0.016129032),
        ("smith2019perovskite", 0, 0.015873016),
        ("lee2021enzymes", 2, 0.015625),
        ("wang2020nitrate", 3, 0.015384615),
        ("lee2021enzymes", 4, 0.015151515),
        ("smith2019perovskite", 2, 0.014925373),
        ("wang2020nitrate", 2, 0.014492754),
        ("wang2020nitrate", 0, 0.014285714),
        ("wang2020nitrate", 1, 0.014084507),
        ("lee2021enzymes", 0, 0.013888889),
        ("lee2021enzymes", 1, 0.01369863),
    ],
    "semantic_perov": [
        ("lee2021enzymes", 3, 0.958118244),
        ("smith2019perovskite", 0, 0.975338399),
        ("<memory>", 0, 0.982533081),
        ("wang2020nitrate", 0, 0.98940465),
        ("smith2019perovskite", 1, 0.999803943),
        ("lee2021enzymes", 1, 1.002501223),
        ("lee2021enzymes", 2, 1.003183508),
        ("wang2020nitrate", 2, 1.007984458),
        ("wang2020nitrate", 3, 1.024408108),
        ("lee2021enzymes", 0, 1.025838377),
        ("smith2019perovskite", 2, 1.036334974),
        ("lee2021enzymes", 4, 1.037870472),
        ("wang2020nitrate", 1, 1.049040079),
    ],
    "fused_perov": [
        ("lee2021enzymes", 3, 0.016393443),
        ("smith2019perovskite", 0, 0.016129032),
        ("<memory>", 0, 0.015873016),
        ("wang2020nitrate", 0, 0.015625),
        ("smith2019perovskite", 1, 0.015384615),
        ("lee2021enzymes", 1, 0.015151515),
        ("lee2021enzymes", 2, 0.014925373),
        ("wang2020nitrate", 2, 0.014705882),
        ("wang2020nitrate", 3, 0.014492754),
        ("lee2021enzymes", 0, 0.014285714),
        ("smith2019perovskite", 2, 0.014084507),
        ("lee2021enzymes", 4, 0.013888889),
        ("wang2020nitrate", 1, 0.01369863),
    ],
    "dispatch_semantic_perov": [
        ("lee2021enzymes", 3, 0.958118244),
        ("smith2019perovskite", 0, 0.975338399),
        ("<memory>", 0, 0.982533081),
        ("wang2020nitrate", 0, 0.98940465),
        ("smith2019perovskite", 1, 0.999803943),
        ("lee2021enzymes", 1, 1.002501223),
        ("lee2021enzymes", 2, 1.003183508),
        ("wang2020nitrate", 2, 1.007984458),
        ("wang2020nitrate", 3, 1.024408108),
        ("lee2021enzymes", 0, 1.025838377),
        ("smith2019perovskite", 2, 1.036334974),
        ("lee2021enzymes", 4, 1.037870472),
        ("wang2020nitrate", 1, 1.049040079),
    ],
    "dispatch_hybrid_perov": [
        ("lee2021enzymes", 3, 0.016393443),
        ("smith2019perovskite", 0, 0.016129032),
        ("<memory>", 0, 0.015873016),
        ("wang2020nitrate", 0, 0.015625),
        ("smith2019perovskite", 1, 0.015384615),
        ("lee2021enzymes", 1, 0.015151515),
        ("lee2021enzymes", 2, 0.014925373),
        ("wang2020nitrate", 2, 0.014705882),
        ("wang2020nitrate", 3, 0.014492754),
        ("lee2021enzymes", 0, 0.014285714),
        ("smith2019perovskite", 2, 0.014084507),
        ("lee2021enzymes", 4, 0.013888889),
        ("wang2020nitrate", 1, 0.01369863),
    ],
    "semantic_kind_paper": [
        ("smith2019perovskite", 0, 0.942743912),
        ("wang2020nitrate", 3, 0.967486654),
        ("wang2020nitrate", 0, 0.981541461),
        ("lee2021enzymes", 1, 0.99238677),
        ("smith2019perovskite", 2, 0.99601075),
        ("lee2021enzymes", 2, 0.998038195),
        ("wang2020nitrate", 1, 1.011955504),
        ("wang2020nitrate", 2, 1.01219055),
        ("lee2021enzymes", 3, 1.013729748),
        ("smith2019perovskite", 1, 1.033741478),
        ("lee2021enzymes", 0, 1.040943769),
        ("lee2021enzymes", 4, 1.066104773),
    ],
    "semantic_kind_memory": [
        ("<memory>", 0, 0.976477287),
    ],
    "fused_kind_paper": [
        ("smith2019perovskite", 0, 0.016393443),
        ("wang2020nitrate", 3, 0.016129032),
        ("wang2020nitrate", 0, 0.015873016),
        ("lee2021enzymes", 1, 0.015625),
        ("smith2019perovskite", 2, 0.015384615),
        ("lee2021enzymes", 2, 0.015151515),
        ("wang2020nitrate", 1, 0.014925373),
        ("wang2020nitrate", 2, 0.014705882),
        ("lee2021enzymes", 3, 0.014492754),
        ("smith2019perovskite", 1, 0.014285714),
        ("lee2021enzymes", 0, 0.014084507),
        ("lee2021enzymes", 4, 0.013888889),
    ],
    "semantic_max_distance": [
        ("lee2021enzymes", 3, 0.952645039),
        ("<memory>", 0, 0.953617598),
        ("smith2019perovskite", 0, 0.956609077),
        ("lee2021enzymes", 2, 0.978068475),
        ("wang2020nitrate", 3, 0.988052051),
    ],
    "fused_page_0": [
        ("smith2019perovskite", 1, 0.016393443),
        ("lee2021enzymes", 3, 0.016393443),
        ("<memory>", 0, 0.016129032),
    ],
    "fused_page_1": [
        ("smith2019perovskite", 0, 0.015873016),
        ("lee2021enzymes", 2, 0.015625),
        ("wang2020nitrate", 3, 0.015384615),
    ],
    "multi_one_leg": [
        ("<memory>", 0, 0.032266458),
        ("smith2019perovskite", 0, 0.016393443),
        ("wang2020nitrate", 3, 0.016129032),
        ("wang2020nitrate", 0, 0.015625),
        ("lee2021enzymes", 1, 0.015384615),
        ("smith2019perovskite", 2, 0.015151515),
        ("lee2021enzymes", 2, 0.014925373),
        ("wang2020nitrate", 1, 0.014705882),
        ("wang2020nitrate", 2, 0.014492754),
        ("lee2021enzymes", 3, 0.014285714),
        ("smith2019perovskite", 1, 0.014084507),
        ("lee2021enzymes", 0, 0.013888889),
        ("lee2021enzymes", 4, 0.01369863),
    ],
}


def _diff(
    name: str, got: list[tuple[str, int, float]], want: list[tuple[str, int, float]]
) -> str:
    lines = [f"[{name}] got {len(got)} hits, golden {len(want)}"]
    for i in range(max(len(got), len(want))):
        g = got[i] if i < len(got) else None
        w = want[i] if i < len(want) else None
        if g is None or w is None or g[:2] != w[:2]:
            lines.append(f"  #{i}: got={g} golden={w}  KEY MOVED")
        else:
            d = g[2] - w[2]
            flag = "" if abs(d) <= TOL else "  SCORE DRIFT"
            lines.append(f"  #{i}: {g[0]}/{g[1]} score delta={d:+.3e}{flag}")
    return "\n".join(lines)


@pytest.fixture
def seeded(store: Store) -> Store:
    _seed(store)
    return store


TIE = 1e-9
# Pages of one query: a tie spanning the boundary may split either way.
PAGED_CASES = ("fused_page_0", "fused_page_1")

Hit = tuple[str, int, float]


def _groups(hits: list[Hit]) -> list[list[Hit]]:
    """Group consecutive hits whose scores are equal within TIE."""
    out: list[list[Hit]] = []
    for h in hits:
        if out and abs(out[-1][-1][2] - h[2]) <= TIE:
            out[-1].append(h)
        else:
            out.append([h])
    return out


def _scores_close(got: list[Hit], want: list[Hit]) -> bool:
    gs, ws = sorted(h[2] for h in got), sorted(h[2] for h in want)
    return len(gs) == len(ws) and all(abs(a - b) <= TOL for a, b in zip(gs, ws))


def _groups_match(got: list[Hit], want: list[Hit]) -> bool:
    """Same group sequence; keys equal as sets within a group; scores close."""
    gg, wg = _groups(got), _groups(want)
    return len(gg) == len(wg) and all(
        {h[:2] for h in a} == {h[:2] for h in b}
        and len(a) == len(b)
        and _scores_close(a, b)
        for a, b in zip(gg, wg)
    )


@pytest.mark.parametrize("name", list(_cases()))
def test_ranked_results_match_golden(seeded: Store, name: str) -> None:
    cases = _cases()
    got = _hits(cases[name](seeded))
    want = GOLDEN[name]
    if name not in PAGED_CASES:
        assert _groups_match(got, want), _diff(name, got, want)
        return
    # Paged: union of keys over both pages, per-page scores, and the tie
    # groups that sit fully inside the page (not cut by the page boundary).
    other = PAGED_CASES[1 - PAGED_CASES.index(name)]
    got_other = _hits(cases[other](seeded))
    keys = lambda hs: {h[:2] for h in hs}
    assert keys(got) | keys(got_other) == keys(want) | keys(GOLDEN[other]), _diff(
        name, got, want
    )
    assert _scores_close(got, want), _diff(name, got, want)
    p0, p1 = GOLDEN[PAGED_CASES[0]], GOLDEN[PAGED_CASES[1]]
    cut = abs(p0[-1][2] - p1[0][2]) <= TIE
    g_groups, w_groups = _groups(got), _groups(want)
    if cut:
        drop = -1 if name == PAGED_CASES[0] else 0
        del g_groups[drop], w_groups[drop]
    assert len(g_groups) == len(w_groups) and all(
        {h[:2] for h in a} == {h[:2] for h in b} for a, b in zip(g_groups, w_groups)
    ), _diff(name, got, want)


def test_vec_param_binds_vector() -> None:
    """Guards the GIL fix (docs/backlog/mcp-concurrency-load-test.md, py-spy
    section) against a revert to raw ``list[float]`` binding."""
    from precis.store._chunks_ops import _vec_param

    assert isinstance(_vec_param([0.1] * 4), Vector)
    v = Vector([0.1] * 4)
    assert _vec_param(v) is v


def test_golden_covers_every_case() -> None:
    assert set(GOLDEN) == set(_cases())
    assert sum(len(v) for v in GOLDEN.values()) >= 6 * 3


@pytest.mark.parametrize("q", [_Q_NITRATE, _Q_AMMONIA, _Q_PEROV])
def test_list_and_pgvector_vector_identical(seeded: Store, q: str) -> None:
    lst = _EMB.embed_one(q)
    vec = Vector(lst)
    sem_l = _hits(seeded.chunks.search_chunks_semantic(query_vec=lst, limit=20))
    sem_v = _hits(
        seeded.chunks.search_chunks_semantic(
            query_vec=vec,  # type: ignore[arg-type]
            limit=20,
        )
    )
    fus_l = _hits(seeded.chunks.search_chunks_fused(q=q, query_vec=lst, limit=20))
    fus_v = _hits(
        seeded.chunks.search_chunks_fused(
            q=q,
            query_vec=vec,  # type: ignore[arg-type]
            limit=20,
        )
    )
    for a, b in ((sem_l, sem_v), (fus_l, fus_v)):
        assert len(a) >= 3
        assert [x[:2] for x in a] == [x[:2] for x in b]
        assert all(abs(x[2] - y[2]) <= TOL for x, y in zip(a, b, strict=True))
