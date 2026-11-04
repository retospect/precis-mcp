"""Stored kinetics census: partitions, exact selectors and quest membership."""

from __future__ import annotations

import re
from collections import defaultdict
from hashlib import sha256
from typing import Any
from uuid import uuid4

import pytest

from precis.errors import BadInput
from precis.store import Store
from precis.utils.pathway_kinetics_summary import kinetics_summary


def _pathway(store: Store, meta: dict[str, Any]) -> int:
    return store.insert_ref(
        kind="pathway",
        slug=f"summary-{uuid4().hex}",
        title="kinetics fixture",
        meta=meta,
    ).id


def _solve(store: Store, v2: dict[str, Any], **meta: Any) -> int:
    return _pathway(store, {"results": {"kinetics_v2": v2}, **meta})


def _counts(body: str) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = defaultdict(dict)
    for line in body.splitlines():
        if " | " in line and "=" in line:
            selector, count = line.rsplit(" | ", 1)
            axis, value = selector.split("=", 1)
            result[axis][value] = int(count)
    return dict(result)


def _ids(body: str) -> list[int]:
    return [
        int(line[2:])
        for line in body.splitlines()
        if line.startswith("pw") and line[2:].isdigit()
    ]


@pytest.fixture
def population(store: Store) -> list[int]:
    """All rungs, all flux boundaries, absent/v1, and structured failures."""
    ids = [
        _solve(
            store,
            {
                "tof_chain": 2,
                "bep_flux_share": 0,
                "mf_status": {"ok": False, "failed_observables": []},
            },
            kinetics_trusted=False,
        ),
        _solve(
            store,
            {
                "tof_mf": 3,
                "bep_flux_share": 0.1,
                "mf_status": {"ok": True, "failed_observables": []},
            },
            kinetics_trusted=True,
        ),
        _solve(
            store,
            {
                "tof_kmc": {"median": 4},
                "tof_mf": 3,
                "tof_chain": 2,
                "bep_flux_share": 0.5,
                "mf_status": {
                    "ok": True,
                    "failed_observables": [{"name": "order", "reason": "unresolved"}],
                },
            },
        ),
        _solve(
            store,
            {
                "tof_mf": -1,
                "bep_flux_share": 0.50001,
                "mf_status": {
                    "ok": False,
                    "diagnosis": {
                        "kind": "poisoned_surface",
                        "covering": ["NO@N", "NH2OH", "NO@N"],
                    },
                    "tof_sign": {"class": "reverse"},
                },
            },
        ),
        _solve(
            store,
            {
                "tof_mf": 0,
                "mf_status": {"ok": False, "tof_sign": {"class": "reverse"}},
            },
        ),
        _solve(store, {"tof_chain": -1, "mf_status": {"ok": False}}),
        _solve(store, {"mf_status": {"ok": False}}),
        _solve(
            store, {"tof_mf": 1, "warnings": ["unresolved slow mode; poisoned surface"]}
        ),
        _pathway(store, {}),
        _pathway(
            store, {"results": {"kinetics": {"tof": 9, "environment_basis": "v1-only"}}}
        ),
    ]
    retired = _solve(store, {"tof_mf": 1})
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (retired,))
    return ids


def test_partitions_and_every_count_selector_roundtrip(
    store: Store, population: list[int]
) -> None:
    body = kinetics_summary(store)
    assert f"{len(population)} pathways" in body
    counts = _counts(body)
    assert set(counts) == {
        "kinetics_method",
        "failed_observables",
        "failure_class",
        "bep_flux_share",
        "kinetics_trusted",
        "environment_basis",
    }
    for axis, values in counts.items():
        assert values["no v2 solve"] == 2
        assert sum(values.values()) == len(population)
        listed: list[int] = []
        for value, count in values.items():
            page = kinetics_summary(
                store, args={"class": f"{axis}={value}", "page_size": 100}
            )
            ids = _ids(page)
            assert len(ids) == count
            assert ids == sorted(ids)
            assert f"{count} total" in page
            listed.extend(ids)
        assert sorted(listed) == sorted(population)
    assert counts["bep_flux_share"] == {
        "0": 1,
        "up to 0.1": 1,
        "up to 0.5": 1,
        "above 0.5": 1,
        "none": 4,
        "no v2 solve": 2,
    }
    assert counts["failed_observables"] == {
        "yes": 1,
        "no": 2,
        "not recorded": 5,
        "no v2 solve": 2,
    }
    assert counts["failure_class"] == {
        "poisoned surface: NH2OH, NO@N": 1,
        "tof_sign: reverse": 1,
        "no positive steady state": 1,
        "chain fallback": 1,
        "mean-field failed": 1,
        "none": 2,
        "not recorded": 1,
        "no v2 solve": 2,
    }


def test_exact_dynamic_classes_and_unknown_options(
    store: Store, population: list[int]
) -> None:
    basis = "sha256:" + "abc123" * 10
    ident = _pathway(
        store,
        {
            "results": {
                "kinetics_v2": {
                    "tof_mf": 1,
                    "mf_status": {"diagnosis": {"kind": "custom_solver_failure"}},
                },
                "kinetics": {"environment_basis": basis},
            }
        },
    )
    for selector in (
        f"environment_basis={basis}",
        f"environment_basis={basis[:12]}",
        "failure_class=diagnosis: custom_solver_failure",
    ):
        assert _ids(kinetics_summary(store, args={"class": selector})) == [ident]
    with pytest.raises(BadInput) as exc:
        kinetics_summary(store, args={"class": "failure_class=poisoned"})
    expected = {
        f"{axis}={value}"
        for axis, values in _counts(kinetics_summary(store)).items()
        for value in values
    }
    expected.remove(f"environment_basis={basis[:12]}")
    expected.add(f"environment_basis={basis}")
    assert set(exc.value.options or []) == expected
    assert "failure_class=poisoned surface: NH2OH, NO@N" in expected
    assert f"environment_basis={basis}" in expected


def test_pagination_is_numeric_id_order_with_replayable_next(store: Store) -> None:
    expected = [_solve(store, {"tof_chain": 1}) for _ in range(23)]
    first = kinetics_summary(store, args={"class": "kinetics_method=chain"})
    assert _ids(first) == expected[:20]
    assert "23 total; 20 shown; offset 0" in first
    assert "'page_size': 20, 'offset': 20" in first
    last = kinetics_summary(
        store, args={"class": "kinetics_method=chain", "offset": 20}
    )
    assert _ids(last) == expected[20:]
    assert "next:" not in last
    assert (
        _ids(
            kinetics_summary(
                store,
                args={"class": "kinetics_method=chain", "page_size": 1, "offset": 1},
            )
        )
        == expected[1:2]
    )


@pytest.mark.parametrize(
    "args",
    [
        {"page_size": 0},
        {"page_size": 101},
        {"page_size": True},
        {"page_size": "1"},
        {"offset": -1},
        {"offset": False},
        {"offset": 1.5},
        {"class": None},
        {"class": []},
        {"axis": "unknown"},
        {"axis": None},
        {"axis": []},
        {"axis": "environment_basis", "class": "environment_basis=not recorded"},
        {"U": -0.3},
    ],
)
def test_invalid_selector_and_paging_refuse(store: Store, args: dict[str, Any]) -> None:
    with pytest.raises(BadInput):
        kinetics_summary(store, args=args)


def test_empty_population_keeps_every_axis_and_no_v2_row(store: Store) -> None:
    counts = _counts(kinetics_summary(store))
    assert len(counts) == 6
    assert all(
        values["no v2 solve"] == 0 and sum(values.values()) == 0
        for values in counts.values()
    )
    assert _ids(kinetics_summary(store, args={"class": "kinetics_method=chain"})) == []


@pytest.mark.parametrize("unsolved_count", [0, 1, 2])
def test_no_v2_row_is_outside_summary_value_limit(
    store: Store, unsolved_count: int
) -> None:
    bases = [sha256(str(i).encode("utf-8")).hexdigest() for i in range(10)]
    for basis in bases:
        _pathway(
            store,
            {
                "results": {
                    "kinetics_v2": {"tof_mf": 1},
                    "kinetics": {"environment_basis": basis},
                }
            },
        )
    for _ in range(unsolved_count):
        _pathway(store, {})

    body = kinetics_summary(store)
    counts = _counts(body)
    assert len(counts) == 6
    for section in body.split("\n## ")[1:]:
        axis = section.splitlines()[0]
        values = counts[axis]
        assert values["no v2 solve"] == unsolved_count
        assert section.count(f"{axis}=no v2 solve | ") == 1
        other = re.search(r"other: (\d+) values, (\d+) pathways", section)
        assert sum(values.values()) + (int(other[2]) if other else 0) == (
            10 + unsolved_count
        )
    assert counts["environment_basis"] == {
        **{basis[:12]: 1 for basis in bases},
        "no v2 solve": unsolved_count,
    }
    assert "other: 1 values, 0 pathways" in body
    offset = 11
    assert f"'axis': 'environment_basis', 'offset': {offset}" in body
    remainder = _counts(
        kinetics_summary(store, args={"axis": "environment_basis", "offset": offset})
    )
    assert remainder["environment_basis"]["not recorded"] == 0


def test_high_cardinality_summary_is_bounded_and_axis_pages_are_complete(
    store: Store,
) -> None:
    """754 stored hashes used to exceed the transport cap and lose count rows."""
    bases = [sha256(str(i).encode("utf-8")).hexdigest() for i in range(754)]
    for i, basis in enumerate(bases):
        _pathway(
            store,
            {
                "results": {
                    "kinetics_v2": {
                        "tof_mf": 1,
                        "mf_status": {"diagnosis": {"kind": f"failure-{i % 15:02d}"}},
                    },
                    "kinetics": {"environment_basis": basis},
                }
            },
        )

    body = kinetics_summary(store)
    assert len(body.encode("utf-8")) < 24_576
    assert "754 pathways" in body
    counts = _counts(body)
    assert len(counts) == 6
    for section in body.split("\n## ")[1:]:
        axis = section.splitlines()[0]
        values = counts[axis]
        assert len(values) <= 11
        assert values["no v2 solve"] == 0
        assert list(values.values()) == sorted(values.values(), reverse=True)
        other = re.search(r"other: (\d+) values, (\d+) pathways", section)
        assert sum(values.values()) + (int(other[2]) if other else 0) == 754
        if other:
            assert int(other[1]) > 0
            assert "get(kind='pathway', view='kinetics-summary'" in section
            assert f"'axis': '{axis}'" in section
            assert "'offset':" in section
    assert list(counts["environment_basis"]) == [
        *[basis[:12] for basis in sorted(bases)[:10]],
        "no v2 solve",
    ]
    assert "other: 745 values, 744 pathways" in body
    assert list(counts["failure_class"].values()) == [51] * 4 + [50] * 6 + [0]

    expected = [(basis[:12], 1) for basis in sorted(bases)] + [
        ("no v2 solve", 0),
        ("not recorded", 0),
    ]
    seen: list[tuple[str, int]] = []
    for offset in range(0, len(expected), 100):
        page = kinetics_summary(
            store,
            args={"axis": "environment_basis", "offset": offset, "page_size": 100},
        )
        assert len(page.encode("utf-8")) < 24_576
        page_counts = _counts(page)
        assert set(page_counts) == {"environment_basis"}
        assert "other:" not in page
        seen.extend(page_counts["environment_basis"].items())
        if offset + 100 < len(expected):
            assert "next: get(kind='pathway', view='kinetics-summary'" in page
            assert "'axis': 'environment_basis'" in page
            assert f"'offset': {offset + 100}" in page
        else:
            assert "next:" not in page
            assert f"environment_basis={sorted(bases)[-1][:12]} | 1" in page
    assert seen == expected
    assert sum(count for _, count in seen) == 754
    beyond = kinetics_summary(
        store, args={"axis": "environment_basis", "offset": len(expected)}
    )
    assert _counts(beyond) == {}
    assert "next:" not in beyond


def test_basis_prefix_collisions_preserve_counts_and_full_selectors(
    store: Store,
) -> None:
    prefix = "123456789abc"
    bases = [prefix + suffix * 52 for suffix in "de"]
    ids = [
        _pathway(
            store,
            {
                "results": {
                    "kinetics_v2": {"tof_mf": 1},
                    "kinetics": {"environment_basis": basis},
                }
            },
        )
        for basis in bases
    ]
    assert (
        _ids(kinetics_summary(store, args={"class": f"environment_basis={prefix}"}))
        == ids
    )
    for basis, ident in zip(bases, ids, strict=True):
        assert _ids(
            kinetics_summary(store, args={"class": f"environment_basis={basis}"})
        ) == [ident]
    page = kinetics_summary(store, args={"axis": "environment_basis"})
    assert page.splitlines().count(f"environment_basis={prefix} | 1") == 2

    exact = _pathway(
        store,
        {
            "results": {
                "kinetics_v2": {"tof_mf": 1},
                "kinetics": {"environment_basis": prefix},
            }
        },
    )
    assert _ids(
        kinetics_summary(store, args={"class": f"environment_basis={prefix}"})
    ) == [exact]


def test_record_precedence_and_explicit_trust_and_basis(store: Store) -> None:
    ident = _pathway(
        store,
        {
            "kinetics_v2": {"tof_chain": 2},
            "kinetics_trusted": True,
            "environment_basis": "wrong-top-level",
            "results": {
                "kinetics_v2": {"tof_mf": 3},
                "kinetics": {"environment_basis": "full-stored-basis"},
            },
            "config": {"environment": {"basis": "wrong-config"}},
        },
    )
    fallback = _pathway(
        store, {"kinetics_v2": {"tof_chain": 1}, "kinetics_trusted": "true"}
    )
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="kinetics job with an unrelated trust stamp",
        meta={"kinetics_trusted": True, "pathway_ref_id": fallback},
    )
    store.add_link(src_ref_id=job.id, dst_ref_id=fallback, relation="related-to")
    counts = _counts(kinetics_summary(store))
    assert counts["kinetics_method"]["mean-field"] == 1
    assert counts["kinetics_method"]["chain"] == 1
    assert counts["kinetics_trusted"]["true"] == 1
    assert counts["kinetics_trusted"]["not recorded"] == 1
    assert counts["environment_basis"]["full-stored-"] == 1
    assert _ids(
        kinetics_summary(store, args={"class": "environment_basis=full-stored-basis"})
    ) == [ident]
    assert _ids(kinetics_summary(store, args={"class": "kinetics_method=chain"})) == [
        fallback
    ]


def test_quest_membership_and_handler_scope(store: Store) -> None:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    quest = store.insert_ref(kind="quest", slug=None, title="kinetics quest")
    candidates = [
        store.insert_ref(
            kind="structure", slug=f"summary-candidate-{i}", title="candidate"
        )
        for i in range(4)
    ]
    for candidate in candidates:
        store.add_link(src_ref_id=candidate.id, dst_ref_id=quest.id, relation="serves")
    by_candidate = _pathway(store, {"candidate_ref": candidates[0].id})
    by_forward, by_reverse, direct, retired_candidate, outside = [
        _pathway(store, {}) for _ in range(5)
    ]
    for src, dst, relation in [
        (candidates[1].id, by_forward, "related-to"),
        (by_reverse, candidates[2].id, "related-to"),
        (direct, quest.id, "serves"),
        (by_candidate, candidates[1].id, "related-to"),
        (retired_candidate, candidates[3].id, "related-to"),
    ]:
        store.add_link(src_ref_id=src, dst_ref_id=dst, relation=relation)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (candidates[3].id,)
        )
    args = {"class": "kinetics_method=no v2 solve", "page_size": 1}
    handler = QuestHandler(hub=Hub(store=store))
    counts = _counts(handler.get(id=quest.id, view="kinetics-summary").body)
    assert all(sum(values.values()) == 4 for values in counts.values())
    with pytest.raises(BadInput, match="concrete quest id"):
        handler.get(view="kinetics-summary")
    body = handler.get(id=quest.id, view="kinetics-summary", args=args).body
    assert body == kinetics_summary(store, quest_id=quest.id, args=args)
    assert f"id='qu{quest.id}'" in body
    ids = _ids(
        kinetics_summary(
            store, quest_id=quest.id, args={"class": "kinetics_method=no v2 solve"}
        )
    )
    assert ids == sorted([by_candidate, by_forward, by_reverse, direct])
    assert outside not in ids and retired_candidate not in ids
    axis_args = {"axis": "kinetics_method", "page_size": 1, "offset": 0}
    axis_page = handler.get(id=quest.id, view="kinetics-summary", args=axis_args).body
    assert axis_page == kinetics_summary(store, quest_id=quest.id, args=axis_args)
    assert _counts(axis_page) == {"kinetics_method": {"no v2 solve": 4}}
    assert (
        f"next: get(kind='quest', id='qu{quest.id}', view='kinetics-summary'"
        in axis_page
    )
    assert "'axis': 'kinetics_method'" in axis_page
    assert "'offset': 1" in axis_page


def test_pathway_handler_population_view_and_id_refusal(store: Store) -> None:
    from types import SimpleNamespace

    from precis_pathway.handler import PathwayHandler

    ident = _solve(store, {"tof_mf": 1})
    handler = object.__new__(PathwayHandler)
    setattr(handler, "hub", SimpleNamespace(live_store=store))
    assert handler.get(view="kinetics-summary").body == kinetics_summary(store)
    assert _ids(
        handler.get(
            view="kinetics-summary", args={"class": "kinetics_method=mean-field"}
        ).body
    ) == [ident]
    with pytest.raises(BadInput):
        handler.get(id=f"pw{ident}", view="kinetics-summary")
