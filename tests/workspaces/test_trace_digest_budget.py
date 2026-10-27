"""Return capture has a traversal/byte cap, including large geometry buffers."""

from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np
import pytest

from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_geom import trace
from precis_geom.assembly import Geom


def _capture(value: Any, tmp_path) -> dict:
    @trace.traced(name="produce")
    def produce():
        return value

    @trace.traced(name="consume")
    def consume(obj):
        return None

    with trace.run(work_root=tmp_path) as tracer:
        consume(produce())
    return {f["qual"]: f for f in tracer.dump()["frames"]}


def test_large_geom_return_caps_traversal_and_hash_input(tmp_path, monkeypatch) -> None:
    class CountedAtom(Atom):
        reads: ClassVar[int] = 0

        def __getattribute__(self, name: str) -> Any:
            if name == "frac":
                CountedAtom.reads += 1
            return super().__getattribute__(name)

    atom = CountedAtom("aC1", "C", np.zeros(3))
    scene = Scene(
        cell=Cell(np.eye(3), (False, False, False)),
        atoms={str(i): atom for i in range(10_000)},
        bonds=[],
    )
    geom = Geom(scene=scene)
    hash_sizes: list[int] = []
    original = trace.ts.sha8

    def measured(text: str) -> str:
        hash_sizes.append(len(text.encode("utf-8")))
        return original(text)

    monkeypatch.setattr(trace.ts, "sha8", measured)
    frames = _capture(geom, tmp_path)
    assert 0 < CountedAtom.reads <= 2 * trace._DIGEST_MAX_ITEMS
    assert max(hash_sizes) <= trace._DIGEST_MAX_BYTES
    assert frames["produce"]["ret"].endswith(" capped>")
    assert frames["produce"]["ret"] == frames["consume"]["args"]["obj"]


@pytest.mark.parametrize("strided", [False, True])
def test_large_array_does_not_materialize_full_buffer(
    strided, tmp_path, monkeypatch
) -> None:
    class Array(np.ndarray):
        copies: ClassVar[int] = 0

        def tobytes(self, *args, **kwargs):
            Array.copies += 1
            raise AssertionError("must not copy the whole array")

    values: np.ndarray[Any, np.dtype[np.float64]] = np.arange(
        100_000, dtype=np.float64
    ).view(Array)
    if strided:
        values = values[::2]
    original = trace.ts.sha8
    hash_sizes: list[int] = []

    def measured(text: str) -> str:
        hash_sizes.append(len(text.encode("utf-8")))
        return original(text)

    monkeypatch.setattr(trace.ts, "sha8", measured)
    frames = _capture(values, tmp_path)
    assert frames["produce"]["ret"].endswith(" capped>")
    assert frames["produce"]["ret"] == frames["consume"]["args"]["obj"]
    assert max(hash_sizes) <= trace._DIGEST_MAX_BYTES
    assert Array.copies == 0
    if not strided:
        values[0] += 1
        assert _capture(values, tmp_path)["produce"]["ret"] != frames["produce"]["ret"]


@pytest.mark.parametrize("owner", ["geom", "scene"])
def test_existing_content_digest_skips_geometry_walk(owner, tmp_path) -> None:
    class UnreadableAtom(Atom):
        reads: ClassVar[int] = 0

        def __getattribute__(self, name: str) -> Any:
            if name == "frac":
                UnreadableAtom.reads += 1
                raise AssertionError("cached content digest must avoid walking atoms")
            return super().__getattribute__(name)

    scene = Scene(
        cell=Cell(np.eye(3), (False, False, False)),
        atoms={"aC1": UnreadableAtom("aC1", "C", np.zeros(3))},
        bonds=[],
    )
    value = Geom(scene=scene) if owner == "geom" else scene
    # These types have no built-in cached SHA today; an already materialised
    # digest supplied by a producer can be reused without invoking a property.
    value.__dict__["content_sha"] = "a" * 64
    frames = _capture(value, tmp_path)
    assert UnreadableAtom.reads == 0
    assert not frames["produce"]["ret"].endswith(" capped>")
    assert frames["produce"]["ret"] == frames["consume"]["args"]["obj"]
    first = frames["produce"]["ret"]
    value.__dict__["content_sha"] = "b" * 64
    assert _capture(value, tmp_path)["produce"]["ret"] != first


def test_digest_does_not_call_computed_hash_property(tmp_path) -> None:
    @dataclass
    class Result:
        value: int = 1
        reads: ClassVar[int] = 0

        @property
        def sha(self):
            Result.reads += 1
            raise AssertionError("expensive digest computation")

    frames = _capture(Result(), tmp_path)
    assert Result.reads == 0
    assert not frames["produce"]["ret"].endswith(" capped>")


@pytest.mark.parametrize("value", ["x" * 100_000, b"x" * 100_000, {"x": "é" * 100_000}])
def test_large_strings_and_bytes_are_capped(value, tmp_path, monkeypatch) -> None:
    original = trace.ts.sha8
    hash_sizes: list[int] = []

    def measured(text: str) -> str:
        hash_sizes.append(len(text.encode("utf-8")))
        return original(text)

    monkeypatch.setattr(trace.ts, "sha8", measured)
    frames = _capture(value, tmp_path)
    assert max(hash_sizes) <= trace._DIGEST_MAX_BYTES
    assert frames["produce"]["ret"].endswith(" capped>")
    assert frames["produce"]["ret"] == frames["consume"]["args"]["obj"]


def test_unknown_result_never_calls_unbounded_repr(tmp_path) -> None:
    class Result:
        reads = 0

        def __repr__(self):
            Result.reads += 1
            raise AssertionError("unbounded repr")

    frames = _capture(Result(), tmp_path)
    assert Result.reads == 0
    assert frames["produce"]["ret"].endswith(" capped>")


def test_tuple_atom_keys_keep_their_contents(tmp_path) -> None:
    first = _capture({(1, 2, "A"): "x", (3, 4, "B"): "y"}, tmp_path)
    changed = _capture({(1, 2, "A"): "x", (3, 5, "B"): "y"}, tmp_path)
    assert not first["produce"]["ret"].endswith(" capped>")
    assert first["produce"]["ret"] != changed["produce"]["ret"]
    assert first["produce"]["ret"] == first["consume"]["args"]["obj"]


def test_deep_result_stops_at_depth_budget(tmp_path) -> None:
    value: list[Any] = [1]
    for _ in range(trace._DIGEST_MAX_DEPTH * 4):
        value = [value]
    frames = _capture(value, tmp_path)
    assert frames["produce"]["ret"].endswith(" capped>")
    assert frames["produce"]["ret"] == frames["consume"]["args"]["obj"]
