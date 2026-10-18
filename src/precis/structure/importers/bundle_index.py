"""Name -> offset index over a downloaded MOF bundle, for one-record imports.

Neither CoRE MOF nor (without an MP key) QMOF has a per-record API, so the
bundle is downloaded once to the NAS (an ops step) and
``precis structure index-bundle <source> <path>`` writes a small JSON index
next to it. ``get(kind='structure', args={'source': 'qmof', 'config_id':
...})`` then reads one CIF out of the bundle through that index and imports
only that record (chemistry-4: the DB stays on-demand; only this lookup
file is bulk).

Index file ``<index dir>/<source>.json`` (the dir is
:func:`precis.config.struct_bundle_index_dir`)::

    {"source": "qmof", "bundle": "<path>", "built_at": "...Z",
     "entries": {"<id>": {"member": "<path in bundle>", "offset": <int>,
                          "energy"?: <eV>}}}

``bundle`` is relative to the index dir when the bundle sits under it, so
a NAS mounted at different paths on different hosts still resolves. A
bundle is a ``.zip`` or an extracted directory; ``offset`` is the zip
member's local-header offset (0 for a directory), checked on read so a
bundle replaced under a stale index is refused rather than misread. A zip
nested inside the bundle is refused at build time: extract it first.

QMOF energies come from the bundle's ``qmof.json`` (``qmof_id``, ``name``,
``outputs.pbe.energy_total`` in eV, per the QMOF documentation, not
checked against a downloaded bundle). Each record is indexed under its
``qmof_id``, matched to a CIF whose file stem is the ``qmof_id`` or the
``name``.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cif import MAX_CIF_BYTES, CifRefused
from .mof import CIF_ADAPTERS, QMOF_DATASET


class BundleIndexError(RuntimeError):
    """The bundle or its index cannot serve a record; ``next`` says what to run."""

    def __init__(self, message: str, *, next: str) -> None:
        super().__init__(message)
        self.next = next


def _build_hint(source: str) -> str:
    return (
        f"download the {source} bundle to the NAS, then run "
        f"`precis structure index-bundle {source} <bundle path>`"
    )


def index_path(source: str, index_dir: Path) -> Path:
    return index_dir / f"{source}.json"


def _iter_members(bundle: Path) -> Iterator[tuple[str, int]]:
    """``(member, offset)`` for every file in a zip or directory bundle."""
    if bundle.is_dir():
        for p in sorted(bundle.rglob("*")):
            if p.is_file():
                yield p.relative_to(bundle).as_posix(), 0
        return
    with zipfile.ZipFile(bundle) as zf:
        for info in zf.infolist():
            if not info.is_dir():
                yield info.filename, info.header_offset


def _read_member(bundle: Path, member: str, offset: int) -> bytes:
    if bundle.is_dir():
        target = (bundle / member).resolve()
        if not target.is_relative_to(bundle.resolve()):
            raise BundleIndexError(
                f"index member {member!r} escapes the bundle directory",
                next="rebuild the index with `precis structure index-bundle`",
            )
        if target.stat().st_size > MAX_CIF_BYTES:
            raise CifRefused(f"bundle member {member!r} is over the CIF size cap")
        return target.read_bytes()
    with zipfile.ZipFile(bundle) as zf:
        try:
            info = zf.getinfo(member)
        except KeyError:
            info = None
        if info is None or info.header_offset != offset:
            raise BundleIndexError(
                f"bundle {bundle} no longer matches its index at {member!r}",
                next="rebuild the index with `precis structure index-bundle`",
            )
        if info.file_size > MAX_CIF_BYTES:
            raise CifRefused(f"bundle member {member!r} is over the CIF size cap")
        return zf.read(info)


def _qmof_energies(bundle: Path, members: list[str]) -> dict[str, dict[str, Any]]:
    """``{stem: {"id": qmof_id, "energy": eV|None}}`` keyed by both the
    ``qmof_id`` and the ``name`` of each ``qmof.json`` record."""
    json_member = next((m for m in members if Path(m).name == "qmof.json"), None)
    if json_member is None:
        return {}
    if bundle.is_dir():
        records = json.loads((bundle / json_member).read_text(encoding="utf-8"))
    else:
        with zipfile.ZipFile(bundle) as zf:
            records = json.loads(zf.read(json_member).decode("utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict) or not rec.get("qmof_id"):
            continue
        pbe = ((rec.get("outputs") or {}).get("pbe")) or {}
        energy = pbe.get("energy_total")
        qmof_id = str(rec["qmof_id"])
        row: dict[str, Any] = {
            "id": qmof_id,
            "energy": float(energy) if isinstance(energy, int | float) else None,
        }
        out[qmof_id] = row
        if rec.get("name"):
            out.setdefault(str(rec["name"]), row)
    return out


def build_index(source: str, bundle: Path) -> dict[str, Any]:
    """Walk ``bundle`` once and return the index document (not yet written)."""
    if source not in CIF_ADAPTERS:
        raise BundleIndexError(
            f"no bundle index for source {source!r}",
            next=f"source is one of {', '.join(CIF_ADAPTERS)}",
        )
    if not bundle.exists():
        raise BundleIndexError(
            f"bundle {bundle} does not exist", next=_build_hint(source)
        )
    members = list(_iter_members(bundle))
    nested = [m for m, _ in members if m.lower().endswith(".zip")]
    if nested:
        raise BundleIndexError(
            f"bundle holds nested archive(s) {nested[:3]}",
            next="extract them into the bundle directory, then index the directory",
        )
    energies = (
        _qmof_energies(bundle, [m for m, _ in members])
        if source == QMOF_DATASET
        else {}
    )
    entries: dict[str, dict[str, Any]] = {}
    for member, offset in members:
        if not member.lower().endswith(".cif"):
            continue
        stem = Path(member).stem
        entry: dict[str, Any] = {"member": member, "offset": offset}
        record_id = stem
        if source == QMOF_DATASET and stem in energies:
            record_id = energies[stem]["id"]
            if energies[stem]["energy"] is not None:
                entry["energy"] = energies[stem]["energy"]
        entries.setdefault(record_id, entry)
    return {
        "source": source,
        "bundle": str(bundle),
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entries": entries,
    }


def write_index(doc: dict[str, Any], index_dir: Path) -> Path:
    """Write ``doc`` to ``<index_dir>/<source>.json``, the bundle path made
    relative to ``index_dir`` when the bundle sits under it."""
    index_dir.mkdir(parents=True, exist_ok=True)
    bundle = Path(doc["bundle"]).resolve()
    try:
        doc = {**doc, "bundle": bundle.relative_to(index_dir.resolve()).as_posix()}
    except ValueError:
        doc = {**doc, "bundle": str(bundle)}
    out = index_path(doc["source"], index_dir)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    tmp.replace(out)
    return out


def load_record(source: str, record_id: str, index_dir: Path) -> dict[str, Any] | None:
    """The raw adapter record ``{id, cif_text, energy?}`` for ``record_id``,
    or ``None`` when the index has no such id. Raises
    :class:`BundleIndexError` when the index or bundle is missing."""
    path = index_path(source, index_dir)
    if not path.is_file():
        raise BundleIndexError(
            f"no {source} bundle index at {path}", next=_build_hint(source)
        )
    rebuild = f"rebuild it: `precis structure index-bundle {source} <bundle path>`"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        entry = (doc.get("entries") or {}).get(record_id)
        if entry is None:
            return None
        bundle = Path(doc["bundle"])
        member, offset = str(entry["member"]), int(entry.get("offset", 0))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BundleIndexError(
            f"{source} bundle index at {path} is unreadable: {exc}", next=rebuild
        ) from exc
    if not bundle.is_absolute():
        bundle = index_dir / bundle
    if not bundle.exists():
        raise BundleIndexError(
            f"{source} index points at missing bundle {bundle}",
            next=_build_hint(source),
        )
    try:
        data = _read_member(bundle, member, offset)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BundleIndexError(
            f"{source} bundle {bundle} could not be read: {exc}", next=rebuild
        ) from exc
    raw: dict[str, Any] = {
        "id": record_id,
        "cif_text": data.decode("utf-8", errors="replace"),
    }
    if entry.get("energy") is not None:
        raw["energy"] = float(entry["energy"])
    return raw


__all__ = [
    "BundleIndexError",
    "build_index",
    "index_path",
    "load_record",
    "write_index",
]
