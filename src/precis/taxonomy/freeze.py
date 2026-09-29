"""Stage 5 — freeze, write, read and diff a versioned ``list.vN.yaml``.

``taxonomy-bootstrap.md`` §Design, stage 5 + AC4. The frozen list is the
artifact every binding document and every ``measures`` row cites (same
discipline as ``ANCHOR_SCHEME``): version, snapshot identity, thresholds,
procedure version, and per entry the mentions/hubs that put it there. A
frozen file is immutable — :func:`write_list` refuses to overwrite one — and
a changed list is a new version, diffable against the last one with
:func:`diff_lists`.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from precis.taxonomy.config import PROCEDURE_VERSION
from precis.taxonomy.types import (
    DimensionSpec,
    ListEntry,
    MeasurandList,
    Snapshot,
    Thresholds,
)

_VERSION_RE = re.compile(r"^list\.v(\d+)\.yaml$")


def freeze(
    entries: Any,
    *,
    campaign: str,
    snapshot: Snapshot,
    thresholds: Thresholds,
    version: int,
    stability: float | None = None,
    rejected: Any = (),
) -> MeasurandList:
    """Assemble the stage-5 artifact from stage-4's output.

    A thin constructor: stamps :data:`~precis.taxonomy.config.PROCEDURE_VERSION`
    so "same numbers, different procedure" stays detectable, and freezes
    ``entries``/``rejected`` into tuples so the result cannot be mutated out
    from under a later :func:`write_list` call.
    """
    return MeasurandList(
        version=version,
        campaign=campaign,
        snapshot=snapshot,
        thresholds=thresholds,
        procedure_version=PROCEDURE_VERSION,
        entries=tuple(entries),
        stability=stability,
        rejected=tuple(rejected),
    )


def _utc_now_z() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_list(
    lst: MeasurandList, directory: Path, *, census_digest: str | None = None
) -> Path:
    """Write ``lst`` to ``directory/list.v<version>.yaml`` and return the path.

    Raises :class:`FileExistsError` if the target already exists — a frozen
    list is immutable; a changed list is a new version (AC4), never an
    in-place edit. Two provenance keys are stamped into the document that do
    not live on :class:`MeasurandList` itself: ``generated_at`` (UTC,
    ``Z``-suffixed, this call's wall-clock time — never the snapshot's
    ``pulled_at``) and, when the caller supplies one, ``census_digest`` (the
    stage-1 output's own digest, distinct from ``snapshot.sha256`` which
    identifies the *input* corpus dump). Both are read-back-optional: they
    round-trip through the YAML file, not through :meth:`MeasurandList.to_json`,
    so :func:`read_list` reconstructing a plain ``MeasurandList`` silently
    drops them rather than needing a field neither the type nor stage 4
    produces.

    ``census_digest`` lives on :func:`write_list` rather than :func:`freeze`
    per the build note in ``taxonomy-bootstrap.md``'s stage-5 spec: threading
    it through ``freeze`` would require either a new ``MeasurandList`` field
    (out of scope here — flagged, not added) or silently discarding a
    caller-supplied value, and the spec says to report that rather than fake
    it.

    ``exists()`` then ``write_text()`` would be a TOCTOU gap: a crash between
    the two leaves a truncated file that a *later* run's ``exists()`` check
    then refuses to ever regenerate — the worst of both guarantees. Instead
    the full document is written to a sibling temp file first, then linked
    into place with :func:`os.link`, which is atomic and itself raises
    ``FileExistsError`` when the target is already taken — so the only file
    that can ever exist at ``path`` is a complete one.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"list.v{lst.version}.yaml"
    document = lst.to_json()
    document["procedure_version"] = PROCEDURE_VERSION
    document["generated_at"] = _utc_now_z()
    if census_digest is not None:
        document["census_digest"] = census_digest
    text = yaml.safe_dump(
        document, sort_keys=True, allow_unicode=True, default_flow_style=False
    )
    fd, tmp_name = tempfile.mkstemp(
        dir=directory, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            raise FileExistsError(
                f"{path} already exists — a frozen list is immutable; freeze a "
                "new version instead"
            ) from None
    finally:
        os.unlink(tmp_name)
    return path


def _snapshot_from_json(data: dict[str, Any]) -> Snapshot:
    return Snapshot(
        source=str(data["source"]),
        row_count=int(data["row_count"]),
        sha256=str(data["sha256"]),
        pulled_at=str(data["pulled_at"]),
        text_field=str(data["text_field"]),
        ref_field=str(data["ref_field"]),
        paper_field=str(data.get("paper_field", "")),
    )


def _thresholds_from_json(data: dict[str, Any]) -> Thresholds:
    return Thresholds(
        min_papers=int(data["min_papers"]),
        min_hubs=int(data["min_hubs"]),
        min_join_side=int(data["min_join_side"]),
        min_stability=float(data["min_stability"]),
        max_escape_rate=float(data["max_escape_rate"]),
        require_both_halves=bool(data["require_both_halves"]),
        require_single_dimension=bool(data["require_single_dimension"]),
    )


def _dimension_from_json(data: dict[str, Any] | None) -> DimensionSpec | None:
    if data is None:
        return None
    return DimensionSpec(
        kind=data["kind"],
        si_vector=data.get("si_vector"),
        currency_code=data.get("currency_code"),
        base_year=data.get("base_year"),
    )


def _entry_from_json(data: dict[str, Any]) -> ListEntry:
    dimension = _dimension_from_json(data["dimension"])
    if dimension is None:
        raise ValueError(f"list entry {data.get('key')!r} has no dimension")
    return ListEntry(
        key=str(data["key"]),
        label=str(data["label"]),
        dimension=dimension,
        canonical_unit=data.get("canonical_unit"),
        convention=data.get("convention"),
        allowed_reference_states=tuple(data.get("allowed_reference_states") or ()),
        normalisation_bases=tuple(data.get("normalisation_bases") or ()),
        required_conditions=tuple(data.get("required_conditions") or ()),
        convert=bool(data.get("convert", False)),
        hub_count=int(data.get("hub_count", 0)),
        paper_count=int(data.get("paper_count", 0)),
        contributing_hubs=tuple(data.get("contributing_hubs") or ()),
        aliases=tuple(data.get("aliases") or ()),
    )


def read_list(path: Path) -> MeasurandList:
    """Reconstruct a :class:`MeasurandList` from a ``list.vN.yaml`` file.

    Round-trips with :func:`write_list`: ``read_list(write_list(lst, d))``
    reconstructs a value equal to ``lst``. Extra document-only keys
    (``generated_at``, ``census_digest``, ``procedure_version`` when it
    matches) are read where the dataclass has a field and otherwise ignored.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    entries = tuple(_entry_from_json(e) for e in data.get("entries") or ())
    rejected = tuple(
        (str(r["key"]), str(r["reason"])) for r in data.get("rejected") or ()
    )
    return MeasurandList(
        version=int(data["version"]),
        campaign=str(data["campaign"]),
        snapshot=_snapshot_from_json(data["snapshot"]),
        thresholds=_thresholds_from_json(data["thresholds"]),
        procedure_version=int(data["procedure_version"]),
        entries=entries,
        stability=data.get("stability"),
        rejected=rejected,
    )


def next_version(directory: Path) -> int:
    """The next free version number for ``directory``: 1 if empty, else
    one past the highest existing ``list.vN.yaml``."""
    directory = Path(directory)
    if not directory.exists():
        return 1
    versions = [
        int(match.group(1))
        for path in directory.iterdir()
        if (match := _VERSION_RE.match(path.name))
    ]
    return max(versions, default=0) + 1


@dataclass(frozen=True, slots=True)
class ListDiff:
    """What changed between two frozen versions of the same campaign list."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[tuple[str, str], ...]
    """``(key, what changed)`` — a human-readable description, not a patch."""
    unchanged: tuple[str, ...]

    def render(self) -> str:
        if not (self.added or self.removed or self.changed):
            return "no changes"
        lines: list[str] = []
        if self.added:
            lines.append("added: " + ", ".join(self.added))
        if self.removed:
            lines.append("removed: " + ", ".join(self.removed))
        if self.changed:
            lines.append("changed:")
            lines.extend(f"  {key}: {what}" for key, what in self.changed)
        if self.unchanged:
            noun = "entry" if len(self.unchanged) == 1 else "entries"
            lines.append(f"unchanged: {len(self.unchanged)} {noun}")
        return "\n".join(lines)


def _display_name(entry: ListEntry, key_counts: Mapping[str, int]) -> str:
    """``entry.key``, or that plus whatever distinguishes it from a sibling.

    ``ListEntry.key`` is not unique — a reference-state or convention split
    (the ``TOF`` case) puts two entries under one key by design
    (:meth:`ListEntry.identity`'s docstring). A bare key is unambiguous and
    stays bare; only once ``key_counts`` shows a real collision does the
    name grow the fields that broke the tie, so an ordinary diff still reads
    as a plain key list.
    """
    if key_counts.get(entry.key, 0) <= 1:
        return entry.key
    parts: list[str] = []
    if entry.convention:
        parts.append(f"convention={entry.convention}")
    if entry.allowed_reference_states:
        parts.append("ref=" + "|".join(entry.allowed_reference_states))
    parts.append(f"dim={entry.dimension.si_vector or entry.dimension.kind}")
    return f"{entry.key} ({', '.join(parts)})"


def diff_lists(old: MeasurandList, new: MeasurandList) -> ListDiff:
    """Compare two frozen lists by entry :meth:`~precis.taxonomy.types.ListEntry.identity`.

    Keying by ``entry.key`` alone would collapse every reference-state or
    convention split onto one dict slot and silently drop its sibling — a
    rerun that changes only the dropped entry would then report "no
    changes", which is exactly the case AC5 requires to exist (two ``TOF``
    entries under one key) and AC4 requires a diff to catch. A changed entry
    reports dimension, canonical unit, required-condition and hub-count
    changes — the fields a re-run over a grown corpus is expected to move.
    Order in the source lists never matters; identities are sorted in every
    output tuple so the diff itself is reproducible.
    """
    old_by_id = {entry.identity(): entry for entry in old.entries}
    new_by_id = {entry.identity(): entry for entry in new.entries}
    combined = {**old_by_id, **new_by_id}
    key_counts: Counter[str] = Counter(entry.key for entry in combined.values())

    added_ids = sorted(set(new_by_id) - set(old_by_id))
    removed_ids = sorted(set(old_by_id) - set(new_by_id))
    added = tuple(_display_name(new_by_id[i], key_counts) for i in added_ids)
    removed = tuple(_display_name(old_by_id[i], key_counts) for i in removed_ids)

    changed: list[tuple[str, str]] = []
    unchanged: list[str] = []
    for ident in sorted(set(old_by_id) & set(new_by_id)):
        before, after = old_by_id[ident], new_by_id[ident]
        name = _display_name(after, key_counts)
        diffs: list[str] = []
        if before.dimension.to_json() != after.dimension.to_json():
            diffs.append(
                f"dimension {before.dimension.si_vector!r} -> "
                f"{after.dimension.si_vector!r}"
            )
        if before.canonical_unit != after.canonical_unit:
            diffs.append(
                f"canonical_unit {before.canonical_unit!r} -> {after.canonical_unit!r}"
            )
        if set(before.required_conditions) != set(after.required_conditions):
            diffs.append(
                f"required_conditions {sorted(before.required_conditions)} -> "
                f"{sorted(after.required_conditions)}"
            )
        if before.hub_count != after.hub_count:
            diffs.append(f"hub_count {before.hub_count} -> {after.hub_count}")
        if diffs:
            changed.append((name, "; ".join(diffs)))
        else:
            unchanged.append(name)
    return ListDiff(
        added=added, removed=removed, changed=tuple(changed), unchanged=tuple(unchanged)
    )


__all__ = [
    "ListDiff",
    "diff_lists",
    "freeze",
    "next_version",
    "read_list",
    "write_list",
]
