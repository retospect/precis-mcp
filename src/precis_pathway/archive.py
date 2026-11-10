"""Portable, inspectable evidence archive for one stored pathway seed run.

The paper-facing half of ``docs/backlog/catpath-repro-bundle.md``: from the
stored-seed envelope (:func:`precis_pathway.bundle.build_envelope`) plus the
frozen pathway snapshot, stage the paper-first subset of the bundle layout —
explicit file inventory, ``MANIFEST.sha256``, RO-Crate metadata (RO-Crate 1.3
with the Process Run Crate 0.6 profile, written through ``ro-crate-py``), the
frozen inputs/outputs/artifacts and environment identities that actually
exist, the recorded verification checks, and a self-contained ``index.html``
inspection page — then zip it deterministically.

Honest-gap rules, same as the envelope: a file is staged only when its bytes
exist in the selected snapshots or the artifact store; everything else lands
in ``provenance/missing.json`` and stays explicitly unavailable. Weights and
the private catpath wheel are *pins*, never bundled payloads — a missing or
private dependency is disclosed, never represented as replayable. The
viewer-less decision (spec decision 1) applies: no catpath report outdir is
retained, so instead of a reconstructed viewer the archive ships an evidence
index (``index.html`` + ``evidence-index.json``) giving the reader path
claim/value → output field → run/envelope/checks → inputs.

``bundle_id`` is the SHA-256 of ``MANIFEST.sha256`` — the identity a nanopub
can cite. It is deliberately NOT inside the archive (any member holding it
would change the manifest it hashes); it rides the zip's root directory name
``<slug>-<bundle_id[:12]>/`` and the ``<stem>.repro.json`` sidecar written
beside the archive, the same sidecar pattern as
``precis.export._data_package``.

Determinism: identical frozen inputs produce identical archive bytes — file
bytes come from canonical serialization, zip entries are sorted with fixed
mtimes and no extra fields, and no wall clock is read anywhere. Staged text
is passed through the publication gate
(:func:`precis.utils.public_scan.scan_public_files`) before the manifest is
written; findings refuse the export.

No engine, compute, DB mutation or network access is involved. The writer
needs the ``rocrate`` distribution (``precis-mcp[repro]``); everything else is
stdlib plus stored bytes.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from precis.errors import BadInput
from precis.utils.public_scan import scan_public_files

from .bundle import ArtifactReader, EnvelopeRefused, build_envelope, canonical_bytes
from .verification import Status, _check, recorded_checks

SCHEMA = "precis_repro_bundle"
SCHEMA_VERSION = 1

#: RO-Crate 1.3 + Process Run Crate 0.6 (the standards-checked pair — do not
#: mix with the older 0.5/1.1 profile pair).
PROCESS_RUN_PROFILE = "https://w3id.org/ro/wfrun/process/0.6"

#: The frozen export marker (spec decision 3): an open tag on the pathway.
FROZEN_TAG = "repro:frozen"

_SIDECAR_SUFFIX = ".repro.json"
_MANIFEST_NAME = "MANIFEST.sha256"
_CRATE_NAME = "ro-crate-metadata.json"
_INDEX_NAME = "index.html"
_EVIDENCE_INDEX_NAME = "evidence-index.json"

#: Leaf-value cap for the evidence index — a state's payload stays
#: inspectable without re-dumping whole geometry blobs into the index.
_MAX_INDEXED_VALUES = 400
_MAX_INLINE_VALUE = 160

_ARCHIVE_MISSING_BASE: dict[str, str] = {
    "viewer/": "no retained catpath report outdir — the viewer-less slice "
    "(spec decision 1) ships the evidence index instead",
    "env/uv.lock": "not staged in this slice — recorded in D10 lock pin when stamped",
    "env/wheels/": "the catpath wheel is private — its sha256/rev is recorded "
    "in provenance when stamped, never bundled",
    "replay": "reproduce.sh/verify.py are a later slice; this archive is "
    "inspection evidence, not a certified replay package",
}


class ArchiveRefused(BadInput):
    """The staged evidence contradicts the requested archive, or a required
    input (artifact bytes, the ``rocrate`` writer, the publication gate) is
    absent — raised, never silently degraded."""


@dataclass(frozen=True)
class ArchiveResult:
    """What :func:`build_archive` produced: the deterministic zip, its
    sidecar identity record, the ``bundle_id`` (sha256 of MANIFEST.sha256)
    and the archive root directory name the id prefixes."""

    archive: Path
    sidecar: Path
    bundle_id: str
    root: str
    files: tuple[str, ...]
    missing: dict[str, str]
    preliminary: bool


def _safe_slug(slug: str | None, pathway_id: int) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (slug or "").strip()).strip("-.")
    return base or f"pathway-{pathway_id}"


def _record_bytes(record: dict[str, Any]) -> bytes:
    """The exact bytes an inline envelope record's sha256 attests."""
    data = record["data"]
    raw = (
        data.encode("utf-8") if record["encoding"] == "utf-8" else canonical_bytes(data)
    )
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise ArchiveRefused(f"{record['name']}: inline sha256 disagrees with data")
    return raw


def _json_pointer(data: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 pointer into a JSON-shaped value; raises
    :class:`ArchiveRefused` naming the dead segment."""
    node = data
    for raw in pointer.strip("/").split("/") if pointer.strip("/") else []:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            raise ArchiveRefused(f"claim pointer {pointer!r} does not resolve")
    return node


def _escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _index_values(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every scalar leaf of the staged output records, addressed by file +
    JSON pointer — the ``claim/value → selected output field`` index step.

    Long strings (geometry blobs, methods text) are pinned by sha256 + byte
    count instead of inlining: the index stays a *reader*, not a copy.
    """
    entries: list[dict[str, Any]] = []
    truncated = False

    def walk(node: Any, name: str, pointer: str) -> None:
        nonlocal truncated
        if len(entries) >= _MAX_INDEXED_VALUES:
            truncated = True
            return
        if isinstance(node, dict):
            for key in sorted(node):
                walk(node[key], name, f"{pointer}/{_escape_token(str(key))}")
            return
        if isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, name, f"{pointer}/{i}")
            return
        entry: dict[str, Any] = {"file": name, "pointer": pointer or "/"}
        if isinstance(node, str) and len(node) > _MAX_INLINE_VALUE:
            raw = node.encode("utf-8")
            entry.update(
                {
                    "kind": "blob",
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        else:
            entry["value"] = node
        entries.append(entry)

    for record in outputs:
        walk(record["data"], record["name"], "")
    if truncated:
        entries.append(
            {
                "file": "",
                "pointer": "",
                "truncated": f"index capped at {_MAX_INDEXED_VALUES} values; "
                "read the outputs/* files for the rest",
            }
        )
    return entries


def _timestamps(job_meta: dict[str, Any]) -> dict[str, Any]:
    """Captured stamps only — the archive never reads a clock; a stamp the
    producer did not write stays absent (an input, not an invention)."""
    return {
        key: job_meta[key]
        for key in ("created_at", "started_at", "finished_at")
        if job_meta.get(key) is not None
    }


def _check_rows(
    envelope: dict[str, Any],
    pathway_meta: dict[str, Any],
    *,
    preliminary: bool,
) -> list[dict[str, Any]]:
    """Recorded D10 projections plus the archive's own availability and
    replay-disclosure rows — all in the ``verification`` check shape."""
    rows = recorded_checks(pathway_meta)
    availability_status: dict[str, Status] = {
        "verified": "passed",
        "unavailable": "unavailable",
        "unverified": "not-run",
    }
    for entry in envelope["artifacts"]:
        status = availability_status[entry["availability"]]
        rows.append(
            _check(
                "artifacts",
                str(entry["name"]),
                status,
                "job-artifacts stored-byte verification",
                {
                    "path": entry["path"],
                    "sha256": entry["sha256"],
                    "availability": entry["availability"],
                },
            )
        )
    weights = envelope["provenance"].get("weights_sha256")
    rows.append(
        _check(
            "replay",
            "weights",
            "unavailable" if weights is None else "not-run",
            "sha256 pin, not bundled",
            {
                "weights_sha256": weights,
                "note": "weights are pinned, never shipped; a missing or "
                "private checkpoint is not replayable",
            },
        )
    )
    rows.append(
        _check(
            "replay",
            "catpath wheel",
            "unavailable",
            "private repository",
            "the wheel is recorded by rev/sha when stamped and never bundled",
        )
    )
    rows.append(
        _check(
            "replay",
            "re-execution",
            "not-run",
            "inspection slice",
            "reproduce.sh/verify.py ship in a later slice; nothing was re-run",
        )
    )
    for row in rows:
        row["preliminary"] = preliminary
    return rows


def _payload_files(
    envelope: dict[str, Any],
    job_meta: dict[str, Any],
    pathway_id: int,
    pathway_meta: dict[str, Any],
    *,
    slug: str,
    title: str | None,
    frozen: bool,
    claims: list[dict[str, Any]],
    artifact_bytes: dict[str, bytes],
    methods: str | None = None,
) -> tuple[dict[str, bytes], dict[str, str]]:
    """The staged ``{relpath: bytes}`` map minus ``ro-crate-metadata.json``
    and ``MANIFEST.sha256`` (the two files that close over the rest), plus
    the full disclosed-gap map (envelope's own plus archive-level)."""
    missing = dict(envelope["missing"])
    manifest_rec = envelope["pathway_context"].get("validation_manifest")
    params = job_meta.get("params") or {}
    mlip = (params.get("config") or {}).get("mlip") or {}
    run = envelope["run"]

    files: dict[str, bytes] = {}
    files["envelope.json"] = canonical_bytes(envelope)
    for record in envelope["inputs"] + envelope["outputs"]:
        files[record["name"]] = _record_bytes(record)
    for entry in envelope["artifacts"]:
        if entry["availability"] == "verified":
            files[f"artifacts/{entry['path']}"] = artifact_bytes[entry["path"]]

    selection: dict[str, Any] = next(
        (r["data"] for r in envelope["inputs"] if r["name"] == "inputs/selection.json"),
        {},
    )
    files["seeds.json"] = canonical_bytes(
        {
            "schema": "precis_repro_seeds",
            "schema_version": 1,
            "selection": selection,
            "manifest_seeds": (manifest_rec or {}).get("seeds"),
        }
    )
    if manifest_rec is not None:
        files["manifest/validation_manifest.json"] = canonical_bytes(manifest_rec)
    files["manifest/pathway_snapshot.json"] = canonical_bytes(pathway_meta)
    methods_record: dict[str, Any] = {
        "path": "manifest/methods.md",
        "source": "pathway ref retained body chunk (chunk_kind="
        "'pathway_body'), restamped by persist_result from the engine's "
        "methods.md — a mutable pathway record, not a seed-job output",
        "staged": methods is not None,
    }
    if methods is not None:
        raw_methods = methods.encode("utf-8")
        files["manifest/methods.md"] = raw_methods
        methods_record.update(
            bytes=len(raw_methods), sha256=hashlib.sha256(raw_methods).hexdigest()
        )
    files["env/platform.json"] = canonical_bytes(
        {
            "schema": "precis_repro_platform",
            "schema_version": 1,
            "source": "pathway_meta.validation_manifest.platform",
            "recorded": (manifest_rec or {}).get("platform"),
            "note": "recorded at run time; excluded from the D10 manifest_id "
            "but still checksummed in MANIFEST.sha256",
        }
    )
    lock_mlip = ((manifest_rec or {}).get("lock") or {}).get("mlip")
    files["env/weights.json"] = canonical_bytes(
        {
            "schema": "precis_repro_weights",
            "schema_version": 1,
            "backend": mlip.get("backend"),
            "model": run.get("model") or mlip.get("model"),
            "weights_sha256": envelope["provenance"].get("weights_sha256"),
            "bundled": False,
            "disclosure": "weights are pinned by sha256, never shipped; a "
            "missing or private checkpoint is not replayable",
            "mlip": lock_mlip,
        }
    )
    files["provenance/run.json"] = canonical_bytes(
        {
            "schema": "precis_repro_run",
            "schema_version": 1,
            "run": run,
            "pathway": {
                "ref_id": pathway_id,
                "slug": slug,
                "title": title,
                "frozen": frozen,
            },
            "pathway_context": envelope["pathway_context"],
            "timestamps": _timestamps(job_meta),
            "finding": pathway_meta.get("finding_ref") or pathway_meta.get("nanopub"),
            "methods": methods_record,
        }
    )
    archive_missing = dict(_ARCHIVE_MISSING_BASE)
    if methods is None:
        archive_missing["manifest/methods.md"] = (
            "no retained methods body chunk on the pathway ref"
        )
    if not (pathway_meta.get("finding_ref") or pathway_meta.get("nanopub")):
        archive_missing["provenance/finding"] = (
            "no finding/nanopub link stamped on the pathway snapshot"
        )
    missing.update(archive_missing)
    files["provenance/missing.json"] = canonical_bytes(
        {"schema": "precis_repro_missing", "schema_version": 1, "missing": missing}
    )

    preliminary = not frozen
    files["checks/evidence.json"] = canonical_bytes(
        {
            "schema": "precis_repro_evidence_checks",
            "schema_version": 1,
            "preliminary": preliminary,
            "checks": _check_rows(envelope, pathway_meta, preliminary=preliminary),
            "note": "recorded checks only — nothing was re-run; archive "
            "integrity is verified post-build against MANIFEST.sha256",
        }
    )

    values = _index_values(envelope["outputs"])
    resolved_claims = _resolve_claims(claims, envelope["outputs"])
    index = {
        "schema": "precis_repro_evidence_index",
        "schema_version": 1,
        "title": title or slug,
        "preliminary": preliminary,
        "reader_path": "claim/value → outputs/<file>#<pointer> → "
        "provenance/run.json + envelope.json → checks/evidence.json → inputs/*",
        "claims": resolved_claims,
        "values": values,
        "chain": {
            "outputs": sorted(r["name"] for r in envelope["outputs"]),
            "run": "provenance/run.json",
            "envelope": "envelope.json",
            "checks": "checks/evidence.json",
            "inputs": sorted(r["name"] for r in envelope["inputs"]) + ["seeds.json"],
        },
        "missing": missing,
        "files": [
            {
                "path": rel,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for rel, data in sorted(files.items())
        ],
        "self": "index.html, README.md, ro-crate-metadata.json, "
        "MANIFEST.sha256 and this index are checksummed in MANIFEST.sha256",
    }
    files[_EVIDENCE_INDEX_NAME] = canonical_bytes(index)
    files["README.md"] = _readme(slug, title, preliminary, missing).encode("utf-8")
    files[_INDEX_NAME] = _index_html(slug, title, preliminary, index, missing).encode(
        "utf-8"
    )
    return files, missing


def _resolve_claims(
    claims: list[dict[str, Any]], outputs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Caller-supplied claim/figure pins, each resolved to a staged output
    pointer — a pin that does not resolve refuses, never silently drops."""
    by_name = {r["name"]: r["data"] for r in outputs}
    out = []
    for claim in claims:
        cid = claim.get("id")
        file, pointer = claim.get("file"), claim.get("pointer", "")
        if not cid or file not in by_name:
            raise ArchiveRefused(
                f"claim pin {claim!r}: id is required and file must name a "
                "staged outputs/* record",
                options=sorted(by_name),
            )
        value = _json_pointer(by_name[file], str(pointer))
        out.append(
            {
                "id": str(cid),
                "label": claim.get("label"),
                "file": file,
                "pointer": str(pointer),
                "value": value,
            }
        )
    return out


def _readme(
    slug: str, title: str | None, preliminary: bool, missing: dict[str, str]
) -> str:
    flag = (
        "\n**PRELIMINARY** — the pathway is not tagged `repro:frozen`; the "
        "result may still change and this archive is evidence, not a frozen "
        "record.\n"
        if preliminary
        else ""
    )
    gaps = "\n".join(f"- `{k}` — {v}" for k, v in sorted(missing.items()))
    return f"""# Reproducibility bundle: {title or slug}

One frozen `autocatpath_seed` run of pathway `{slug}`, packaged for
inspection off-cluster. Every file is SHA-256 checksummed in
`MANIFEST.sha256`; the archive identity (`bundle_id`) is the SHA-256 of that
manifest and rides the zip's root directory name — it is deliberately not a
member (a member holding it would change the manifest it hashes).
{flag}
## Inspect

- Open `index.html` — the offline evidence index: reported value → output
  field → run/envelope → recorded checks → inputs. No network access needed.
- `evidence-index.json` is the same index machine-readable.
- `ro-crate-metadata.json` describes the crate (RO-Crate 1.3, Process Run
  Crate 0.6 profile).

## Verify integrity

Inside the extracted root directory:

    sha256sum -c MANIFEST.sha256

or, with precis installed (no database needed):

    precis pathway inspect <this-archive>.zip
    precis pathway verify-report <this-archive>.zip \\
        --record manifest/pathway_snapshot.json --output-dir <dir>

A modified file fails the checksum pass *before* any compute — the archive
never asks a reader to re-run it.

## Honest limits

- No viewer: the catpath report outdir is not retained by precis, so this
  archive ships the evidence index rather than a reconstructed viewer.
- Weights and the catpath wheel are sha256 *pins*, not payloads — a missing
  or private checkpoint is disclosed, never replayable.
- `provenance/missing.json` is the authoritative list of what the stored
  record does not attest.
- Numerical reproduction is judged by the recorded D10 re-validation in
  `checks/evidence.json`, not by this archive.

## Disclosed gaps

{gaps}
"""


def _index_html(
    slug: str,
    title: str | None,
    preliminary: bool,
    index: dict[str, Any],
    missing: dict[str, str],
) -> str:
    """The offline inspection page — self-contained, relative links only,
    no fonts/scripts/CDN (``file://`` must render it whole)."""
    esc = html.escape
    prelim = (
        '<p class="warn"><b>PRELIMINARY</b> — the pathway is not tagged '
        "<code>repro:frozen</code>; this is evidence, not a frozen record.</p>"
        if preliminary
        else ""
    )
    claims_rows = "".join(
        f"<tr><td>{esc(str(c['id']))}</td><td>{esc(str(c.get('label') or ''))}</td>"
        f"<td><a href='{esc(c['file'])}'><code>{esc(c['file'])}</code></a>"
        f"<code>{esc(c['pointer'])}</code></td><td>{esc(json.dumps(c['value'])[:120])}</td></tr>"
        for c in index["claims"]
    )
    claims_table = (
        f"<table><tr><th>claim</th><th>label</th><th>output field</th><th>value</th></tr>{claims_rows}</table>"
        if index["claims"]
        else "<p>No claim pins supplied — the reported values below are the evidence.</p>"
    )
    value_rows = "".join(
        f"<tr><td><a href='{esc(v['file'])}'><code>{esc(v['file'])}</code></a></td>"
        f"<td><code>{esc(v['pointer'])}</code></td>"
        f"<td>{esc(json.dumps(v.get('value'))[:120]) if 'value' in v else esc(v.get('truncated', 'blob ' + v.get('sha256', '')[:16]))}</td></tr>"
        for v in index["values"]
    )
    file_rows = "".join(
        f"<tr><td><a href='{esc(f['path'])}'><code>{esc(f['path'])}</code></a></td>"
        f"<td>{f['bytes']}</td><td><code>{esc(f['sha256'][:16])}…</code></td></tr>"
        for f in index["files"]
    )
    missing_rows = "".join(
        f"<tr><td><code>{esc(k)}</code></td><td>{esc(v)}</td></tr>"
        for k, v in sorted(missing.items())
    )
    chain = index["chain"]
    chain_links = " · ".join(
        f"<a href='{esc(p)}'><code>{esc(p)}</code></a>"
        for p in [chain["run"], chain["envelope"], chain["checks"], *chain["inputs"]]
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Repro bundle — {esc(title or slug)}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 60em; margin: 2em auto; padding: 0 1em; }}
table {{ border-collapse: collapse; width: 100%; font-size: 90%; }}
td, th {{ border: 1px solid #ccc; padding: 2px 6px; text-align: left; vertical-align: top; }}
code {{ font-size: 88%; }}
.warn {{ border: 1px solid #a60; background: #ffd; padding: .6em; }}
h2 {{ border-bottom: 1px solid #ccc; }}
</style></head><body>
<h1>Reproducibility bundle — {esc(title or slug)}</h1>
{prelim}
<p>Schema <code>{esc(index["schema"])}</code> v{index["schema_version"]} ·
read order: <b>{esc(index["reader_path"])}</b></p>
<h2>Claims</h2>
{claims_table}
<h2>Reported values</h2>
<table><tr><th>file</th><th>pointer</th><th>value</th></tr>{value_rows}</table>
<h2>Evidence chain</h2>
<p>{chain_links}</p>
<h2>Files (see MANIFEST.sha256)</h2>
<table><tr><th>path</th><th>bytes</th><th>sha256</th></tr>{file_rows}</table>
<p>{esc(index["self"])}</p>
<h2>Disclosed gaps</h2>
<table><tr><th>item</th><th>status</th></tr>{missing_rows}</table>
</body></html>
"""


def _crate_bytes(
    files: dict[str, bytes],
    *,
    slug: str,
    title: str | None,
    job_id: int,
    job_meta: dict[str, Any],
    pathway_id: int,
    pathway_meta: dict[str, Any],
    preliminary: bool,
) -> bytes:
    """``ro-crate-metadata.json`` via ``ro-crate-py`` — the existing writer
    the standards check names. Refuses with a next step when the ``repro``
    extra is absent; never hand-rolls the JSON-LD."""
    try:
        from rocrate.model.softwareapplication import SoftwareApplication
        from rocrate.rocrate import ROCrate
    except ImportError as exc:
        raise ArchiveRefused(
            "RO-Crate metadata needs the 'repro' extra (ro-crate-py)",
            next="uv sync --extra repro  (or: uv pip install 'precis-mcp[repro]')",
        ) from exc

    crate = ROCrate()
    crate.metadata.append_to("conformsTo", {"@id": PROCESS_RUN_PROFILE})
    # The profile URI must resolve to an in-graph entity — RO-Crate's
    # conformsTo MUST reference prof:Profile entities and the Process Run
    # Crate check wants a CreativeWork, so it is typed as both.
    crate.add_jsonld(
        {
            "@id": PROCESS_RUN_PROFILE,
            "@type": ["CreativeWork", "http://www.w3.org/ns/dx/prof/Profile"],
            "name": "Process Run Crate",
            "version": PROCESS_RUN_PROFILE.rsplit("/", 1)[-1],
        }
    )
    root = crate.root_dataset
    root["conformsTo"] = {"@id": PROCESS_RUN_PROFILE}
    root["name"] = title or slug
    root["description"] = (
        "precis pathway seed-run reproducibility bundle: frozen inputs, "
        "outputs, artifacts, environment pins and recorded checks for one "
        "autocatpath_seed job."
    )
    root["license"] = {"@id": "https://creativecommons.org/licenses/by/4.0/"}
    root["identifier"] = f"precis:pathway:{pathway_id}"
    if preliminary:
        root["creativeWorkStatus"] = "preliminary"
    stamps = _timestamps(job_meta)
    # ro-crate-py stamps datePublished with the wall clock — a build-time
    # stamp the deterministic archive cannot carry. The profile REQUIRES a
    # datePublished, so it rides the run's own recorded stamps (the
    # record's stamps are inputs, never a clock read); a snapshot with no
    # stamps stays absent — an input gap, not an invention.
    del root["datePublished"]
    published = (
        stamps.get("finished_at")
        or stamps.get("started_at")
        or stamps.get("created_at")
    )
    if published is not None:
        root["datePublished"] = published

    data_entities = {}
    for rel, data in sorted(files.items()):
        entity = crate.add_file(
            io.BytesIO(data),
            dest_path=rel,
            properties={
                "contentSize": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        )
        data_entities[rel] = entity
    crate.add_jsonld(
        {
            "@id": _MANIFEST_NAME,
            "@type": "File",
            "description": "SHA-256 checksum manifest covering every other "
            "file; its own digest is the bundle_id carried in the archive "
            "root directory name (a member cannot hash the manifest it "
            "joins).",
        }
    )

    rec = pathway_meta.get("validation_manifest") or {}
    engine_ver = pathway_meta.get("autocatpath_version") or (
        (rec.get("code") or {}).get("autocatpath_version")
    )
    # The pathway's engine stamp is mutable related context — bundle.py's
    # execution_binding is "unproven". The version stays on the instrument
    # (the profile REQUIRES it) but its provenance is declared on the
    # entity, and the stamp files it came from sit under #pathway-context,
    # never in the action's object/result.
    engine_props: dict[str, Any] = {
        "name": "autocatpath",
        "url": "https://pypi.org/project/autocatpath/",
        "description": "engine identity; the version is the pathway ref's "
        "recorded stamp — whether this seed ran under it is unproven "
        "(envelope.json: pathway_context.execution_binding)",
    }
    if engine_ver:
        engine_props["version"] = str(engine_ver)
    engine = crate.add(
        SoftwareApplication(crate, "#autocatpath", properties=engine_props)
    )
    context_parts = [
        p
        for p in sorted(files)
        if p.startswith(("manifest/", "env/")) or p == "seeds.json"
    ]
    crate.add_jsonld(
        {
            "@id": "#pathway-context",
            "@type": "Dataset",
            "name": "pathway stamps — related context",
            "description": "records stamped on the pathway's mutable "
            "snapshot at export time (engine version, environment pins, "
            "validation manifest, methods body, aggregate seeds): the seed "
            "envelope marks this binding 'unproven' — pathway stamps do "
            "not attest to the selected run's execution environment "
            "(envelope.json: pathway_context.execution_binding).",
            "hasPart": [{"@id": p} for p in context_parts],
        }
    )
    objects = [data_entities[p] for p in sorted(files) if p.startswith("inputs/")]
    results = [
        data_entities[p]
        for p in sorted(files)
        if p.startswith(("outputs/", "artifacts/"))
    ]
    action_props: dict[str, Any] = {
        "name": f"autocatpath_seed job {job_id}",
        "description": "the stored autocatpath_seed run this bundle freezes; "
        "recorded evidence, not a re-execution. Pathway stamps "
        "(#pathway-context) are related context, not proof of this run's "
        "execution environment.",
    }
    if "started_at" in stamps:
        action_props["startTime"] = stamps["started_at"]
    if "finished_at" in stamps:
        action_props["endTime"] = stamps["finished_at"]
    action = crate.add_action(
        engine,
        identifier=f"#seed-run-{job_id}",
        object=objects,
        result=results,
        properties=action_props,
    )
    # The root MUST hasPart every data entity — the file entities plus the
    # MANIFEST descriptor and the pathway-context group (its members are
    # linked transitively through it).
    root["hasPart"] = [{"@id": rel} for rel in sorted(data_entities)] + [
        {"@id": _MANIFEST_NAME},
        {"@id": "#pathway-context"},
    ]
    root["mentions"] = [action, {"@id": "#pathway-context"}]
    metadata = crate.metadata.generate()
    return (
        json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _manifest_bytes(files: dict[str, bytes]) -> bytes:
    """``<sha256>  <relpath>`` per file, sorted — ``sha256sum -c`` and
    ``verification.verify_archive`` compatible."""
    lines = [
        f"{hashlib.sha256(data).hexdigest()}  {rel}"
        for rel, data in sorted(files.items())
        if rel != _MANIFEST_NAME
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _zip_bytes(files: dict[str, bytes], root: str) -> bytes:
    """Deterministic zip: sorted names, fixed mtime (1980-01-01, the zip
    epoch), fixed system/attrs, no extra fields or comments — identical
    frozen inputs give identical archive bytes on any platform."""
    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zf:
        for rel in sorted(files):
            info = zipfile.ZipInfo(f"{root}{rel}", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 0
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(
                info, files[rel], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
            )
    return buf.getvalue()


def _gather(
    *,
    job_id: int,
    job_meta: dict[str, Any],
    pathway_id: int,
    pathway_meta: dict[str, Any],
    slug: str | None,
    title: str | None,
    frozen: bool,
    claims: list[dict[str, Any]],
    read_artifact: ArtifactReader | None,
    methods: str | None,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, str], str]:
    """Envelope + staged payload — the shared body of plan and build."""
    artifact_bytes: dict[str, bytes] = {}

    def caching_reader(entry: dict[str, Any]) -> bytes:
        if read_artifact is not None:
            data = read_artifact(entry)
        else:
            from precis.store.job_artifacts import read_artifact as store_read

            data = store_read(entry)
        artifact_bytes[str(entry["path"])] = data
        return data

    try:
        envelope = build_envelope(
            job_id=job_id,
            job_meta=job_meta,
            pathway_id=pathway_id,
            pathway_meta=pathway_meta,
            read_artifact=caching_reader,
        )
    except EnvelopeRefused as exc:
        raise ArchiveRefused(str(exc)) from exc
    safe = _safe_slug(slug, pathway_id)
    files, missing = _payload_files(
        envelope,
        job_meta,
        pathway_id,
        pathway_meta,
        slug=safe,
        title=title,
        frozen=frozen,
        claims=claims,
        artifact_bytes=artifact_bytes,
        methods=methods,
    )
    return files, envelope, missing, safe


def plan_archive(
    *,
    job_id: int,
    job_meta: dict[str, Any],
    pathway_id: int,
    pathway_meta: dict[str, Any],
    slug: str | None = None,
    title: str | None = None,
    frozen: bool = False,
    read_artifact: ArtifactReader | None = None,
    methods: str | None = None,
) -> dict[str, Any]:
    """The ``view='bundle-plan'`` preview: the exact staged inventory the
    export would write (paths, byte counts, sha256 pins), the disclosed
    gaps, the publication-gate findings that would refuse it, and the
    preliminary flag — without writing anything."""
    files, envelope, missing, safe = _gather(
        job_id=job_id,
        job_meta=job_meta,
        pathway_id=pathway_id,
        pathway_meta=pathway_meta,
        slug=slug,
        title=title,
        frozen=frozen,
        claims=[],
        read_artifact=read_artifact,
        methods=methods,
    )
    inventory = [
        {
            "path": rel,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for rel, data in sorted(files.items())
    ]
    crate_row: dict[str, Any] = {"path": _CRATE_NAME}
    try:
        crate = _crate_bytes(
            files,
            slug=safe,
            title=title,
            job_id=job_id,
            job_meta=job_meta,
            pathway_id=pathway_id,
            pathway_meta=pathway_meta,
            preliminary=not frozen,
        )
        crate_row.update(bytes=len(crate), sha256=hashlib.sha256(crate).hexdigest())
    except ArchiveRefused as exc:
        crate_row.update(status="unwritable", reason=exc.cause)
    inventory.append(crate_row)
    inventory.append(
        {"path": _MANIFEST_NAME, "status": "written last — hashes every other file"}
    )
    gate = scan_public_files(files)
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "slug": safe,
        "title": title,
        "preliminary": not frozen,
        "frozen_tag": FROZEN_TAG,
        "files": inventory,
        "missing": missing,
        "gate_findings": gate,
        "artifacts": [
            {"name": a["name"], "path": a["path"], "availability": a["availability"]}
            for a in envelope["artifacts"]
        ],
    }


def build_archive(
    *,
    job_id: int,
    job_meta: dict[str, Any],
    pathway_id: int,
    pathway_meta: dict[str, Any],
    out_dir: Path,
    slug: str | None = None,
    title: str | None = None,
    read_artifact: ArtifactReader | None = None,
    frozen: bool = False,
    claims: list[dict[str, Any]] | None = None,
    methods: str | None = None,
) -> ArchiveResult:
    """Stage, gate, manifest and zip the archive into ``out_dir``.

    ``methods`` is the pathway ref's retained body chunk (the frozen
    methods prose ``persist_result`` writes from the engine's
    ``methods.md``); it is staged under ``manifest/`` with its source
    recorded, and disclosed as missing when absent — never an output of
    the seed run itself.

    Returns the :class:`ArchiveResult` carrying ``bundle_id`` (the sha256 of
    ``MANIFEST.sha256`` — the citable identity) and writes
    ``<stem>.repro.json`` beside the zip: the off-archive record the paper's
    draft export can cite, since the id cannot live inside the archive.
    """
    files, envelope, missing, safe = _gather(
        job_id=job_id,
        job_meta=job_meta,
        pathway_id=pathway_id,
        pathway_meta=pathway_meta,
        slug=slug,
        title=title,
        frozen=frozen,
        claims=claims or [],
        read_artifact=read_artifact,
        methods=methods,
    )
    files[_CRATE_NAME] = _crate_bytes(
        files,
        slug=safe,
        title=title,
        job_id=job_id,
        job_meta=job_meta,
        pathway_id=pathway_id,
        pathway_meta=pathway_meta,
        preliminary=not frozen,
    )
    findings = scan_public_files(files)
    if findings:
        raise ArchiveRefused(
            "publication gate refused the staged archive — "
            + "; ".join(
                f"{rel}: {', '.join(hits[:2])}"
                for rel, hits in sorted(findings.items())
            ),
            next="scrub the named stored fields; the gate matches the repo "
            "secret scan plus node hostnames and absolute user paths",
        )
    files[_MANIFEST_NAME] = _manifest_bytes(files)
    bundle_id = hashlib.sha256(files[_MANIFEST_NAME]).hexdigest()
    root = f"{safe}-{bundle_id[:12]}/"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    archive = out / f"{root.rstrip('/')}.zip"
    archive.write_bytes(_zip_bytes(files, root))
    sidecar = out / f"{root.rstrip('/')}{_SIDECAR_SUFFIX}"
    sidecar.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "bundle_id": bundle_id,
                "root": root,
                "archive": archive.name,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "files": len(files),
                "preliminary": not frozen,
                "missing": sorted(missing),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return ArchiveResult(
        archive=archive,
        sidecar=sidecar,
        bundle_id=bundle_id,
        root=root,
        files=tuple(sorted(files)),
        missing=missing,
        preliminary=not frozen,
    )


def seed_jobs_for_pathway(
    store: Any, pathway_ref_id: int
) -> list[tuple[int, dict[str, Any]]]:
    """Every succeeded ``autocatpath_seed`` job of the pathway, oldest first —
    the exportable evidence units (:func:`modes_job.seed_jobs` additionally
    requires stored geometry, which a valid bundle does not need)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.ref_id, j.meta FROM refs j
             WHERE j.kind = 'job' AND j.retired_at IS NULL
               AND j.meta->>'job_type' = 'autocatpath_seed'
               AND (j.meta->'params'->>'pathway_ref_id')::bigint = %s
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'
                        AND t.value = 'succeeded'
                   )
             ORDER BY j.ref_id ASC
            """,
            (pathway_ref_id,),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


__all__ = [
    "FROZEN_TAG",
    "PROCESS_RUN_PROFILE",
    "SCHEMA",
    "SCHEMA_VERSION",
    "ArchiveRefused",
    "ArchiveResult",
    "build_archive",
    "plan_archive",
    "seed_jobs_for_pathway",
]
