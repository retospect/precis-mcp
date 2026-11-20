"""Tests for :mod:`precis.quest.figures` — the static matplotlib twins of
the web Pareto scatter + pathway energy-profile renderers, and the
``precis quest figure`` CLI subcommand that attaches them to a draft.

Pure-Python fixtures for the renderer/snapshot unit tests (no DB); the CLI
attach-flow test runs against the real ``store`` fixture (a draft + a real
quest ref) with :func:`precis.quest.figures.quest_pareto_figure`
monkeypatched — building a Pareto-plottable candidate set end-to-end
belongs to the frontier/compute tests, not here.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from precis.quest import figures
from precis.quest.frontier import (
    Candidate,
    FrontierResult,
    ProvisionalCandidate,
    build_frontier_scatter,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class _FakeRef:
    id: int
    kind: str
    title: str
    meta: dict[str, Any] = field(default_factory=dict)


class _FakeStore:
    """Just enough of the ``Store`` surface for
    :func:`figures.build_pareto_snapshot`'s ``_objectives_for`` call."""

    def __init__(self, quest_ref: _FakeRef) -> None:
        self._quest_ref = quest_ref

    def get_ref(self, *, kind: str, id: int) -> _FakeRef | None:
        if kind == "quest" and id == self._quest_ref.id:
            return self._quest_ref
        return None


def _sample_frontier() -> FrontierResult:
    frontier = [
        Candidate(1, "st1", "Fe-N4", {"barrier": 0.3, "energy": -20.0}, True),
        Candidate(2, "st2", "Cu-N4", {"barrier": 0.9, "energy": -22.0}, True),
    ]
    dominated = [
        Candidate(3, "st3", "Ni-N4", {"barrier": 1.2, "energy": -5.0}, True),
    ]
    prov_candidate = Candidate(
        4,
        "st4",
        "Pd-N4",
        {},
        False,
        flags={"barrier_trusted": False, "barrier_untrusted_value": 0.6},
    )
    provisional = [
        ProvisionalCandidate(
            candidate=prov_candidate,
            measures={"barrier": 0.6, "energy": -12.0},
            untrusted_keys=frozenset({"barrier"}),
            reasons=["barrier untrusted"],
            on_frontier=False,
        )
    ]
    return FrontierResult(
        objectives=[("barrier", "min"), ("energy", "min")],
        frontier=frontier,
        dominated=dominated,
        provisional=provisional,
        unevaluated=[],
    )


class TestParetoRenderer:
    def test_render_pareto_png_returns_png_bytes(self) -> None:
        fr = _sample_frontier()
        scatter = build_frontier_scatter(
            [*fr.frontier, *fr.dominated],
            provisional=fr.provisional,
            frontier_ref_ids={c.ref_id for c in fr.frontier},
        )
        assert scatter is not None
        png = figures.render_pareto_png(scatter, title="Test quest")
        assert png.startswith(_PNG_MAGIC)

    def test_build_pareto_snapshot_contract(self) -> None:
        fr = _sample_frontier()
        quest_ref = _FakeRef(
            id=97,
            kind="quest",
            title="A test catalyst quest",
            meta={
                "rubric_objectives": [
                    {"key": "barrier", "sense": "min"},
                    {"key": "energy", "sense": "min"},
                ]
            },
        )
        store = _FakeStore(quest_ref)

        snapshot = figures.build_pareto_snapshot(cast(Any, store), quest_ref, fr)

        assert snapshot["schema"] == 1
        assert snapshot["source"] == {
            "kind": "quest",
            "ref_id": 97,
            "handle": "qu97",
            "title": "A test catalyst quest",
        }
        assert set(snapshot["columns"]) >= {
            "handle",
            "name",
            "band",
            "on_frontier",
            "converged",
            "trusted",
            "tier",
            "barrier",
            "energy",
        }
        # every plotted point (frontier + dominated + the one provisional
        # candidate that carries an objective value) gets exactly one row.
        assert len(snapshot["rows"]) == 4
        by_handle = {r["handle"]: r for r in snapshot["rows"]}
        assert by_handle["st1"]["band"] == "confirmed"
        assert by_handle["st1"]["on_frontier"] is True
        assert by_handle["st1"]["barrier"] == 0.3
        assert isinstance(by_handle["st1"]["barrier"], float)  # raw float, not "0.3"
        assert by_handle["st3"]["on_frontier"] is False
        assert by_handle["st4"]["band"] == "provisional"
        assert by_handle["st4"]["barrier"] == 0.6
        assert snapshot["params"]["objectives"] == [
            {"key": "barrier", "sense": "min"},
            {"key": "energy", "sense": "min"},
        ]
        assert "precis" in snapshot and "version" in snapshot["precis"]
        assert "generated_at" in snapshot

    def test_build_pareto_snapshot_degrades_to_empty_rows_below_min_points(
        self,
    ) -> None:
        """``build_pareto_snapshot`` is handed an already-computed
        ``FrontierResult`` — below the 2-point scatter floor it degrades to
        an empty ``rows`` list rather than raising; the ``ValueError`` guard
        lives in the convenience :func:`figures.quest_pareto_figure`."""
        quest_ref = _FakeRef(id=5, kind="quest", title="Underpopulated")
        store = _FakeStore(quest_ref)
        fr = FrontierResult(
            objectives=[("barrier", "min")],
            frontier=[Candidate(1, "st1", "Fe-N4", {"barrier": 0.3}, True)],
        )
        snapshot = figures.build_pareto_snapshot(cast(Any, store), quest_ref, fr)
        assert snapshot["rows"] == []
        assert snapshot["schema"] == 1

    def test_pareto_figure_axis_labels_carry_better_arrow_suffix(self) -> None:
        """The PNG twin's axis labels carry the SAME "which way is better"
        suffix (:func:`~precis.quest.frontier.better_arrow_for`) the web
        scatter shows — reused off ``scatter.x_better``/``y_better``, never
        re-derived, so the two never drift."""
        a = Candidate(1, "st1", "A", {"barrier": 0.3, "energy": -20.0}, True)
        b = Candidate(2, "st2", "B", {"barrier": 0.9, "energy": -10.0}, True)
        scatter = build_frontier_scatter(
            [a, b],
            x_label="Barrier (eV)",
            y_label="Relaxed energy (eV)",
            objectives=[("barrier", "min"), ("energy", "max")],
        )
        assert scatter is not None
        fig = figures._pareto_figure(scatter, title="Test quest")
        ax = fig.axes[0]
        assert ax.get_xlabel() == "Barrier (eV)  ← better"
        assert ax.get_ylabel() == "Relaxed energy (eV)  ↑ better"

    def test_pareto_figure_no_arrow_suffix_when_sense_unknown(self) -> None:
        a = Candidate(1, "st1", "A", {"barrier": 0.3, "energy": -20.0}, True)
        b = Candidate(2, "st2", "B", {"barrier": 0.9, "energy": -10.0}, True)
        scatter = build_frontier_scatter(
            [a, b], x_label="Barrier (eV)", y_label="Relaxed energy (eV)"
        )
        assert scatter is not None
        fig = figures._pareto_figure(scatter, title="Test quest")
        ax = fig.axes[0]
        assert ax.get_xlabel() == "Barrier (eV)"
        assert ax.get_ylabel() == "Relaxed energy (eV)"

    def test_build_pareto_snapshot_follows_per_quest_kinetics_axes(self) -> None:
        """Kinetics cutover: a quest declaring ``log_tof``/``atom_cost`` as
        its first two rubric objectives plots THOSE axes, not the old
        ``barrier``/``energy`` hub-v2 starter pick — the PNG twin must stay
        in sync with the web scatter's :func:`~precis.quest.frontier.
        plot_axes_for` pick."""
        frontier = [
            Candidate(1, "st1", "Fe-N4", {"log_tof": 2.0, "atom_cost": 1.0}, True),
            Candidate(2, "st2", "Cu-N4", {"log_tof": 1.0, "atom_cost": 0.5}, True),
        ]
        fr = FrontierResult(
            objectives=[("log_tof", "max"), ("atom_cost", "min")],
            frontier=frontier,
            dominated=[],
        )
        quest_ref = _FakeRef(
            id=98,
            kind="quest",
            title="A kinetics-cutover catalyst quest",
            meta={
                "rubric_objectives": [
                    {"key": "log_tof", "sense": "max"},
                    {"key": "atom_cost", "sense": "min"},
                ]
            },
        )
        store = _FakeStore(quest_ref)

        snapshot = figures.build_pareto_snapshot(cast(Any, store), quest_ref, fr)
        assert snapshot["params"]["x_measure"] == "log_tof"
        assert snapshot["params"]["y_measure"] == "atom_cost"
        by_handle = {r["handle"]: r for r in snapshot["rows"]}
        assert by_handle["st1"]["log_tof"] == 2.0
        assert by_handle["st1"]["atom_cost"] == 1.0
        # the PNG twin plots the same axis pair (marker grammar stays in sync)
        scatter = build_frontier_scatter(
            [*fr.frontier, *fr.dominated],
            x_measure=snapshot["params"]["x_measure"],
            y_measure=snapshot["params"]["y_measure"],
        )
        assert scatter is not None
        png = figures.render_pareto_png(scatter, title=quest_ref.title)
        assert png.startswith(_PNG_MAGIC)


def test_pareto_axes_and_trusted_only() -> None:
    """Renderer args for the draft figure source (gr592643): an explicit
    ``x=``/``y=`` pair selects the plotted columns in the snapshot rows;
    ``trusted_only`` drops provisional points; ``top_n`` caps the points;
    an unknown axis raises ``ValueError`` naming the objective keys."""
    quest_ref = _FakeRef(
        id=99,
        kind="quest",
        title="A quest with three objectives",
        meta={
            "rubric_objectives": [
                {"key": "log_tof", "sense": "max"},
                {"key": "atom_cost", "sense": "min"},
                {"key": "barrier", "sense": "min"},
            ]
        },
    )
    store = _FakeStore(quest_ref)
    fr = FrontierResult(
        objectives=[("log_tof", "max"), ("atom_cost", "min"), ("barrier", "min")],
        frontier=[
            Candidate(
                1,
                "st1",
                "Fe-N4",
                {"log_tof": 2.0, "atom_cost": 1.0, "barrier": 0.3},
                True,
            ),
            Candidate(
                2,
                "st2",
                "Cu-N4",
                {"log_tof": 1.0, "atom_cost": 0.5, "barrier": 0.9},
                True,
            ),
        ],
        dominated=[
            Candidate(
                3,
                "st3",
                "Ni-N4",
                {"log_tof": 0.5, "atom_cost": 2.0, "barrier": 1.2},
                True,
            ),
        ],
        provisional=[
            ProvisionalCandidate(
                candidate=Candidate(
                    4,
                    "st4",
                    "Pd-N4",
                    {},
                    False,
                    flags={"barrier_trusted": False},
                ),
                measures={"log_tof": 1.5, "atom_cost": 0.8, "barrier": 0.6},
                untrusted_keys=frozenset({"barrier"}),
                reasons=["barrier untrusted"],
                on_frontier=False,
            )
        ],
    )

    swapped = figures.build_pareto_snapshot(
        cast(Any, store), quest_ref, fr, x="barrier", y="log_tof"
    )
    assert swapped["params"]["x_measure"] == "barrier"
    assert swapped["params"]["y_measure"] == "log_tof"
    assert swapped["params"]["x"] == "barrier"
    assert swapped["params"]["y"] == "log_tof"
    assert "log_tof" in swapped["columns"]  # non-default axis becomes a column
    by_handle = {r["handle"]: r for r in swapped["rows"]}
    assert by_handle["st1"]["barrier"] == 0.3
    assert by_handle["st1"]["log_tof"] == 2.0

    trusted = figures.build_pareto_snapshot(
        cast(Any, store), quest_ref, fr, trusted_only=True
    )
    assert trusted["params"]["trusted_only"] is True
    assert "st4" not in {r["handle"] for r in trusted["rows"]}
    assert "st4" in {r["handle"] for r in swapped["rows"]}

    capped = figures.build_pareto_snapshot(cast(Any, store), quest_ref, fr, top_n=3)
    assert len(capped["rows"]) == 3
    assert capped["params"]["top_n"] == 3

    labelled = figures.build_pareto_snapshot(
        cast(Any, store), quest_ref, fr, label="handles"
    )
    assert labelled["params"]["label"] == "handles"

    with pytest.raises(ValueError, match="rubric objective keys"):
        figures.build_pareto_snapshot(
            cast(Any, store), quest_ref, fr, x="nope", y="barrier"
        )
    with pytest.raises(ValueError, match="rubric objective keys"):
        figures.build_pareto_snapshot(
            cast(Any, store), quest_ref, fr, x="barrier", y=None
        )


# ---------------------------------------------------------------------------
# Pathway profile
# ---------------------------------------------------------------------------

_GRAPH3: dict[str, Any] = {
    "nodes": [
        {"id": "s1", "energy": -10.0, "rel_energy": 0.0, "low_confidence": False},
        {"id": "s2", "energy": -9.6, "rel_energy": 0.4, "low_confidence": False},
        {"id": "s3", "energy": -9.9, "rel_energy": 0.1, "low_confidence": True},
    ],
    "links": [
        {
            "source": "s1",
            "target": "s2",
            "kind": "reaction",
            "barrier": 0.55,
            "delta_e": 0.4,
            "low_confidence": False,
        },
        {
            "source": "s2",
            "target": "s3",
            "kind": "reaction",
            "barrier": 0.2,
            "delta_e": -0.3,
            "low_confidence": False,
        },
    ],
}


def _pathway_ref(**meta_overrides: Any) -> _FakeRef:
    meta: dict[str, Any] = {
        "graph": _GRAPH3,
        "autocatpath_version": "0.14.0",
        "tier": "verify",
        "content_key": "abc123",
        "config": {
            "template": "no_to_nh3_pd",
            "mlip": {
                "backend": "mace",
                "model": "medium",
                "device": "cuda",
                "dtype": "mixed",
            },
            "search": {"neb_images": 7},
        },
    }
    meta.update(meta_overrides)
    return _FakeRef(id=42, kind="pathway", title="NO -> NH3 on Pd(111)", meta=meta)


class TestPathwayProfile:
    def test_kinetics_annotation_from_meta(self) -> None:
        ref = _pathway_ref(
            results={
                "kinetics_v2": {
                    "tof_mf": 0.01,
                    "trust": "provisional",
                    "environment": {"T": 350, "gases": {"inputs": {"H2": 0.2}}},
                }
            }
        )
        annotation = figures.pathway_kinetics_annotation(ref.meta)
        assert annotation is not None
        assert annotation["tof_log10"] == -2
        assert annotation["method"] == "mean-field"
        assert annotation["trust"] == "provisional"
        assert annotation["T"] == 350
        assert annotation["gases"] == {"H2": 0.2}
        assert annotation["rls_step"] == "s1→s2"
        assert annotation["steps"][0] == {"name": "s1→s2", "ea": 0.55, "sigma": None}
        assert figures.pathway_kinetics_annotation(_pathway_ref().meta) is None

    def test_profile_route_only_drops_side_states(self) -> None:
        graph = {
            "nodes": [*_GRAPH3["nodes"], {"id": "side", "rel_energy": 3}],
            "links": [
                *_GRAPH3["links"],
                {"source": "s1", "target": "side", "barrier": 4},
            ],
        }
        full = figures._profile_positions(graph, "s1", "s3", kinetics=True)
        route = figures._profile_positions(graph, "s1", "s3", route_only=True)
        assert len(route) < len(full)
        assert "side" not in {r["label"] for r in route}

    def test_profile_png_has_tof_annotation(self) -> None:
        ref = _pathway_ref(results={"kinetics_v2": {"tof_chain": 0.01}})
        png, snapshot = figures.pathway_profile_figure(cast(Any, None), ref)
        assert png.startswith(_PNG_MAGIC)
        assert "log10 TOF (/s): -2.0; chain" in snapshot["params"]["annotation"]
        _, unsolved = figures.pathway_profile_figure(cast(Any, None), _pathway_ref())
        assert unsolved["params"]["annotation"] == "kinetics not solved"

    def test_pathway_profile_figure_png_and_snapshot(self) -> None:
        pathway_ref = _pathway_ref()
        png, snapshot = figures.pathway_profile_figure(
            cast(Any, _FakeStore(_FakeRef(0, "quest", "x"))), pathway_ref
        )

        assert png.startswith(_PNG_MAGIC)
        assert snapshot["schema"] == 1
        assert snapshot["source"]["kind"] == "pathway"
        assert snapshot["source"]["ref_id"] == 42
        assert snapshot["autocatpath_version"] == "0.14.0"

        rows = snapshot["rows"]
        kinds = [r["kind"] for r in rows]
        assert kinds == ["state", "ts", "state", "ts", "state"]
        ts_rows = [r for r in rows if r["kind"] == "ts"]
        assert [r["barrier"] for r in ts_rows] == [0.55, 0.2]
        assert [r["delta_e"] for r in ts_rows] == [0.4, -0.3]
        state_rows = [r for r in rows if r["kind"] == "state"]
        assert [r["rel_energy"] for r in state_rows] == [0.0, 0.4, 0.1]
        assert state_rows[2]["low_confidence"] is True

        params = snapshot["params"]
        assert params["tier"] == "verify"
        assert params["template"] == "no_to_nh3_pd"
        assert params["mlip"] == {
            "backend": "mace",
            "model": "medium",
            "device": "cuda",
            "dtype": "mixed",
        }
        # search sub-keys this module reads aren't in the fixture's config —
        # missing -> None, not a KeyError.
        assert params["search"] == {
            "neb_schedule": None,
            "neb_optimizer": None,
            "screening": None,
        }
        assert params["content_key"] == "abc123"

    def test_build_profile_snapshot_missing_config_does_not_crash(self) -> None:
        pathway_ref = _pathway_ref(config={})
        snapshot = figures.build_profile_snapshot(
            cast(Any, _FakeStore(_FakeRef(0, "quest", "x"))), pathway_ref
        )
        assert snapshot["params"]["mlip"] == {
            "backend": None,
            "model": None,
            "device": None,
            "dtype": None,
        }
        assert snapshot["params"]["template"] is None

    def test_render_profile_png_marks_rate_limiting_step(self) -> None:
        rows = figures._profile_positions(_GRAPH3, "s1", "s3")
        png = figures.render_profile_png(rows, title="NO -> NH3")
        assert png.startswith(_PNG_MAGIC)


# ---------------------------------------------------------------------------
# CLI: `precis quest figure`
# ---------------------------------------------------------------------------


def _created_quest_id(resp: Any) -> int:
    import re

    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, f"no quest handle in ack: {resp.body!r}"
    return int(m.group(1))


def _new_project(hub: Any) -> int:
    from precis.handlers.todo import TodoHandler

    return int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )


class TestCmdFigure:
    def test_cmd_figure_attaches_pareto_figure_and_links_derived_from(
        self, store: Any, hub: Any, monkeypatch: Any
    ) -> None:
        """``_cmd_figure`` end-to-end against the real store: resolves the
        quest + draft by id, attaches an ``own_graph`` figure chunk carrying
        the snapshot in ``meta.figure.data_package``, and links it
        ``derived-from`` the quest. ``quest_pareto_figure`` is monkeypatched
        (assembling a real plottable candidate set belongs to the frontier
        tests) — this test is about the CLI's resolve/attach/link plumbing,
        not the render."""
        from precis.cli.quest import _cmd_figure
        from precis.handlers.draft import DraftHandler
        from precis.handlers.quest import QuestHandler

        qid = _created_quest_id(
            QuestHandler(hub=hub).put(text="A NO->NH3 catalyst with no external energy")
        )

        pid = _new_project(hub)
        draft = DraftHandler(hub=hub)
        draft.put(id="dfig", title="T", project=pid)
        draft_ref = store.get_ref(kind="draft", id="dfig")
        assert draft_ref is not None

        fake_snapshot = {"schema": 1, "source": {"kind": "quest", "ref_id": qid}}
        monkeypatch.setattr(
            "precis.quest.figures.quest_pareto_figure",
            lambda s, ref: (_PNG_MAGIC + b"rest", fake_snapshot),
        )

        args = argparse.Namespace(target=str(qid), draft="dfig", caption=None, pos=None)
        _cmd_figure(store, args)

        chunks = [
            c
            for c in store.drafts.reading_order(draft_ref.id)
            if c.chunk_kind == "figure"
        ]
        assert len(chunks) == 1
        fig_chunk = chunks[0]
        assert fig_chunk.meta["figure"]["origin"] == "own_graph"
        assert fig_chunk.meta["figure"]["data_package"] == fake_snapshot
        assert "Pareto frontier" in fig_chunk.text

        links = store.links_for(
            fig_chunk.chunk_id, direction="out", relation="derived-from"
        )
        assert any(link.dst_ref_id == qid for link in links)

        blob = store.drafts.get_chunk_blob(fig_chunk.handle)
        assert blob is not None
        assert blob[0].startswith(_PNG_MAGIC)

    def test_resolve_target_ref_by_bare_numeric_id(self, store: Any, hub: Any) -> None:
        from precis.cli.quest import _resolve_target_ref
        from precis.handlers.quest import QuestHandler

        qid = _created_quest_id(QuestHandler(hub=hub).put(text="Resolve me"))
        ref = _resolve_target_ref(store, str(qid))
        assert ref is not None
        assert ref.kind == "quest"
        assert ref.id == qid

    def test_resolve_target_ref_unknown_returns_none(self, store: Any) -> None:
        from precis.cli.quest import _resolve_target_ref

        assert _resolve_target_ref(store, "no-such-slug-at-all") is None
