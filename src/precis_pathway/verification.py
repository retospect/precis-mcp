"""Portable, read-only verification of archived bytes and recorded D10 checks.

``build_report`` hashes a zip without extracting or executing it, then projects
``revalidate_job.revalidate`` records into versioned, scoped checks. Numerical
decisions remain the engine's recorded ``compare`` decisions; this reader never
rejudges tolerances or calls ``rerun_cpu_legs``. Missing evidence is unavailable,
not a pass. CPU replay cannot validate energies, and neither reproduction nor
integrity confers scientific support: that remains with evidence/review gates.

``append_report`` creates a new JSON file beside (never inside) the frozen run.
The optional run-envelope identity is opaque, allowing envelope capture and
this reader to ship independently. The CLI consumes an explicit pathway-meta
JSON snapshot; its digest binds the reported evidence even if it is not one of
the zip's members. No DB, engine installation, migration or compute is needed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import lzma
import re
import stat
import zlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from precis_pathway import manifest as vm

Status = Literal["passed", "failed", "not-run", "unavailable"]
SCHEMA_VERSION = 1
_MEMBER_READ_ERRORS = (
    BadZipFile,
    RuntimeError,
    NotImplementedError,
    EOFError,
    OSError,
    lzma.LZMAError,
    zlib.error,
)


def _check(
    scope: str,
    name: str,
    status: Status,
    method: str,
    observed: Any,
    *,
    evidence: Any = None,
    tolerance: Any = None,
    platform: Any = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "check": name,
        "status": status,
        "method": method,
        "evidence": evidence,
        "observed": copy.deepcopy(observed),
        "tolerance": copy.deepcopy(tolerance),
        "platform": copy.deepcopy(platform),
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


def verify_archive(archive: Path) -> list[dict[str, Any]]:
    """Check every non-directory member against one MANIFEST.sha256.

    Accept a flat zip or a single enclosing directory. Reject ambiguous paths,
    duplicate entries, symlinks, unlisted files and malformed manifests. Hashes
    establish consistency with this manifest, not authorship or claim validity.
    Unreadable members fail individually so later members still get checked;
    an unreadable manifest prevents checksum verification of the remaining files.
    """
    rows: list[dict[str, Any]] = []

    def issue(name: str, reason: str, status: Status = "failed") -> None:
        rows.append(_check("integrity", name, status, "sha256", reason))

    try:
        with ZipFile(archive) as zf:
            files = [i for i in zf.infolist() if not i.is_dir()]
            names = [i.filename for i in files]
            for name, count in Counter(names).items():
                if count > 1:
                    issue(name, "duplicate archive member")
            for info in files:
                if not _safe_member(info.filename) or stat.S_ISLNK(
                    info.external_attr >> 16
                ):
                    issue(info.filename, "unsafe archive member")
            manifests = [n for n in names if PurePosixPath(n).name == "MANIFEST.sha256"]
            if len(manifests) != 1:
                issue(
                    "MANIFEST.sha256",
                    "exactly one checksum manifest is required",
                    "unavailable",
                )
                return rows
            if rows:
                return rows
            manifest_name = manifests[0]
            try:
                raw = zf.read(manifest_name)
            except _MEMBER_READ_ERRORS as exc:
                issue(manifest_name, str(exc))
                return rows
            manifest_sha = hashlib.sha256(raw).hexdigest()
            parent = PurePosixPath(manifest_name).parent
            expected: dict[str, str] = {}
            for line in raw.decode("utf-8").splitlines():
                match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
                if not match or not _safe_member(match[2]):
                    issue("MANIFEST.sha256", "malformed checksum entry")
                    return rows
                name = str(parent / match[2])
                if name in expected or name == manifest_name:
                    issue(name, "duplicate or self-referential checksum entry")
                    return rows
                expected[name] = match[1].lower()
            if not expected:
                issue("MANIFEST.sha256", "empty checksum manifest")
                return rows
            for name in sorted(set(names) - {manifest_name} - expected.keys()):
                issue(name, "archive member has no checksum")
            for name, want in sorted(expected.items()):
                if name not in names:
                    issue(name, "manifest member missing from archive")
                    continue
                try:
                    digest = hashlib.sha256()
                    open_member = zf.open  # binary: takes no encoding
                    with open_member(name) as member:
                        for chunk in iter(lambda: member.read(1 << 20), b""):
                            digest.update(chunk)
                    got = digest.hexdigest()
                except _MEMBER_READ_ERRORS as exc:
                    issue(name, str(exc))
                    continue
                rows.append(
                    _check(
                        "integrity",
                        name,
                        "passed" if got == want else "failed",
                        "sha256",
                        {"expected": want, "actual": got},
                        evidence={"manifest_sha256": manifest_sha, "member": name},
                    )
                )
    except OSError as exc:
        issue("archive", str(exc), "unavailable")
    except (
        BadZipFile,
        UnicodeError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zlib.error,
    ) as exc:
        issue("archive", str(exc))
    return rows


def _prerequisites(
    rec: dict[str, Any],
    *,
    engine: tuple[str, str | None] | None,
    lock: tuple[str | None, str | None] | None,
    weights: dict[str, str] | None,
) -> list[dict[str, Any]]:
    """Compare explicitly supplied host stamps; never install or locate anything."""
    code, pins = rec.get("code") or {}, rec.get("lock") or {}
    rows = []
    expected_engine = (code.get("autocatpath_version"), code.get("autocatpath_sha"))
    expected_lock = (pins.get("uv_lock_sha256"), pins.get("lock_source"))
    for name, expected, actual in (
        ("engine", expected_engine, engine),
        ("lock", expected_lock, lock),
    ):
        status: Status = "unavailable"
        if actual:
            if any(
                want and got and want != got
                for want, got in zip(expected, actual, strict=True)
            ):
                status = "failed"
            elif all(actual) and all(expected):
                status = "passed"
        rows.append(
            _check(
                "prerequisites",
                name,
                status,
                "explicit-stamp-comparison",
                {
                    "expected": expected,
                    "actual": actual,
                    "note": "missing stamps are unavailable; lock source kinds must match",
                },
            )
        )
    for mlip in pins.get("mlip") or []:
        key = f"{mlip.get('backend')}:{mlip.get('model')}"
        want = mlip.get("weights_sha256")
        got = (weights or {}).get(key)
        status = "unavailable"
        if mlip.get("backend") == "emt":
            status = "not-run"
        elif want and got:
            status = "passed" if want == got else "failed"
        rows.append(
            _check(
                "prerequisites",
                f"weights:{key}",
                status,
                "explicit-stamp-comparison",
                {
                    "expected": want,
                    "actual": got,
                    "note": "EMT has no weights"
                    if status == "not-run"
                    else "CPU revalidation does not check or load MLIP weights",
                },
            )
        )
    if not pins.get("mlip"):
        rows.append(
            _check(
                "prerequisites",
                "weights",
                "unavailable",
                "recorded-stamp",
                "No MLIP weights inventory recorded",
            )
        )
    return rows


def recorded_checks(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Project all recorded D10 attempts; never infer a fresh execution."""
    rec = vm.manifest_of(meta) or {}
    mid = rec.get("manifest_id")
    runs = (meta.get("revalidation") or {}).get(mid) or []
    rows = []
    tolerances = {t["observable"]: t for t in rec.get("tolerances") or []}
    if not runs:
        for scope in ("execution", "numerical_agreement"):
            rows.append(
                _check(
                    scope,
                    "D10",
                    "unavailable",
                    "recorded-D10",
                    "No recorded revalidation for this manifest",
                )
            )
    for index, run in enumerate(runs):
        evidence = {
            "manifest_id": mid,
            "attempt": index,
            "job_id": run.get("job_id"),
            "at": run.get("at"),
        }
        platform = {k: run.get(k) for k in ("platform_then", "platform_now", "column")}
        verdict = str(run.get("verdict") or "")
        # A drift verdict proves comparison ran, not numerical agreement.
        execution: Status = "unavailable"
        if verdict.startswith(("reproduced", "drifted(")):
            execution = "passed"
        elif verdict.startswith("failed("):
            execution = "failed"
        rows.append(
            _check(
                "execution",
                "D10",
                execution,
                "recorded-D10",
                run,
                evidence=evidence,
                platform=platform,
            )
        )
        for leg in run.get("not_run") or []:
            rows.append(
                _check(
                    "execution",
                    str(leg),
                    "not-run",
                    "recorded-D10",
                    (run.get("not_run_why") or {}).get(
                        leg, vm.NOT_RUN_LEGS.get(leg, "not requested")
                    ),
                    evidence=evidence,
                    platform=platform,
                )
            )
        for observable in run.get("observables") or []:
            name = observable.get("observable", "unknown")
            ok = observable.get("ok")
            status: Status = (
                "passed" if ok is True else "failed" if ok is False else "unavailable"
            )
            rows.append(
                _check(
                    "numerical_agreement",
                    name,
                    status,
                    "autocatpath.manifest.compare (recorded)",
                    observable,
                    evidence=evidence,
                    platform=platform,
                    tolerance={
                        "contract": tolerances.get(name),
                        "column": run.get("column"),
                        "rule": observable.get("rule"),
                        "value": observable.get("tolerance"),
                    },
                )
            )
        if not run.get("observables"):
            rows.append(
                _check(
                    "numerical_agreement",
                    "D10",
                    "unavailable",
                    "recorded-D10",
                    "No observable comparisons recorded",
                    evidence=evidence,
                )
            )
    rows.append(
        _check(
            "claim_support",
            "scientific-support",
            "unavailable",
            "existing-evidence-and-review-gates",
            {
                "note": "Not assessed here. Hashes, execution and numerical agreement do not establish scientific validity.",
                "trust_summary": (meta.get("results") or {}).get("trust_summary"),
            },
            evidence={"manifest_id": mid},
        )
    )
    return rows


def build_report(
    archive: Path,
    meta: dict[str, Any],
    *,
    envelope_id: str | None = None,
    engine: tuple[str, str | None] | None = None,
    lock: tuple[str | None, str | None] | None = None,
    weights: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a report without mutating inputs or launching a computation.

    Host engine/lock/weight stamps are optional evidence, not auto-detected.
    Their absence must not stop offline rendering of a recorded D10 report.
    """
    rec = vm.manifest_of(meta) or {}
    checks = [
        *verify_archive(archive),
        *recorded_checks(meta),
        *_prerequisites(rec, engine=engine, lock=lock, weights=weights),
    ]
    snapshot_sha = hashlib.sha256(
        json.dumps(meta, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    for row in checks:
        row["envelope_id"] = envelope_id
        row["evidence"] = {**(row["evidence"] or {}), "meta_sha256": snapshot_sha}
    return {
        "schema": "pathway_verification_report",
        "schema_version": SCHEMA_VERSION,
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "envelope_id": envelope_id,
        "manifest_id": rec.get("manifest_id"),
        "archive": archive.name,
        "compute_launched": False,
        "recompute": {
            "job_type": "autocatpath_revalidate",
            "cpu_legs": list(vm.CPU_LEGS),
            "not_run_legs": dict(vm.NOT_RUN_LEGS),
        },
        "checks": checks,
        "recorded_manifest_text": vm.manifest_text(meta),
        "next": "Actual recomputation requires an explicit autocatpath_revalidate job; CPU legs leave energies not run.",
    }


def append_report(report: dict[str, Any], directory: Path) -> Path:
    """Append a distinct portable JSON record; never replace a previous report."""
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"verify-report-{uuid4().hex}.json"
    with path.open("x", encoding="utf-8") as fh:
        fh.write(payload)
    return path
