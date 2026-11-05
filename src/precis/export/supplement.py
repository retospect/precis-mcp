"""Stored evidence accompanying an external draft export.

The audience walk owns ordering and numbering; this module only snapshots
the records it selected. Pathway manifests are copied, never rebuilt: today's
code, weights or lock cannot attest to an earlier calculation. Seed-job
extxyz is preferred over mutable structure refs; a fallback names that source.
The profile uses the existing deterministic figure renderer over stored graph
values, without the figure snapshot builder's current-runtime provenance.
Missing fields and unavailable optional renderers become explicit entry notes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from precis.export._provenance_strip import short_title
from precis.utils import handle_registry

log = logging.getLogger(__name__)
ABSENT = "absent"


@dataclass(frozen=True)
class SupplementEntry:
    """One numbered record and its files, relative to its ZIP directory."""

    number: str
    handle: str
    kind: str
    description: str
    files: dict[str, bytes]


def _json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode(
        "utf-8"
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _present(value: Any) -> Any:
    """Keep meaningful zero/false/empty collections; distinguish only missing."""
    return ABSENT if value is None or value == "" else value


def _ref(store: Any, handle: str) -> Any:
    parsed = handle_registry.parse(handle)
    if parsed is None:
        return None
    kind, is_chunk, pk = parsed
    if is_chunk:
        resolved = store.resolve_handle(handle)
        if resolved is None:
            return None
        kind, pk = resolved.kind, resolved.ref_id
    return store.get_ref(kind=kind, id=pk)


def _stamps(meta: dict[str, Any]) -> dict[str, Any]:
    manifest = _dict(meta.get("validation_manifest"))
    code, lock = _dict(manifest.get("code")), _dict(manifest.get("lock"))
    inputs = _dict(manifest.get("inputs"))
    weights = [
        {
            "backend": _present(model.get("backend")),
            "model": _present(model.get("model")),
            "weights_sha256": _present(model.get("weights_sha256")),
        }
        for model in lock.get("mlip") or []
        if isinstance(model, dict)
    ]
    return {
        "manifest_id": _present(meta.get("manifest_id") or manifest.get("manifest_id")),
        "precis_sha": _present(code.get("precis_sha")),
        "autocatpath_sha": _present(code.get("autocatpath_sha")),
        "lock_source": _present(lock.get("lock_source")),
        "uv_lock_sha256": _present(lock.get("uv_lock_sha256")),
        "weights": weights or ABSENT,
        "seeds": _present(manifest.get("seeds")),
        "inputs": {
            name: _present(inputs.get(name))
            for name in (
                "slab_extxyz_sha256",
                "config_snapshot_sha256",
                "environment_sha256",
                "tpd_reference_set_sha256",
            )
        },
        "network_digest": _present(_dict(inputs.get("network")).get("digest")),
    }


def _pathway_description(store: Any, meta: dict[str, Any]) -> str:
    results, config = _dict(meta.get("results")), _dict(meta.get("config"))
    substrate = _present(results.get("substrate") or config.get("substrate"))
    target = _present(results.get("target") or config.get("target"))
    candidate_id = meta.get("candidate_ref")
    candidate = ABSENT
    if candidate_id is not None:
        candidate = f"st{candidate_id}"
        try:
            ref = store.get_ref(kind="structure", id=candidate_id)
            if ref is not None and ref.title:
                candidate += " (" + short_title(ref.title) + ")"
        except Exception:
            log.debug("supplement: candidate title unavailable", exc_info=True)
    mlip = _dict(config.get("mlip"))
    model_key = "models" if "models" in mlip else "model"
    potential = ":".join(
        str(mlip[k]) for k in ("backend", model_key) if mlip.get(k) is not None
    )
    search = _dict(config.get("search"))
    manifest_seeds = _dict(_dict(meta.get("validation_manifest")).get("seeds"))
    seeds = manifest_seeds.get("search_seeds", search.get("seeds"))
    voltage = _dict(_dict(config.get("environment")).get("electrochemical")).get(
        "U_vs_RHE"
    )
    if voltage is None:
        voltage = _dict(config.get("electrochemistry")).get("U_vs_RHE")
    if voltage is None:
        voltage = _dict(config.get("conditions")).get("potential_U")
    return (
        f"{substrate} → {target}; candidate {candidate}; "
        f"potential {_present(potential)}; U vs RHE {_present(voltage)}; "
        f"seeds {_present(seeds)}; tier {_present(meta.get('tier'))}"
    )


def _structure_xyz(store: Any, ref_id: Any) -> str | None:
    try:
        from precis.structure.export import to_extxyz

        ref_id = int(ref_id)
        ref = store.get_ref(kind="structure", id=ref_id)
        if ref is None:
            return None
        scene, _labels = store.structure_load(ref_id)
        return to_extxyz(scene)
    except Exception:
        log.debug("supplement: structure %s unavailable", ref_id, exc_info=True)
        return None


def _structures(
    store: Any, ref: Any, files: dict[str, bytes]
) -> list[dict[str, Any]] | str:
    meta = _dict(ref.meta)
    geometries: dict[str, str] = {}
    try:
        from precis.dispatch import Hub

        geometries, _key = (
            Hub(store=store).sibling("pathway").seed_geometry(int(ref.id))
        )
    except Exception:
        # Plugin-free exporters and older monolith records still have the
        # linked structures; a missing seed record must not fail an export.
        log.debug("supplement: seed geometry unavailable", exc_info=True)
    refs = _dict(meta.get("structure_refs"))
    states = list(dict.fromkeys([*geometries, *refs]))
    records: list[dict[str, Any]] = []
    for state in states:
        xyz = geometries.get(state)
        source = "stored seed geometry"
        if xyz is None and refs.get(state) is not None:
            xyz = _structure_xyz(store, refs[state])
            source = "current linked structure"
        path = f"structures/{len(records) + 1:03d}.extxyz"
        record = {
            "state": state,
            "structure_ref": _present(refs.get(state)),
            "source": source if xyz is not None else ABSENT,
            "file": path if xyz is not None else ABSENT,
        }
        if xyz is not None:
            files[path] = xyz.encode("utf-8")
        records.append(record)
    candidate = meta.get("candidate_ref")
    if candidate is not None:
        xyz = _structure_xyz(store, candidate)
        path = "structures/candidate.extxyz"
        if xyz is not None:
            files[path] = xyz.encode("utf-8")
        records.append(
            {
                "state": "candidate",
                "structure_ref": candidate,
                "source": "current candidate structure" if xyz is not None else ABSENT,
                "file": path if xyz is not None else ABSENT,
            }
        )
    return records or ABSENT


def _profile(ref: Any) -> bytes | None:
    try:
        from precis.quest.figures import (
            _profile_positions,
            _profile_root_target,
            render_profile_png,
        )

        meta = _dict(ref.meta)
        graph, results = _dict(meta.get("graph")), _dict(meta.get("results"))
        root, target = _profile_root_target(graph, results)
        rows = _profile_positions(graph, root, target)
        if not any(row.get("rel_energy") is not None for row in rows):
            return None
        return render_profile_png(rows, title=ref.title)
    except Exception:
        log.debug("supplement: profile unavailable", exc_info=True)
        return None


def _pathway_files(store: Any, ref: Any, files: dict[str, bytes]) -> dict[str, Any]:
    meta = _dict(ref.meta)
    artifacts: dict[str, Any] = {}
    for key, filename in (
        ("validation_manifest", "manifest.json"),
        ("config", "config.json"),
        ("results", "results.json"),
        ("graph", "graph.json"),
    ):
        value = meta.get(key)
        artifacts[key] = filename if value is not None else ABSENT
        if value is not None:
            files[filename] = _json(value)
    snapshot = meta.get("config_snapshot_yaml")
    artifacts["config_snapshot"] = "config.yaml" if snapshot else ABSENT
    if snapshot:
        files["config.yaml"] = str(snapshot).encode("utf-8")
    results = _dict(meta.get("results"))
    kinetics = {k: v for k, v in results.items() if k.startswith("kinetics")}
    artifacts["kinetics"] = "kinetics.json" if kinetics else ABSENT
    if kinetics:
        files["kinetics.json"] = _json(kinetics)
    artifacts["structures"] = _structures(store, ref, files)
    profile = _profile(ref)
    artifacts["profile"] = "profile.png" if profile is not None else ABSENT
    if profile is not None:
        files["profile.png"] = profile
    return artifacts


def guard_entry(
    entry: SupplementEntry,
    base_url: str,
    *,
    source_chunks: list[str] | None = None,
) -> None:
    """Check all outgoing text, including decoded JSON keys and values.

    Check keys before using them as field labels: even an error message must
    not disclose a credential embedded in a key. Binary images are generated
    locally; every UTF-8 member is scanned regardless of its filename suffix.
    """
    from precis.errors import BadInput
    from precis.export.audience import guard_text
    from precis.utils.secret_scan import find_secrets

    def guard(text: str, label: str) -> None:
        if find_secrets(text):
            raise BadInput(
                f"external export contains a credential in {label}",
                next="remove the credential from the named field, then re-export",
            )
        guard_text(text, label, base_url)

    def guard_json(value: Any, label: str) -> None:
        if isinstance(value, dict):
            for index, (key, item) in enumerate(value.items()):
                guard(key, f"{label}, key {index}")
                field_label = f"{label}.{key}"
                guard_json(item, field_label)
                # Retain assignment context for password/header detectors.
                if isinstance(item, str):
                    guard(json.dumps({key: item}, ensure_ascii=False), field_label)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                guard_json(item, f"{label}[{index}]")
        elif isinstance(value, str):
            guard(value, label)

    label = f"supplement {entry.number} ({entry.handle})"
    if source_chunks:
        label += ", chunks " + ", ".join(source_chunks)
    guard(entry.description, f"{label}, description")
    for index, (filename, data) in enumerate(entry.files.items()):
        guard(filename, f"{label}, filename {index}")
        field_label = f"{label}, {filename}"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if filename.endswith(".json"):
            guard_json(json.loads(text), field_label)
        guard(text, field_label)


def collect_entry(
    store: Any,
    handle: str,
    number: str,
    *,
    base_url: str = "",
    source_chunks: list[str] | None = None,
) -> SupplementEntry:
    """Snapshot a referenced record; unresolved records remain numbered entries."""
    parsed = handle_registry.parse(handle)
    kind = parsed[0] if parsed is not None else "record"
    try:
        ref = _ref(store, handle)
    except Exception:
        log.debug("supplement: record %s unavailable", handle, exc_info=True)
        ref = None
    files: dict[str, bytes] = {}
    record: dict[str, Any] = {"number": number, "handle": handle, "kind": kind}
    if ref is None:
        description = f"{kind}: record absent"
        record["record"] = ABSENT
    else:
        meta = _dict(getattr(ref, "meta", None))
        if kind == "paper" and meta.get("record_type") == "dataset":
            kind = "dataset"
            record["kind"] = kind
        description = short_title(getattr(ref, "title", None) or kind)
        record["title"] = _present(getattr(ref, "title", None))
        if kind == "pathway":
            description = _pathway_description(store, meta)
            record["stamps"] = _stamps(meta)
            record["artifacts"] = _pathway_files(store, ref, files)
        else:
            files["record.json"] = _json(meta)
            if kind == "structure":
                xyz = _structure_xyz(store, int(ref.id))
                record["structure"] = "structure.extxyz" if xyz is not None else ABSENT
                if xyz is not None:
                    files["structure.extxyz"] = xyz.encode("utf-8")
            text = None
            try:
                text = "\n\n".join(store.chunks.live_body_text_for_ref(ref.id))
            except Exception:
                log.debug("supplement: record body unavailable", exc_info=True)
            record["body"] = "body.txt" if text else ABSENT
            if text:
                files["body.txt"] = text.encode("utf-8")
    record["description"] = description
    files["entry.json"] = _json(record)
    entry = SupplementEntry(number, handle, kind, description, files)
    guard_entry(entry, base_url, source_chunks=source_chunks)
    return entry


def write_supplement(entries: list[SupplementEntry], path: Path) -> None:
    """Write the one supplement ZIP beside an exported document, even if empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    index = [
        {
            "number": entry.number,
            "handle": entry.handle,
            "kind": entry.kind,
            "description": entry.description,
            "entry": f"{entry.number}/entry.json",
        }
        for entry in entries
    ]
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.json", _json(index))
        for entry in entries:
            if re.fullmatch(r"S[1-9]\d*\.[1-9]\d*", entry.number) is None:
                raise ValueError("invalid supplement number")
            for name, data in entry.files.items():
                archive.writestr(f"{entry.number}/{name}", data)
