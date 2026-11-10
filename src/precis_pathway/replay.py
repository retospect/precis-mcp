"""External replay acceptance harness (gr564215).

The reader side of the reproduction contract in
``docs/backlog/catpath-repro-bundle.md``: a fresh runner receives only the
exported bundle directory — no precis checkout, store, MCP, credentials or
cached dependencies — and must still be able to re-run the computation and
judge it. This module is that runner's engine:

1. Integrity: every file is checked against ``MANIFEST.sha256``
   (``sha256sum -c`` semantics) before any compute; a modified bundle refuses
   naming the path, so a tampered input or weight never reaches the run.
2. Staging: declared files are copied into a fresh work directory, inputs are
   made read-only, shipped weights named in ``env/weights.json`` are
   hash-verified, and the staged ``env/uv.lock`` must hash-match the D10
   record's pinned lock. A mismatch refuses naming both digests — it never
   substitutes, fetches an unpinned replacement or consults a local cache.
   Weights recorded hash-only (no bundled file) stay unverified and are said
   so; that is the documented honest gap for a private engine, not a pass.
3. Execution: the declared entrypoint (``reproduce.sh``) runs with
   networking disabled (a socket guard fails the run naming the attempted
   address), index/cache isolation in the environment, immutable inputs and
   a new ``out/`` directory. Shipped ``outputs/`` references are never given
   to the entrypoint, so they cannot be reused as fresh results.
4. Numerical agreement: freshly computed observables are judged against the
   shipped references with the engine's D10 tolerance table
   (``autocatpath.manifest.compare``) — the same-platform or cross-platform
   column by recorded vs current platform. No second tolerance policy is
   implemented anywhere in this repo (gr564215 point 3); without the engine
   the agreement scope is unavailable, never passed.
5. Coverage: ``coverage.json`` maps claims to runs, required legs and an
   acceptance test; a required leg the replay did not run stays ``not-run``
   and a claim without a named test stays ``unavailable``, so one toy
   fixture cannot read as paper-wide coverage.

Integrity, execution, numerical agreement and scientific support remain
distinct scopes, as in ``verification.py``: the claim-support row is always
``unavailable`` — hashes, execution and agreement do not establish
scientific validity. The harness opens no store, needs no DB and mutates
nothing outside its work directory; the engine is needed only for the
judging step, so the integrity/staging/guard legs double as the always-run
packaging check on hosts without the ``catalyst`` extra.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from precis_pathway import manifest as vm

REPLAY_SCHEMA = "pathway_replay_report"
SCHEMA_VERSION = 1

#: Legs a full external replay can in principle run, in the engine's own
#: vocabulary (the D10 verdict's ``not_run`` never lists ``kmc`` separately —
#: it is part of the kinetics solve). A leg whose outputs are absent from
#: the fresh results stays ``not-run`` (named), the same rule as the CPU
#: revalidation's ``NOT_RUN_LEGS``: GPU/HPC legs need their declared
#: resources and an independent replay, never a silent substitution.
ALL_LEGS = ("energies", "tpd", "kinetics", "sweep")
_NOT_RUN_WHY = {
    "energies": "the min-energy-path leg did not run in this replay; "
    "it needs its declared GPU/HPC resources",
    "tpd": "the TPD leg did not run in this replay",
    "kinetics": "the kinetics leg did not run in this replay",
    "sweep": "the (T, p_total) sweep leg did not run in this replay",
}

#: Files the bundle must declare for a replay to stage at all — a missing
#: one refuses naming the path (gr564215 point 5: missing dependencies fail
#: with an actionable report).
REQUIRED_PATHS = (
    "reproduce.sh",
    "seeds.json",
    "env/pyproject.toml",
    "env/uv.lock",
    "env/weights.json",
    "manifest/validation_manifest.json",
    "outputs/results.json",
)

#: Environment isolation handed to the entrypoint: no index, no cache
#: fallback, fresh output location. Recorded in the report verbatim.
_ISOLATION_ENV = {
    "UV_OFFLINE": "1",
    "PIP_NO_INDEX": "1",
    "HF_HUB_OFFLINE": "1",
}

_MANIFEST_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)")


class ReplayRefused(Exception):
    """The bundle cannot be replayed as declared; ``str()`` names why."""


class NetworkAttempt(RuntimeError):
    """The entrypoint attempted network access while networking was disabled."""

    def __init__(self, address: Any) -> None:
        self.address = address
        super().__init__(f"entrypoint attempted network access to {address!r}")


def _row(
    scope: str,
    name: str,
    status: str,
    method: str,
    observed: Any,
    *,
    evidence: Any = None,
    tolerance: Any = None,
    platform: Any = None,
) -> dict[str, Any]:
    """One report row, the same shape ``verification.py`` emits."""
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "check": name,
        "status": status,
        "method": method,
        "evidence": evidence,
        "observed": observed,
        "tolerance": tolerance,
        "platform": platform,
    }


def _safe_member(name: str) -> bool:
    p = PurePosixPath(name)
    return (
        bool(name)
        and not p.is_absolute()
        and ".." not in p.parts
        and "\\" not in name
        and p.as_posix() == name
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def parse_manifest(bundle: Path) -> dict[str, str]:
    """Declared ``relative path -> sha256`` from ``MANIFEST.sha256``.

    Refuses a missing, unreadable or malformed manifest, unsafe paths and
    duplicate or self-referential entries — the same discipline as
    ``verification.verify_archive`` for a directory layout.
    """
    manifest_path = bundle / "MANIFEST.sha256"
    if not manifest_path.is_file():
        raise ReplayRefused("MANIFEST.sha256 is missing from the bundle")
    expected: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        match = _MANIFEST_LINE.fullmatch(line)
        if not match or not _safe_member(match[2]):
            raise ReplayRefused(
                f"MANIFEST.sha256 has a malformed checksum entry: {line!r}"
            )
        name = match[2]
        if name in expected or name == "MANIFEST.sha256":
            raise ReplayRefused(
                f"MANIFEST.sha256 has a duplicate or self-referential entry: {name}"
            )
        expected[name] = match[1].lower()
    if not expected:
        raise ReplayRefused("MANIFEST.sha256 is empty")
    return expected


def check_integrity(bundle: Path) -> list[dict[str, Any]]:
    """``sha256sum -c`` over the bundle directory; one row per file.

    Undeclared files fail (a shipped archive must be exactly its manifest);
    a missing or mismatched declared file fails naming the path. Rows, not
    exceptions: the caller builds the report from every row.
    """
    rows: list[dict[str, Any]] = []
    try:
        expected = parse_manifest(bundle)
    except ReplayRefused as exc:
        return [_row("integrity", "MANIFEST.sha256", "failed", "sha256", str(exc))]
    for name in sorted(p for p in os.listdir(bundle) if p != "MANIFEST.sha256"):
        if (bundle / name).is_file() and name not in expected:
            rows.append(
                _row("integrity", name, "failed", "sha256", "file has no checksum")
            )
    for name, want in sorted(expected.items()):
        path = bundle / name
        if not path.is_file():
            rows.append(
                _row("integrity", name, "failed", "sha256", "manifest member missing")
            )
            continue
        got = _sha256_file(path)
        rows.append(
            _row(
                "integrity",
                name,
                "passed" if got == want else "failed",
                "sha256",
                {"expected": want, "actual": got},
            )
        )
    return rows


def _check_weights(bundle: Path) -> list[dict[str, Any]]:
    """Verify bundled weights against ``env/weights.json``; never fetch.

    An entry naming a file must hash-match — a mismatch refuses naming both
    digests. An entry recorded hash-only (private-engine fetch recipe) stays
    ``unavailable``: it cannot be attested offline, and silently pulling a
    replacement is exactly what the contract forbids.
    """
    rows: list[dict[str, Any]] = []
    raw = _read_json(bundle / "env" / "weights.json")
    entries = raw if isinstance(raw, list) else [raw]
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReplayRefused("env/weights.json has a malformed weights entry")
        name = (
            f"{entry.get('backend')}:{entry.get('model')}"
            if entry.get("backend")
            else str(entry.get("file") or "?")
        )
        want = entry.get("sha256")
        rel = entry.get("file")
        if not rel:
            rows.append(
                _row(
                    "integrity",
                    f"weights:{name}",
                    "unavailable",
                    "sha256",
                    {
                        "sha256": want,
                        "note": "weights not bundled (hash-only record); "
                        "verify on the documented outsider-accessible route",
                    },
                )
            )
            continue
        path = bundle / str(rel)
        if not path.is_file():
            raise ReplayRefused(f"weights:{name}: declared file {rel} is missing")
        got = _sha256_file(path)
        rows.append(
            _row(
                "integrity",
                f"weights:{name}",
                "passed" if got == want else "failed",
                "sha256",
                {"file": rel, "expected": want, "actual": got},
            )
        )
    return rows


def _check_lock_pin(bundle: Path, record: dict[str, Any]) -> dict[str, Any]:
    """The staged ``env/uv.lock`` must hash-match the D10 record's pin."""
    lock = record.get("lock") or {}
    want, source = lock.get("uv_lock_sha256"), lock.get("lock_source")
    staged = _sha256_file(bundle / "env" / "uv.lock")
    if source == "uv.lock" and want and staged != want:
        raise ReplayRefused(
            f"env/uv.lock: staged {staged[:12]} != manifest pin {str(want)[:12]} "
            f"({source}); the pinned lock is never substituted"
        )
    return {
        "uv_lock_sha256": want,
        "lock_source": source,
        "staged_uv_lock_sha256": staged,
    }


def stage_bundle(bundle: Path, work: Path) -> dict[str, Any]:
    """Copy declared files into a fresh ``work`` dir and pin-check them.

    Inputs land read-only; the returned mapping carries what the run step
    needs. Refuses (naming the path or both digests) on a non-empty work
    directory, a missing declared/structural file, a weights-hash mismatch
    or a staged-lock pin mismatch — before any compute.
    """
    expected = parse_manifest(bundle)
    for rel in REQUIRED_PATHS:
        if not (bundle / rel).is_file():
            raise ReplayRefused(f"missing dependency: {rel} is required to replay")
    if work.exists() and any(work.iterdir()):
        raise ReplayRefused(
            f"work directory {work} is not empty; replays write a new one"
        )
    work.mkdir(parents=True)
    for rel in sorted(expected):
        dest = work / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle / rel, dest)
    shutil.copyfile(bundle / "MANIFEST.sha256", work / "MANIFEST.sha256")
    inputs = work / "inputs"
    if inputs.is_dir():
        for path in sorted(inputs.rglob("*")):
            if path.is_file():
                path.chmod(0o444)
    weights_rows = _check_weights(bundle)
    record = _read_json(bundle / "manifest" / "validation_manifest.json")
    if not isinstance(record, dict):
        raise ReplayRefused(
            "manifest/validation_manifest.json is not a manifest record"
        )
    lock_info = _check_lock_pin(bundle, record)
    out_dir = work / "out"
    out_dir.mkdir()
    return {
        "expected": expected,
        "record": record,
        "inputs": inputs,
        "out_dir": out_dir,
        "weights_rows": weights_rows,
        "lock_info": lock_info,
        "bundle_id": _sha256_file(bundle / "MANIFEST.sha256"),
    }


@contextlib.contextmanager
def network_disabled() -> Iterator[None]:
    """Fail any socket the entrypoint opens, naming the attempted address.

    In-process guard beneath the container's ``--network none``: it blocks
    the undeclared-service-access leg of the acceptance contract even when
    the harness runs outside a container.
    """

    real_socket = socket.socket
    real_create_connection = socket.create_connection

    def blocked_socket(*args: Any, **kwargs: Any) -> Any:
        raise NetworkAttempt("socket creation while networking is disabled")

    def blocked_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        raise NetworkAttempt(address)

    setattr(socket, "socket", blocked_socket)
    setattr(socket, "create_connection", blocked_create_connection)
    try:
        yield
    finally:
        setattr(socket, "socket", real_socket)
        setattr(socket, "create_connection", real_create_connection)


Runner = Callable[[Path, Path, Mapping[str, str]], Any]
Judge = Callable[[dict[str, Any], dict[str, Any], dict[str, Any], Any], Any]


def _subprocess_runner(
    timeout_seconds: float,
) -> Runner:
    def run(inputs_dir: Path, out_dir: Path, env: Mapping[str, str]) -> None:
        completed = subprocess.run(
            ["sh", "reproduce.sh"],
            cwd=out_dir.parent,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        log = (completed.stdout or "")[-65536:] + (completed.stderr or "")[-65536:]
        if completed.returncode != 0:
            raise ReplayRefused(
                f"reproduce.sh exited {completed.returncode}: {log[-400:]}"
            )

    return run


def _leg_present(leg: str, results: Mapping[str, Any]) -> bool:
    if leg == "energies":
        return bool(results.get("nodes"))
    if leg == "tpd":
        tpd = results.get("kinetics_tpd")
        return isinstance(tpd, dict) and bool(tpd.get("tpd"))
    if leg == "kinetics":
        return bool(results.get("kinetics_v2"))
    if leg == "sweep":
        return bool(results.get("kinetics_sweep"))
    return False


def _engine_judge(
    shipped: dict[str, Any],
    fresh: dict[str, Any],
    record: dict[str, Any],
    platform_now: Any,
) -> dict[str, Any]:
    """Judge fresh observables against the shipped references with the D10
    table — the engine's ``compare``, same-platform or cross-platform column
    by platform, legs the replay did not run named ``not-run``."""
    m = vm.engine_manifest()
    if m is None:
        raise ReplayRefused(
            "autocatpath >= 0.38 (autocatpath.manifest) is required to judge "
            "numerical agreement; agreement is unavailable without the engine, "
            "never passed"
        )
    manifest = m.ValidationManifest.from_dict(record)
    then_obs = m.observables(shipped)
    now_obs = m.observables(fresh)
    plat_now = platform_now or m.capture_platform()
    column = m.column_for(manifest.platform, plat_now)
    not_run = tuple(leg for leg in ALL_LEGS if not _leg_present(leg, fresh))
    report = m.compare(
        then_obs,
        now_obs,
        manifest.tolerances,
        column=column,
        failed_legs=(),
        skip_legs=not_run,
        caveats=(),
    )
    return {
        "report": report.as_dict(),
        "column": column,
        "not_run": not_run,
        "platform_now": plat_now.as_dict(),
    }


def coverage_rows(coverage: Any, not_run: tuple[str, ...]) -> list[dict[str, Any]]:
    """Join the bundle's claim table with the legs this replay ran.

    A required leg that stayed ``not-run`` keeps its claim ``not-run``; a
    claim without a named acceptance test is ``unavailable`` — coverage is
    per claim and per leg, so one fixture cannot stand in for the paper.
    """
    rows: list[dict[str, Any]] = []
    claims = coverage.get("claims") if isinstance(coverage, dict) else None
    if not claims:
        return [
            _row(
                "coverage",
                "claims",
                "unavailable",
                "coverage.json",
                "bundle declares no claim/run/required-leg/acceptance-test table",
            )
        ]
    for claim in claims:
        if not isinstance(claim, dict) or not claim.get("claim"):
            raise ReplayRefused("coverage.json has a malformed claim entry")
        name = str(claim["claim"])
        test = claim.get("acceptance_test")
        if not test:
            rows.append(
                _row(
                    "coverage",
                    name,
                    "unavailable",
                    "coverage.json",
                    "claim names no acceptance test",
                )
            )
            continue
        legs = claim.get("required_legs") or []
        missing = [leg for leg in legs if leg in not_run]
        rows.append(
            _row(
                "coverage",
                name,
                "not-run" if missing else "passed",
                "coverage.json",
                {
                    "runs": claim.get("runs") or [],
                    "required_legs": legs,
                    "acceptance_test": test,
                    "not_run": missing,
                },
            )
        )
    return rows


def replay(
    bundle: Path,
    *,
    work_dir: Path,
    run: Runner | None = None,
    judge: Judge | None = None,
    platform_now: Any = None,
    timeout_seconds: float = 3600.0,
) -> dict[str, Any]:
    """Replay one exported bundle outside precis; returns the machine-readable
    report (never raises for a refused or failed replay — the report is the
    acceptance artifact).

    ``run`` stands in for the declared entrypoint: called as
    ``run(inputs_dir, out_dir, env)`` it must compute fresh results and write
    ``out_dir / "results.json"``. The default shells ``sh reproduce.sh`` in
    the staged directory under :func:`network_disabled` with the isolation
    environment. ``judge`` defaults to the engine's D10 judge; a caller
    without the engine can inject an equivalent callable, and the integrity,
    staging, execution and coverage legs still run engine-free.
    """
    checks: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    staged: dict[str, Any] | None = None
    verdict = "failed(unknown)"
    complete = False
    refusal: str | None = None

    integrity = check_integrity(bundle)
    checks.extend(integrity)
    if any(row["status"] != "passed" for row in integrity):
        refusal = next(
            str(row["observed"]) for row in integrity if row["status"] != "passed"
        )
        verdict = f"refused(integrity): {refusal}"

    if refusal is None:
        try:
            staged = stage_bundle(bundle, work_dir)
            checks.extend(staged["weights_rows"])
        except ReplayRefused as exc:
            refusal = str(exc)
            verdict = f"refused(staging): {refusal}"
        else:
            bad = next(
                (row for row in staged["weights_rows"] if row["status"] == "failed"),
                None,
            )
            if bad is not None:
                refusal = f"{bad['check']}: {bad['observed']}"
                verdict = f"refused(staging): {refusal}"

    shipped: dict[str, Any] | None = None
    fresh: dict[str, Any] | None = None
    judge_out: dict[str, Any] | None = None
    if staged is not None and refusal is None:
        shipped = _read_json(bundle / "outputs" / "results.json")
        env = {
            **os.environ,
            **_ISOLATION_ENV,
            "REPLAY_INPUTS": str(staged["inputs"]),
            "REPLAY_OUT": str(staged["out_dir"]),
        }
        runner = run if run is not None else _subprocess_runner(timeout_seconds)
        command: dict[str, Any] = {
            "argv": ["sh", "reproduce.sh"],
            "cwd": str(work_dir),
            "isolation": dict(_ISOLATION_ENV),
        }
        try:
            with network_disabled():
                runner(staged["inputs"], staged["out_dir"], env)
            command["exit_code"] = 0
        except NetworkAttempt as exc:
            command["exit_code"] = "network-blocked"
            command["log"] = str(exc)
            checks.append(
                _row("execution", "reproduce.sh", "failed", "network-guard", str(exc))
            )
            verdict = "failed(network-access)"
            refusal = str(exc)
        except ReplayRefused as exc:
            command["exit_code"] = "refused"
            command["log"] = str(exc)
            checks.append(
                _row("execution", "reproduce.sh", "failed", "exit-code", str(exc))
            )
            verdict = "failed(entrypoint)"
            refusal = str(exc)
        except subprocess.TimeoutExpired:
            command["exit_code"] = "timeout"
            checks.append(
                _row(
                    "execution",
                    "reproduce.sh",
                    "failed",
                    "timeout",
                    f"exceeded {timeout_seconds}s",
                )
            )
            verdict = "failed(timeout)"
            refusal = "entrypoint timed out"
        commands.append(command)
        if refusal is None:
            results_path = staged["out_dir"] / "results.json"
            if not results_path.is_file():
                checks.append(
                    _row(
                        "execution",
                        "results.json",
                        "failed",
                        "fresh-output",
                        "entrypoint wrote no out/results.json",
                    )
                )
                verdict = "failed(no-results)"
                refusal = "entrypoint produced no fresh results"
            else:
                checks.append(
                    _row(
                        "execution",
                        "reproduce.sh",
                        "passed",
                        "exit-code",
                        {"exit_code": 0, "out": str(staged["out_dir"])},
                    )
                )
                fresh = _read_json(results_path)

    record = (staged or {}).get("record") or {}
    if refusal is None and shipped is not None and fresh is not None:
        judging = judge if judge is not None else _engine_judge
        try:
            judged = judging(shipped, fresh, record, platform_now)
            judge_out = dict(judged)
        except ReplayRefused as exc:
            checks.append(
                _row("numerical_agreement", "D10", "unavailable", "D10-judge", str(exc))
            )
            verdict = "failed(judge)"
            refusal = str(exc)

    not_run: tuple[str, ...] = ()
    if judge_out is not None:
        report = judge_out["report"]
        verdict = str(report["verdict"])
        complete = bool(report.get("complete"))
        not_run = tuple(judge_out.get("not_run") or ())
        for observable in report.get("observables") or []:
            checks.append(
                _row(
                    "numerical_agreement",
                    str(observable.get("observable", "unknown")),
                    "passed" if observable.get("ok") is True else "failed",
                    "autocatpath.manifest.compare",
                    observable,
                    tolerance={
                        "column": judge_out.get("column"),
                        "rule": observable.get("rule"),
                        "value": observable.get("tolerance"),
                    },
                    platform={
                        "column": judge_out.get("column"),
                        "now": judge_out.get("platform_now"),
                    },
                )
            )
        if not report.get("observables"):
            checks.append(
                _row(
                    "numerical_agreement",
                    "D10",
                    "unavailable",
                    "autocatpath.manifest.compare",
                    "no observable comparisons produced",
                )
            )
        for leg in not_run:
            checks.append(
                _row("execution", leg, "not-run", "D10-judge", _NOT_RUN_WHY[leg])
            )
    elif refusal is None:
        checks.append(
            _row(
                "numerical_agreement",
                "D10",
                "unavailable",
                "D10-judge",
                "no judgment produced",
            )
        )

    image_digest: str | None = None
    build_path = bundle / "provenance" / "build.json"
    if build_path.is_file():
        build = _read_json(build_path)
        if isinstance(build, dict):
            image_digest = build.get("image_digest")
    if image_digest is None:
        checks.append(
            _row(
                "integrity",
                "image_digest",
                "unavailable",
                "provenance/build.json",
                "bundle declares no pinned base-image digest",
            )
        )
    legs = {
        leg: {
            "status": "not-run"
            if (leg in not_run or refusal is not None)
            else "passed",
            "why": _NOT_RUN_WHY.get(leg) if leg in not_run else (refusal or None),
        }
        for leg in ALL_LEGS
    }
    coverage_path = bundle / "coverage.json"
    coverage = (
        coverage_rows(_read_json(coverage_path), not_run)
        if coverage_path.is_file()
        else []
    )
    checks.extend(coverage)
    checks.append(
        _row(
            "claim_support",
            "scientific-support",
            "unavailable",
            "existing-evidence-and-review-gates",
            "Not assessed here. Integrity, execution and numerical agreement "
            "do not establish scientific validity.",
            evidence={"manifest_id": record.get("manifest_id")},
        )
    )

    return {
        "schema": REPLAY_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bundle": {"path": bundle.name, "bundle_id": (staged or {}).get("bundle_id")},
        "ids": {
            "manifest_id": record.get("manifest_id"),
            "code": record.get("code") or {},
            "lock": (staged or {}).get("lock_info") or {},
            "weights": {
                f"{ml.get('backend')}:{ml.get('model')}": ml.get("weights_sha256")
                for ml in (record.get("lock") or {}).get("mlip") or []
            },
        },
        "commands": commands,
        "legs": legs,
        "tolerances": record.get("tolerances") or [],
        "image_digest": image_digest,
        "checks": checks,
        "verdict": verdict,
        "complete": complete,
        "next": (
            "none"
            if complete
            else "a refused or partial replay names its refusal; legs not run "
            "need their declared resources and an independent replay"
        ),
    }


def append_report(report: dict[str, Any], directory: Path) -> Path:
    """Append a distinct ``replay-report-<uuid>.json``; never replace one."""
    from uuid import uuid4

    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"replay-report-{uuid4().hex}.json"
    with path.open("x", encoding="utf-8") as fh:
        fh.write(payload)
    return path


__all__ = [
    "ALL_LEGS",
    "REPLAY_SCHEMA",
    "SCHEMA_VERSION",
    "NetworkAttempt",
    "ReplayRefused",
    "append_report",
    "check_integrity",
    "coverage_rows",
    "network_disabled",
    "parse_manifest",
    "replay",
    "stage_bundle",
]
