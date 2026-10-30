"""Board writer comments retain supplied identities without inventing handles."""

from __future__ import annotations

from precis.pcb.board_script import board_batch, emit
from precis.provenance.refs import Annotation, find_annotations
from tests.test_pcb_board_script import _exec, _tiny_batch


def _call_annotation(text: str, opening: str) -> Annotation:
    lines = text.splitlines()
    matches = [
        annotation
        for annotation in find_annotations(text)
        if opening in lines[annotation.lineno - 1]
    ]
    assert len(matches) == 1, opening
    assert matches[0].trailing
    return matches[0]


def test_supplied_board_part_and_net_provenance_is_parseable() -> None:
    batch = _tiny_batch()
    text = emit(
        batch,
        "output-board",
        "source.json",
        annotations={
            "board": "pb123",
            "board_slug": "source-board",
            "parts": {"R1": "pn456 (chosen resistor)"},
            "instance_ids": {"R1": 77},
            "net_ids": {"GND": 88},
        },
    )
    part = _call_annotation(text, "r1 = b.part(")
    assert set(part.handles) == {"pb123", "pn456"}
    assert "board='source-board'" in part.gloss
    assert "instance_id=77" in part.gloss
    assert "chosen resistor" in part.gloss
    net = _call_annotation(text, "gnd = b.net(")
    assert net.handles == ("pb123",)
    assert "board='source-board'" in net.gloss
    assert "net_id=88" in net.gloss
    assert "board='output-board'" not in part.gloss
    assert _exec(text).build().to_batch() == batch


def test_long_call_provenance_stays_on_opening_line() -> None:
    batch = _tiny_batch()
    batch["components"][1]["note"] = "Long part detail " * 20
    batch["nets"][0]["note"] = "Long net detail " * 20
    text = emit(
        batch,
        "output-board",
        "source.json",
        annotations={
            "board": "pb123",
            "board_slug": "source-board",
            "parts": {"R1": "pn456"},
            "instance_ids": {"R1": 77},
            "net_ids": {"GND": 88},
        },
    )
    for opening in ("r1 = b.part(", "gnd = b.net("):
        annotation = _call_annotation(text, opening)
        line = text.splitlines()[annotation.lineno - 1]
        assert line.split("#", 1)[0].rstrip().endswith("(")
    assert _exec(text).build().to_batch() == batch


def test_missing_identities_are_not_fabricated() -> None:
    text = emit(_tiny_batch(), "local-board", "offline.json")
    assert not find_annotations(text)
    assert "instance_id=" not in text
    assert "net_id=" not in text
    assert "board='local-board'" in text
    assert "lcsc:C25804" in text


def test_legacy_part_and_footprint_annotations_remain_supported() -> None:
    text = emit(
        _tiny_batch(),
        "local-board",
        "offline.json",
        annotations={
            "parts": {"R1": "pn456 resistor choice"},
            "footprints": {"HDR-2.54-1x2": "fp42 source geometry"},
        },
    )
    assert _call_annotation(text, "r1 = b.part(").handles == ("pn456",)
    assert any(annotation.handles == ("fp42",) for annotation in find_annotations(text))
    assert "instance_id=" not in text
    assert "net_id=" not in text


def test_annotation_and_source_newlines_cannot_inject_code() -> None:
    batch = _tiny_batch()
    text = emit(
        batch,
        "output-board",
        'source.json"""\nraise RuntimeError("source injection")\n#',
        annotations={
            "board": "pb123",
            "board_slug": "source-board\nraise RuntimeError('slug injection')",
            "parts": {"R1": "pn456 choice\nraise RuntimeError('part injection')"},
            "footprints": {
                "HDR-2.54-1x2": "fp42 geometry\nraise RuntimeError('footprint injection')"
            },
        },
    )
    assert _exec(text).build().to_batch() == batch
    part = _call_annotation(text, "r1 = b.part(")
    assert set(part.handles) == {"pb123", "pn456"}
    assert "choice" in part.gloss


def test_readback_annotations_use_existing_store_identities(store) -> None:
    batch = _tiny_batch()
    slug = "board-provenance"
    store.pcb_apply(
        slug=slug,
        title="Provenance fixture",
        components=batch["components"],
        nets=batch["nets"],
        connections=batch["connections"],
        footprints=batch["footprints"],
    )
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    load = store.pcb_load(int(ref.id))
    _batch, annotations = board_batch(store, slug)
    assert annotations["board"] == f"pb{ref.id}"
    assert annotations["board_slug"] == slug
    assert annotations["instance_ids"] == {
        instance["refdes"]: instance["instance_id"] for instance in load["instances"]
    }
    assert annotations["net_ids"] == {
        net["name"]: net["net_id"] for net in load["nets"]
    }
    part_id = store.part_ref_id("C25804")
    if part_id is None:
        assert "R1" not in annotations["parts"]
    else:
        assert annotations["parts"]["R1"] == f"pn{part_id}"
