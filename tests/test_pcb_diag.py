"""``precis.pcb.diag`` — the pcb Diag adapter (gr522027, shared-core
layers S1b): ``DrcFinding`` ↔ ``Diag`` round-trips, the scriptless-op
``op`` decorator's ``exc.diag``/``anchor.op_id`` stamp, the builder's
``diag_refs``/``diag_code`` → manifest ``error`` → run-failed Diag
path, and the traced-run adapter's conformance registration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import precis_geom.pcb as pcb
from precis.errors import BadInput
from precis.pcb import board_script, check_run, drc, local
from precis.pcb import diag as pcb_diag
from precis.pcb.capabilities import capability_for
from precis_diag import Diag, Status, diag_from_dict, diag_to_dict
from precis_geom import out as geom_out
from tests.workspaces._script import load_script

_CAP4 = capability_for("4layer")


def _track(net: str, layer: str, start, end, *, width_mm: float = 0.2):
    return {
        "ctype": "track",
        "layer": layer,
        "net": net,
        "width_mm": width_mm,
        "segments": [{"shape": "line", "start": list(start), "end": list(end)}],
    }


#: Findings across the ``_object_identity`` vocabulary — a real
#: clearance engine finding, then hand-built shapes for the paired,
#: fixed-copper, part-pair and via rules.
def _findings() -> list[drc.DrcFinding]:
    model = {
        "layers": ["F.Cu"],
        "copper": [
            _track("A", "F.Cu", (0.0, 0.0), (1.0, 0.0)),
            _track("B", "F.Cu", (0.0, 0.25), (1.0, 0.25)),
        ],
    }
    real = drc.check_clearance(model, _CAP4)
    assert real, "the clearance fixture must produce a finding"
    return [
        *real,
        drc.DrcFinding(
            rule="via_pad_keepout",
            severity="error",
            where="F.Cu",
            detail="via inside pad keep-out",
            objects=({"via_net": "VCC", "pad_refdes": "U1", "pad_pin": "3"},),
            margin_mm=-0.05,
        ),
        drc.DrcFinding(
            rule="via_via_keepout",
            severity="warn",
            where="F.Cu",
            detail="via/via keep-out",
            objects=({"a_net": "A", "b_net": "B"},),
        ),
        drc.DrcFinding(
            rule="courtyard_overlap",
            severity="error",
            where="top",
            detail="courtyards overlap",
            objects=({"a": "U1", "b": "hole-1"},),
            margin_mm=-1.2,
        ),
        drc.DrcFinding(
            rule="fixed_copper_clearance",
            severity="error",
            where="F.Cu",
            detail="fixed copper too close",
            objects=({"fixed_id": 7, "ctype": "track", "net": "GND"},),
            margin_mm=-0.01,
        ),
        drc.DrcFinding(
            rule="unconnected_pin",
            severity="warn",
            where="netlist",
            detail="pin not wired",
            objects=({"refdes": "R2", "pin": "1"},),
        ),
        drc.DrcFinding(
            rule="invariant:min_gap",
            severity="error",
            where="min_gap violated — off (value 0.05, goal 0.1)",
            detail="measure #3 (strength=invariant) on net VCC",
            objects=({"net": "VCC"},),
        ),
    ]


def test_drc_round_trips_every_finding() -> None:
    """Acceptance (a): ``drc_from_diag(drc_to_diag(f)) == f`` — objects
    and margin ride ``data``, participants ride ``refs``."""
    for f in _findings():
        d = pcb_diag.drc_to_diag(f)
        assert d.code == "pcb." + f.rule.replace(":", ".")
        assert d.message == f.detail and d.where == f.where
        assert d.status is (Status.ERROR if f.severity == "error" else Status.WARN)
        assert pcb_diag.drc_from_diag(d) == f
        assert diag_from_dict(diag_to_dict(d)) == d  # wire-valid
        assert hash(d)  # data stays a scalar map — the Diag hash works


def test_drc_to_diag_refs_are_the_participant_identities() -> None:
    f = _findings()[1]  # via_pad_keepout
    d = pcb_diag.drc_to_diag(f)
    assert "pad:U1/3" in d.refs and "via:VCC" in d.refs


def test_drc_to_diag_refuses_an_unknown_severity() -> None:
    f = drc.DrcFinding(rule="x", severity="info", where="", detail="", objects=())
    with pytest.raises(ValueError, match="severity"):
        pcb_diag.drc_to_diag(f)


def test_drc_from_diag_refuses_non_pcb_and_non_finding() -> None:
    with pytest.raises(ValueError, match="not a pcb"):
        pcb_diag.drc_from_diag(
            Diag(status=Status.ERROR, code="cad.run-failed", message="x")
        )
    d = pcb_diag.drc_to_diag(_findings()[0])
    with pytest.raises(ValueError, match="no DRC severity"):
        pcb_diag.drc_from_diag(
            Diag(
                status=Status.NOT_CHECKED,
                code=d.code,
                message=d.message,
                data=d.data,
            )
        )


def test_error_diag_carries_op_anchor_and_refs() -> None:
    exc = ValueError("blew up")
    d = pcb_diag.error_diag(
        exc,
        op="place",
        refs=("part:U1",),
        next="fix it",
        file="main.py",
        line=12,
    )
    assert d.status is Status.ERROR
    assert d.code == "pcb.place-failed" and d.message == "blew up"
    assert d.anchor.op_id == "place"
    assert (d.anchor.file, d.anchor.line) == ("main.py", 12)
    assert d.refs == ("part:U1",) and d.next == "fix it"


def test_script_call_site_names_the_callers_own_line() -> None:
    file, line = pcb_diag.script_call_site()
    assert file.endswith("test_pcb_diag.py")
    assert line > 0
    # The call site is the line above the assert.
    assert (
        line
        == test_script_call_site_names_the_callers_own_line.__code__.co_firstlineno + 1
    )


def test_op_decorator_stamps_diag_on_raise() -> None:
    @pcb_diag.op("probe")
    def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError) as exc_info:
        boom()
    exc: Any = exc_info.value
    d = exc.diag
    assert isinstance(d, Diag)
    assert d.code == "pcb.probe-failed" and d.anchor.op_id == "probe"
    assert d.anchor.file.endswith("test_pcb_diag.py")
    assert exc.diag_code == "pcb.probe-failed" and exc.diag_refs == ()


def test_drc_to_diag_maps_an_invariant_rule() -> None:
    """``invariant:<metric>`` (``session.invariant_measure_findings``)
    carries a literal ``:`` — the code encodes it ``:`` → ``.`` and
    ``drc_from_diag`` restores it."""
    d = pcb_diag.drc_to_diag(_findings()[-1])
    assert d.code == "pcb.invariant.min_gap"
    assert pcb_diag.drc_from_diag(d).rule == "invariant:min_gap"


def test_op_never_replaces_the_original_error() -> None:
    """A vendor-ish exception whose ``code`` is not a Diag code keeps
    its type and message — the stamp falls back, it never replaces the
    raise (an int ``code=404`` used to make ``Diag.__post_init__`` raise
    in its place)."""

    class Vendorish(Exception):
        code = 404
        refs = 5
        next = None

    @pcb_diag.op("drc")
    def boom() -> None:
        raise Vendorish("vendor said no")

    with pytest.raises(Vendorish, match="vendor said no") as exc_info:
        boom()
    stamped: Any = exc_info.value
    assert stamped.diag.code == "pcb.drc-failed"
    assert stamped.diag.refs == () and stamped.diag.next == ""


def test_op_normalises_preciserror_next() -> None:
    """``PrecisError.next`` is ``str | list[str] | None`` — the Diag
    field is one string: ``None`` → empty, a list joins on ``"; "``."""

    @pcb_diag.op("run")
    def boom(hint: Any) -> None:
        raise BadInput("bad", next=hint)

    for hint, want in (
        (None, ""),
        (["add kind=", "check spelling"], "add kind=; check spelling"),
    ):
        with pytest.raises(BadInput) as exc_info:
            boom(hint)
        stamped: Any = exc_info.value
        assert stamped.diag.next == want


def test_op_reads_diag_fields_off_a_wrapped_cause() -> None:
    """``local.py``'s ``raise LocalError(...) from exc`` wraps a
    ``PcbBuilderError`` — the stamp falls back to the cause's
    ``diag_refs``/``diag_code`` instead of losing them."""

    @pcb_diag.op("place")
    def boom() -> None:
        try:
            raise pcb.PcbBuilderError("dup part U1", refs=("part:U1",))
        except ValueError as exc:
            raise local.LocalError(str(exc)) from exc

    with pytest.raises(local.LocalError) as exc_info:
        boom()
    d = exc_info.value.diag
    assert d is not None
    assert d.code == "pcb.builder-error" and d.refs == ("part:U1",)


def test_op_inner_stamp_wins_over_the_outer_op() -> None:
    @pcb_diag.op("place")
    def inner() -> None:
        raise ValueError("deep")

    @pcb_diag.op("run")
    def outer() -> None:
        inner()

    with pytest.raises(ValueError) as exc_info:
        outer()
    stamped: Any = exc_info.value
    assert stamped.diag.anchor.op_id == "place"


def test_op_decorator_reads_refs_code_next_off_the_exception() -> None:
    class Refusing(ValueError):
        refs = ("net:VCC",)
        code = "pcb.custom"
        next = "try again"
        diag: Diag

    @pcb_diag.op("drc")
    def boom() -> None:
        raise Refusing("bad board")

    with pytest.raises(Refusing) as exc_info:
        boom()
    d = exc_info.value.diag
    assert d.code == "pcb.custom"
    assert d.refs == ("net:VCC",) and d.next == "try again"


def test_failed_run_result_delegates_to_the_shared_adapter() -> None:
    """The pcb domain's named door — a bare failed manifest yields the
    shared ``cad.run-failed`` record (the ``code``/``refs`` override the
    instrumentation feeds it is tested with the instrumented path)."""
    rr = pcb_diag.failed_run_result(
        {"status": "failed", "error": {"type": "PcbBuilderError", "message": "x"}},
        None,
    )
    assert not rr.ok
    assert rr.diags[0].code == "cad.run-failed"


def test_failed_run_result_falls_back_on_a_malformed_code() -> None:
    """A serialized ``error.code`` that is not a ``<domain>.<code>``
    (nothing validates it upstream) falls back to ``cad.run-failed``
    instead of refusing the record."""
    for bad in ("404", "nonsense"):
        rr = pcb_diag.failed_run_result(
            {
                "status": "failed",
                "error": {"type": "X", "message": "m", "code": bad},
            },
            None,
        )
        [d] = rr.diags
        assert d.code == "cad.run-failed"


# ── the instrumented paths ────────────────────────────────────────────

#: A dup refdes — the second traced ``b.part`` raises the stamped
#: ``PcbBuilderError`` (gr522027, same fixture as the conformance
#: registration).
_PCB_FAIL_SCRIPT = (
    "import precis_geom.pcb as pcb\n"
    "\n"
    "\n"
    "def build():\n"
    "    b = pcb.Board('x')\n"
    "    b.part('U1')\n"
    "    b.part('U1')\n"
)


def test_builder_error_stamps_diag_fields() -> None:
    """``PcbBuilderError(refs=)`` lands on ``diag_refs``; the code
    derives from the exception type — ``PinConflict`` →
    ``pcb.pin-conflict``, the base → ``pcb.builder-error``."""
    b = pcb.Board("x")
    u1 = b.part("U1")
    u1.pin(1, name="SDA")
    with pytest.raises(pcb.PinConflict) as exc_info:
        u1.pin(1, name="SCL")
    exc = exc_info.value
    assert exc.diag_code == "pcb.pin-conflict"
    assert "part:U1" in exc.diag_refs
    assert any(r.startswith("pad:U1/") for r in exc.diag_refs)


def test_run_failure_diag_carries_builder_code_and_refs(tmp_path: Path) -> None:
    """Acceptance (c): a failed board-script run → a Diag with
    ``anchor.frame_key`` + ``file:line`` + the entity refs — the
    builder's ``diag_refs``/``diag_code`` serialized on
    ``manifest.error``, folded into the run-failed record."""
    mod = load_script(tmp_path, _PCB_FAIL_SCRIPT)
    out_dir = tmp_path / "out" / "x"
    with (
        pytest.raises(pcb.PcbBuilderError),
        geom_out.run(out_dir, work_root=tmp_path) as o,
    ):
        mod.build()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["error"]["code"] == "pcb.builder-error"
    assert manifest["error"]["refs"] == ["part:U1"]
    assert o.result is not None
    [d] = o.result.diags
    assert d.code == "pcb.builder-error"
    assert d.refs == ("part:U1",)
    assert d.anchor.frame_key
    assert (d.anchor.file, d.anchor.line) == ("main.py", 7)
    assert diag_from_dict(diag_to_dict(d)) == d


def test_local_op_failure_diag_anchors_op_id_and_call_site() -> None:
    """Acceptance (b): a failed scriptless pcb op → ``exc.diag`` with
    ``anchor.op_id`` and the caller's ``file:line``."""
    with pytest.raises(local.LocalError) as exc_info:
        local.route(object())
    d = exc_info.value.diag
    assert d is not None
    assert d.status is Status.ERROR
    assert d.code == "pcb.route-failed"
    assert d.anchor.op_id == "route"
    assert d.anchor.file.endswith("test_pcb_diag.py")
    assert d.anchor.line > 0


def test_local_place_failure_names_the_failing_step() -> None:
    """``run`` calling ``place`` keeps the inner stamp — ``place`` is
    the op that failed (and a ``part:`` ref when the refusal names
    one)."""
    batch = {
        "meta": {},
        "components": [
            {
                "refdes": "U1",
                "pins": [{"name": "1"}],
                "supplies": [{"role": "VDD", "blocks": [["1"]]}],
            }
        ],
        "nets": [],
        "connections": [],
        "features": [
            {
                "ftype": "outline",
                "geom": {"path": [[0, 0], [10, 0], [10, 10], [0, 10]]},
            }
        ],
    }
    with pytest.raises(local.LocalError) as exc_info:
        local.place(batch)
    d = exc_info.value.diag
    assert d is not None
    assert d.anchor.op_id == "place"
    assert "part:U1" in d.refs


def test_board_script_loss_raise_carries_diag_and_refs() -> None:
    """``emit``'s unexpressable-key refusal → ``exc.diag`` with
    ``pcb.script-error`` and refs naming the refdes it cannot emit."""
    batch = {
        "components": [{"refdes": "U1", "pin_decls": {"1": {}}}],
        "nets": [],
        "connections": [],
    }
    with pytest.raises(board_script.BoardScriptError) as exc_info:
        board_script.emit(batch, "slug", "test")
    exc = exc_info.value
    assert exc.diag.code == "pcb.script-error"
    assert exc.diag.refs == ("part:U1",)
    assert exc.diag_refs == ("part:U1",)
    assert exc.diag_code == "pcb.script-error"
    assert exc.diag.anchor.op_id == "emit"


def test_findings_text_accepts_diags() -> None:
    f = _findings()[0]
    text = local.findings_text([pcb_diag.drc_to_diag(f)])
    assert f.rule in text and f.severity in text and f.detail in text


def test_drc_run_diags_property_converts_findings() -> None:
    findings = _findings()
    run = check_run.DrcRun(
        run_id=None,
        findings=findings,
        pads_only=True,
        delta=drc.FindingDelta(previous_run_id=None),
    )
    assert [d.code for d in run.diags] == [
        "pcb." + f.rule.replace(":", ".") for f in findings
    ]
    assert all(d.status is not Status.OK for d in run.diags)
