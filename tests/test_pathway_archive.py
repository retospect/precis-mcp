"""The paper-first repro bundle (gr564186): a stored seed run exported as a
deterministic zip — MANIFEST.sha256, RO-Crate metadata, frozen inputs/
outputs/artifacts/env pins, recorded checks and an offline evidence index —
inspectable and integrity-checked without a database, engine or compute.

The persisted ``repro_case.json`` snapshot is the recorded November case —
the real ``pw455722`` record (NO → NH3 on Pd, pathway 455722, seed job
455725) exported read-only from prod, its retained ``pathway_body`` chunk
riding as ``methods``. ``repro_case_bundle.zip`` is its packaged archive,
committed so an offline reader can verify integrity without building
anything. The record predates artifact retention and the D10 manifest, so
those pins are genuinely absent and disclosed, never fabricated.
"""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from precis_pathway.archive import (
    ArchiveRefused,
    build_archive,
    plan_archive,
)
from precis_pathway.verification import verify_archive

_FIXTURE = Path(__file__).parent / "fixtures" / "pathway" / "repro_case.json"
_BUNDLED_ZIP = Path(__file__).parent / "fixtures" / "pathway" / "repro_case_bundle.zip"
_CONTEXT_13 = (
    Path(__file__).parent / "fixtures" / "pathway" / "ro-crate-1.3-context.jsonld"
)


@pytest.fixture
def stored() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _reader(stored: dict[str, Any]) -> Any:
    return lambda entry: stored.get("artifact_text", {})[entry["name"]].encode("utf-8")


def _build(stored: dict[str, Any], out_dir: Path, **overrides: Any) -> Any:
    kw: dict[str, Any] = {
        "job_id": stored["job_id"],
        "job_meta": stored["job_meta"],
        "pathway_id": stored["pathway_id"],
        "pathway_meta": stored["pathway_meta"],
        "out_dir": out_dir,
        "slug": stored["slug"],
        "title": stored["title"],
        "frozen": stored["frozen"],
        "claims": stored["claims"],
        "methods": stored["methods"],
        "read_artifact": _reader(stored),
    }
    kw.update(overrides)
    return build_archive(**kw)


def _members(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as zf:
        return {i.filename: zf.read(i.filename) for i in zf.infolist()}


def _text(members: dict[str, bytes], root: str, rel: str) -> str:
    return members[f"{root}{rel}"].decode("utf-8")


def test_persisted_november_case_verifies_offline() -> None:
    """The packaged fixture integrity-checks without DB, engine or compute."""
    rows = verify_archive(_BUNDLED_ZIP)
    assert rows, "verify_archive produced no checks"
    assert all(row["status"] == "passed" for row in rows), rows


def test_rebuild_matches_the_packaged_case(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path)
    assert res.archive.read_bytes() == _BUNDLED_ZIP.read_bytes()


def test_identical_inputs_produce_identical_archive_bytes(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    first = _build(stored, tmp_path)
    second = _build(copy.deepcopy(stored), tmp_path)
    assert first.archive.read_bytes() == second.archive.read_bytes()
    assert first.bundle_id == second.bundle_id


def test_bundle_id_is_the_manifest_hash_not_a_member(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    manifest = _text(members, res.root, "MANIFEST.sha256")
    assert res.bundle_id == hashlib.sha256(manifest.encode()).hexdigest()
    assert all(name.startswith(res.root) for name in members)
    # the id itself must not ride inside the archive it identifies
    assert res.bundle_id not in b"".join(members.values()).decode(
        "utf-8", errors="ignore"
    )


def test_manifest_lists_every_other_member(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    listed = {
        line.split("  ", 1)[1]
        for line in _text(members, res.root, "MANIFEST.sha256").splitlines()
    }
    names = {n.removeprefix(res.root) for n in members}
    assert listed == names - {"MANIFEST.sha256"}
    # platform.json is checksummed even though D10 excludes it from manifest_id
    assert "env/platform.json" in listed
    assert "manifest/methods.md" in listed


def test_tampering_fails_before_any_compute(
    tmp_path: Path, stored: dict[str, Any]
) -> None:
    res = _build(stored, tmp_path)
    data = bytearray(res.archive.read_bytes())
    with zipfile.ZipFile(res.archive) as zf:
        info = next(
            i for i in zf.infolist() if i.filename.endswith("outputs/partial.json")
        )
        # This fixture member is nonempty, so the former one-byte read's
        # tell() - 1 was zero. ZipFile.read returns bytes without text IO.
        assert zf.read(info)
        data_start = info.header_offset
    data[data_start] ^= 0xFF
    tampered = tmp_path / "tampered.zip"
    tampered.write_bytes(bytes(data))
    rows = verify_archive(tampered)
    failed = [r["check"] for r in rows if r["status"] == "failed"]
    assert any(c.endswith("outputs/partial.json") for c in failed), failed


def test_offline_index_resolves_locally(stored: dict[str, Any], tmp_path: Path) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    page = _text(members, res.root, "index.html")
    assert "http://" not in page and "https://" not in page
    assert "src=" not in page  # no external assets at all
    for href in ("outputs/partial.json", "envelope.json", "checks/evidence.json"):
        assert f"href='{href}'" in page or f'href="{href}"' in page


def test_evidence_index_reader_path(stored: dict[str, Any], tmp_path: Path) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    index = json.loads(_text(members, res.root, "evidence-index.json"))
    assert index["schema"] == "precis_repro_evidence_index"
    # claim → output field resolved (real NH3 state energy, seed 0)
    assert index["claims"][0]["value"] == -198.36859813898877
    assert index["chain"]["envelope"] == "envelope.json"
    assert index["chain"]["checks"] == "checks/evidence.json"
    assert "inputs/config.json" in index["chain"]["inputs"]
    values = {
        (v["file"], v["pointer"]): v.get("value") for v in index["values"] if v["file"]
    }
    assert values[("outputs/partial.json", "/states/NH3")] == -198.36859813898877


def test_crate_metadata_is_standard_and_files_hashed(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    crate = json.loads(_text(members, res.root, "ro-crate-metadata.json"))
    assert crate["@context"] == "https://w3id.org/ro/crate/1.3/context"
    graph = {e["@id"]: e for e in crate["@graph"]}
    root = graph["./"]
    assert root["conformsTo"] == {"@id": "https://w3id.org/ro/wfrun/process/0.6"}
    # the profile URI resolves to an in-graph profile entity (spec example shape)
    profile = graph["https://w3id.org/ro/wfrun/process/0.6"]
    assert "CreativeWork" in profile["@type"]
    # datePublished rides the run's recorded stamp, never a wall clock
    assert root["datePublished"] == "2026-09-29T03:43:53.149474+00:00"
    action = graph["#seed-run-455725"]
    assert action["@type"] == "CreateAction"
    assert action["startTime"] == "2026-09-29T03:43:53.149474+00:00"
    part = graph["outputs/partial.json"]
    assert (
        part["sha256"]
        == hashlib.sha256(members[f"{res.root}outputs/partial.json"]).hexdigest()
    )
    # every staged member and the MANIFEST entity are hasPart'd
    parts = {p["@id"] for p in root["hasPart"]}
    assert "MANIFEST.sha256" in parts and "#pathway-context" in parts


def test_crate_keeps_pathway_stamps_out_of_the_seed_execution(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    """bundle.py's ``execution_binding`` is unproven: the pathway's mutable
    stamps (engine version, env pins, manifest, methods, seeds) must not
    land in the seed action's object/result — they group under
    ``#pathway-context`` instead, and the instrument declares the stamp's
    provenance."""
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    crate = json.loads(_text(members, res.root, "ro-crate-metadata.json"))
    graph = {e["@id"]: e for e in crate["@graph"]}
    action = graph["#seed-run-455725"]
    bound = {
        e["@id"]
        for key in ("object", "result")
        for e in (
            [action.get(key)]
            if isinstance(action.get(key), dict)
            else action.get(key) or []
        )
    }
    assert bound and not any(
        p.startswith(("env/", "manifest/")) or p in {"seeds.json", "envelope.json"}
        for p in bound
    )
    context = graph["#pathway-context"]
    stamped = {p["@id"] for p in context["hasPart"]}
    assert {
        "env/platform.json",
        "env/weights.json",
        "manifest/methods.md",
        "manifest/pathway_snapshot.json",
        "seeds.json",
    } <= stamped
    assert "unproven" in context["description"]
    engine = graph["#autocatpath"]
    assert engine["version"] == "0.22.0"  # the pathway's recorded stamp
    assert "unproven" in engine["description"]


def test_retained_methods_body_staged_with_source_identity(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    staged = _text(members, res.root, "manifest/methods.md")
    assert staged == stored["methods"]
    assert staged.startswith("# Methods — NO -> NH3 on Pd")
    run = json.loads(_text(members, res.root, "provenance/run.json"))
    assert run["methods"]["path"] == "manifest/methods.md"
    assert "pathway_body" in run["methods"]["source"]
    assert (
        run["methods"]["sha256"] == hashlib.sha256(staged.encode("utf-8")).hexdigest()
    )


def test_missing_methods_body_is_disclosed_not_dropped(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path, methods=None)
    members = _members(res.archive)
    assert f"{res.root}manifest/methods.md" not in members
    missing = json.loads(_text(members, res.root, "provenance/missing.json"))["missing"]
    assert "manifest/methods.md" in missing
    run = json.loads(_text(members, res.root, "provenance/run.json"))
    assert run["methods"]["staged"] is False


def test_absent_artifact_inventory_and_weights_stay_explicit(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    """The real record predates artifact retention and D10: no fabricated
    pins — the bundle discloses them."""
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    assert not any(n.startswith(f"{res.root}artifacts/") for n in members)
    envelope = json.loads(_text(members, res.root, "envelope.json"))
    assert "artifacts" in envelope["missing"]
    weights = json.loads(_text(members, res.root, "env/weights.json"))
    assert weights["weights_sha256"] is None
    assert weights["bundled"] is False
    missing = json.loads(_text(members, res.root, "provenance/missing.json"))["missing"]
    assert "env/wheels/" in missing and "viewer/" in missing
    assert "pathway_context/validation_manifest" in missing
    checks = json.loads(_text(members, res.root, "checks/evidence.json"))["checks"]
    replay = {c["check"]: c["status"] for c in checks if c["scope"] == "replay"}
    assert replay["weights"] == "unavailable"
    assert replay["catpath wheel"] == "unavailable"
    assert replay["re-execution"] == "not-run"


def test_unavailable_artifact_stays_explicit(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    """A recorded artifact whose bytes are gone stays pinned and disclosed."""
    stored["job_meta"]["artifacts"] = [
        {
            "name": "ts.extxyz",
            "path": f"job-artifacts/{stored['job_id']}/ts.extxyz",
            "bytes": 0,
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        }
    ]

    def missing_reader(entry: dict[str, Any]) -> bytes:
        raise FileNotFoundError(entry["name"])

    res = _build(stored, tmp_path, read_artifact=missing_reader)
    members = _members(res.archive)
    assert f"{res.root}artifacts/job-artifacts/7/ts.extxyz" not in members
    envelope = json.loads(_text(members, res.root, "envelope.json"))
    assert (
        f"artifacts/job-artifacts/{stored['job_id']}/ts.extxyz/data"
        in envelope["missing"]
    )


def test_publication_gate_refuses_staged_secrets(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    stored["job_meta"]["params"]["config"]["search"]["note"] = (
        "run on spark at 100.126.0.1"  # secret-gate: allow — planted sample
    )
    with pytest.raises(ArchiveRefused, match="publication gate"):
        _build(stored, tmp_path)


def test_unresolvable_claim_pin_refuses(stored: dict[str, Any], tmp_path: Path) -> None:
    stored["claims"] = [
        {"id": "bad", "file": "outputs/partial.json", "pointer": "/nope"}
    ]
    with pytest.raises(ArchiveRefused, match="does not resolve"):
        _build(stored, tmp_path)


def test_unfrozen_record_is_marked_preliminary(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    """pw455722 carries no ``repro:frozen`` tag — the packaged case is
    preliminary; a frozen flag flips it."""
    res = _build(stored, tmp_path)
    assert res.preliminary is True
    members = _members(res.archive)
    index = json.loads(_text(members, res.root, "evidence-index.json"))
    assert index["preliminary"] is True
    crate = json.loads(_text(members, res.root, "ro-crate-metadata.json"))
    root = next(e for e in crate["@graph"] if e["@id"] == "./")
    assert root["creativeWorkStatus"] == "preliminary"
    assert "PRELIMINARY" in _text(members, res.root, "README.md")

    frozen = _build(stored, tmp_path, frozen=True)
    assert frozen.preliminary is False


def test_plan_preview_matches_the_export(stored: dict[str, Any]) -> None:
    plan = plan_archive(
        job_id=stored["job_id"],
        job_meta=stored["job_meta"],
        pathway_id=stored["pathway_id"],
        pathway_meta=stored["pathway_meta"],
        slug=stored["slug"],
        title=stored["title"],
        frozen=stored["frozen"],
        methods=stored["methods"],
        read_artifact=_reader(stored),
    )
    assert plan["schema"] == "precis_repro_bundle"
    paths = {f["path"] for f in plan["files"]}
    assert {"index.html", "MANIFEST.sha256", "ro-crate-metadata.json"} <= paths
    assert "manifest/methods.md" in paths
    assert plan["gate_findings"] == {}
    assert plan["preliminary"] is True


def test_missing_rocrate_writer_refuses_with_next(
    stored: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("rocrate"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ArchiveRefused, match="repro"):
        _build(stored, tmp_path)


@pytest.fixture
def _offline_ro_crate_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the vendored RO-Crate 1.3 JSON-LD context fixture instead of
    fetching ``w3id.org`` — the suite makes no network calls, and the
    validator's own context check still sees a retrievable URI."""
    pytest.importorskip("rocrate_validator")
    from rocrate_validator.utils import document_loader
    from rocrate_validator.utils.http import HttpRequester

    document_loader.install_document_loader()
    ctx = json.loads(_CONTEXT_13.read_text(encoding="utf-8"))
    body = json.dumps(ctx)

    class _Response:
        status_code = 200
        headers = {"Content-Type": "application/ld+json"}
        text = body

        @staticmethod
        def json() -> Any:
            return ctx

    def _get(self: Any, url: Any, **kwargs: Any) -> Any:
        if str(url) == "https://w3id.org/ro/crate/1.3/context":
            return _Response()
        raise AssertionError(f"validator reached for the network: {url}")

    # ``get`` is delegated to the requester's session via ``__getattr__``,
    # so it does not exist on the class — a class-level patch still wins
    # the instance lookup.
    monkeypatch.setattr(HttpRequester, "get", _get, raising=False)


@pytest.mark.usefixtures("_offline_ro_crate_context")
def test_crate_validates_against_the_pinned_profiles(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    """The shipped ``ro-crate-metadata.json`` must pass the pinned
    ``roc-validator`` at REQUIRED severity — the writer/validator proof for
    the declared RO-Crate 1.3 / Process Run Crate 0.6 pair.

    The bundled Process Run profile is the 0.5 generation: its only
    REQUIRED holdout on a 0.6 crate is ``ro-crate-1.1_5.3``, which pins the
    descriptor's ``conformsTo`` to ``ro/crate/1.1`` — a version pin the 0.6
    spec's own example contradicts (it declares ``1.3``, as this crate
    does). Everything else the validator can check must pass."""
    from rocrate_validator.models import ValidationSettings
    from rocrate_validator.services import validate

    res = _build(stored, tmp_path)
    crate_dir = tmp_path / "crate"
    with zipfile.ZipFile(res.archive) as zf:
        zf.extractall(crate_dir)
    crate_root = crate_dir / res.root.rstrip("/")

    def run(profile: str) -> Any:
        return validate(
            ValidationSettings(
                rocrate_uri=crate_root,
                profile_identifier=profile,
                requirement_severity="REQUIRED",
                offline=True,
            )
        )

    assert run("ro-crate-1.3").passed()
    process = run("process-run-crate-0.5")
    holdouts = {
        getattr(getattr(i, "check", None), "identifier", None) for i in process.issues
    }
    assert holdouts == {"ro-crate-1.1_5.3"}, [str(i) for i in process.issues]
