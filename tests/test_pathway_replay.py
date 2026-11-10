"""External replay acceptance harness (gr564215).

The always-run half: a synthetic CPU fixture bundle in tmp_path exercises
integrity, staging (weights and lock pins), the network guard, coverage and
the machine-readable report without the engine — the packaging check the
acceptance contract asks for on every run. Several tests run the fixture's
real ``reproduce.sh`` as an isolated subprocess (the default runner), so
the entrypoint path is covered end to end, not only through injected
callbacks. The engine half (below, skipped without the ``catalyst``
extra) replays the trimmed pw455722 case and pins the D10 tolerance
judgments; it never rejudges tolerances itself, mirroring
test_pathway_revalidate.py.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import socket
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from precis_pathway import manifest as vm
from precis_pathway import replay
from precis_pathway.replay import NetworkAttempt, ReplayRefused
from precis_pathway.replay import replay as run_replay

WEIGHTS_BLOB = b"fake MACE checkpoint"
WEIGHTS_SHA = hashlib.sha256(WEIGHTS_BLOB).hexdigest()
LOCK_TEXT = "version = 1\n"
LOCK_SHA = hashlib.sha256(LOCK_TEXT.encode("utf-8")).hexdigest()

RECORD: dict[str, Any] = {
    "manifest_id": "a" * 64,
    "code": {"precis_sha": "p" * 40, "autocatpath_version": "0.50.0"},
    "lock": {"uv_lock_sha256": LOCK_SHA, "lock_source": "uv.lock", "mlip": []},
    "tolerances": [],
}

SHIPPED_RESULTS: dict[str, Any] = {"nodes": {"*": {"energy": 0.0}}, "edges": []}
#: Fresh results carry every leg's outputs; the entrypoint declares the
#: legs it ran in ``out/legs.json`` — ``nodes`` alone would be copied-
#: through data, not proof the energies leg executed.
FRESH_RESULTS: dict[str, Any] = {
    **copy.deepcopy(SHIPPED_RESULTS),
    "kinetics_tpd": {"tpd": {"peaks": 1}},
    "kinetics_v2": {"tof_chain": 1.0},
    "kinetics_sweep": {"points": 2},
}
FRESH_LEGS: tuple[str, ...] = ("energies", "tpd", "kinetics", "sweep")

#: The fixture entrypoint: a real sh script that folds the staged inputs
#: into fresh observables, declares its executed legs, echoes a log marker
#: and reports whether a canary host variable leaked into its environment.
REPRODUCE_SH = b"""#!/bin/sh
set -eu
out="${REPLAY_OUT:?}"
inputs="${REPLAY_INPUTS:?}"
bytes=$(wc -c < "$inputs/slab.extxyz" | tr -d ' ')
cat > "$out/results.json" <<JSON
{"nodes": {"*": {"energy": 0.0}}, "edges": [], "kinetics_v2": {"tof_chain": 1.0}, "kinetics_tpd": {"tpd": {"peaks": 1}}, "kinetics_sweep": {"points": 2}, "slab_bytes": $bytes, "canary": "${PRECIS_REPLAY_CANARY:-scrubbed}"}
JSON
printf '%s\\n' '["energies", "tpd", "kinetics", "sweep"]' > "$out/legs.json"
echo "fixture-ran slab_bytes=$bytes"
"""


def _write_manifest(bundle: Path, files: dict[str, bytes]) -> None:
    lines = []
    for rel in sorted(files):
        lines.append(f"{hashlib.sha256(files[rel]).hexdigest()}  {rel}")
    (bundle / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rehash(bundle: Path, *rels: str) -> None:
    """Re-stamp the manifest after a test edits a file, so the leg under
    test is the staging/pin check rather than the checksum."""
    lines = [
        ln
        for ln in (bundle / "MANIFEST.sha256").read_text("utf-8").splitlines()
        if PurePosixPath(ln.split("  ", 1)[1]).as_posix() not in rels
    ]
    for rel in rels:
        lines.append(
            f"{hashlib.sha256((bundle / rel).read_bytes()).hexdigest()}  {rel}"
        )
    (bundle / "MANIFEST.sha256").write_text(
        "\n".join(sorted(lines)) + "\n", encoding="utf-8"
    )


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """The small public CPU fixture: an EMT-class bundle, all pins satisfied."""
    root = tmp_path / "bundle"
    root.mkdir()
    files: dict[str, bytes] = {
        "README.md": b"# fixture bundle\n",
        "reproduce.sh": REPRODUCE_SH,
        "seeds.json": json.dumps({"search_seeds": [0]}).encode("utf-8"),
        "coverage.json": json.dumps(
            {
                "claims": [
                    {
                        "claim": "fixture-tof",
                        "runs": ["fixture-run-1"],
                        "required_legs": ["energies", "kinetics"],
                        "acceptance_test": "tests/test_pathway_replay.py",
                    }
                ]
            }
        ).encode("utf-8"),
        "env/pyproject.toml": b"[project]\nname='fixture'\n",
        "env/uv.lock": LOCK_TEXT.encode("utf-8"),
        "env/weights.json": json.dumps(
            {
                "backend": "mace",
                "model": "medium",
                "file": "env/weights/medium.model",
                "sha256": WEIGHTS_SHA,
                "licence": "CC-BY-4.0",
            }
        ).encode("utf-8"),
        "env/weights/medium.model": WEIGHTS_BLOB,
        "env/platform.json": json.dumps({"os": "linux", "arch": "x86_64"}).encode(
            "utf-8"
        ),
        "manifest/validation_manifest.json": json.dumps(RECORD).encode("utf-8"),
        "inputs/slab.extxyz": b"1\nfixture slab\n",
        "outputs/results.json": json.dumps(SHIPPED_RESULTS).encode("utf-8"),
        "provenance/build.json": json.dumps(
            {"image_digest": "sha256:" + "f" * 64}
        ).encode("utf-8"),
    }
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    _write_manifest(root, files)
    return root


def _fake_runner(
    fresh: dict[str, Any] | None = None,
    legs: tuple[str, ...] = FRESH_LEGS,
):
    def run(inputs_dir: Path, out_dir: Path, env: Any) -> None:
        assert inputs_dir.is_dir()
        assert out_dir.is_dir()
        assert env["UV_OFFLINE"] == "1"
        results = FRESH_RESULTS if fresh is None else fresh
        (out_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
        (out_dir / "legs.json").write_text(json.dumps(list(legs)), encoding="utf-8")

    return run


def _fake_judge(verdict: str = "reproduced", not_run: tuple[str, ...] = ()):
    def judge(
        shipped: Any, fresh: Any, record: Any, platform_now: Any
    ) -> dict[str, Any]:
        return {
            "report": {
                "verdict": verdict,
                "complete": not not_run,
                "observables": [
                    {
                        "observable": "tof_chain",
                        "ok": True,
                        "rule": "decades",
                        "tolerance": 0.01,
                    }
                ],
            },
            "column": "same_platform",
            "not_run": not_run,
            "platform_now": {"arch": "x86_64"},
        }

    return judge


def test_a_clean_bundle_replays_and_reports_every_contract_field(
    bundle: Path, tmp_path: Path
) -> None:
    report = run_replay(
        bundle,
        work_dir=tmp_path / "work",
        run=_fake_runner(),
        judge=_fake_judge(),
    )
    assert report["schema"] == "pathway_replay_report"
    assert report["verdict"] == "reproduced" and report["complete"] is True
    assert (
        report["bundle"]["bundle_id"]
        == hashlib.sha256((bundle / "MANIFEST.sha256").read_bytes()).hexdigest()
    )
    assert report["ids"]["manifest_id"] == RECORD["manifest_id"]
    assert report["ids"]["lock"]["staged_uv_lock_sha256"] == LOCK_SHA
    assert report["image_digest"] == "sha256:" + "f" * 64
    assert report["commands"][0]["argv"] == ["sh", "reproduce.sh"]
    assert report["commands"][0]["exit_code"] == 0
    assert report["commands"][0]["network_guard"] == "in-process socket guard"
    scopes = {row["scope"] for row in report["checks"]}
    assert {
        "integrity",
        "execution",
        "numerical_agreement",
        "claim_support",
        "coverage",
    } <= scopes
    assert report["legs"] == {
        leg: {"status": "passed", "why": None} for leg in replay.ALL_LEGS
    }
    support = next(r for r in report["checks"] if r["scope"] == "claim_support")
    assert support["status"] == "unavailable"
    cov = next(r for r in report["checks"] if r["scope"] == "coverage")
    assert cov["status"] == "passed" and cov["observed"]["acceptance_test"].endswith(
        "test_pathway_replay.py"
    )


def test_staged_inputs_are_read_only_and_shipped_outputs_never_reused(
    bundle: Path, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def run(inputs_dir: Path, out_dir: Path, env: Any) -> None:
        seen["inputs"] = inputs_dir
        seen["out"] = out_dir
        # the runner's contract: staged inputs and a fresh out dir; the
        # harness never hands it the shipped references as fresh results
        assert inputs_dir.name == "inputs" and out_dir.name == "out"
        assert not (out_dir / "results.json").exists()
        (out_dir / "results.json").write_text(
            json.dumps(FRESH_RESULTS), encoding="utf-8"
        )

    run_replay(bundle, work_dir=tmp_path / "work", run=run, judge=_fake_judge())
    slab = seen["inputs"] / "slab.extxyz"
    assert slab.exists() and not slab.stat().st_mode & 0o222
    assert seen["out"].name == "out"
    # shipped references never reach the entrypoint's working directory
    assert not (seen["out"].parent / "outputs").exists()
    # and the bundle's copy is untouched by the replay
    assert json.loads((bundle / "outputs" / "results.json").read_text("utf-8")) == (
        SHIPPED_RESULTS
    )


def test_a_tampered_file_refuses_before_any_compute(
    bundle: Path, tmp_path: Path
) -> None:
    (bundle / "inputs" / "slab.extxyz").write_bytes(b"tampered slab")
    ran: list[bool] = []
    report = run_replay(
        bundle,
        work_dir=tmp_path / "work",
        run=lambda *a: ran.append(True),
        judge=_fake_judge(),
    )
    assert report["verdict"].startswith("refused(integrity)")
    assert report["complete"] is False
    assert ran == []  # no compute on a modified bundle
    row = next(r for r in report["checks"] if r["check"] == "inputs/slab.extxyz")
    assert row["status"] == "failed" and row["method"] == "sha256"


def test_an_undeclared_extra_file_fails_the_manifest(
    bundle: Path, tmp_path: Path
) -> None:
    (bundle / "stray.py").write_text("print('hi')\n", encoding="utf-8")
    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=_fake_runner(), judge=_fake_judge()
    )
    assert report["verdict"].startswith("refused(integrity)")
    row = next(r for r in report["checks"] if r["check"] == "stray.py")
    assert row["observed"] == "file has no checksum"


def test_a_nested_undeclared_file_fails_the_manifest(
    bundle: Path, tmp_path: Path
) -> None:
    """Integrity recurses: an extra file deep in the tree is caught."""
    (bundle / "inputs" / "stray.py").write_text("print('hi')\n", encoding="utf-8")
    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=_fake_runner(), judge=_fake_judge()
    )
    assert report["verdict"].startswith("refused(integrity)")
    row = next(r for r in report["checks"] if r["check"] == "inputs/stray.py")
    assert row["observed"] == "file has no checksum"


def test_a_symlinked_member_fails_as_unsafe(bundle: Path, tmp_path: Path) -> None:
    """A symlink member is unsafe even undeclared: it is never followed or
    hashed, so nothing outside the bundle can stand in for a declared file."""
    outside = tmp_path / "outside.extxyz"
    outside.write_bytes(b"1\nfixture slab\n")
    (bundle / "inputs" / "linked.extxyz").symlink_to(outside)
    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=_fake_runner(), judge=_fake_judge()
    )
    assert report["verdict"].startswith("refused(integrity)")
    row = next(r for r in report["checks"] if r["check"] == "inputs/linked.extxyz")
    assert "symlink" in str(row["observed"])


def test_a_declared_member_replaced_by_a_symlink_fails(
    bundle: Path, tmp_path: Path
) -> None:
    """Even a declared path whose content would hash correctly fails when it
    is a symlink: the member itself is unsafe, whatever its target holds."""
    outside = tmp_path / "outside.extxyz"
    outside.write_bytes((bundle / "inputs" / "slab.extxyz").read_bytes())
    (bundle / "inputs" / "slab.extxyz").unlink()
    (bundle / "inputs" / "slab.extxyz").symlink_to(outside)
    _rehash(bundle, "inputs/slab.extxyz")  # same bytes — the hash would pass
    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=_fake_runner(), judge=_fake_judge()
    )
    assert report["verdict"].startswith("refused(integrity)")
    row = next(r for r in report["checks"] if r["check"] == "inputs/slab.extxyz")
    assert "symlink" in str(row["observed"])


def test_a_tampered_weight_refuses_naming_both_digests(
    bundle: Path, tmp_path: Path
) -> None:
    (bundle / "env" / "weights" / "medium.model").write_bytes(b"tampered checkpoint")
    _rehash(bundle, "env/weights/medium.model")
    ran: list[bool] = []
    report = run_replay(
        bundle,
        work_dir=tmp_path / "work",
        run=lambda *a: ran.append(True),
        judge=_fake_judge(),
    )
    assert report["verdict"].startswith("refused(staging)")
    assert WEIGHTS_SHA[:12] in report["verdict"]
    assert ran == []
    row = next(r for r in report["checks"] if r["check"] == "weights:mace:medium")
    assert row["status"] == "failed"


def test_hash_only_weights_stay_unavailable_and_never_fetched(
    bundle: Path, tmp_path: Path
) -> None:
    """A private-engine fetch recipe (hash, no file) cannot be attested
    offline: the leg is said so and the replay proceeds without fetching."""
    entry = json.loads((bundle / "env" / "weights.json").read_text("utf-8"))
    del entry["file"]
    body = json.dumps(entry).encode("utf-8")
    (bundle / "env" / "weights.json").write_bytes(body)
    lines = [
        ln
        for ln in (bundle / "MANIFEST.sha256").read_text("utf-8").splitlines()
        if not ln.endswith("env/weights.json")
    ]
    lines.append(f"{hashlib.sha256(body).hexdigest()}  env/weights.json")
    (bundle / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=_fake_runner(), judge=_fake_judge()
    )
    assert report["verdict"] == "reproduced"
    # an unattestable weight still withholds completeness — never a pass
    assert report["complete"] is False
    row = next(r for r in report["checks"] if r["check"] == "weights:mace:medium")
    assert row["status"] == "unavailable"


def test_a_missing_dependency_refuses_naming_the_path(
    bundle: Path, tmp_path: Path
) -> None:
    """A manifest edited to drop its lock line is checksum-consistent but
    cannot stage: the declared dependency is gone and nothing replaces it."""
    (bundle / "env" / "uv.lock").unlink()
    lines = [
        ln
        for ln in (bundle / "MANIFEST.sha256").read_text("utf-8").splitlines()
        if not ln.endswith("env/uv.lock")
    ]
    (bundle / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = run_replay(
        bundle,
        work_dir=tmp_path / "work",
        run=lambda *a: pytest.fail("compute ran on a broken bundle"),
        judge=_fake_judge(),
    )
    assert report["verdict"].startswith("refused(staging)")
    assert (
        "missing dependency" in report["verdict"] and "env/uv.lock" in report["verdict"]
    )


def test_an_unpinned_staged_lock_refuses_against_the_manifest_pin(
    bundle: Path, tmp_path: Path
) -> None:
    (bundle / "env" / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    _rehash(bundle, "env/uv.lock")
    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=_fake_runner(), judge=_fake_judge()
    )
    assert report["verdict"].startswith("refused(staging)")
    assert LOCK_SHA[:12] in report["verdict"]


def test_undeclared_service_access_fails_naming_the_address(
    bundle: Path, tmp_path: Path
) -> None:
    def run(inputs_dir: Path, out_dir: Path, env: Any) -> None:
        socket.create_connection(("db.internal", 5432), timeout=1)

    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=run, judge=_fake_judge()
    )
    assert report["verdict"] == "failed(network-access)"
    assert report["complete"] is False
    assert "db.internal" in str(report["commands"][0]["log"])
    row = next(r for r in report["checks"] if r["method"] == "network-guard")
    assert row["status"] == "failed"


def test_the_network_guard_blocks_raw_sockets_too() -> None:
    with pytest.raises(NetworkAttempt):
        with replay.network_disabled():
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_a_runner_that_writes_no_results_fails_actionably(
    bundle: Path, tmp_path: Path
) -> None:
    def run(inputs_dir: Path, out_dir: Path, env: Any) -> None:
        return None

    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=run, judge=_fake_judge()
    )
    assert report["verdict"] == "failed(no-results)"
    assert report["complete"] is False


def test_a_non_empty_work_directory_is_refused(bundle: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "leftover.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ReplayRefused, match="not empty"):
        replay.stage_bundle(bundle, work)


def test_malformed_manifests_refuse(bundle: Path) -> None:
    (bundle / "MANIFEST.sha256").write_text("not-a-checksum line\n", encoding="utf-8")
    rows = replay.check_integrity(bundle)
    assert rows[0]["status"] == "failed" and "malformed" in str(rows[0]["observed"])
    (bundle / "MANIFEST.sha256").write_text(
        f"{'a' * 64}  ../escape.txt\n", encoding="utf-8"
    )
    with pytest.raises(ReplayRefused, match="malformed"):
        replay.parse_manifest(bundle)


def test_legs_the_replay_did_not_run_stay_named_and_block_coverage(
    bundle: Path, tmp_path: Path
) -> None:
    cov = json.loads((bundle / "coverage.json").read_text("utf-8"))
    cov["claims"].append(
        {
            "claim": "paper-sweep",
            "runs": ["paper-run-1"],
            "required_legs": ["energies", "sweep"],
            "acceptance_test": "tests/test_pathway_replay.py",
        }
    )
    (bundle / "coverage.json").write_text(json.dumps(cov), encoding="utf-8")
    _rehash(bundle, "coverage.json")

    # the entrypoint declares only the legs it ran; their outputs exist
    fresh = {k: FRESH_RESULTS[k] for k in ("nodes", "edges", "kinetics_v2")}
    report = run_replay(
        bundle,
        work_dir=tmp_path / "work",
        run=_fake_runner(fresh, legs=("energies", "kinetics")),
        judge=_fake_judge("reproduced_partial(tpd,sweep)"),
    )
    assert report["verdict"] == "reproduced_partial(tpd,sweep)"
    assert report["complete"] is False
    for leg in ("tpd", "sweep"):
        assert report["legs"][leg]["status"] == "not-run"
        assert report["legs"][leg]["why"]
    row = next(
        r
        for r in report["checks"]
        if r["scope"] == "coverage" and r["check"] == "paper-sweep"
    )
    assert row["status"] == "not-run"
    assert set(row["observed"]["not_run"]) == {"sweep"}
    fixture = next(
        r
        for r in report["checks"]
        if r["scope"] == "coverage" and r["check"] == "fixture-tof"
    )
    assert fixture["status"] == "passed"


def test_results_without_a_legs_declaration_count_nothing_as_run(
    bundle: Path, tmp_path: Path
) -> None:
    """Outputs alone are never execution proof — copied-through energy
    nodes appear in any carried-forward results.json. Without the
    entrypoint's ``out/legs.json`` every leg stays ``not-run``."""

    def run(inputs_dir: Path, out_dir: Path, env: Any) -> None:
        (out_dir / "results.json").write_text(
            json.dumps(FRESH_RESULTS), encoding="utf-8"
        )

    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=run, judge=_fake_judge()
    )
    assert all(v["status"] == "not-run" for v in report["legs"].values())
    assert report["complete"] is False
    row = next(r for r in report["checks"] if r["check"] == "legs.json")
    assert row["status"] == "unavailable"


def test_copied_energy_nodes_do_not_register_an_energies_leg(
    bundle: Path, tmp_path: Path
) -> None:
    """A kinetics-only re-run carries ``nodes`` forward; declaring only the
    legs executed keeps ``energies`` ``not-run`` despite their presence."""

    def run(inputs_dir: Path, out_dir: Path, env: Any) -> None:
        fresh = {k: FRESH_RESULTS[k] for k in ("nodes", "edges", "kinetics_v2")}
        (out_dir / "results.json").write_text(json.dumps(fresh), encoding="utf-8")
        (out_dir / "legs.json").write_text('["kinetics"]', encoding="utf-8")

    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=run, judge=_fake_judge()
    )
    assert report["legs"]["kinetics"]["status"] == "passed"
    assert report["legs"]["energies"]["status"] == "not-run"
    assert report["complete"] is False


def test_a_declared_leg_without_fresh_outputs_is_not_run(
    bundle: Path, tmp_path: Path
) -> None:
    """Declaration is necessary, not sufficient: a leg the entrypoint
    claims but produced no outputs for stays ``not-run``, named why."""

    def run(inputs_dir: Path, out_dir: Path, env: Any) -> None:
        (out_dir / "results.json").write_text(
            json.dumps({"kinetics_v2": {"tof_chain": 1.0}}), encoding="utf-8"
        )
        (out_dir / "legs.json").write_text('["energies", "kinetics"]', encoding="utf-8")

    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=run, judge=_fake_judge()
    )
    assert report["legs"]["energies"]["status"] == "not-run"
    assert "declared" in report["legs"]["energies"]["why"]
    assert report["legs"]["kinetics"]["status"] == "passed"


def test_a_malformed_legs_declaration_fails_the_run(
    bundle: Path, tmp_path: Path
) -> None:
    def run(inputs_dir: Path, out_dir: Path, env: Any) -> None:
        (out_dir / "results.json").write_text(
            json.dumps(FRESH_RESULTS), encoding="utf-8"
        )
        (out_dir / "legs.json").write_text('["bogus-leg"]', encoding="utf-8")

    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=run, judge=_fake_judge()
    )
    assert report["verdict"] == "failed(entrypoint)"
    assert "bogus-leg" in str(
        next(r for r in report["checks"] if r["method"] == "legs-declaration")[
            "observed"
        ]
    )


def test_a_bundle_without_a_coverage_table_is_not_complete(
    bundle: Path, tmp_path: Path
) -> None:
    (bundle / "coverage.json").unlink()
    lines = [
        ln
        for ln in (bundle / "MANIFEST.sha256").read_text("utf-8").splitlines()
        if not ln.endswith("coverage.json")
    ]
    (bundle / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=_fake_runner(), judge=_fake_judge()
    )
    assert report["verdict"] == "reproduced"
    assert report["complete"] is False
    row = next(r for r in report["checks"] if r["scope"] == "coverage")
    assert row["status"] == "unavailable"


def test_a_claim_without_an_acceptance_test_is_unavailable(
    bundle: Path, tmp_path: Path
) -> None:
    cov = json.loads((bundle / "coverage.json").read_text("utf-8"))
    cov["claims"].append(
        {"claim": "paper-energy", "runs": ["run-1"], "required_legs": ["energies"]}
    )
    (bundle / "coverage.json").write_text(json.dumps(cov), encoding="utf-8")
    _rehash(bundle, "coverage.json")  # keep the manifest honest
    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=_fake_runner(), judge=_fake_judge()
    )
    assert report["verdict"] == "reproduced"
    row = next(
        r
        for r in report["checks"]
        if r["scope"] == "coverage" and r["check"] == "paper-energy"
    )
    assert row["status"] == "unavailable"
    # an unattestable claim withholds completeness — never a pass
    assert report["complete"] is False


# --- real entrypoint: the default runner shells `sh reproduce.sh` ---------


def test_the_real_entrypoint_runs_isolated_and_reports_its_log(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No injected runner: the fixture's ``reproduce.sh`` actually executes
    in a subprocess with the scrubbed environment. The log and exit code
    are kept on success, no host variable leaks in, the shipped references
    are never staged, and the guard's real reach is reported — never
    silently passed."""
    monkeypatch.setenv("PRECIS_REPLAY_CANARY", "host-secret")
    report = run_replay(bundle, work_dir=tmp_path / "work", judge=_fake_judge())
    command = report["commands"][0]
    assert command["argv"][-2:] == ["sh", "reproduce.sh"]
    assert command["exit_code"] == 0
    assert "fixture-ran" in command["log"]
    guard = command["network_guard"]
    guard_row = next(r for r in report["checks"] if r["check"] == "network-guard")
    if guard.startswith("unavailable"):
        assert guard_row["status"] == "unavailable"
        # an uncertified guard withholds completeness — never a pass
        assert report["complete"] is False
    else:
        assert guard == "netns(unshare -rn)"
        assert guard_row["status"] == "passed"
        assert report["complete"] is True
    assert "PRECIS_REPLAY_CANARY" not in command["isolation"]
    work = tmp_path / "work"
    fresh = json.loads((work / "out" / "results.json").read_text("utf-8"))
    assert fresh["canary"] == "scrubbed"  # the host env did not leak in
    assert fresh["slab_bytes"] == len(b"1\nfixture slab\n")
    assert not (work / "outputs").exists()  # references never staged
    assert report["verdict"] == "reproduced"


def test_the_real_entrypoint_reports_its_exit_code_and_log_on_failure(
    bundle: Path, tmp_path: Path
) -> None:
    (bundle / "reproduce.sh").write_text(
        "#!/bin/sh\necho fixture-failed >&2\nexit 7\n", encoding="utf-8"
    )
    _rehash(bundle, "reproduce.sh")
    report = run_replay(bundle, work_dir=tmp_path / "work", judge=_fake_judge())
    command = report["commands"][0]
    assert command["exit_code"] == 7
    assert "fixture-failed" in command["log"]
    assert report["verdict"] == "failed(entrypoint)"
    assert report["complete"] is False


@pytest.mark.skipif(
    replay._netns_prefix() is None or shutil.which("python3") is None,
    reason="needs an unprivileged network namespace and python3 on PATH",
)
def test_a_real_entrypoint_network_attempt_fails_in_its_namespace(
    bundle: Path, tmp_path: Path
) -> None:
    """On a host with ``unshare -rn`` the subprocess genuinely has no
    network: a socket attempt fails inside the detached namespace and the
    report says the guard was real."""
    (bundle / "reproduce.sh").write_text(
        "#!/bin/sh\npython3 -c "
        "'import socket; socket.create_connection((\"203.0.113.1\", 9), timeout=2)'\n",
        encoding="utf-8",
    )
    _rehash(bundle, "reproduce.sh")
    report = run_replay(bundle, work_dir=tmp_path / "work", judge=_fake_judge())
    command = report["commands"][0]
    assert command["network_guard"] == "netns(unshare -rn)"
    assert command["argv"][0].endswith("unshare")
    assert command["exit_code"] != 0
    assert report["verdict"] == "failed(entrypoint)"
    row = next(r for r in report["checks"] if r["check"] == "network-guard")
    assert row["status"] == "passed"


def test_append_report_never_replaces_a_prior_report(
    bundle: Path, tmp_path: Path
) -> None:
    report = run_replay(
        bundle, work_dir=tmp_path / "work", run=_fake_runner(), judge=_fake_judge()
    )
    out = tmp_path / "reports"
    first = replay.append_report(report, out)
    second = replay.append_report(report, out)
    assert first != second and first.exists() and second.exists()
    loaded = json.loads(first.read_text("utf-8"))
    assert loaded["schema"] == "pathway_replay_report"


# --- engine half: the fixture case judged by the real D10 table --------------
#
# Needs the ``catalyst`` extra (skipped without it, as every pathway engine
# test): the shipped and fresh results are two solves of the trimmed pw455722
# fixture, exactly the acceptance replay in test_pathway_revalidate.py.


@pytest.fixture
def engine() -> Any:
    pytest.importorskip("autocatpath", reason="needs catalyst extra")
    return pytest.importorskip(
        "autocatpath.manifest", reason="needs catalyst extra (engine >= 0.38)"
    )


@pytest.fixture(scope="module")
def fx() -> dict[str, Any]:
    fx_path = Path(__file__).parent / "fixtures" / "pw455722_results.json"
    with fx_path.open(encoding="utf-8") as fh:
        return json.load(fh)


class _Net:
    """The slice of ``Network`` ``kinetics.solve`` reads."""

    def __init__(self, fx: dict[str, Any]) -> None:
        from types import SimpleNamespace

        self.links = [(a, b) for a, b, kind in fx["links"] if kind != "reaction"]
        self.steps = [
            SimpleNamespace(
                name=f"{a}->{b}",
                reactant=SimpleNamespace(name=a),
                product=SimpleNamespace(name=b),
            )
            for a, b, kind in fx["links"]
            if kind == "reaction"
        ]
        self._states = sorted(fx["nodes"])

    def order(self) -> list[str]:
        return ["*", *(s for s in self._states if s != "*")]

    def states(self) -> list[str]:
        return list(self._states)

    def slab(self) -> None:
        return None


@pytest.fixture
def solve(fx: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    from autocatpath import kinetics

    monkeypatch.setattr(kinetics, "site_area_A2", lambda _slab: fx["site_area_A2"])
    net = _Net(fx)

    def run(config: dict[str, Any], scale: float = 1.0) -> dict[str, Any]:
        from autocatpath.config import Config

        _v1, v2 = kinetics.solve(
            Config.from_dict(config),
            fx["nodes"],
            fx["edges"],
            net,
            fx["refs"],
            span=fx["score"]["activity"]["span_eV"],
            log=lambda *a, **k: None,
        )
        results: dict[str, Any] = {
            "nodes": copy.deepcopy(fx["nodes"]),
            "edges": copy.deepcopy(fx["edges"]),
            "score": fx["score"],
            "kinetics_v2": v2,
        }
        if scale != 1.0:
            results["kinetics_v2"]["tof_chain"] *= scale
        return results

    return run


def _paper_bundle(tmp_path: Path, fx: dict[str, Any], shipped: dict[str, Any]) -> Path:
    import autocatpath

    config = copy.deepcopy(fx["config"])
    config["kinetics"]["n_samples"] = 0
    rec = vm.build(
        config=config,
        config_snapshot_yaml=json.dumps(config, sort_keys=True),
        results=shipped,
        autocatpath_version=autocatpath.__version__,
    )
    assert rec is not None
    root = tmp_path / "paper-bundle"
    root.mkdir(parents=True)
    files: dict[str, bytes] = {
        "reproduce.sh": b"#!/bin/sh\nexit 0\n",
        "seeds.json": b"{}",
        "env/pyproject.toml": b"[project]\nname='paper'\n",
        "env/uv.lock": LOCK_TEXT.encode("utf-8"),
        "env/weights.json": json.dumps([]).encode("utf-8"),
        "manifest/validation_manifest.json": json.dumps(rec).encode("utf-8"),
        "inputs/slab.extxyz": b"1\nPd slab\n",
        "outputs/results.json": json.dumps(shipped).encode("utf-8"),
    }
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    _write_manifest(root, files)
    return root


def _out_runner(fresh: dict[str, Any], legs: tuple[str, ...] = ("kinetics",)):
    def run(inputs_dir: Path, out_dir: Path, env: Any) -> None:
        (out_dir / "results.json").write_text(json.dumps(fresh), encoding="utf-8")
        (out_dir / "legs.json").write_text(json.dumps(list(legs)), encoding="utf-8")

    return run


def test_fixture_case_reproduces_under_the_real_d10_judge(
    engine: Any,
    fx: dict[str, Any],
    solve: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = copy.deepcopy(fx["config"])
    config["kinetics"]["n_samples"] = 0
    shipped = solve(config)
    # the paper host's lock pin is the lock the external runner stages
    monkeypatch.setenv(vm.LOCK_ENV, str(tmp_path / "env" / "uv.lock"))
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "uv.lock").write_text(LOCK_TEXT, encoding="utf-8")
    bundle = _paper_bundle(tmp_path / "staged", fx, shipped)
    report = run_replay(
        bundle,
        work_dir=tmp_path / "paper-work",
        run=_out_runner(solve(config)),
    )
    assert report["verdict"].startswith("reproduced")
    assert report["complete"] is False  # only the kinetics leg re-ran
    # nodes are carried forward as inputs — declaring the legs actually
    # executed keeps energies not-run, exactly as in the CPU revalidation
    for leg in ("energies", "tpd", "sweep"):
        assert report["legs"][leg]["status"] == "not-run"
    compared = {
        row["check"]
        for row in report["checks"]
        if row["scope"] == "numerical_agreement"
    }
    assert "tof_chain" in compared
    assert all(
        row["status"] == "passed"
        for row in report["checks"]
        if row["scope"] == "numerical_agreement"
    )
    row = next(row for row in report["checks"] if row["scope"] == "numerical_agreement")
    assert row["tolerance"]["rule"] and row["tolerance"]["column"]


def test_fixture_case_drift_is_judged_by_the_engine_table(
    engine: Any,
    fx: dict[str, Any],
    solve: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = copy.deepcopy(fx["config"])
    config["kinetics"]["n_samples"] = 0
    shipped = solve(config)
    monkeypatch.setenv(vm.LOCK_ENV, str(tmp_path / "env" / "uv.lock"))
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "uv.lock").write_text(LOCK_TEXT, encoding="utf-8")
    bundle = _paper_bundle(tmp_path / "staged", fx, shipped)
    # +0.03 decades: inside cross-platform tolerance, past same-platform
    report = run_replay(
        bundle,
        work_dir=tmp_path / "paper-work",
        run=_out_runner(solve(config, scale=10**0.03)),
    )
    rows = {
        row["check"]: row
        for row in report["checks"]
        if row["scope"] == "numerical_agreement"
    }
    assert report["verdict"] == "drifted(tof_chain)"
    assert report["complete"] is False
    assert rows["tof_chain"]["status"] == "failed"
    assert rows["tof_chain"]["tolerance"]["value"] == pytest.approx(0.01)
    assert rows["tof_chain"]["tolerance"]["column"] == engine.SAME_PLATFORM
