"""Unsaved explicit intake echo: independent oracle and zero-write proof."""

from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
from precis.pcb import eyes

_CENTERS = [(-2, 1), (0, 1), (3, 1), (-2, -1), (0, -1), (3, -1)]
_NAMES = ["VCC", "CLK", "DATA", "GND", "NC", "AUX"]
_TOP = [(8, 21), (10, 21), (13, 21), (8, 19), (10, 19), (13, 19)]
_BOTTOM = [(31, 38), (31, 40), (31, 43), (29, 38), (29, 40), (29, 43)]


def _fp(numbers=None):
    numbers = numbers or ["1", "2", "3", "4", "5", "6"]
    return {
        "pads": [
            {
                "number": n,
                "x": x,
                "y": y,
                "w": 0.6,
                "h": 0.8,
                "shape": "RECT",
                "layer": "F.Cu",
            }
            for n, (x, y) in zip(numbers, _CENTERS, strict=True)
        ],
        "pin_map": {str(i): {"name": name} for i, name in enumerate(_NAMES, 1)},
    }


def _proposals():
    return [{"name": name, "pad": str(i)} for i, name in enumerate(_NAMES, 1)]


@pytest.mark.parametrize(
    "numbers,clk_local",
    [
        (["1", "2", "3", "4", "5", "6"], (0, 1)),
        (["1", "3", "5", "2", "4", "6"], (-2, -1)),
    ],
)
@pytest.mark.parametrize(
    "pose,expected,layer",
    [
        ({"x": 10, "y": 20, "rot": 0, "layer": "top"}, _TOP, "F.Cu"),
        ({"x": 30, "y": 40, "rot": 90, "layer": "bottom"}, _BOTTOM, "B.Cu"),
    ],
)
def test_independent_asymmetric_numbering_and_transform(
    numbers, clk_local, pose, expected, layer
):
    fp, proposals = _fp(numbers), _proposals()
    stored = [{"pin": "CLK", "pad": "2", "net": "STORED_CLK"}]
    before = copy.deepcopy((pose, fp, stored, proposals))
    result = eyes.pinout_preview(pose, fp, stored, ["F.Cu", "B.Cu"], proposals)
    rows = result["rows"]
    assert [(r["board_x_mm"], r["board_y_mm"]) for r in rows] == pytest.approx(expected)
    assert [r["pad_number"] for r in rows] == numbers
    assert all(r["board_layers"] == [layer] for r in rows)
    clk = next(r for r in rows if r["pad_number"] == "2")
    assert (clk["local_x_mm"], clk["local_y_mm"]) == clk_local
    assert clk["proposed_names"] == ["CLK"] and clk["net_names"] == ["STORED_CLK"]
    assert clk["proposal_provenance"] == "proposed-explicit"
    assert (pose, fp, stored, proposals) == before


def test_draft_conflicts_duplicates_unknowns_and_stored_ambiguity_remain_separate():
    fp = _fp()
    fp["pads"].append({**fp["pads"][-1], "x": 4})
    stored: list[dict[str, Any]] = [
        {"pin": "WRONG", "pad": "2", "net": "STORED_NET"},
        {"pin": "CLK", "pad": None, "net": "CLK_NET"},
    ]
    proposals: list[dict[str, Any]] = [
        {"name": "CLK", "pad": "2"},
        {"name": "OTHER", "pad": "2"},
        {"name": "CLK", "pad": "3"},
        {"name": "UNKNOWN", "pad": None},
        {"name": "LOST", "pad": "99"},
        {"name": "AUX", "pad": "6"},
        {"name": "AUX", "pad": "6"},
    ]
    result = eyes.pinout_preview({}, fp, stored, ["F.Cu", "B.Cu"], proposals)
    rows = result["rows"]
    assert rows[1]["mapping_state"] == "ambiguous"
    assert rows[1]["pin_names"] == ["CLK", "WRONG"]
    assert rows[1]["proposed_names"] == ["CLK", "OTHER"]
    assert rows[1]["proposal_state"] == rows[2]["proposal_state"] == "conflicting"
    assert "canonical authoring would refuse" in " ".join(rows[1]["proposal_notes"])
    assert result["proposals"][3]["state"] == "unknown"
    assert result["proposals"][4]["state"] == "unmatched"
    assert rows[5]["duplicate_indices"] == rows[6]["duplicate_indices"] == [6, 7]
    assert rows[5]["proposal_indices"] == rows[6]["proposal_indices"] == [6, 7]
    assert rows[5]["proposal_state"] == "repeated"
    assert rows[5]["pin_names"] == [] and rows[5]["net_names"] == []
    assert all(r["board_x_mm"] is None for r in rows)


def test_invalid_geometry_and_mixed_unknown_binding_do_not_choose_a_pad():
    fp = _fp()
    fp["pads"][1]["x"] = float("nan")
    result = eyes.pinout_preview(
        {"x": 0, "y": 0, "rot": 0},
        fp,
        [],
        ["F.Cu"],
        [{"name": "CLK", "pad": "2"}, {"name": "CLK", "pad": None}],
    )
    row = result["rows"][1]
    assert row["geometry"] == "invalid_geometry"
    assert row["local_x_mm"] is None and row["board_x_mm"] is None
    assert row["proposal_state"] == "conflicting"
    assert result["proposals"][1]["pad"] is None
    assert "pad binding unknown" in " ".join(result["proposals"][1]["notes"])


def test_distinct_null_names_remain_unknown_without_ownership_conflict():
    result = eyes.pinout_preview(
        {},
        _fp(),
        [],
        ["F.Cu"],
        [{"name": "UNKNOWN_A", "pad": None}, {"name": "UNKNOWN_B", "pad": None}],
    )
    assert [e["state"] for e in result["proposals"]] == ["unknown", "unknown"]
    assert all(row["proposal_state"] == "not-proposed" for row in result["rows"])
    assert all(
        "canonical authoring would refuse" not in " ".join(e["notes"])
        for e in result["proposals"]
    )


@pytest.fixture
def board(store):
    pcb = PcbHandler(hub=Hub(store=store))
    fp = _fp()
    fp["pads"].append({**fp["pads"][-1], "x": 4})
    pcb.put(
        id="preview-test",
        args={
            "footprints": [
                {
                    "name": "ASYM6",
                    "pads": [{**p, "pin": p["number"]} for p in fp["pads"]],
                    "pin_map": fp["pin_map"],
                }
            ],
            "components": [
                {
                    "refdes": refdes,
                    "footprint": "ASYM6",
                    "x": x,
                    "y": y,
                    "rot": rot,
                    "layer": layer,
                    "pins": [{"name": "CLK", "pad": "2"}],
                }
                for refdes, x, y, rot, layer in [
                    ("J_TOP", 10, 20, 0, "top"),
                    ("J_BOTTOM", 30, 40, 90, "bottom"),
                ]
            ]
            + [
                {"refdes": "J_UNPLACED", "footprint": "ASYM6"},
                {"refdes": "J_MISSING", "footprint": "NO_GEOMETRY"},
            ],
            "connections": [
                {"refdes": refdes, "pin": "CLK", "net": "STORED_CLK"}
                for refdes in ("J_TOP", "J_BOTTOM")
            ],
        },
    )
    return pcb


def _snapshot(store):
    with store.pool.connection() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND (tablename LIKE 'pcb_%' OR tablename IN "
                "('refs','chunks','ref_identifiers','ref_links','jobs'))"
            ).fetchall()
        ]
        return {
            table: sorted(
                json.dumps(r[0], sort_keys=True, default=str)
                for r in conn.execute(f'SELECT to_jsonb(t) FROM "{table}" t').fetchall()
            )
            for table in tables
        }


def test_handler_read_only_repeatable_and_legacy_compatibility(
    board, store, monkeypatch
):
    before = _snapshot(store)
    baselines = [
        board.get(id=id, **kw).body
        for id, kw in [
            ("preview-test", {}),
            ("preview-test#J_TOP", {}),
            ("preview-test@STORED_CLK", {}),
            ("preview-test#J_TOP", {"view": "pinout"}),
        ]
    ]

    def forbidden(*args, **kwargs):
        pytest.fail("preview attempted authoring, provider fetch or enqueue")

    monkeypatch.setattr("precis.handlers.pcb.fetch_footprint", forbidden)
    monkeypatch.setattr(board, "_queue_datasheets", forbidden)
    monkeypatch.setattr(store, "pcb_apply", forbidden)
    with store.pool.connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        with monkeypatch.context() as patch:
            patch.setattr(store.pool, "connection", lambda *a, **k: nullcontext(conn))
            args = {"pins": _proposals()}
            top = board.get(
                id="preview-test#J_TOP", view="pinout-preview", args=args
            ).body
            bottom = board.get(
                id="preview-test#J_BOTTOM", view="pinout-preview", args=args
            ).body
            assert (
                top
                == board.get(
                    id="preview-test#J_TOP", view="pinout-preview", args=args
                ).body
            )
            assert "8.0000\t21.0000" in top and "31.0000\t38.0000" in bottom
            assert "29.0000\t44.0000" in bottom and "B.Cu" in bottom
            assert "Unsaved read-only proposal" in top and "proposed-explicit" in top
            assert "proposed nets/NC semantics: unknown" in top and "STORED_CLK" in top
            assert "design-local-authored" in top
    after_reads = [
        board.get(id=id, **kw).body
        for id, kw in [
            ("preview-test", {}),
            ("preview-test#J_TOP", {}),
            ("preview-test@STORED_CLK", {}),
            ("preview-test#J_TOP", {"view": "pinout"}),
        ]
    ]
    assert after_reads == baselines and _snapshot(store) == before


def test_missing_geometry_keeps_explicit_proposals_and_unknowns(board):
    text = board.get(
        id="preview-test#J_MISSING",
        view="pinout-preview",
        args={"pins": [{"name": "NC", "pad": None}, {"name": "LOST", "pad": "99"}]},
    ).body
    assert "geometry: unavailable" in text and "NO_GEOMETRY" in text
    assert "unknown" in text and "unmatched" in text and "LOST" in text
    assert "author footprints[]" in text and "net assignments" in text


@pytest.mark.parametrize(
    "args",
    [
        None,
        {},
        {"pins": "prose"},
        {"pins": [{}]},
        {"pins": [{"name": "CLK"}]},
        {"pins": [{"name": "", "pad": "2"}]},
        {"pins": [{"name": "A" * 65, "pad": "2"}]},
        {"pins": [{"name": "CLK", "pad": 2}]},
        {"pins": [{"name": "CLK", "pad": ""}]},
        {"pins": [{"name": "CLK", "pad": "2" * 33}]},
        {"pins": [{"name": "CLK", "pad": "2", "net": "INVENTED"}]},
        {"pins": [], "mating": "top"},
        {"pins": [{"name": "CLK", "pad": "2"}] * 33},
    ],
)
def test_typed_input_bounds_and_no_prose_inference(board, args):
    with pytest.raises(BadInput) as error:
        board.get(id="preview-test#J_TOP", view="pinout-preview", args=args)
    assert "pinout-preview" in str(error.value.next)


@pytest.mark.parametrize(
    "id",
    [
        None,
        "",
        "/",
        "preview-test",
        "#J_TOP",
        "preview-test#",
        "preview-test@N",
        "preview-test#J#X",
    ],
)
def test_missing_selector_is_typed(board, id):
    with pytest.raises(BadInput) as error:
        board.get(id=id, view="pinout-preview", args={"pins": []})
    assert "pinout-preview" in str(error.value.next)


def test_unknown_refdes_empty_proposal_and_unplaced(board):
    with pytest.raises(NotFound):
        board.get(id="preview-test#ABSENT", view="pinout-preview", args={"pins": []})
    text = board.get(
        id="preview-test#J_UNPLACED", view="pinout-preview", args={"pins": []}
    ).body
    assert "not-proposed" in text and "board coordinates: unavailable" in text
    assert "No proposed assignments (pins=[])." in text
    assert "pinout-preview" in board.spec.views


def test_physical_row_and_rendered_context_caps_are_actionable(
    board, store, monkeypatch
):
    fp = _fp()
    fp["pads"] = [{**fp["pads"][1]} for _ in range(65)]
    monkeypatch.setattr(store, "pcb_local_footprints_for", lambda *a: {"ASYM6": fp})
    with pytest.raises(BadInput, match="64 physical pad rows") as error:
        board.get(id="preview-test#J_TOP", view="pinout-preview", args={"pins": []})
    assert "pinout'" in str(error.value.next)
    fp["pads"].pop()
    with pytest.raises(BadInput, match="16384 text characters") as error:
        board.get(
            id="preview-test#J_TOP", view="pinout-preview", args={"pins": _proposals()}
        )
    assert "reduce proposals" in str(error.value.next)


def test_catalog_cache_provenance_missing_cache_never_fetches(
    board, store, monkeypatch
):
    store.pcb_apply(
        slug="preview-test",
        title="preview",
        nets=[],
        connections=[],
        components=[
            {
                "refdes": "J_CACHED",
                "part": "C999999999",
                "footprint": "LOCAL_MUST_NOT_FALLBACK",
            }
        ],
    )

    def forbidden(*args, **kwargs):
        pytest.fail("preview fetched a provider or queued work")

    monkeypatch.setattr("precis.handlers.pcb.fetch_footprint", forbidden)
    monkeypatch.setattr(board, "_queue_datasheets", forbidden)
    missing = board.get(
        id="preview-test#J_CACHED", view="pinout-preview", args={"pins": _proposals()}
    ).body
    assert (
        "catalog-cache; source=unknown" in missing
        and "geometry: unavailable" in missing
    )
    assert "no automatic fetch" in missing
    store.part_footprint_put("C999999999", {**_fp(), "source": "fixture-stored-source"})
    text = board.get(
        id="preview-test#J_CACHED", view="pinout-preview", args={"pins": _proposals()}
    ).body
    assert "catalog-cache; source=fixture-stored-source" in text
    assert "proposed-explicit" in text and "physical pads: 6" in text
