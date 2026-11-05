"""Portable verification reads archived evidence without executing the engine."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import stat
from pathlib import Path
from typing import Any
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_LZMA, ZipFile, ZipInfo

import pytest

from precis_pathway import cli, revalidate_job
from precis_pathway import manifest as vm
from precis_pathway.verification import append_report, build_report, verify_archive

MID = "a" * 64
ENGINE_SHA = "b" * 40
LOCK_SHA = "c" * 64
WEIGHTS_SHA = "d" * 64


def _zip(path: Path, files: dict[str, bytes], *, prefix: str = "") -> Path:
    manifest = "".join(
        f"{hashlib.sha256(body).hexdigest()}  {name}\n"
        for name, body in sorted(files.items())
    )
    with ZipFile(path, "w") as zf:
        zf.writestr(f"{prefix}MANIFEST.sha256", manifest)
        for name, body in files.items():
            zf.writestr(f"{prefix}{name}", body)
    return path


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return _zip(tmp_path / "run.zip", {"outputs/results.json": b'{"rate": 1}'})


@pytest.fixture
def meta() -> dict[str, Any]:
    platform = {"arch": "arm64", "os": "darwin", "accelerator": "cpu"}
    manifest = {
        "schema": "validation_manifest",
        "schema_version": 1,
        "manifest_id": MID,
        "code": {"autocatpath_version": "0.38.0", "autocatpath_sha": ENGINE_SHA},
        "lock": {
            "uv_lock_sha256": LOCK_SHA,
            "lock_source": "uv.lock",
            "mlip": [
                {"backend": "mace", "model": "medium", "weights_sha256": WEIGHTS_SHA}
            ],
        },
        "platform": platform,
        "tolerances": [
            {
                "observable": "tof_chain",
                "unit": "decades",
                "same_platform": {"rule": "log10", "value": 0.01},
                "cross_platform": {"rule": "log10", "value": 0.05},
            }
        ],
    }
    run = {
        "manifest_id": MID,
        "at": "2026-11-05T00:00:00Z",
        "job_id": 17,
        "verdict": "reproduced_partial(energies)",
        "complete": False,
        "column": "same_platform",
        "platform_then": platform,
        "platform_now": platform,
        "not_run": ["energies"],
        "not_run_why": vm.NOT_RUN_LEGS.copy(),
        "caveats": [],
        "observables": [
            {
                "observable": "tof_chain",
                "ok": True,
                "delta": 0.005,
                "tolerance": 0.01,
                "rule": "log10",
            }
        ],
    }
    return {
        "validation_manifest": manifest,
        "revalidation": {MID: [run]},
        "results": {"trust_summary": {"blockers": ["neb_convergence"]}},
    }


def _row(report: dict[str, Any], scope: str, check: str) -> dict[str, Any]:
    return next(
        r for r in report["checks"] if r["scope"] == scope and r["check"] == check
    )


def test_hash_pass_and_successful_cpu_execution_do_not_promote_claim_support(
    archive: Path,
    meta: dict[str, Any],
) -> None:
    report = build_report(archive, meta, envelope_id="opaque:future-envelope-format")
    assert _row(report, "integrity", "outputs/results.json")["status"] == "passed"
    assert _row(report, "execution", "D10")["status"] == "passed"
    assert _row(report, "numerical_agreement", "tof_chain")["status"] == "passed"
    support = _row(report, "claim_support", "scientific-support")
    assert support["status"] == "unavailable"
    assert support["observed"]["trust_summary"]["blockers"] == ["neb_convergence"]
    assert "review" in support["method"]
    assert report["envelope_id"] == "opaque:future-envelope-format"
    assert all(r["envelope_id"] == report["envelope_id"] for r in report["checks"])
    assert all(r["schema_version"] == 1 and r["evidence"] for r in report["checks"])


def test_cpu_replay_stays_incomplete_with_energy_leg_not_run(
    archive: Path,
    meta: dict[str, Any],
) -> None:
    report = build_report(archive, meta)
    execution = _row(report, "execution", "D10")
    assert execution["observed"]["complete"] is False
    assert execution["observed"]["verdict"] == "reproduced_partial(energies)"
    assert _row(report, "execution", "energies")["status"] == "not-run"
    assert "validated: NO" in report["recorded_manifest_text"]
    assert "GPU seed lane" in report["recorded_manifest_text"]


def test_missing_engine_weights_and_commit_are_explicit(
    archive: Path,
    meta: dict[str, Any],
) -> None:
    meta["validation_manifest"]["code"]["autocatpath_sha"] = None
    meta["validation_manifest"]["lock"]["mlip"][0]["weights_sha256"] = None
    meta["revalidation"][MID][0]["caveats"] = ["engine-unstamped-then"]
    report = build_report(archive, meta)
    assert _row(report, "prerequisites", "engine")["status"] == "unavailable"
    weights = _row(report, "prerequisites", "weights:mace:medium")
    assert weights["status"] == "unavailable"
    assert weights["observed"]["expected"] is None
    assert "does not check or load" in weights["observed"]["note"]
    assert "ENGINE COMMIT NOT STAMPED" in report["recorded_manifest_text"]
    assert "not located when stamped" in report["recorded_manifest_text"]
    assert _row(report, "execution", "D10")["observed"]["caveats"] == [
        "engine-unstamped-then"
    ]


@pytest.mark.parametrize("lock", [(LOCK_SHA, "constraints.txt"), ("f" * 64, "uv.lock")])
def test_incompatible_lock_source_or_hash_fails_explicitly(
    archive: Path,
    meta: dict[str, Any],
    lock: tuple[str, str],
) -> None:
    report = build_report(archive, meta, lock=lock)
    check = _row(report, "prerequisites", "lock")
    assert check["status"] == "failed"
    assert check["observed"]["actual"] == lock
    assert check["observed"]["expected"] == (LOCK_SHA, "uv.lock")


def test_matching_stamps_do_not_establish_scientific_support(
    archive: Path,
    meta: dict[str, Any],
) -> None:
    report = build_report(
        archive,
        meta,
        engine=("0.38.0", ENGINE_SHA),
        lock=(LOCK_SHA, "uv.lock"),
        weights={"mace:medium": WEIGHTS_SHA},
    )
    assert all(
        r["status"] == "passed"
        for r in report["checks"]
        if r["scope"] == "prerequisites"
    )
    assert (
        _row(report, "claim_support", "scientific-support")["status"] == "unavailable"
    )


def test_in_and_out_of_tolerance_recorded_attempts_remain_distinct(
    archive: Path,
    meta: dict[str, Any],
) -> None:
    drift = copy.deepcopy(meta["revalidation"][MID][0])
    drift.update(job_id=18, verdict="drifted(tof_chain)")
    drift["observables"][0].update(ok=False, delta=0.03)
    meta["revalidation"][MID].append(drift)
    report = build_report(archive, meta)
    rows = [r for r in report["checks"] if r["scope"] == "numerical_agreement"]
    assert [r["status"] for r in rows] == ["passed", "failed"]
    assert [r["evidence"]["job_id"] for r in rows] == [17, 18]
    assert [r["observed"]["delta"] for r in rows] == pytest.approx([0.005, 0.03])
    assert all(r["tolerance"]["value"] == pytest.approx(0.01) for r in rows)
    assert all(r["platform"]["column"] == "same_platform" for r in rows)
    assert all("autocatpath.manifest.compare" in r["method"] for r in rows)
    executions = [
        r for r in report["checks"] if r["scope"] == "execution" and r["check"] == "D10"
    ]
    assert [r["status"] for r in executions] == ["passed", "passed"]


def test_missing_records_are_unavailable_not_a_silent_pass(archive: Path) -> None:
    report = build_report(archive, {})
    assert _row(report, "execution", "D10")["status"] == "unavailable"
    assert _row(report, "numerical_agreement", "D10")["status"] == "unavailable"
    assert _row(report, "prerequisites", "weights")["status"] == "unavailable"


def test_report_never_launches_compute_or_changes_frozen_evidence(
    archive: Path,
    meta: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline reporting attempted engine work")

    monkeypatch.setattr(revalidate_job, "revalidate", forbidden)
    monkeypatch.setattr(revalidate_job, "rerun_cpu_legs", forbidden)
    monkeypatch.setattr(vm, "engine_manifest", forbidden)
    before_meta = copy.deepcopy(meta)
    before_zip = archive.read_bytes()
    report = build_report(archive, meta)
    first = append_report(report, tmp_path / "reports")
    first_bytes = first.read_bytes()
    second = append_report(report, tmp_path / "reports")
    assert first != second
    assert first.read_bytes() == first_bytes
    assert json.loads(first.read_text(encoding="utf-8")) == json.loads(
        second.read_text(encoding="utf-8")
    )
    assert json.loads(first.read_text(encoding="utf-8"))["compute_launched"] is False
    assert archive.read_bytes() == before_zip
    assert meta == before_meta
    _row(report, "execution", "D10")["observed"]["caveats"].append("report-only")
    assert meta == before_meta


def test_enclosing_directory_and_binary_checksum_marker_are_supported(
    tmp_path: Path,
) -> None:
    archive = _zip(tmp_path / "nested.zip", {"results.json": b"{}"}, prefix="compound/")
    assert {r["status"] for r in verify_archive(archive)} == {"passed"}
    binary = tmp_path / "binary.zip"
    with ZipFile(binary, "w") as zf:
        zf.writestr("results.json", b"{}")
        zf.writestr(
            "MANIFEST.sha256", f"{hashlib.sha256(b'{}').hexdigest()} *results.json\n"
        )
    assert {r["status"] for r in verify_archive(binary)} == {"passed"}


@pytest.mark.parametrize(
    "manifest",
    [
        "",
        "not a checksum\n",
        f"{'a' * 64}  ../outside\n",
        f"{'a' * 64}  MANIFEST.sha256\n",
    ],
)
def test_malformed_empty_and_unsafe_checksum_entries_fail(
    tmp_path: Path, manifest: str
) -> None:
    archive = tmp_path / "bad.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("MANIFEST.sha256", manifest)
    assert any(r["status"] == "failed" for r in verify_archive(archive))


def test_missing_changed_and_unlisted_files_are_distinct_failures(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "mismatch.zip"
    digest = hashlib.sha256(b"original").hexdigest()
    with ZipFile(archive, "w") as zf:
        zf.writestr("MANIFEST.sha256", f"{digest}  changed\n{digest}  missing\n")
        zf.writestr("changed", b"modified")
        zf.writestr("unlisted", b"original")
    rows = {r["check"]: r for r in verify_archive(archive)}
    assert {r["status"] for r in rows.values()} == {"failed"}
    assert rows["changed"]["observed"]["actual"] != digest
    assert "missing" in rows["missing"]["observed"]
    assert "no checksum" in rows["unlisted"]["observed"]


def test_duplicate_archive_members_fail_even_when_bytes_match(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "duplicate.zip", {"result": b"ok"})
    with ZipFile(archive, "a") as zf, pytest.warns(UserWarning, match="Duplicate name"):
        zf.writestr("result", b"ok")
    assert any(
        r["status"] == "failed" and "duplicate" in r["observed"]
        for r in verify_archive(archive)
    )


@pytest.mark.parametrize(
    "name", ["../outside", "/outside", "a/../outside", "a\\outside"]
)
def test_archive_traversal_members_are_rejected_without_extraction(
    tmp_path: Path, name: str
) -> None:
    archive = _zip(tmp_path / "unsafe.zip", {name: b"unsafe"})
    rows = verify_archive(archive)
    assert any(r["status"] == "failed" and "unsafe" in r["observed"] for r in rows)
    assert not (tmp_path / "outside").exists()


def test_symlink_members_are_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    link = ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive, "w") as zf:
        zf.writestr(link, "outside")
        zf.writestr(
            "MANIFEST.sha256", f"{hashlib.sha256(b'outside').hexdigest()}  link\n"
        )
    assert any(
        r["status"] == "failed" and "unsafe" in r["observed"]
        for r in verify_archive(archive)
    )


def test_missing_archive_or_manifest_is_unavailable(tmp_path: Path) -> None:
    assert verify_archive(tmp_path / "absent.zip")[0]["status"] == "unavailable"
    empty = tmp_path / "empty.zip"
    with ZipFile(empty, "w"):
        pass
    assert verify_archive(empty)[0]["status"] == "unavailable"


@pytest.mark.parametrize(
    ("compression", "damage_offset", "damage_byte"),
    [
        pytest.param(ZIP_DEFLATED, 0, 0x07, id="deflate-reserved-block"),
        pytest.param(ZIP_BZIP2, 0, 0x00, id="bzip2-invalid-magic"),
        pytest.param(ZIP_LZMA, 4, 0xFF, id="lzma-invalid-properties"),
    ],
)
@pytest.mark.parametrize("member_name", ["result", "MANIFEST.sha256"])
def test_corrupt_compressed_member_is_a_failed_check(
    tmp_path: Path,
    compression: int,
    damage_offset: int,
    damage_byte: int,
    member_name: str,
) -> None:
    """Valid ZIP headers must not hide a decompression failure from the report."""
    archive = tmp_path / "corrupt.zip"
    body = b"payload" * 50
    digest = hashlib.sha256(body).hexdigest()
    with ZipFile(archive, "w", compression=compression) as zf:
        zf.writestr("result", body)
        zf.writestr("z-result", body)
        zf.writestr("MANIFEST.sha256", f"{digest}  result\n{digest}  z-result\n")
        info = zf.getinfo(member_name)
        start = (
            info.header_offset
            + 30
            + len(info.filename.encode("utf-8"))
            + len(info.extra)
        )
    damaged = bytearray(archive.read_bytes())
    # Corrupt only compressed data, leaving the ZIP headers intact.
    damaged[start + damage_offset] = damage_byte
    archive.write_bytes(damaged)
    report = build_report(archive, {})
    assert _row(report, "integrity", member_name)["status"] == "failed"
    if member_name == "result":
        assert _row(report, "integrity", "z-result")["status"] == "passed"
    path = append_report(report, tmp_path / "reports")
    assert path.is_file()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert _row(saved, "integrity", member_name)["status"] == "failed"
    assert archive.read_bytes() == bytes(damaged)


@pytest.mark.parametrize("error", [EOFError, NotImplementedError])
@pytest.mark.parametrize("member_name", ["result", "MANIFEST.sha256"])
def test_unreadable_member_is_a_failed_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
    member_name: str,
) -> None:
    archive = _zip(tmp_path / "unreadable.zip", {"result": b"bad", "z-result": b"ok"})
    original_open = ZipFile.open

    def unreadable(zf: ZipFile, name: Any, *args: Any, **kwargs: Any) -> Any:
        if name == member_name:
            raise error("member cannot be decompressed")
        return original_open(zf, name, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "open", unreadable)
    report = build_report(archive, {})
    row = _row(report, "integrity", member_name)
    assert row["status"] == "failed"
    assert row["observed"] == "member cannot be decompressed"
    if member_name == "result":
        assert _row(report, "integrity", "z-result")["status"] == "passed"


@pytest.fixture
def offline_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("verification CLI attempted database or engine work")

    monkeypatch.setattr(cli, "_open_store", forbidden)
    monkeypatch.setattr(revalidate_job, "revalidate", forbidden)
    monkeypatch.setattr(revalidate_job, "rerun_cpu_legs", forbidden)
    monkeypatch.setattr(vm, "engine_manifest", forbidden)


def _cli_args(archive: Path, record: Path, output: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers())
    return parser.parse_args(
        [
            "pathway",
            "verify-report",
            str(archive),
            "--record",
            str(record),
            "--output-dir",
            str(output),
            "--envelope-id",
            "opaque:cli-envelope",
        ]
    )


def test_cli_appends_reports_without_database_compute_or_input_mutation(
    archive: Path,
    meta: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    offline_cli: None,
) -> None:
    record = tmp_path / "frozen-meta.json"
    record.write_text(json.dumps(meta), encoding="utf-8")
    frozen_record = record.read_bytes()
    frozen_archive = archive.read_bytes()
    output = tmp_path / "reports"
    args = _cli_args(archive, record, output)
    cli.run(args)
    first = Path(capsys.readouterr().out.strip())
    assert first.parent == output
    first_bytes = first.read_bytes()
    report = json.loads(first.read_text(encoding="utf-8"))
    assert report["compute_launched"] is False
    assert report["envelope_id"] == "opaque:cli-envelope"
    assert _row(report, "execution", "energies")["status"] == "not-run"
    cli.run(args)
    second = Path(capsys.readouterr().out.strip())
    assert second != first and second.is_file()
    assert first.read_bytes() == first_bytes
    assert archive.read_bytes() == frozen_archive
    assert record.read_bytes() == frozen_record


@pytest.mark.parametrize("kind", ["bad", "missing", "missing-manifest"])
def test_cli_writes_failed_integrity_report_then_exits_one(
    tmp_path: Path,
    meta: dict[str, Any],
    kind: str,
    capsys: pytest.CaptureFixture[str],
    offline_cli: None,
) -> None:
    archive = tmp_path / "run.zip"
    if kind == "bad":
        archive.write_bytes(b"not a zip archive")
    elif kind == "missing-manifest":
        with ZipFile(archive, "w") as zf:
            zf.writestr("result", b"ok")
    before_archive = archive.read_bytes() if archive.exists() else None
    record = tmp_path / "frozen-meta.json"
    record.write_text(json.dumps(meta), encoding="utf-8")
    before_record = record.read_bytes()
    output = tmp_path / "reports"
    with pytest.raises(SystemExit) as exc:
        cli.run(_cli_args(archive, record, output))
    assert exc.value.code == 1
    report_path = Path(capsys.readouterr().out.strip())
    assert report_path.parent == output
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["compute_launched"] is False
    assert any(
        r["scope"] == "integrity" and r["status"] in {"failed", "unavailable"}
        for r in report["checks"]
    )
    assert record.read_bytes() == before_record
    if before_archive is None:
        assert not archive.exists()
    else:
        assert archive.read_bytes() == before_archive
