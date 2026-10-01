"""pcb-epro-import slice 1b — the netlist/placement half of the pure reader.

Where :mod:`tests.test_pcb_epro_reader` pins the container and the copper,
this pins what :func:`precis.pcb.epro.build_design` hands to
``Store.pcb_apply``: components with a side and a rotation, footprints with
real pads and a pin map, and connections that resolve.

**The load-bearing test is** ``test_a_bottom_component_takes_the_half_turn``.
precis mirrors a bottom instance's pads in X and EasyEDA mirrors them in Y;
those two reflections differ by exactly a half turn, so the importer adds
180° to a bottom-side instance's rotation. Get it wrong and every
bottom-side pad lands diametrically opposite where it belongs — a board
that renders correctly in every view and cannot be built. The test checks
it against ``padplace``'s own transform rather than a hand-copied matrix,
because a hand copy can agree with itself and still disagree with the
router.
"""

from __future__ import annotations

import io
import math
import pathlib
import zipfile

import pytest

from precis.pcb import epro, padplace

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "pcb_epro_tiny"


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in ("project2.json", "board.epru"):
            zf.writestr(name, (_FIXTURE / name).read_bytes())
    return buf.getvalue()


@pytest.fixture
def project():
    return epro.read_archive(_zip())


@pytest.fixture
def design(project):
    return epro.build_design(project)[0]


def _by_refdes(design, refdes):
    return next(c for c in design.components if c["refdes"] == refdes)


def _by_name(design, name):
    return next(f for f in design.footprints if f["name"] == name)


# ── components ───────────────────────────────────────────────────────────
def test_components_carry_refdes_pose_side_and_footprint(design) -> None:
    assert design.stats["components"] == 2
    r1 = _by_refdes(design, "R1")
    # 300, 150 mil in a 1000x500 mil board -> 7.62, 8.89 mm with Y flipped.
    assert (r1["x"], r1["y"]) == (7.62, 8.89)
    assert r1["layer"] == "top"
    assert r1["rot"] == 90.0
    assert r1["footprint"] == "R0603"


def test_the_refdes_comes_from_an_attr_not_the_component_body(project) -> None:
    """A ``COMPONENT`` body carries pose and side and NOTHING that names
    it; the designator is a separate ``ATTR`` pointing back by
    ``parentId``. A reader that looked only at COMPONENT would import a
    board of anonymous parts."""
    pcb = project.by_type("PCB")[0]
    body = next(r.body for r in pcb.of_type("COMPONENT") if r.id == "c1")
    assert "Designator" not in body and "refdes" not in body
    assert epro.component_attrs(pcb)["c1"]["Designator"] == "R1"


def test_a_bottom_component_takes_the_half_turn(project, design) -> None:
    """THE handedness test. ``padplace`` mirrors a bottom instance in X,
    EasyEDA mirrors it in Y, and the two differ by 180° — so the import
    adds a half turn and the pads still land where the source file put
    them. Checked against ``padplace.place_pad_point`` itself.
    """
    pcb = project.by_type("PCB")[0]
    comp = next(r.body for r in pcb.of_type("COMPONENT") if r.id == "c2")
    assert comp["layerId"] == 2, "c2 must be the bottom-side component"
    assert comp["angle"] == 90

    u1 = _by_refdes(design, "U1")
    assert u1["layer"] == "bottom"
    assert u1["rot"] == 270.0, "a bottom instance is angle + 180"

    fp = _by_name(design, "SOT-23-3_L2.9-W1.3-P0.95")
    frame = epro.build_design(project)[1]

    # Ground truth: the spike-verified source-frame rule, mapped through
    # the board frame. Pad 3 is the one that can tell the axes apart --
    # it is the only pad off the footprint's own centre line in Y.
    for pad in fp["pads"]:
        src = next(
            p
            for p in project.by_type("FOOTPRINT")[1].bodies("PAD")
            if str(p["num"]) == pad["pin"]
        )
        px, py = float(src["centerX"]), -float(src["centerY"])  # bottom: mirror in Y
        a = math.radians(comp["angle"])
        want = frame.xy(
            comp["x"] + px * math.cos(a) - py * math.sin(a),
            comp["y"] + px * math.sin(a) + py * math.cos(a),
        )
        got = padplace.place_pad_point(pad, u1)
        assert got == pytest.approx(want, abs=1e-6), f"pad {pad['pin']}"


def test_dropping_the_half_turn_moves_every_bottom_pad(project, design) -> None:
    """The half turn is not cosmetic: without it a bottom pad lands
    diametrically opposite its true position about the component origin.
    Asserted so nobody 'simplifies' the +180 away."""
    u1 = _by_refdes(design, "U1")
    naive = u1 | {"rot": (u1["rot"] - 180.0) % 360.0}
    fp = _by_name(design, "SOT-23-3_L2.9-W1.3-P0.95")
    pad = next(p for p in fp["pads"] if p["pin"] == "3")

    right = padplace.place_pad_point(pad, u1)
    wrong = padplace.place_pad_point(pad, naive)
    assert right != pytest.approx(wrong, abs=1e-6)
    # ...and specifically, it is a point reflection through the origin.
    assert (right[0] + wrong[0]) / 2 == pytest.approx(u1["x"], abs=1e-6)
    assert (right[1] + wrong[1]) / 2 == pytest.approx(u1["y"], abs=1e-6)


def _reread(**replacements: str):
    """Re-read the fixture with literal substitutions applied to the
    stream — how a case the fixture does not carry (a locked part, a rule
    area) gets exercised without a second fixture directory."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    for old, new in replacements.items():
        assert old in stream, f"fixture no longer contains {old!r}"
        stream = stream.replace(old, new)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("project2.json", (_FIXTURE / "project2.json").read_bytes())
        zf.writestr("board.epru", stream.encode("utf-8"))
    return epro.build_design(epro.read_archive(buf.getvalue()))[0]


def test_a_locked_component_imports_frozen() -> None:
    """``COMPONENT.locked`` is the author saying "do not move this" —
    exactly what the workflow's "freeze the mechanicals" step wants, so it
    carries through as ``fixed`` rather than being dropped."""
    design = _reread(
        **{
            '"attrs":{"Unique ID":"gge1"},"locked":false': (
                '"attrs":{"Unique ID":"gge1"},"locked":true'
            )
        }
    )
    assert _by_refdes(design, "R1")["fixed"] == "both"
    assert "fixed" not in _by_refdes(design, "U1")


def test_no_component_claims_an_lcsc_part(design) -> None:
    """``pcb_components.footprint`` only joins ``pcb_local_footprints``
    when ``part_lcsc IS NULL``, so setting a part number would orphan
    every pad the reader just recovered."""
    assert not any("part_lcsc" in c or "part" in c for c in design.components)


# ── footprints + pin names ───────────────────────────────────────────────
def test_only_placed_footprints_are_emitted(project, design) -> None:
    """The project may carry a library; the board places a subset. An
    unreferenced footprint in ``pcb_local_footprints`` is noise a reader
    of the imported board would have to explain away."""
    assert len(project.by_type("FOOTPRINT")) == 2
    assert design.stats["footprints"] == 2
    assert {f["name"] for f in design.footprints} == {
        "R0603",
        "SOT-23-3_L2.9-W1.3-P0.95",
    }


def test_footprints_are_named_by_title_not_uuid(design) -> None:
    """The uuid is what a COMPONENT's attribute names, but it is
    meaningless in ``view='bom'``."""
    assert all(not f["name"].startswith("epro:") for f in design.footprints)


def test_pad_shapes_lower_to_the_four_precis_shapes(design) -> None:
    fp = _by_name(design, "SOT-23-3_L2.9-W1.3-P0.95")
    shapes = {p["pin"]: p["shape"] for p in fp["pads"]}
    assert shapes == {"1": "rect", "2": "circle", "3": "obround"}
    # A through-hole pad keeps its drill; the SMD ones have none.
    drills = {p["pin"]: p.get("drill") for p in fp["pads"]}
    assert drills["2"] == pytest.approx(25 * 0.0254, abs=1e-9)
    assert drills["1"] is None and drills["3"] is None


def test_pad_local_y_is_flipped_like_the_board(design) -> None:
    """The pad frame's Y negation is the SAME reflection the board frame
    applies. Two different conventions inside one importer is how a
    bottom-side part ends up plausible and wrong."""
    fp = _by_name(design, "SOT-23-3_L2.9-W1.3-P0.95")
    pad3 = next(p for p in fp["pads"] if p["pin"] == "3")
    # source centerY = +35 mil (down) -> -0.889 mm (up)
    assert pad3["y"] == pytest.approx(-35 * 0.0254, abs=1e-9)


def test_a_pad_on_the_centre_line_is_positive_zero(design) -> None:
    """``-0.0`` compares equal to ``0.0`` but serialises differently, so
    it would break a round-trip byte comparison on a pad that is exactly
    centred. The fixture's R0603 pads 1 and 2 sit at centerY 0, which the
    Y negation turns into ``-0.0`` unless it is collapsed."""
    centred = [p for p in _by_name(design, "R0603")["pads"] if p["y"] == 0.0]
    assert len(centred) == 2, "the fixture must keep a pad on the centre line"
    for pad in centred:
        assert math.copysign(1.0, pad["y"]) == 1.0, f"pad {pad['pin']} is -0.0"


def test_semantic_pin_names_come_through_the_device_symbol_chain(design) -> None:
    """``COMPONENT`` -> ``ATTR[Device]`` -> ``DEVICE.META.attributes
    ["Symbol"]`` -> the ``SYMBOL``'s paired ``Pin Number``/``Pin Name``
    attributes. Four documents deep, and it is the only place a pad
    number becomes a name a netlist can address."""
    fp = _by_name(design, "SOT-23-3_L2.9-W1.3-P0.95")
    assert fp["pin_map"]["1"] == "VDD"
    u1 = _by_refdes(design, "U1")
    assert {p["name"] for p in u1["pins"]} == {"VDD", "GND", "GND_3"}
    # The pad number is preserved alongside the name -- the export half
    # has to write it back out.
    assert {p["pad"] for p in u1["pins"]} == {"1", "2", "3"}


def test_two_pins_named_the_same_are_broken_apart(design) -> None:
    """Six ``GND`` pins on one part is ordinary, and ``pcb_pins`` is keyed
    ``(component_id, name)`` — so without a rename the second pin and its
    connections vanish silently."""
    fp = _by_name(design, "SOT-23-3_L2.9-W1.3-P0.95")
    assert fp["pin_map"]["2"] == "GND"
    assert fp["pin_map"]["3"] == "GND_3"
    assert any("both named 'GND'" in w for w in design.warnings)


def test_a_pad_the_symbol_does_not_name_keeps_its_number(design) -> None:
    """Degradation is per-pad, not all-or-nothing: the fixture's R0603
    has three pads and its symbol declares two pins."""
    fp = _by_name(design, "R0603")
    assert fp["pin_map"] == {"1": "1", "2": "2", "3": "3"}


def test_pads_sharing_a_number_are_one_pin(design) -> None:
    """A split thermal pad is several PADs with one number, and they are
    one electrical pin. Renaming them apart would invent a pin the board
    does not have — and would collide anyway, since the suffix is the
    same shared number."""
    assert epro.pad_numbers(
        [{"pin": "1"}, {"pin": "1"}, {"pin": "2"}, {"pin": "1"}]
    ) == ["1", "2"]
    resolved, warnings = epro._dedup_pin_names(["1", "2"], {"1": "EP", "2": "EP"}, "x")
    assert resolved == {"1": "EP", "2": "EP_2"}
    assert len(warnings) == 1


# ── nets + connections ───────────────────────────────────────────────────
def test_connections_resolve_to_real_components_pins_and_nets(design) -> None:
    """Referential integrity is the property that makes the import
    trustworthy: ``pcb_apply`` would happily create a pin row for a
    typo'd name, so a connection naming something absent becomes a
    dangling net nobody can route."""
    refdes = {c["refdes"] for c in design.components}
    pins = {
        (c["refdes"], p["name"]) for c in design.components for p in c.get("pins", [])
    }
    nets = {n["name"] for n in design.nets}
    for conn in design.connections:
        assert conn["refdes"] in refdes
        assert (conn["refdes"], conn["pin"]) in pins
        assert conn["net"] in nets


def test_a_connection_names_the_pin_name_not_the_pad_number(design) -> None:
    """The netlist addresses pins. ``U1`` pad 1 is ``VDD``, so its
    connection says ``VDD`` — a connection carrying ``"1"`` would create
    a second pin beside the named one."""
    u1_conns = {c["pin"]: c["net"] for c in design.connections if c["refdes"] == "U1"}
    assert u1_conns == {"VDD": "SIG", "GND": "GND"}


def test_nets_are_name_only(design) -> None:
    """Inventing a ``net_class`` at import would fabricate the very
    constraint the annotation step exists to set deliberately."""
    assert all(set(n) == {"name"} for n in design.nets)


def test_no_duplicate_connections(design) -> None:
    keys = [(c["net"], c["refdes"], c["pin"]) for c in design.connections]
    assert len(keys) == len(set(keys))


# ── features ─────────────────────────────────────────────────────────────
def test_the_outline_is_the_first_feature_and_is_real(design) -> None:
    outline = design.features[0]
    assert outline["ftype"] == "outline"
    path = outline["geom"]["path"]
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    assert max(xs) - min(xs) == pytest.approx(1000 * 0.0254, abs=1e-6)
    assert max(ys) - min(ys) == pytest.approx(500 * 0.0254, abs=1e-6)


def test_a_non_plated_free_pad_becomes_a_mounting_hole(design) -> None:
    holes = [f for f in design.features if f["ftype"] == "mounting_hole"]
    assert len(holes) == 1
    assert holes[0]["geom"]["dia_mm"] == pytest.approx(6.0, abs=1e-5)
    # No 'fixed': the column is inert (nothing reads pcb_features.fixed),
    # so writing one would claim a freeze that does not exist.
    assert "fixed" not in holes[0]


def test_a_plated_free_pad_is_reported_not_silently_a_hole(design) -> None:
    """A plated free pad is copper on a real net. Turning it into a
    mounting hole would drop the net; dropping it silently would lose a
    connection nobody knew about."""
    assert any("plated free pad" in w and "GND" in w for w in design.warnings), (
        design.warnings
    )


def test_imported_rule_areas_are_reported_as_binding_nothing() -> None:
    """precis has no keepout mechanism at all
    (docs/backlog/pcb-keepout-does-not-bind.md), so a board whose author
    drew keepouts must be told they did not come across — otherwise the
    re-route silently violates areas the source board respected. The real
    spike board carries 184 such records."""
    design = _reread(
        **{
            '{"type":"COMPONENT","ticket":60,"id":"c1"}': (
                '{"type":"RULE","ticket":59,"id":"r1"}||{"ruleName":"KEEPOUT"}|\n'
                '{"type":"COMPONENT","ticket":60,"id":"c1"}'
            )
        }
    )
    assert any("bind nothing" in w for w in design.warnings)


# ── the whole thing is deterministic ─────────────────────────────────────
def test_two_reads_of_one_file_agree_exactly(project) -> None:
    """A re-import must be comparable to the first, and the export half's
    round-trip test compares geometry — float residue from the mil->mm
    multiply would make both test noise."""
    a = epro.build_design(project)[0]
    b = epro.build_design(epro.read_archive(_zip()))[0]
    assert a.components == b.components
    assert a.footprints == b.footprints
    assert a.connections == b.connections
    assert a.features == b.features
    assert a.stats == b.stats
