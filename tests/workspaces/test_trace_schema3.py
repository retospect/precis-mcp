"""Trace-b: elapsed calls, output provenance and lossless fold detection."""

from dataclasses import dataclass
from typing import Any

import pytest

from precis_geom import trace, trace_schema
from precis_se.traceview import Trace, diff
from tests.workspaces._script import load_script


def _named(doc: dict, qual: str) -> dict:
    return next(f for f in doc["frames"] if f["qual"] == qual)


def test_nested_duration_freezes_at_return(tmp_path, monkeypatch) -> None:
    clock = [10.0]
    monkeypatch.setattr(trace.time, "perf_counter", lambda: clock[0])

    @trace.traced(name="inner")
    def inner():
        clock[0] += 0.25
        return 7

    @trace.traced(name="outer")
    def outer():
        inner()
        clock[0] += 0.5

    with trace.run(work_root=tmp_path) as tr:
        outer()
        clock[0] += 20
        doc = tr.dump()
    assert _named(doc, "inner")["dur_ms"] == pytest.approx(250)
    assert _named(doc, "outer")["dur_ms"] == pytest.approx(750)
    assert _named(doc, "inner")["ret"] == 7
    assert _named(doc, "outer")["ret"] is None
    assert tr.stack == [tr.root]


def test_exception_has_duration_without_return(tmp_path, monkeypatch) -> None:
    clock = [10.0]
    monkeypatch.setattr(trace.time, "perf_counter", lambda: clock[0])

    @trace.traced(name="fail")
    def fail():
        clock[0] += 0.125
        raise ValueError("expected")

    with trace.run(work_root=tmp_path) as tr:
        with pytest.raises(ValueError, match="expected"):
            fail()
    frame = _named(tr.dump(), "fail")
    assert frame["dur_ms"] == pytest.approx(125)
    assert frame["error"] == "ValueError"
    assert "ret" not in frame


def test_context_frame_duration(tmp_path, monkeypatch) -> None:
    clock = [10.0]
    monkeypatch.setattr(trace.time, "perf_counter", lambda: clock[0])
    with trace.run(work_root=tmp_path) as tr:
        with trace.trace("group"):
            clock[0] += 0.5
        clock[0] += 1
    frame = _named(tr.dump(), "group")
    assert frame["dur_ms"] == pytest.approx(500)
    assert "ret" not in frame


def test_entity_return_joins_consumer_argument(tmp_path) -> None:
    @dataclass
    class Entity:
        label: str
        maker: int | None

    @trace.traced(name="produce")
    def produce():
        return Entity("x" * 1000, trace.leaf_id())

    @trace.traced(name="consume")
    def consume(entity):
        return entity.label

    with trace.run(work_root=tmp_path) as tr:
        entity = produce()
        consume(entity)
    doc = tr.dump()
    producer = _named(doc, "produce")
    consumer = _named(doc, "consume")
    assert producer["id"] == entity.maker
    assert producer["ret"] == consumer["args"]["entity"]
    assert len(producer["ret"]) < 80
    assert consumer["ret"].startswith("<str 1000 ")


def test_cyclic_return_does_not_break_success(tmp_path) -> None:
    @trace.traced(name="cyclic")
    def cyclic():
        result: list[Any] = []
        result.append(result)
        return result

    with trace.run(work_root=tmp_path) as tr:
        result = cyclic()
    assert result[0] is result
    assert _named(tr.dump(), "cyclic")["ret"].startswith("<list 1 ")


def _folded_run(tmp_path, *, changed: bool = False) -> dict:
    @trace.traced(name="item")
    def item(i):
        return i * 2

    with trace.run(work_root=tmp_path) as tr:
        for i in range(trace_schema.COLLAPSE_SIBLINGS + 4):
            item(-i if changed and i == trace_schema.COLLAPSE_SIBLINGS + 1 else i)
    return tr.dump()


def test_fold_preserves_endpoints_and_middle_changes(tmp_path) -> None:
    old = _folded_run(tmp_path)
    new = _folded_run(tmp_path, changed=True)
    before = next(f for f in old["frames"] if f.get("collapsed"))
    after = next(f for f in new["frames"] if f.get("collapsed"))
    assert before["args"] == after["args"] == {"i": 64}
    assert before["args_last"] == after["args_last"] == {"i": 67}
    assert before["args_digest"] != after["args_digest"]
    assert before["returns"]["count"] == 4
    assert before["returns"]["first"] == 128
    assert before["returns"]["last"] == 134
    assert before["returns"]["digest"] != after["returns"]["digest"]
    rows = diff(Trace(old), Trace(new))
    changed = next(r for r in rows if r.frame["key"] == after["key"])
    assert changed.kind == "changed"
    assert "args_digest" in changed.note


def test_fold_duration_is_sum(tmp_path, monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(trace.time, "perf_counter", lambda: clock[0])

    @trace.traced(name="item")
    def item():
        clock[0] += 0.125

    with trace.run(work_root=tmp_path) as tr:
        for _ in range(trace_schema.COLLAPSE_SIBLINGS + 4):
            item()
    folded = next(f for f in tr.dump()["frames"] if f.get("collapsed"))
    assert folded["dur_ms"] == pytest.approx(500)
    assert folded["returns"]["count"] == 4


def test_semantic_diff_ignores_timing_but_compares_returns(tmp_path) -> None:
    result = [1]

    @trace.traced(name="compute")
    def compute():
        return result[0]

    docs = []
    for value in (1, 1, 2):
        result[0] = value
        with trace.run(work_root=tmp_path) as tr:
            compute()
        docs.append(tr.dump())
    _named(docs[1], "compute")["dur_ms"] += 1000
    assert all(r.kind == "unchanged" for r in diff(Trace(docs[0]), Trace(docs[1])))
    assert any(r.note == "ret" for r in diff(Trace(docs[1]), Trace(docs[2])))


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_reader_accepts_all_versions(tmp_path, schema) -> None:
    doc = _folded_run(tmp_path)
    doc["schema"] = schema
    if schema < 3:
        for frame in doc["frames"]:
            for key in ("dur_ms", "ret", "args_last", "args_digest", "returns"):
                frame.pop(key, None)
            if schema == 1:
                frame.pop("src")
                frame.pop("line_in_def")
    assert trace_schema.loads(trace_schema.dumps(doc)) == doc
    if schema == 2:
        doc["frames"][0].pop("src")
        with pytest.raises(ValueError, match="src"):
            trace_schema.loads(trace_schema.dumps(doc))


def test_traced_call_closes_sampled_helper_frames(tmp_path) -> None:
    script = load_script(
        tmp_path,
        "from precis_geom.trace import traced\n"
        "@traced(name='outer')\n"
        "def outer():\n"
        "    helper()\n"
        "def helper():\n"
        "    leaf()\n"
        "@traced(name='leaf')\n"
        "def leaf():\n"
        "    return 1\n",
    )
    with trace.run(work_root=tmp_path) as tr:
        script.outer()
        assert tr.stack == [tr.root]
    frames = tr.dump()["frames"]
    assert all(f["dur_ms"] >= 0 for f in frames)
    helper = next(f for f in frames if f["qual"].endswith(".helper"))
    assert "ret" not in helper
