"""EasyEDA Pro ``.epro2`` reader — `docs/backlog/pcb-epro-import.md` 1a.

The fixture is a plain-text directory (``tests/fixtures/pcb_epro_tiny/``)
zipped in memory here, following the ``easyeda_c*_trimmed.json``
precedent: a committed binary ``.epro2`` would be unreviewable and would
rot silently, whereas every record in the text fixture is a reviewed
claim about the format.

The load-bearing test is :func:`test_bottom_side_pad_frame_mirrors_in_y`.
Rotation sense and bottom-side mirroring fail SILENTLY — the board
renders and the parts are simply in the wrong place — so the convention
the spike settled by net agreement is pinned here rather than left in a
docstring.
"""

from __future__ import annotations

import io
import json
import math
import os
import pathlib
import zipfile

import pytest

from precis.pcb import epro

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "pcb_epro_tiny"
_MIL = 0.0254
#: The fixture outline is 1000 x 500 mil.
_H_MIL = 500


def _zip(**overrides: bytes | None) -> bytes:
    """Zip the text fixture, optionally replacing or dropping members."""
    buf = io.BytesIO()
    members: dict[str, bytes | None] = {
        "project2.json": (_FIXTURE / "project2.json").read_bytes(),
        "board.epru": (_FIXTURE / "board.epru").read_bytes(),
    }
    members.update(overrides)
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            if data is not None:
                zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def project():
    return epro.read_archive(_zip())


@pytest.fixture
def pcb(project):
    return project.by_type("PCB")[0]


@pytest.fixture
def frame(pcb):
    return epro.board_outline(pcb)[1]


# ── container ────────────────────────────────────────────────────────────
def test_reads_the_manifest_and_splits_every_document(project):
    assert project.title == "epro_tiny"
    assert project.editor_version == "3.2.149"
    assert [d.doc_type for d in project.documents] == ["PCB", "FOOTPRINT"]


def test_empty_string_body_is_a_payload_absent_not_an_error(pcb):
    """``""`` bodies are ordinary in this format — the stale PAD_NET rows
    and half the NET rows on a real board carry one. Treating them as
    malformed would refuse every real export."""
    nets = pcb.of_type("NET")
    assert [n.body for n in nets].count(None) == 1
    # ...and `bodies()` hands back only the ones that said something.
    assert len(pcb.bodies("NET")) == len(nets) - 1


def test_old_epro_archive_is_refused_by_name():
    """A ZIP of project.json + *.epcb is the format KiCad documents, not
    this one. The error has to say which, or the next person spends an
    afternoon on it."""
    data = _zip(**{"project2.json": None, "project.json": b"{}"})
    with pytest.raises(epro.EproError, match="older .epro format"):
        epro.read_archive(data)


def test_repeated_record_id_is_refused_as_an_event_log():
    """The reader assumes a snapshot. If a stream ever repeats an id it is
    an event log, and snapshot semantics would silently keep the WRONG
    copy of a moved track."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    # Inside the PCB document — ids are scoped per document on purpose, so
    # appending at the end of the stream would land in the FOOTPRINT and
    # prove nothing (a footprint's own `e7` legitimately repeats).
    dup = stream.replace(
        '{"type":"LINE","ticket":31,',
        '{"type":"LINE","ticket":99,"id":"t1"}||{"netName":"SIG"}|\n'
        '{"type":"LINE","ticket":31,',
    )
    with pytest.raises(epro.EproError, match="appears twice"):
        epro.split_documents(dup)


def test_the_same_short_id_in_two_documents_is_fine():
    """Element ids are scoped to their document: two footprints each
    numbering a pad ``e7`` is normal, and 1413 such collisions exist on
    the spike board."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    docs = epro.split_documents(stream)
    pcb_ids = {r.id for r in docs[0].records}
    fp_ids = {r.id for r in docs[1].records}
    assert pcb_ids & fp_ids == set()  # the fixture keeps them distinct...
    doubled = stream + stream[stream.index('{"type":"DOCHEAD","ticket":70}') :]
    assert len(epro.split_documents(doubled)) == 3  # ...and a repeat is fine


def test_record_before_any_dochead_is_refused():
    with pytest.raises(epro.EproError, match="before any DOCHEAD"):
        epro.split_documents('{"type":"LINE","ticket":1}||{}|')


def test_a_line_without_the_separator_is_refused():
    with pytest.raises(epro.EproError, match="separator"):
        epro.split_documents('{"type":"DOCHEAD"}')


# ── frame ────────────────────────────────────────────────────────────────
def test_outline_lands_in_precis_frame_with_y_flipped(pcb):
    outline, _ = epro.board_outline(pcb)
    xs = [x for x, _ in outline]
    ys = [y for _, y in outline]
    assert (min(xs), max(xs)) == (0.0, pytest.approx(1000 * _MIL))
    assert (min(ys), max(ys)) == (0.0, pytest.approx(_H_MIL * _MIL))


def test_frame_flips_y_about_the_outline_not_about_zero(frame):
    """EasyEDA Y grows down; precis' grows up. The top edge of the board
    in EasyEDA (y=0) must become the HIGHEST y in precis."""
    assert frame.xy(0, 0) == (0.0, pytest.approx(_H_MIL * _MIL))
    assert frame.xy(0, _H_MIL) == (0.0, 0.0)


def test_missing_outline_is_a_refusal_not_a_bounding_box():
    """Without an outline every geometry consumer falls back to
    ``export.board_bbox``'s synthetic rectangle around the placed parts —
    a board shape that is a lie. Refuse instead."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in stream.split("\n") if '"id":"outline1"' not in line
    )
    doc = epro.split_documents(stripped)[0]
    with pytest.raises(epro.EproError, match="no POLY on the board-outline"):
        epro.board_outline(doc)


def test_coordinates_round_to_the_nanometre(frame):
    """Determinism: the mil->mm multiply leaves float residue that makes
    two runs over one file compare unequal."""
    x, y = frame.xy(123.4567, 89.0123)
    assert x == round(x, 6)
    assert y == round(y, 6)


# ── layers ───────────────────────────────────────────────────────────────
def test_copper_layers_are_named_in_stack_order(pcb):
    assert epro.copper_layers(pcb) == {
        1: "F.Cu",
        15: "In1.Cu",
        16: "In2.Cu",
        2: "B.Cu",
    }


def test_a_board_without_both_outer_layers_is_refused():
    """A stackup the reader cannot name is better refused here than handed
    on as a short, mislabelled layer list."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in stream.split("\n") if '"layerType":"BOTTOM"' not in line
    )
    doc = epro.split_documents(stripped)[0]
    with pytest.raises(epro.EproError, match="one TOP and one BOTTOM"):
        epro.copper_layers(doc)


def test_unused_layers_are_not_in_the_map(pcb):
    """The spike board declares 92 LAYER records and uses 27. A reader
    that took them all would claim a 60-layer stackup."""
    assert 13 not in epro.layer_map(pcb)


# ── copper ───────────────────────────────────────────────────────────────
def test_segments_chain_into_polyline_tracks(pcb, frame):
    ext = epro.extract_tracks(pcb, frame)
    sig = [t for t in ext.tracks if t["net"] == "SIG"]
    assert len(sig) == 1
    assert len(sig[0]["geom"]["segments"]) == 2
    assert sig[0]["layer"] == "F.Cu"
    assert sig[0]["geom"]["width_mm"] == pytest.approx(0.254)


def test_a_tee_ends_the_polyline_on_every_branch(pcb, frame):
    """Three segments meeting at a point are a tee. Chaining through it
    would invent a corner the author never drew and drop a branch."""
    ext = epro.extract_tracks(pcb, frame)
    tee = [t for t in ext.tracks if t["net"] == "TEE"]
    assert len(tee) == 3
    assert all(len(t["geom"]["segments"]) == 1 for t in tee)


def test_every_input_segment_survives_chaining(pcb, frame):
    """Chaining is regrouping, never loss. On the real board this is
    1579 in and 1579 out across 449 tracks."""
    ext = epro.extract_tracks(pcb, frame)
    out = sum(len(t["geom"]["segments"]) for t in ext.tracks)
    routable = epro.copper_layers(pcb)
    src = [
        b
        for kind in ("LINE", "ARC")
        for b in pcb.bodies(kind)
        if b["layerId"] in routable
        and b["netName"]
        and (b["startX"], b["startY"]) != (b["endX"], b["endY"])
        # a degenerate sweep has no centre and is dropped, not chorded
        and not (kind == "ARC" and b.get("angle") in (0, 0.0))
    ]
    assert out == len(src)


def test_copper_off_the_routable_layers_is_ignored(pcb, frame):
    """The fixture puts a LINE on the Document layer. Silk and
    documentation strokes are not copper and must not become tracks."""
    ext = epro.extract_tracks(pcb, frame)
    for t in ext.tracks:
        assert t["layer"] in set(epro.copper_layers(pcb).values())


def test_a_track_with_no_net_is_skipped_and_reported(pcb, frame):
    """``pcb_fixed_copper_put`` refuses an unknown net by design — a
    copper row inventing its own net is a bug. So drop it here, loudly."""
    ext = epro.extract_tracks(pcb, frame)
    assert all(t["net"] for t in ext.tracks)
    assert any("carries no net name" in w for w in ext.warnings)


def test_zero_length_segment_is_dropped_and_reported(pcb, frame):
    ext = epro.extract_tracks(pcb, frame)
    assert any("zero-length" in w for w in ext.warnings)


def test_track_rows_match_the_fixed_copper_row_shape(pcb, frame):
    ext = epro.extract_tracks(pcb, frame)
    for t in ext.tracks:
        assert set(t) == {"ctype", "layer", "net", "geom"}
        assert t["ctype"] == "track"
        assert set(t["geom"]) == {"segments", "width_mm"}
        for s in t["geom"]["segments"]:
            assert s["shape"] in ("line", "arc")
            assert len(s["start"]) == len(s["end"]) == 2
            if s["shape"] == "arc":
                assert len(s["center"]) == 2
                assert isinstance(s["cw"], bool)


# ── vias ─────────────────────────────────────────────────────────────────
def test_through_hole_via_carries_the_full_span(pcb, frame):
    ext = epro.extract_vias(pcb, frame)
    assert len(ext.vias) == 1
    via = ext.vias[0]
    assert via["net"] == "SIG"
    assert via["geom"]["span"] == ["F.Cu", "B.Cu"]
    assert via["geom"]["drill_mm"] == pytest.approx(0.305, abs=1e-4)
    assert via["geom"]["dia_mm"] == pytest.approx(0.61, abs=1e-4)


def test_buried_via_is_skipped_not_flattened(pcb, frame):
    """Emitting a blind/buried via as through-hole would short it to
    layers it must not touch — a short that no view would show."""
    ext = epro.extract_vias(pcb, frame)
    assert [v["net"] for v in ext.vias] == ["SIG"]
    assert any("not a plain through-hole" in w and "BURIED" in w for w in ext.warnings)


def test_via_lands_at_the_end_of_its_track(pcb, frame):
    """The frame applies identically to LINE and VIA, so the fixture's via
    sits exactly on the SIG track's far end. If the two ever disagreed the
    router would see a dangling stub."""
    tracks = epro.extract_tracks(pcb, frame).tracks
    via = epro.extract_vias(pcb, frame).vias[0]
    ends = [
        tuple(s["end"])
        for t in tracks
        if t["net"] == "SIG"
        for s in t["geom"]["segments"]
    ]
    assert (via["geom"]["x"], via["geom"]["y"]) in ends


# ── pours ────────────────────────────────────────────────────────────────
def test_pours_are_reported_not_imported_as_fixed_copper(pcb, frame):
    """``pcb_fixed_copper``'s CHECK allows only track|via, and a
    single-net pour belongs in precis as a plane assignment."""
    ext = epro.extract_copper(pcb, frame)
    assert all(r["ctype"] in ("track", "via") for r in ext.rows)
    assert any("POUR region(s) on GND" in w for w in ext.warnings)


def test_extract_copper_merges_both_halves(pcb, frame):
    ext = epro.extract_copper(pcb, frame)
    assert len(ext.tracks) == len(epro.extract_tracks(pcb, frame).tracks)
    assert len(ext.vias) == len(epro.extract_vias(pcb, frame).vias)
    assert ext.rows == [*ext.tracks, *ext.vias]


# ── the frame convention that fails silently ─────────────────────────────
def _place(comp, pad, *, rot_sign=1, mirror_y=True):
    px, py = pad["centerX"], pad["centerY"]
    if mirror_y and comp["layerId"] == 2:
        py = -py
    a = math.radians(rot_sign * comp["angle"])
    return (
        comp["x"] + px * math.cos(a) - py * math.sin(a),
        comp["y"] + px * math.sin(a) + py * math.cos(a),
    )


def test_bottom_side_pad_frame_mirrors_in_y(project):
    """Spike-verified 2026-09-29 by net agreement against 1579 routed
    segments on a real 140-component board: ``+angle`` with a bottom-side
    mirror in **Y** put 573 track endpoints on a same-net pad and zero on
    a pad of a different net; every other rotation/mirror combination
    produced disagreements.

    Pinned here because this is the failure that renders plausibly: a
    wrong handedness gives a board that looks right in every view and
    cannot be built.
    """
    pcb = project.by_type("PCB")[0]
    fp = project.by_type("FOOTPRINT")[0]
    comp = pcb.bodies("COMPONENT")[0]
    pads = {p["num"]: p for p in fp.bodies("PAD")}

    # Top side, angle 90, +rotation: local (-30, 0) -> (0, -30) added to
    # the component origin, in the Y-DOWN source frame.
    assert comp["angle"] == 90
    x, y = _place(comp, pads["1"])
    assert (round(x, 6), round(y, 6)) == (300.0, 120.0)

    # Pad 3 is the one that can tell the mirror axes apart: pad 1 sits at
    # local y=0, where mirroring in Y is a no-op and the test would pass
    # whatever the reader did.
    assert pads["3"]["centerY"] != 0
    bottom = {**comp, "layerId": 2}
    assert _place(bottom, pads["3"]) == pytest.approx((325.0, 150.0))
    assert _place(bottom, pads["3"], mirror_y=False) == pytest.approx((275.0, 150.0))
    # ...and a negated rotation sense is a different place again.
    assert _place(bottom, pads["3"], rot_sign=-1) != pytest.approx((325.0, 150.0))


def test_live_pad_net_rows_carry_the_net_in_the_body(pcb):
    """The net is ``body.padNet``. The id tuple's fourth element looks
    like a net reference and is not one — it is the footprint-local pad
    element id, and on the spike board it resolved to zero of the 244 net
    names. A reader keying on it would produce an empty netlist and no
    error."""
    rows = [
        r for r in pcb.of_type("PAD_NET") if r.body is not None and r.body.get("padNet")
    ]
    assert len(rows) == 1
    assert rows[0].body["padNet"] == "SIG"
    assert json.loads(rows[0].id)[3] == "e7"


def test_stale_pad_net_rows_are_the_ones_with_no_body(pcb):
    """On the spike board 914 of 1583 PAD_NET rows point at components
    that no longer exist; all of them carry the empty body. The live 669
    all carry a payload, so 'has a body' is the liveness test."""
    stale = [r for r in pcb.of_type("PAD_NET") if r.body is None]
    assert len(stale) == 1
    assert json.loads(stale[0].id)[1] == "stale9"


# ── arcs ─────────────────────────────────────────────────────────────────
def test_arc_becomes_a_centre_and_a_direction(pcb, frame):
    """``ARC`` stores start/end/signed-sweep; precis wants start/end/
    centre/cw. The fixture's arc is a +180 semicircle from (600,100) to
    (600,300) mil, so its centre is (600,200) and its radius 100 mil."""
    ext = epro.extract_tracks(pcb, frame)
    curve = [t for t in ext.tracks if t["net"] == "CURVE"][0]
    arcs = [s for s in curve["geom"]["segments"] if s["shape"] == "arc"]
    assert len(arcs) == 1
    arc = arcs[0]
    assert arc["center"] == pytest.approx(list(frame.xy(600, 200)))
    assert math.dist(arc["start"], arc["center"]) == pytest.approx(100 * _MIL)
    assert math.dist(arc["end"], arc["center"]) == pytest.approx(100 * _MIL)


def test_arc_chains_with_the_straight_segment_it_touches(pcb, frame):
    """A curve is a segment like any other — a lead-in line and the arc it
    meets belong to one track, or the router sees two stubs."""
    ext = epro.extract_tracks(pcb, frame)
    curve = [t for t in ext.tracks if t["net"] == "CURVE"][0]
    shapes = [s["shape"] for s in curve["geom"]["segments"]]
    assert shapes == ["line", "arc"]
    line, arc = curve["geom"]["segments"]
    assert line["end"] == arc["start"]


def test_y_flip_reverses_arc_handedness(pcb, frame):
    """The frame transform is a REFLECTION, so a sweep stored positive in
    EasyEDA's Y-down frame is CLOCKWISE once Y points up. Getting this
    backwards mirrors the arc through its own chord — same endpoints,
    same radius, wrong side, and no view would call it out."""
    ext = epro.extract_tracks(pcb, frame)
    arc = [
        s
        for t in ext.tracks
        if t["net"] == "CURVE"
        for s in t["geom"]["segments"]
        if s["shape"] == "arc"
    ][0]
    assert arc["cw"] is True  # source sweep was +180
    # The bulge must be on the +X side: the source arc runs down the
    # Y-down frame's left normal, which the flip puts on the right.
    assert arc["center"][0] > min(arc["start"][0], arc["end"][0]) - 1e-9


def test_degenerate_arc_is_dropped_not_chorded(pcb, frame):
    """A 0 (or 360) degree sweep has no finite centre -- ``tan(theta/2)``
    is 0. Chording it silently would put a straight trace where the author
    drew a curve."""
    ext = epro.extract_tracks(pcb, frame)
    assert any("no finite centre" in w for w in ext.warnings)


def test_arc_centre_round_trips_the_stored_sweep():
    """Property check on the solver itself, independent of any fixture:
    the centre it returns must be equidistant from both endpoints and must
    reproduce the swept angle it was given."""
    for sweep in (85.161, -180.0, 180.0, 112.3584, -30.0, 350.0):
        a, b = (1100.0, 1210.0), (1160.0, 2630.0)
        cx, cy = epro._arc_centre(a, b, sweep)
        r1 = math.hypot(a[0] - cx, a[1] - cy)
        r2 = math.hypot(b[0] - cx, b[1] - cy)
        assert r1 == pytest.approx(r2, abs=1e-9)
        a0 = math.atan2(a[1] - cy, a[0] - cx)
        a1 = math.atan2(b[1] - cy, b[0] - cx)
        got = math.degrees((a1 - a0) % (2 * math.pi))
        if sweep < 0:
            got -= 360
        assert got == pytest.approx(sweep, abs=1e-6)


# ── which board ──────────────────────────────────────────────────────────
def test_several_pcb_documents_refuses_rather_than_taking_the_first():
    """Spike-verified 2026-09-30: the arc project holds SEVEN PCB
    documents and the interesting one is LAST. Taking [0] reported zero
    arcs — a wrong answer with no error."""
    stream = (_FIXTURE / "board.epru").read_text(encoding="utf-8")
    two = stream + (
        '{"type":"DOCHEAD","ticket":900}||'
        '{"docType":"PCB","uuid":"second00000000pcb"}|\n'
        '{"type":"META","ticket":901,"id":"META"}||{"title":"PCB2"}|\n'
    )
    project = epro.EproProject("t", "3.2.149", epro.split_documents(two))
    with pytest.raises(epro.EproError, match="2 PCB documents"):
        project.pcb()
    assert project.pcb("second00000000pcb").uuid == "second00000000pcb"
    assert project.pcb("tiny0000000000pcb").uuid == "tiny0000000000pcb"


def test_naming_a_board_that_is_not_there_lists_the_ones_that_are(project):
    with pytest.raises(epro.EproError, match="no PCB document with uuid"):
        project.pcb("nope")


def test_single_board_project_needs_no_uuid(project):
    assert project.pcb().uuid == "tiny0000000000pcb"


# ── stale rows (Reto's ruling, 2026-09-30) ───────────────────────────────
def test_superseded_pad_net_rows_are_dropped(pcb):
    """Leftovers are how the files come; the import drops them. A row with
    no payload names no net, and one naming a deleted component would bind
    a real net to a part that is not on the board."""
    live, notes = epro.live_pad_nets(pcb)
    assert live == {("c1", "1"): "SIG"}
    assert any("superseded" in n for n in notes)


def test_a_pad_net_row_with_an_empty_net_is_not_a_net_named_empty(pcb):
    """``padNet: ""`` is an unconnected pad, not a net called "". The
    fixture's c1 pad 2 carries one."""
    live, _ = epro.live_pad_nets(pcb)
    assert ("c1", "2") not in live


def test_nets_nothing_references_are_dropped(pcb):
    """An imported net with no members survives every later re-put (an
    existing net is reused by name) and sits in the unrouted census
    forever as something that can never be routed."""
    kept, notes = epro.live_nets(pcb)
    assert "ORPHAN" not in kept
    assert {"SIG", "GND", "CURVE"} <= set(kept)
    assert any("no pad and no copper" in n for n in notes)


def test_the_empty_named_net_record_is_dropped_and_counted(pcb):
    kept, notes = epro.live_nets(pcb)
    assert "" not in kept
    assert any("empty name" in n for n in notes)


# ── the real board, when someone points at one ───────────────────────────
@pytest.mark.skipif(
    not os.environ.get("PRECIS_EPRO_FIXTURE"),
    reason="set PRECIS_EPRO_FIXTURE=/path/to/real.epro2 to run",
)
def test_real_board_parses_without_surprises():
    """Not in CI: a real board is large and usually proprietary. Run this
    against one during a spike and whenever format drift is suspected —
    EasyEDA has already moved format details inside this repo's history.
    """
    data = pathlib.Path(os.environ["PRECIS_EPRO_FIXTURE"]).read_bytes()
    project = epro.read_archive(data)
    pcb = project.by_type("PCB")[0]
    outline, frame = epro.board_outline(pcb)
    xs = [x for x, _ in outline]
    ys = [y for _, y in outline]
    assert 1.0 < max(xs) - min(xs) < 1000.0
    assert 1.0 < max(ys) - min(ys) < 1000.0

    ext = epro.extract_copper(pcb, frame)
    assert ext.tracks or ext.vias
    # Everything must land on the board. A frame bug shows up here first.
    for t in ext.tracks:
        for s in t["geom"]["segments"]:
            for px, py in (s["start"], s["end"]):
                assert min(xs) - 0.01 <= px <= max(xs) + 0.01
                assert min(ys) - 0.01 <= py <= max(ys) + 0.01
    for v in ext.vias:
        assert min(xs) <= v["geom"]["x"] <= max(xs)
        assert min(ys) <= v["geom"]["y"] <= max(ys)
    # Chaining regroups, never loses.
    routable = epro.copper_layers(pcb)
    src = sum(
        1
        for b in pcb.bodies("LINE")
        if b["layerId"] in routable
        and b["netName"]
        and (b["startX"], b["startY"]) != (b["endX"], b["endY"])
    )
    assert sum(len(t["geom"]["segments"]) for t in ext.tracks) == src
