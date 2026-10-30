"""gr521596 slice 2 — ``view='frames'``: a board's construction-trace
asks against the run's stored ``ws_trace`` doc and the entity
``meta.frame`` bindings the batch carries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis_geom import trace, trace_schema
from precis_geom.pcb import Board
from tests.workspaces._script import load_script


def _traced_batch(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], Board]:
    """A real traced run → ``(trace doc, batch, board)`` — the batch's
    maker stamps land on the rows as ``frame`` keys."""
    with trace.run(work_root=tmp_path) as tr:
        board = Board("frames-board")
        u1 = board.part("U1", pins=["1"])
        u2 = board.part("U2", pins=["1"])
        u1["1"].etype("bidi", fmax_hz=0.0, assume="fixture pin")
        u2["1"].etype("bidi", fmax_hz=0.0, assume="fixture pin")
        vcc = board.wire(u1["1"], u2["1"], name="VCC")
        board.hole(1, 1, drill=2.0, ring=3.0)
        doc = tr.dump()
    batch = board.to_batch()
    assert batch["components"][0]["frame"] == u1.maker
    # U2 deliberately unbound — the "no maker" answer.
    batch["components"][1].pop("frame")
    assert batch["nets"][0]["frame"] == vcc.maker
    return doc, batch, board


def _put_traced(store: Store, tmp_path: Path) -> tuple[PcbHandler, dict[str, Any]]:
    doc, batch, _board = _traced_batch(tmp_path)
    job = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    h = PcbHandler(hub=Hub(store=store))
    h.put(
        id="tb",
        args={**batch, "trace": trace_schema.dumps(doc), "run_job": int(job.id)},
    )
    return h, doc


def test_summary_lists_run_and_bound_counts(store: Store, tmp_path: Path) -> None:
    h, doc = _put_traced(store, tmp_path)
    body = h.get(id="tb", view="frames").body
    assert f"{len(doc['frames'])} frames" in body
    assert "bound entities: 1 parts, 1 nets" in body
    assert "args={'refdes'" in body


def test_refdes_answers_its_maker_chain(store: Store, tmp_path: Path) -> None:
    h, _doc = _put_traced(store, tmp_path)
    body = h.get(id="tb", view="frames", args={"refdes": "U1"}).body
    assert "pcb.Board.part" in body
    assert "made 1 parts" in body


def test_net_answers_its_maker_chain(store: Store, tmp_path: Path) -> None:
    h, _doc = _put_traced(store, tmp_path)
    body = h.get(id="tb", view="frames", args={"net": "VCC"}).body
    assert "pcb.Board.net" in body
    assert "made 1 nets" in body


def test_frame_card_names_bound_entities(store: Store, tmp_path: Path) -> None:
    h, doc = _put_traced(store, tmp_path)
    part_frame = next(f for f in doc["frames"] if f.get("made") == {"parts": 1})
    body = h.get(id="tb", view="frames", args={"frame": part_frame["key"]}).body
    assert "selector: frame:" in body
    assert "U1" in body


def test_frame_ask_accepts_a_maker_id(store: Store, tmp_path: Path) -> None:
    h, doc = _put_traced(store, tmp_path)
    part_frame = next(f for f in doc["frames"] if f.get("made") == {"parts": 1})
    body = h.get(id="tb", view="frames", args={"frame": int(part_frame["id"])}).body
    assert "selector: frame:" in body


def test_unbound_entity_says_no_maker(store: Store, tmp_path: Path) -> None:
    h, _doc = _put_traced(store, tmp_path)
    body = h.get(id="tb", view="frames", args={"refdes": "U2"}).body
    assert "no maker" in body


def test_unknown_refdes_is_not_found(store: Store, tmp_path: Path) -> None:
    h, _doc = _put_traced(store, tmp_path)
    with pytest.raises(NotFound):
        h.get(id="tb", view="frames", args={"refdes": "U99"})


def test_unknown_frame_refuses_with_top_keys(store: Store, tmp_path: Path) -> None:
    h, _doc = _put_traced(store, tmp_path)
    with pytest.raises(BadInput, match="not in this board's trace") as exc:
        h.get(id="tb", view="frames", args={"frame": "deadbeefdeadbeef"})
    assert exc.value.options


def test_no_binding_is_a_clean_answer(store: Store) -> None:
    h = PcbHandler(hub=Hub(store=store))
    h.put(
        id="plain",
        args={
            "components": [{"refdes": "R1", "pins": [{"name": "1"}]}],
            "nets": [{"name": "VCC"}],
            "connections": [{"net": "VCC", "refdes": "R1", "pin": "1"}],
        },
    )
    body = h.get(id="plain", view="frames").body
    assert "no construction trace bound to plain" in body
    with pytest.raises(BadInput, match="no construction trace"):
        h.get(id="plain", view="frames", args={"refdes": "R1"})


_E2E_SCRIPT = """\
import precis_geom.pcb as pcb


def build():
    b = pcb.Board("e2e")
    u1 = b.part("U1", pins=["1"])
    u2 = b.part("U2", pins=["1"])
    u1["1"].etype("bidi", fmax_hz=0.0, assume="fixture pin")
    u2["1"].etype("bidi", fmax_hz=0.0, assume="fixture pin")
    b.wire(u1["1"], u2["1"], name="VCC")
    return b
"""


def test_put_trace_binds_the_run_end_to_end(store: Store, tmp_path: Path) -> None:
    """gr521596 slice 3 — the producer: a board script run's trace lands
    via ``args['trace']`` (no run_job → the board's own chunks) and the
    rows' ``frame`` stamps the traced ``to_batch`` emits resolve to maker
    frames with the script line."""
    mod = load_script(tmp_path, _E2E_SCRIPT)
    with trace.run(work_root=tmp_path) as tr:
        board = mod.build()
        doc = tr.dump()
    h = PcbHandler(hub=Hub(store=store))
    h.put(id="e2e", args={**board.to_batch(), "trace": trace_schema.dumps(doc)})
    ref = store.get_ref(kind="pcb", id="e2e")
    assert ref is not None
    gen = (ref.meta or {}).get("generated") or {}
    assert gen.get("trace", {}).get("run_job") is None
    assert gen.get("trace", {}).get("root")
    body = h.get(id="e2e", view="frames", args={"refdes": "U1"}).body
    assert "main.py:" in body
    assert "pcb.Board.part" in body
    assert "made 1 parts" in body


def test_card_subtree_sums_made_per_frame() -> None:
    """sub_made must SUM each subtree frame's made — the dict merge that
    keyed on entity type collapsed three children's parts to one."""
    from precis.pcb import frames as pcb_frames

    doc: dict[str, Any] = {
        "schema": 3,
        "frames": [
            {"id": 0, "key": "r", "qual": "run", "kind": "root", "parent": None},
            {"id": 1, "key": "a", "qual": "main.build", "kind": "user", "parent": "r"},
            *[
                {
                    "id": 2 + i,
                    "key": f"c{i}",
                    "qual": "pcb.Board.part",
                    "kind": "core",
                    "parent": "a",
                    "made": {"parts": 1},
                }
                for i in range(3)
            ],
        ],
    }
    body = pcb_frames.card(pcb_frames.Trace(doc), "a")
    assert "subtree 3" in body


def test_put_trace_refuses_a_payload_over_2mb(store: Store) -> None:
    h = PcbHandler(hub=Hub(store=store))
    batch = {
        "components": [{"refdes": "R1", "pins": [{"name": "1"}]}],
        "nets": [{"name": "VCC"}],
        "connections": [],
    }
    with pytest.raises(BadInput, match="byte") as exc:
        h.put(id="big", args={**batch, "trace": " " * 2_000_001})
    assert exc.value.next and "omit" in str(exc.value.next)
    assert store.get_ref(kind="pcb", id="big") is None


def test_put_trace_clips_long_frame_text_fields(store: Store, tmp_path: Path) -> None:
    from precis.pcb import frames as pcb_frames

    doc, batch, _board = _traced_batch(tmp_path)
    fr = doc["frames"][1]
    fr["qual"] = "q" * 600
    fr["file"] = "f" * 600
    fr["src"] = "s" * 600
    fr["custom_note"] = "c" * 600
    h = PcbHandler(hub=Hub(store=store))
    h.put(id="clip", args={**batch, "trace": trace_schema.dumps(doc)})
    ref = store.get_ref(kind="pcb", id="clip")
    assert ref is not None
    stored = pcb_frames.read_ws_trace(store, int(ref.id), None)
    assert stored is not None
    got = stored["frames"][1]
    for field in ("qual", "file", "src", "custom_note"):
        assert len(got[field]) == 500
        assert got[field].endswith("…")
    assert got["key"] == fr["key"]


def _ws_trace_texts(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'ws_trace' ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [str(text) for (text,) in rows]


def test_identical_trace_reput_stores_one_chunk(store: Store, tmp_path: Path) -> None:
    doc, batch, _board = _traced_batch(tmp_path)
    h = PcbHandler(hub=Hub(store=store))
    args = {**batch, "trace": trace_schema.dumps(doc)}
    h.put(id="dedup", args=args)
    h.put(id="dedup", args=args)
    ref = store.get_ref(kind="pcb", id="dedup")
    assert ref is not None
    assert len(_ws_trace_texts(store, int(ref.id))) == 1
    changed = {**doc, "run": {**(doc.get("run") or {}), "truncated": True}}
    h.put(id="dedup", args={**batch, "trace": trace_schema.dumps(changed)})
    assert len(_ws_trace_texts(store, int(ref.id))) == 2


@pytest.mark.parametrize("bad_key", [["k1"], {"k": 1}])
def test_put_trace_rejects_a_non_string_frame_key(store: Store, bad_key: Any) -> None:
    """An unhashable ``key`` surfaced as a raw TypeError out of
    ``loads`` — now a shape refusal, BadInput through ``put``."""
    frame = {
        "id": 0,
        "key": bad_key,
        "qual": "run",
        "file": "f",
        "line": 1,
        "occ": 0,
        "kind": "root",
        "line_in_def": 0,
        "src": None,
        "parent": None,
    }
    text = trace_schema.dumps({"schema": 3, "frames": [frame]})
    with pytest.raises(ValueError, match="key"):
        trace_schema.loads(text)
    h = PcbHandler(hub=Hub(store=store))
    batch = {
        "components": [{"refdes": "R1", "pins": [{"name": "1"}]}],
        "nets": [{"name": "VCC"}],
        "connections": [],
    }
    with pytest.raises(BadInput, match="key"):
        h.put(id="badkey", args={**batch, "trace": text})
    assert store.get_ref(kind="pcb", id="badkey") is None


def test_put_trace_refusals(store: Store, tmp_path: Path) -> None:
    h = PcbHandler(hub=Hub(store=store))
    batch = {
        "components": [{"refdes": "R1", "pins": [{"name": "1"}]}],
        "nets": [{"name": "VCC"}],
        "connections": [],
    }
    with pytest.raises(BadInput, match="schema"):
        h.put(id="bad", args={**batch, "trace": '{"schema": 9}'})
    assert store.get_ref(kind="pcb", id="bad") is None
    with pytest.raises(BadInput, match="without args\\['trace'\\]"):
        h.put(id="bad", args={**batch, "run_job": 1})
    with pytest.raises(BadInput, match="not a job ref"):
        h.put(
            id="bad",
            args={
                **batch,
                "trace": '{"schema": 3, "frames": []}',
                "run_job": 424242,
            },
        )
    with pytest.raises(BadInput, match="takes no 'trace'"):
        h.put(id="bad", args={"op": "check", "trace": "{}"})
