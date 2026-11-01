"""gr523401: a finished run carries trace provenance without manual plumbing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from precis.cli import pcb as pcb_cli
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.ingest.pcb_workspace import import_pcb_run
from precis.response import Response
from precis.store import Store
from precis.workers.executors import _sandbox_harvest as harvest
from precis_geom import trace
from precis_geom.pcb import write_manifest
from tests.workspaces._script import load_script


def _run(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    mod = load_script(
        tmp_path,
        """\
from precis_geom.pcb import Board

def build():
    board = Board("auto-trace")
    board.part("U1", pins=["1"])
    board.hole(1, 1, drill=2, ring=3)
    return board
""",
    )
    with trace.run(work_root=tmp_path):
        write_manifest(mod.build(), out)
    return out


def test_local_run_import_automatically_attaches_trace(
    store: Store, tmp_path: Path
) -> None:
    out = _run(tmp_path)
    import_pcb_run(store, out)
    body = (
        PcbHandler(hub=Hub(store=store))
        .get(id="auto-trace", view="frames", args={"refdes": "U1"})
        .body
    )
    assert "pcb.Board.part" in body
    assert "main.py:" in body
    ref = store.get_ref(kind="pcb", id="auto-trace")
    assert ref is not None
    assert ref.meta["generated"]["trace"]["root"]
    assert ref.meta["generated"]["trace"]["run_job"] is None


def test_run_envelope_attaches_job_and_rejects_mix_before_writing(
    store: Store, tmp_path: Path
) -> None:
    out = _run(tmp_path)
    files = {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}
    handler = PcbHandler(hub=Hub(store=store))
    job = store.insert_ref(kind="job", slug=None, title="run", meta={})
    with pytest.raises(BadInput, match="cannot mix"):
        handler.put(id="bad", args={"run": files, "components": []})
    assert store.get_ref(kind="pcb", id="bad") is None
    handler.put(id="envelope", args={"run": files, "run_job": job.id})
    ref = store.get_ref(kind="pcb", id="envelope")
    assert ref is not None
    assert ref.meta["generated"]["trace"]["run_job"] == job.id
    assert (
        "pcb.Board.part"
        in handler.get(id="envelope", view="frames", args={"refdes": "U1"}).body
    )


@pytest.mark.parametrize(
    "broken", ["missing", "invalid", "failed", "requests", "op", "symlink"]
)
def test_bad_run_refuses_before_board_mutation(
    store: Store, tmp_path: Path, broken: str
) -> None:
    out = _run(tmp_path)
    if broken == "missing":
        (out / "trace.json").unlink()
    elif broken == "invalid":
        (out / "trace.json").write_text("{}", encoding="utf-8")
    elif broken == "symlink":
        source = out / "trace.json"
        source.rename(tmp_path / "trace.json")
        source.symlink_to(tmp_path / "trace.json")
    else:
        path = out / "manifest.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        if broken == "failed":
            doc["status"] = "failed"
        elif broken == "requests":
            doc["requests"] = [{"kind": "place"}]
        else:
            doc["board"]["op"] = "move"
        path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(BadInput):
        import_pcb_run(store, out)
    assert store.get_ref(kind="pcb", id="auto-trace") is None


@pytest.mark.parametrize("succeeded", [True, False])
def test_sandbox_harvest_binds_trace_to_producing_job_only_on_success(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, succeeded: bool
) -> None:
    monkeypatch.delenv("PRECIS_ROOT", raising=False)
    _run(tmp_path)
    job = store.insert_ref(kind="job", slug=None, title="sandbox", meta={})
    result = harvest.harvest_out(
        store,
        job_ref_id=job.id,
        container_name="pcb-run",
        work_dir=tmp_path,
        image="fixture",
        model="",
        artifact_store_root=tmp_path / "artifacts",
        succeeded=succeeded,
    )
    ref = store.get_ref(kind="pcb", id="auto-trace")
    if not succeeded:
        assert ref is None
        return
    assert ref is not None
    assert ref.meta["generated"]["trace"]["run_job"] == job.id
    body = (
        PcbHandler(hub=Hub(store=store))
        .get(id="auto-trace", view="frames", args={"refdes": "U1"})
        .body
    )
    assert "pcb.Board.part" in body
    assert "PCB run imported:" in harvest.summarize(result)


def test_identical_run_does_not_duplicate_features_and_changed_run_refuses(
    store: Store, tmp_path: Path
) -> None:
    out = _run(tmp_path)
    import_pcb_run(store, out)
    import_pcb_run(store, _run(tmp_path))
    ref = store.get_ref(kind="pcb", id="auto-trace")
    assert ref is not None
    assert len(store.pcb_features_list(ref.id)) == 1
    path = out / "manifest.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["board"]["components"].append({"refdes": "U2", "pins": [{"name": "1"}]})
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(BadInput, match="does not replace"):
        import_pcb_run(store, out)
    assert len(store.pcb_graph(ref.id)["instances"]) == 1
    assert len(store.pcb_features_list(ref.id)) == 1


def test_sandbox_import_refusal_keeps_artifacts_and_is_in_summary(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_ROOT", raising=False)
    out = _run(tmp_path)
    (out / "trace.json").unlink()
    job = store.insert_ref(kind="job", slug=None, title="sandbox", meta={})
    result = harvest.harvest_out(
        store,
        job_ref_id=job.id,
        container_name="pcb-run",
        work_dir=tmp_path,
        image="fixture",
        model="",
        artifact_store_root=tmp_path / "artifacts",
    )
    assert result.artifact is not None
    assert result.folder_ref_id is not None
    assert "PCB run import refused:" in harvest.summarize(result)
    assert store.get_ref(kind="pcb", id="auto-trace") is None


@pytest.mark.parametrize("refused", [False, True])
def test_local_cli_passes_the_whole_run_and_reports_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    refused: bool,
) -> None:
    import precis.ingest.pcb_workspace as intake

    calls: list[tuple[Path, str | None, int | None]] = []
    closed: list[bool] = []

    class CliStore:
        def close(self) -> None:
            closed.append(True)

    def run_import(
        store: Any, out: Path, *, slug: str | None, run_job: int | None
    ) -> Response:
        assert isinstance(store, CliStore)
        calls.append((out, slug, run_job))
        if refused:
            raise BadInput("trace missing", next="supply out/trace.json")
        return Response(body="imported with trace")

    monkeypatch.setattr(intake, "import_pcb_run", run_import)
    monkeypatch.setattr(Store, "connect", lambda _dsn: CliStore())
    monkeypatch.setattr(pcb_cli, "resolve_dsn", lambda _override: "unused")
    parser = argparse.ArgumentParser()
    pcb_cli.add_parser(parser.add_subparsers())
    args = parser.parse_args(
        [
            "pcb",
            "import-run",
            str(tmp_path),
            "--slug",
            "preview",
            "--run-job",
            "7",
        ]
    )
    if refused:
        with pytest.raises(SystemExit) as caught:
            pcb_cli.run(args)
        assert caught.value.code == pcb_cli.EXIT_REFUSED
        captured = capsys.readouterr()
        assert "trace missing" in captured.err
        assert captured.out == ""
    else:
        pcb_cli.run(args)
        assert "imported with trace" in capsys.readouterr().out
    assert calls == [(tmp_path, "preview", 7)]
    assert closed == [True]
