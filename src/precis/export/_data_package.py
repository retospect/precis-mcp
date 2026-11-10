"""Shared "Data package" export appendix: a figure chunk that carries a
``meta["figure"]["data_package"]`` snapshot gets a small-print end-matter
table (species/energies/params/versions) in both exporters, plus a
machine-readable JSON copy.

**Generated, not curated.** The snapshot is stamped by whatever minted the
figure (a quest/pathway export), not authored by hand — the exporter never
recomputes or re-derives it, only formats what's already frozen on the
chunk. That freeze matters: the appendix must describe the *exact* numbers
behind the plotted pixels, at the moment the figure was minted, not
whatever the live quest/pathway looks like by the time someone exports the
draft — a run can be re-scored, re-tiered, or re-versioned between figure
mint and draft export, and the reader needs the two to agree.

**Three carriers, cheapest to most durable.** (1) the printed 7 pt
monospace appendix in the compiled PDF/docx — human-skimmable, survives
however the document travels, but not copy-paste-clean at print
resolution; (2) the same JSON embedded *in* the PDF as a named attachment
(LaTeX ``embedfile``, extracted with ``pdfdetach``) — travels with the PDF
through self-distribution and arXiv, which recompiles from our own
``main.tex``; (3) the ``data-package.json`` sidecar written next to the
export — the canonical copy-paste source, because a publisher's
re-typesetting pipeline strips PDF attachments (and XMP) on the way to
its own PDF, so the sidecar is the only carrier guaranteed to survive
publication. This module resolves one shared shape (header lines + a
fixed-width table + the JSON string) so the two exporters render
identical numbers and never drift.

**Computed source contracts ride verbatim and are checked at the door.**
A snapshot may carry ``computed_sources`` — :mod:`precis.computed_sources`
contracts pinning a computed number in the figure to its producer's
frozen output bytes, unit and method (a computed measure row cited by the
draft). The exporters run :func:`verify_snapshot` when collecting a figure
and refuse the export on a malformed contract, never publishing a
citation chain they cannot re-check; the sidecar freezes every carried
contract's :func:`~precis.computed_sources.contract_fingerprint`
alongside, so a contract edited after export shows as a fingerprint
change. Standing against producer evidence (the envelope or bytes) is
re-derived by whoever restates the value, through
:func:`~precis.computed_sources.verify_source` — this module never
derives numbers from formatted text, only formats and fingerprints what
the snapshot already froze.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from precis import computed_sources
from precis.errors import BadInput

#: Section heading, identical in both exporters.
SECTION_TITLE = "Data package"

#: Sidecar filename written next to the export.
SIDECAR_NAME = "data-package.json"


@dataclass(frozen=True, slots=True)
class DataPackageFigure:
    """One figure's data-package entry: the exporter's own figure
    label/anchor (the ``\\label{chunk:...}`` handle in LaTeX, the chunk
    handle in docx), its rendered caption text, and the raw snapshot dict
    (``meta["figure"]["data_package"]``, schema 1)."""

    label: str
    caption: str
    snapshot: dict[str, Any]


def collect_entry(chunk: Any) -> dict[str, Any] | None:
    """The figure chunk's data-package snapshot, or ``None`` when absent
    or malformed. Tolerant of ``meta`` being ``None`` or not a dict (an
    ordinary figure chunk with no snapshot at all) — never raises, so a
    figure without this key is completely unaffected."""
    meta = getattr(chunk, "meta", None)
    if not isinstance(meta, dict):
        return None
    figure = meta.get("figure")
    if not isinstance(figure, dict):
        return None
    snapshot = figure.get("data_package")
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("schema") != 1:
        return None
    if "columns" not in snapshot or "rows" not in snapshot:
        return None
    return snapshot


def source_contracts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """The computed source contracts a snapshot carries under
    ``computed_sources`` (:mod:`precis.computed_sources`) — dict entries
    only; anything else is left for :func:`verify_snapshot` to refuse."""
    raw = snapshot.get("computed_sources")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict)]


def verify_snapshot(snapshot: dict[str, Any], *, pins: Any = None) -> list[str]:
    """Every problem with the snapshot's carried computed source contracts,
    as human lines (empty: the contracts are well-formed, and — when
    ``pins`` (a frozen computed pin set) is supplied — covered by it, so
    the export never publishes a citation chain it cannot re-check)."""
    out: list[str] = []
    for i, contract in enumerate(source_contracts(snapshot)):
        for problem in computed_sources.validate_contract(contract):
            out.append(f"computed source contract [{i}]: {problem}")
        if pins is not None:
            for issue in computed_sources.check_frozen_pins(contract, pins):
                out.append(
                    f"computed source contract [{i}] ({issue.kind}): {issue.message}"
                )
    return out


def require_verified_snapshot(
    snapshot: dict[str, Any], *, label: str, pins: Any = None
) -> None:
    """:func:`verify_snapshot` as a refusal: raises :class:`BadInput`
    naming every problem (and the figure) when a carried contract is
    malformed or uncovered — an export is a publication surface, so a
    broken citation chain stops it rather than rendering a notice."""
    problems = verify_snapshot(snapshot, pins=pins)
    if problems:
        raise BadInput(
            f"figure {label}: " + "; ".join(problems),
            next="re-derive the computed source with build_pathway_source / "
            "build_external_source, or drop the computed_sources key from "
            "the figure's data_package snapshot",
        )


def fmt(v: Any) -> str:
    """Format one snapshot value for the plain-text appendix / table:
    floats to 6 significant figures, ``None`` as ``-``, bools as
    ``true``/``false``, everything else via ``str()``."""
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, dict):
        # Objective specs ({"key": "barrier", "sense": "min"}) read as
        # "barrier:min"; any other dict as space-joined k=v pairs — never
        # a Python repr in the printed appendix.
        if set(v) == {"key", "sense"}:
            return f"{v['key']}:{v['sense']}"
        return " ".join(f"{k}={fmt(x)}" for k, x in v.items())
    return str(v)


def _flatten_params(params: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten a (possibly nested) params dict to ``dotted.key: value``
    pairs, in insertion order. A list value is joined with ``", "``; a
    nested dict recurses with a dotted prefix (``mlip.backend: mace``)."""
    out: list[tuple[str, str]] = []
    for key, val in params.items():
        dotted = f"{prefix}{key}"
        if isinstance(val, dict):
            out.extend(_flatten_params(val, prefix=f"{dotted}."))
        elif isinstance(val, list):
            out.append((dotted, ", ".join(fmt(x) for x in val)))
        else:
            out.append((dotted, fmt(val)))
    return out


def header_lines(fig: DataPackageFigure) -> list[str]:
    """Key: value header lines shared by both exporters — source, mint
    time, engine/precis versions, then the flattened params."""
    snap = fig.snapshot
    source = snap.get("source") or {}
    lines: list[str] = []
    kind = source.get("kind")
    handle = source.get("handle")
    title = source.get("title")
    src_bits = [b for b in (kind, handle, title) if b]
    if src_bits:
        lines.append("source: " + " ".join(str(b) for b in src_bits))
    generated_at = snap.get("generated_at")
    if generated_at:
        lines.append(f"generated_at: {generated_at}")
    autocatpath_version = snap.get("autocatpath_version")
    lines.append(f"autocatpath_version: {fmt(autocatpath_version)}")
    precis = snap.get("precis") or {}
    version = precis.get("version")
    sha = precis.get("sha")
    precis_bits = [b for b in (version, sha) if b]
    lines.append("precis: " + (" ".join(str(b) for b in precis_bits) or "-"))
    params = snap.get("params") or {}
    for key, value in _flatten_params(params):
        lines.append(f"{key}: {value}")
    return lines


def table_lines(fig: DataPackageFigure) -> list[str]:
    """Fixed-width table lines (header row + one row per entry) from the
    snapshot's ``columns``/``rows`` — two-space column separator, widths
    from the max of the header and every formatted cell in that column."""
    columns = [str(c) for c in (fig.snapshot.get("columns") or [])]
    rows = fig.snapshot.get("rows") or []
    if not columns:
        return []
    cells: list[list[str]] = [
        [fmt(row.get(c)) for c in columns] for row in rows if isinstance(row, dict)
    ]
    widths = [
        max(len(columns[i]), *(len(r[i]) for r in cells)) if cells else len(columns[i])
        for i in range(len(columns))
    ]
    sep = "  "
    lines = [sep.join(columns[i].ljust(widths[i]) for i in range(len(columns)))]
    for r in cells:
        lines.append(sep.join(r[i].ljust(widths[i]) for i in range(len(columns))))
    return lines


def sidecar_json(figures: list[DataPackageFigure]) -> str:
    """The ``data-package.json`` sidecar body: one entry per figure,
    carrying the label/caption plus every key of its raw snapshot. A
    figure that carries computed source contracts also gets their frozen
    ``contract_fingerprint`` list — the export is the freezer
    (:func:`precis.computed_sources.contract_fingerprint`), so a contract
    hand-edited after export is visible as a fingerprint change even when
    every pin still verifies."""
    entries: list[dict[str, Any]] = []
    for fig in figures:
        entry: dict[str, Any] = {
            "label": fig.label,
            "caption": fig.caption,
            **fig.snapshot,
        }
        contracts = source_contracts(fig.snapshot)
        if contracts:
            entry["computed_source_fingerprints"] = [
                computed_sources.contract_fingerprint(c) for c in contracts
            ]
        entries.append(entry)
    return json.dumps(
        {
            "schema": 1,
            "generator": "precis draft export",
            "figures": entries,
        },
        indent=2,
        sort_keys=False,
    )


__all__ = [
    "SECTION_TITLE",
    "SIDECAR_NAME",
    "DataPackageFigure",
    "collect_entry",
    "fmt",
    "header_lines",
    "require_verified_snapshot",
    "sidecar_json",
    "source_contracts",
    "table_lines",
    "verify_snapshot",
]
