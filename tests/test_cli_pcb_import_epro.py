"""``precis pcb import-epro`` — the shell contract (pcb-epro-import 1b).

Pins which stream, which exit code, and that every warning is printed
individually. Not the message text, and not the import itself — that is
``test_pcb_epro_import.py``'s job against a real DB.

Two distinct exit codes because a script around this command needs to
tell "your file is fine, precis cannot take it yet" (an unsupported layer
count, a slug already in use) apart from "this file is not readable" — the
first is worth waiting for, the second is worth re-exporting. A single
non-zero code would collapse them, and the same defect
``test_cli_tools_exit_codes.py`` records for ``precis tools`` (a refusal
on stdout with exit 0) would make either invisible to ``cmd && next``.
"""

from __future__ import annotations

import argparse
import io
import pathlib
import zipfile
from typing import Any

import pytest

from precis.cli import pcb as pcb_cli
from precis.ingest.pcb_epro import EproImportError, ImportResult
from precis.pcb import copper_report
from precis.pcb.epro import EproError

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "pcb_epro_tiny"


@pytest.fixture
def epro_file(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "board.epro2"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in ("project2.json", "board.epru"):
            zf.writestr(name, (_FIXTURE / name).read_bytes())
    path.write_bytes(buf.getvalue())
    return path


def _args(path: pathlib.Path, **over: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "pcb_cmd": "import-epro",
        "path": str(path),
        "slug": "cli-epro",
        "title": None,
        "board": None,
        "dry_run": False,
        "database_url": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


def _stub(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    """Replace the import with a fixed outcome, and the Store with a
    no-op — these tests are about the shell surface, so a real DB would
    only add a way for them to fail for an unrelated reason."""

    class _Store:
        @staticmethod
        def connect(_dsn: object) -> _Store:
            return _Store()

        def close(self) -> None:
            return None

    monkeypatch.setattr(pcb_cli, "resolve_dsn", lambda _u: "postgresql:///x")
    import precis.store as store_mod

    monkeypatch.setattr(store_mod.Store, "connect", staticmethod(_Store.connect))

    def _import(*_a: object, **_k: object) -> object:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    import precis.ingest.pcb_epro as ingest_mod

    monkeypatch.setattr(ingest_mod, "import_epro", _import)


_OK = ImportResult(
    slug="cli-epro",
    ref_id=7,
    created=True,
    stackup=[{"name": n} for n in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")],
    planes={"In1.Cu": "GND"},
    warnings=["first thing dropped", "second thing dropped"],
    stats={
        "components": 2,
        "footprints": 2,
        "nets": 3,
        "connections": 3,
        "mounting_holes": 1,
        "outline_vertices": 5,
    },
)


def test_a_successful_import_prints_counts_stackup_and_planes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], epro_file
) -> None:
    _stub(monkeypatch, _OK)
    pcb_cli.run(_args(epro_file))
    out = capsys.readouterr().out
    assert "imported 'cli-epro'" in out
    assert "2 component(s)" in out
    assert "F.Cu, In1.Cu, In2.Cu, B.Cu" in out
    assert "In1.Cu=GND" in out


def test_every_warning_is_printed_not_counted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], epro_file
) -> None:
    """Each warning names something the imported board does NOT carry. A
    count standing in for them leaves the user to assume the rest came
    across — which for a keepout or a plated free pad is exactly wrong."""
    _stub(monkeypatch, _OK)
    pcb_cli.run(_args(epro_file))
    out = capsys.readouterr().out
    assert "warn: first thing dropped" in out
    assert "warn: second thing dropped" in out


def test_a_dry_run_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], epro_file
) -> None:
    _stub(monkeypatch, _OK)
    pcb_cli.run(_args(epro_file, dry_run=True))
    out = capsys.readouterr().out
    assert "would import" in out
    assert "nothing was written" in out


def test_an_unreadable_board_exits_2_with_the_reason_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], epro_file
) -> None:
    _stub(monkeypatch, EproError("no POLY on the board-outline layer"))
    with pytest.raises(SystemExit) as exc:
        pcb_cli.run(_args(epro_file))
    assert exc.value.code == pcb_cli.EXIT_UNREADABLE
    captured = capsys.readouterr()
    assert "board-outline" in captured.err
    assert captured.out == "", "a failure must not also print a success line"


def test_a_refused_board_exits_3_with_the_reason_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], epro_file
) -> None:
    _stub(monkeypatch, EproImportError("pcb 'cli-epro' already exists"))
    with pytest.raises(SystemExit) as exc:
        pcb_cli.run(_args(epro_file))
    assert exc.value.code == pcb_cli.EXIT_REFUSED
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert captured.out == ""


def test_the_two_failure_codes_are_distinct() -> None:
    """Collapsing them would lose the only signal that says whether to
    re-export the file or wait for a slice."""
    assert pcb_cli.EXIT_UNREADABLE != pcb_cli.EXIT_REFUSED
    assert 0 not in (pcb_cli.EXIT_UNREADABLE, pcb_cli.EXIT_REFUSED)


def test_a_missing_file_fails_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Connecting first would turn a typo'd path into a database error."""

    def _boom(_u: object) -> str:
        raise AssertionError("resolved a DSN for a file that does not exist")

    monkeypatch.setattr(pcb_cli, "resolve_dsn", _boom)
    with pytest.raises(SystemExit, match="cannot read"):
        pcb_cli.run(_args(tmp_path / "nope.epro2"))


# ── copper-report (slice 1c) ─────────────────────────────────────────────
def _report_stub(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    _stub(monkeypatch, _OK)
    import precis.ingest.pcb_epro as ingest_mod

    def _report(*_a: object, **_k: object) -> object:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(ingest_mod, "report_copper", _report)


def _report_args(path: pathlib.Path) -> argparse.Namespace:
    return argparse.Namespace(
        pcb_cmd="copper-report",
        path=str(path),
        slug="cli-epro",
        board=None,
        database_url=None,
    )


def _a_report() -> copper_report.CopperReport:
    from precis.pcb.capabilities import capability_for
    from precis.pcb.rules import resolve_net_rules

    cap = capability_for("4layer")

    def track(net: str, y: float, width: float) -> dict[str, object]:
        return {
            "ctype": "track",
            "layer": "F.Cu",
            "net": net,
            "segments": [{"shape": "line", "start": [0.0, y], "end": [5.0, y]}],
            "width_mm": width,
        }

    copper = [track("PWR", 0.0, 1.0), track("SIG", 3.0, 0.15), track("SIG", 6.0, 0.15)]
    rules = {
        n: resolve_net_rules("", layer_is_outer=True, fab_caps=cap)
        for n in ("PWR", "SIG")
    }
    return copper_report.report(copper, [], ["F.Cu", "B.Cu"], rules, cap)


def test_a_routable_plane_is_labelled_as_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], epro_file
) -> None:
    import dataclasses

    stackup: list[dict[str, Any]] = [
        {"name": "F.Cu", "role": "signal"},
        {"name": "In1.Cu", "role": "plane", "plane_net": "GND", "routable": True},
        {"name": "In2.Cu", "role": "plane", "plane_net": "VCC"},
        {"name": "B.Cu", "role": "signal"},
    ]
    _stub(monkeypatch, dataclasses.replace(_OK, stackup=stackup))
    pcb_cli.run(_args(epro_file))
    assert (
        "stackup: F.Cu, In1.Cu (plane, routable), In2.Cu (plane), B.Cu"
        in capsys.readouterr().out
    )


def test_copper_report_prints_every_finding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], epro_file
) -> None:
    _report_stub(monkeypatch, _a_report())
    pcb_cli.run(_report_args(epro_file))
    out = capsys.readouterr().out
    assert "copper-report: 'cli-epro'" in out
    assert "! source widens to 1.000 mm" in out


def test_copper_report_on_a_missing_slug_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], epro_file
) -> None:
    _report_stub(monkeypatch, EproImportError("no pcb 'cli-epro'"))
    with pytest.raises(SystemExit) as exc:
        pcb_cli.run(_report_args(epro_file))
    assert exc.value.code == pcb_cli.EXIT_REFUSED
    captured = capsys.readouterr()
    assert "no pcb" in captured.err
    assert captured.out == ""


def test_an_update_prints_what_it_did_and_what_it_left(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], epro_file
) -> None:
    import dataclasses

    from precis.ingest.pcb_epro import UpdatePlan

    plan = UpdatePlan(
        moved=[("R1", (0.0, 0.0, 0.0, "top"), (1.0, 0.0, 90.0, "top"), False)],
        added=["R2"],
        removed=["U1"],
        rewired=["R1.1: board GND, source SIG"],
    )
    _stub(monkeypatch, dataclasses.replace(_OK, update=plan))
    pcb_cli.run(_args(epro_file, update=True))
    out = capsys.readouterr().out
    assert "updated 'cli-epro'" in out
    assert "moved R1" in out
    assert "added R2" in out
    assert "kept U1" in out
    assert "differs R1.1: board GND, source SIG" in out


def test_an_import_prints_its_copper_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], epro_file
) -> None:
    import dataclasses

    _stub(monkeypatch, dataclasses.replace(_OK, copper=_a_report()))
    pcb_cli.run(_args(epro_file))
    assert "! source widens to 1.000 mm" in capsys.readouterr().out
