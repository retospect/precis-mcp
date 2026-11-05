"""Versioned evidence envelope for one stored pathway seed job.

The seed is the first producer with both retained outputs and a real artifact
inventory. Aggregate membership is not retained, and the pathway's results and
D10 manifest can change after a seed finishes. Consequently the pathway record
is explicitly related context, never proof of this seed's execution environment.
Code, lock, wheel, image, effective config, units and platform remain missing:
this producer does not stamp them. Requested config and engine_token are intent.

Callers supply selected stored snapshots and retain canonical envelope bytes.
No engine, current host provenance, DB mutation or compute is involved. The
artifact store supplies verified bytes; absent historical pins are not rebuilt.
D10 identity is unchanged; output_id pins outputs and bundle_id pins the whole
envelope (including observed availability). This is an internal evidence record,
not a publication-sanitized archive or a numerical revalidation verdict.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

ArtifactReader = Callable[[dict[str, Any]], bytes]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OUTPUT_FIELDS = ("partial", "lattice", "structures", "bands", "ts")
_SELECTION_FIELDS = (
    "seed",
    "model_index",
    "force_backend",
    "only_steps",
    "step_retry",
    "engine_token",
)


class EnvelopeRefused(ValueError):
    """Stored selection or artifact evidence contradicts the requested envelope."""


def canonical_bytes(value: Any) -> bytes:
    """Canonical v1 JSON bytes; no clock, ASCII escaping or non-finite numbers."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _inline(name: str, source: str, data: Any, *, text: bool = False) -> dict[str, Any]:
    raw = data.encode("utf-8") if text else canonical_bytes(data)
    return {
        "name": name,
        "source": source,
        "encoding": "utf-8" if text else "json",
        "data": data,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def _artifact(
    entry: dict[str, Any], job_id: int, reader: ArtifactReader, missing: dict[str, str]
) -> dict[str, Any]:
    name, path = entry.get("name"), entry.get("path")
    if not isinstance(name, str) or not name:
        raise EnvelopeRefused("artifact name is missing; select a recorded inventory")
    if not isinstance(path, str):
        raise EnvelopeRefused(f"artifact {name}: recorded path is missing")
    parts = PurePosixPath(path).parts
    if (
        len(parts) != 3
        or parts[:2] != ("job-artifacts", str(job_id))
        or parts[2] in {".", ".."}
        or str(PurePosixPath(path)) != path
        or "\\" in path
    ):
        raise EnvelopeRefused(
            f"artifact {name}: path must stay in this job's directory"
        )
    sha, size = entry.get("sha256"), entry.get("bytes")
    if sha is not None and (not isinstance(sha, str) or not _SHA256.fullmatch(sha)):
        raise EnvelopeRefused(f"artifact {name}: invalid recorded sha256")
    if size is not None and (type(size) is not int or size < 0):
        raise EnvelopeRefused(f"artifact {name}: invalid recorded byte count")
    out = {"name": name, "path": path, "sha256": sha, "bytes": size}
    for field, value in (("sha256", sha), ("bytes", size)):
        if value is None:
            missing[f"artifacts/{path}/{field}"] = "not recorded by the producer"
    if sha is None or size is None:
        return {**out, "availability": "unverified"}
    try:
        data = reader(entry)
    except FileNotFoundError:
        missing[f"artifacts/{path}/data"] = "recorded artifact is unavailable"
        return {**out, "availability": "unavailable"}
    except ValueError as exc:
        raise EnvelopeRefused(f"artifact {name}: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != sha:
        raise EnvelopeRefused(f"artifact {name}: sha256 mismatch")
    if len(data) != size:
        raise EnvelopeRefused(f"artifact {name}: byte count mismatch")
    return {**out, "availability": "verified"}


def build_envelope(
    *,
    job_id: int,
    job_meta: dict[str, Any],
    pathway_id: int,
    pathway_meta: dict[str, Any],
    read_artifact: ArtifactReader | None = None,
) -> dict[str, Any]:
    """Freeze selected stored seed evidence, refusing contradictory pins.

    Args:
        job_id: Existing selected autocatpath_seed ref id.
        job_meta: Stored job metadata, including params, partial and artifacts.
        pathway_id: Existing pathway ref id named by the job.
        pathway_meta: Related stored pathway snapshot, not historical seed outputs.
        read_artifact: Optional reader of existing job-artifact entries. Its bytes
            are checked here too; FileNotFoundError records unavailability.
            Other read errors propagate rather than disguising an access fault.
    """
    if type(job_id) is not int or job_id <= 0:
        raise EnvelopeRefused("select a positive job ref id")
    if type(pathway_id) is not int or pathway_id <= 0:
        raise EnvelopeRefused("select a positive pathway ref id")
    job, pathway = copy.deepcopy(job_meta), copy.deepcopy(pathway_meta)
    if job.get("job_type") != "autocatpath_seed":
        raise EnvelopeRefused("first-slice envelope requires an autocatpath_seed job")
    params = job.get("params") or {}
    if not isinstance(params, dict) or not isinstance(job.get("partial"), dict):
        raise EnvelopeRefused(
            "select a seed job with stored params and partial outputs"
        )
    links = [job.get("pathway_ref"), params.get("pathway_ref_id")]
    recorded_links = [value for value in links if value is not None]
    if not recorded_links or any(value != pathway_id for value in recorded_links):
        raise EnvelopeRefused(
            "pathway_ref mismatch or missing; select the recorded pathway"
        )
    for key in ("seed", "model_index", "content_key"):
        observed, requested = job.get(key), params.get(key)
        if observed is not None and requested is not None and observed != requested:
            raise EnvelopeRefused(f"{key}: job stamp disagrees with params")
    manifest = pathway.get("validation_manifest")
    if manifest is not None and not isinstance(manifest, dict):
        raise EnvelopeRefused("pathway validation_manifest is not a stored object")
    manifest_id = pathway.get("manifest_id")
    if (
        manifest
        and manifest_id is not None
        and manifest.get("manifest_id") != manifest_id
    ):
        raise EnvelopeRefused("pathway manifest_id disagrees with validation_manifest")

    missing: dict[str, str] = {}
    inputs, outputs = [], []
    for key, name, is_text in (
        ("config", "inputs/config.json", False),
        ("slab_extxyz", "inputs/slab.extxyz", True),
    ):
        value = params.get(key)
        if value is None:
            missing[name] = "not recorded in job params"
        else:
            if is_text and not isinstance(value, str):
                raise EnvelopeRefused("slab_extxyz must be stored text")
            inputs.append(_inline(name, f"job.meta.params.{key}", value, text=is_text))
    selection = {key: params[key] for key in _SELECTION_FIELDS if key in params}
    inputs.append(_inline("inputs/selection.json", "job.meta.params", selection))
    for key in _OUTPUT_FIELDS:
        name = f"outputs/{key}.json"
        if job.get(key) is None:
            missing[name] = "not recorded by the seed producer"
        else:
            outputs.append(_inline(name, f"job.meta.{key}", job[key]))

    provenance: dict[str, Any] = dict.fromkeys(
        ("code", "lock", "wheel", "image", "platform", "units", "effective_config")
    )
    for key in provenance:
        missing[f"provenance/{key}"] = "not stamped by the seed producer"
    provenance["weights_sha256"] = job.get("weights_sha256")
    if provenance["weights_sha256"] is None:
        missing["provenance/weights_sha256"] = (
            "not located or not stamped; no inference"
        )
    elif not isinstance(provenance["weights_sha256"], str) or not _SHA256.fullmatch(
        provenance["weights_sha256"]
    ):
        raise EnvelopeRefused("invalid seed weights_sha256")

    run: dict[str, Any] = {"job_id": job_id, "job_type": "autocatpath_seed"}
    for key in ("content_key", "seed", "model_index", "model"):
        run[key] = job.get(key)
        if run[key] is None:
            missing[f"run/{key}"] = "not stamped by the seed producer"
    context = {
        "pathway_ref_id": pathway_id,
        "content_key": pathway.get("content_key"),
        "manifest_id": manifest_id,
        "validation_manifest": manifest,
        "execution_binding": "unproven: pathway stamps do not attest to seed execution",
    }
    for key in ("content_key", "manifest_id", "validation_manifest"):
        if context[key] is None:
            missing[f"pathway_context/{key}"] = "not recorded on the pathway snapshot"

    if read_artifact is None:
        from precis.store.job_artifacts import read_artifact as store_read

        read_artifact = store_read
    entries = job.get("artifacts")
    if entries is None:
        missing["artifacts"] = "no retained artifact inventory; completeness unknown"
        entries = []
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise EnvelopeRefused("job artifacts must be a recorded list of entries")
    artifacts = [_artifact(entry, job_id, read_artifact, missing) for entry in entries]
    artifacts.sort(
        key=lambda entry: (entry["name"], entry["path"], entry["sha256"] or "")
    )
    pins = [
        {k: v for k, v in entry.items() if k != "availability"} for entry in artifacts
    ]
    envelope = {
        "schema": "precis_pathway_seed_envelope",
        "schema_version": 1,
        "run": run,
        "pathway_context": context,
        "provenance": provenance,
        "inputs": inputs,
        "outputs": outputs,
        "artifacts": artifacts,
        "missing": missing,
        "output_id": _digest({"inline": outputs, "artifacts": pins}),
    }
    envelope["bundle_id"] = _digest(envelope)
    return envelope
