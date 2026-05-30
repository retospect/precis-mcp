"""Markdown / JSON report renderer for ``ProvenanceResult``.

Renders the four-severity-tier report described in
``docs/provenance-kind-plan.md`` § "Report shape". Pure formatting —
no DB access, no I/O. Takes one ``ProvenanceResult`` (single-DOI
path) or a list of them (Phase 2 batch path) and returns a string.

Views shipping in Phase 2:

- (default) — full triaged markdown report
- ``blockers`` — only 🔴 + 🟠 entries; other tiers collapsed to a count
- ``json``    — structured payload for downstream tooling
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal

from precis.ingest.provenance import (
    Notice,
    ProvenanceResult,
    Severity,
)

View = Literal["default", "blockers", "json"]

_VALID_VIEWS: tuple[View, ...] = ("default", "blockers", "json")

_SEVERITY_GLYPH: dict[Severity, str] = {
    "blocker": "🔴",
    "review":  "🟠",
    "note":    "🟡",
    "info":    "🟢",
}

_SEVERITY_LABEL: dict[Severity, str] = {
    "blocker": "Blocker",
    "review":  "Review",
    "note":    "Correction",
    "info":    "Info",
}


def _format_authors(authors: list[dict[str, str]] | None) -> str:
    """Render an author list as ``Smith, J., Doe, A.`` or fall back to ``—``."""
    if not authors:
        return "—"
    names = [a.get("name") or "" for a in authors if a.get("name")]
    if not names:
        return "—"
    if len(names) <= 3:
        return ", ".join(names)
    return f"{', '.join(names[:3])} et al."


def _format_notice_line(notice: Notice) -> str:
    """One bullet per notice — Crossref type, notice DOI, date, local slug."""
    parts: list[str] = []
    glyph = _SEVERITY_GLYPH[notice.severity]
    parts.append(f"{glyph} **{notice.update_type}**")
    if notice.notice_date is not None:
        parts.append(notice.notice_date.strftime("%Y-%m-%d"))
    parts.append(f"notice DOI: `{notice.notice_doi}`")
    if notice.persisted_ref_id is not None:
        parts.append(f"ingested as ref id={notice.persisted_ref_id}")
    return "- " + " · ".join(parts)


def _severity_action(severity: Severity) -> str:
    """Short action prompt the model can lean on when rendering."""
    if severity == "blocker":
        return (
            "**Action**: do not cite this paper without addressing the "
            "retraction. Drop the citation or replace the supporting "
            "argument."
        )
    if severity == "review":
        return (
            "**Action**: re-read. Check whether your argument depends on "
            "the contested claim — the paper is under investigation but "
            "has not been retracted."
        )
    if severity == "note":
        return (
            "**Action**: skim the correction notice — most corrigenda "
            "are housekeeping (author affiliation, typo in an equation) "
            "but some change substantive claims."
        )
    return ""


def render_single(result: ProvenanceResult) -> str:
    """Render one ``ProvenanceResult`` as a markdown document."""
    lines: list[str] = []
    lines.append(f"# Provenance check — `{result.doi}`")
    lines.append("")

    if result.status == "malformed":
        lines.append(
            "**Status**: malformed DOI — input does not match the "
            "``10.<registrant>/<suffix>`` shape. No HTTP call was made."
        )
        return "\n".join(lines) + "\n"

    if result.status == "unknown":
        lines.append(
            "**Status**: unknown — Crossref has no record for this DOI. "
            "Likely a hallucinated or mistyped identifier. (Fuzzy "
            "resolution from bibliographic hints lands in Phase 5; "
            "until then, check the DOI source.)"
        )
        return "\n".join(lines) + "\n"

    if result.status == "check_failed":
        lines.append(f"**Status**: check failed — {result.error or 'unknown error'}.")
        lines.append("")
        lines.append("Crossref returned a transport error. Try again later.")
        return "\n".join(lines) + "\n"

    # status == "ok"
    if result.paper_title:
        lines.append(f"**Title**: {result.paper_title}")
    if result.paper_authors:
        lines.append(f"**Authors**: {_format_authors(result.paper_authors)}")
    if result.paper_year is not None:
        lines.append(f"**Year**: {result.paper_year}")
    lines.append("")

    overall = result.overall_severity
    glyph = _SEVERITY_GLYPH[overall]
    label = _SEVERITY_LABEL[overall]
    if not result.notices:
        lines.append("🟢 **Clean** — Crossref reports no retraction, expression of "
                     "concern, or correction notices on this DOI.")
        lines.append("")
        if result.paper_in_store:
            lines.append(
                f"Local paper ref (id={result.paper_ref_id}) marked as checked."
            )
        return "\n".join(lines) + "\n"

    lines.append(f"## {glyph} Overall: {label}")
    lines.append("")
    if result.applied_status is not None:
        lines.append(
            f"Dominant status applied to local ref: "
            f"`{result.applied_status}`. {_severity_action(overall)}"
        )
        lines.append("")

    # Group notices by severity, render in dominance order.
    by_sev: dict[Severity, list[Notice]] = {
        "blocker": [],
        "review":  [],
        "note":    [],
        "info":    [],
    }
    for n in result.notices:
        by_sev[n.severity].append(n)

    severity_order: tuple[Severity, ...] = ("blocker", "review", "note", "info")
    for sev in severity_order:
        bucket = by_sev[sev]
        if not bucket:
            continue
        lines.append(
            f"### {_SEVERITY_GLYPH[sev]} {_SEVERITY_LABEL[sev]} ({len(bucket)})"
        )
        for n in bucket:
            lines.append(_format_notice_line(n))
        lines.append("")

    if result.paper_in_store:
        lines.append("---")
        lines.append(
            f"Persisted to local store: ref id={result.paper_ref_id}. "
            "Notice refs (when ingested) are addressable as `paper`."
        )
    else:
        lines.append("---")
        lines.append(
            "Paper not in local store — report is informational only. "
            "Notice refs were not created; ingest the paper first to "
            "persist the retraction graph."
        )

    return "\n".join(lines) + "\n"


def render_batch(results: list[ProvenanceResult], view: View = "default") -> str:
    """Render a list of ``ProvenanceResult`` as one report.

    The default view emits a summary header followed by per-DOI
    sections grouped by severity. The ``blockers`` view drops 🟡/🟢
    entries entirely (keeps a count line so the user knows how many
    were suppressed). The ``json`` view emits a structured JSON
    document — handy when downstream tooling wants to apply its own
    severity rules without re-parsing markdown.

    Empty input → a short "nothing to check" stub.
    """
    if view == "json":
        return _render_json(results)

    if not results:
        return "# Provenance check\n\n_No DOIs to check._\n"

    bucketed = _bucket_by_severity(results)
    summary = _format_summary(results)

    lines: list[str] = []
    lines.append(f"# Provenance check — {len(results)} DOI{_pl(len(results))}")
    lines.append("")
    lines.append(summary)
    lines.append("")

    visible_severities: tuple[Severity, ...]
    if view == "blockers":
        visible_severities = ("blocker", "review")
        suppressed = (
            len(bucketed.get("note", []))
            + len(bucketed.get("info_clean", []))
            + len(bucketed.get("info_notice", []))
        )
        if suppressed:
            lines.append(
                f"_view='blockers' — {suppressed} entr"
                f"{'y' if suppressed == 1 else 'ies'} hidden (🟡/🟢)._"
            )
            lines.append("")
    else:
        visible_severities = ("blocker", "review", "note", "info_notice")

    for sev_key in visible_severities:
        entries = bucketed.get(sev_key, [])
        if not entries:
            continue
        glyph, label = _BUCKET_GLYPH_LABEL[sev_key]
        lines.append(f"## {glyph} {label} ({len(entries)})")
        lines.append("")
        for r in entries:
            lines.extend(_render_per_doi_block(r, sev_key))
        lines.append("")

    # Always surface unknown / malformed / check_failed at the bottom
    # so the user sees the coverage gap explicitly.
    for bad_key in ("unknown", "malformed", "check_failed"):
        entries = bucketed.get(bad_key, [])
        if not entries:
            continue
        glyph, label = _BUCKET_GLYPH_LABEL[bad_key]
        lines.append(f"## {glyph} {label} ({len(entries)})")
        lines.append("")
        for r in entries:
            note = ""
            if r.status == "check_failed" and r.error:
                note = f" — {r.error}"
            lines.append(f"- `{r.doi}`{note}")
        lines.append("")

    # Clean papers — single condensed line at the bottom of the
    # default view (don't show in blockers view; already suppressed).
    clean = bucketed.get("info_clean", [])
    if clean and view != "blockers":
        lines.append(f"## 🟢 Clean ({len(clean)})")
        lines.append("")
        for r in clean:
            title = f" — {r.paper_title}" if r.paper_title else ""
            lines.append(f"- `{r.doi}`{title}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _pl(n: int) -> str:
    return "s" if n != 1 else ""


# Bucket keys differ slightly from ``Severity`` because we split the
# ``info`` tier into "clean" (no notices at all) and "info_notice" (an
# informational notice exists, e.g. addendum) — the default view shows
# both but the headers and rendering differ.
_BucketKey = Literal[
    "blocker",
    "review",
    "note",
    "info_notice",
    "info_clean",
    "unknown",
    "malformed",
    "check_failed",
]


_BUCKET_GLYPH_LABEL: dict[str, tuple[str, str]] = {
    "blocker":      ("🔴", "Blocker"),
    "review":       ("🟠", "Review"),
    "note":         ("🟡", "Correction"),
    "info_notice":  ("🟢", "Informational notice"),
    "info_clean":   ("🟢", "Clean"),
    "unknown":      ("⚪", "Unknown DOI (Crossref 404)"),
    "malformed":    ("⚪", "Malformed DOI"),
    "check_failed": ("⚠️", "Check failed (transport error)"),
}


def _bucket_by_severity(
    results: list[ProvenanceResult],
) -> dict[str, list[ProvenanceResult]]:
    """Group results by severity / status into buckets ready for rendering."""
    buckets: dict[str, list[ProvenanceResult]] = {}
    for r in results:
        if r.status == "malformed":
            key = "malformed"
        elif r.status == "unknown":
            key = "unknown"
        elif r.status == "check_failed":
            key = "check_failed"
        elif not r.notices:
            key = "info_clean"
        else:
            sev = r.overall_severity
            key = "info_notice" if sev == "info" else sev
        buckets.setdefault(key, []).append(r)
    return buckets


def _format_summary(results: list[ProvenanceResult]) -> str:
    """Single-line summary of the batch outcome."""
    counts = {
        "ok": sum(1 for r in results if r.status == "ok"),
        "unknown": sum(1 for r in results if r.status == "unknown"),
        "malformed": sum(1 for r in results if r.status == "malformed"),
        "check_failed": sum(1 for r in results if r.status == "check_failed"),
    }
    blockers = sum(1 for r in results if r.overall_severity == "blocker")
    review = sum(
        1
        for r in results
        if r.status == "ok" and r.overall_severity == "review"
    )
    corrections = sum(
        1
        for r in results
        if r.status == "ok" and r.overall_severity == "note"
    )
    parts = [
        f"**{counts['ok']}/{len(results)}** resolved",
        f"🔴 {blockers}",
        f"🟠 {review}",
        f"🟡 {corrections}",
    ]
    if counts["unknown"]:
        parts.append(f"⚪ unknown: {counts['unknown']}")
    if counts["malformed"]:
        parts.append(f"⚪ malformed: {counts['malformed']}")
    if counts["check_failed"]:
        parts.append(f"⚠️ failed: {counts['check_failed']}")
    return " · ".join(parts)


def _render_per_doi_block(
    r: ProvenanceResult, bucket_key: str
) -> list[str]:
    """Render one result inside a batch section (concise — not the full
    single-DOI template, which would explode the report at 250 DOIs)."""
    lines: list[str] = []
    title = r.paper_title or "(no title)"
    authors_str = _format_authors(r.paper_authors)
    year_str = f" {r.paper_year}" if r.paper_year else ""
    lines.append(f"### `{r.doi}` — {authors_str}{year_str}")
    lines.append(f"_{title}_")
    if r.applied_status is not None:
        lines.append(f"- **Applied status**: `{r.applied_status}` (local ref id={r.paper_ref_id})")
    for n in r.notices:
        # In blockers view, hide 🟡/🟢 sub-notices — but in default
        # view we want to show every notice attached to a parent
        # paper, even if the *overall* severity put it in a lower
        # bucket.
        if bucket_key in ("blocker", "review") and n.severity not in ("blocker", "review"):
            continue
        lines.append(_format_notice_line(n))
    lines.append("")
    return lines


def _render_json(results: list[ProvenanceResult]) -> str:
    """Emit results as a JSON document.

    Schema is the obvious dataclass shape with datetime values
    rendered as ISO-8601 strings. ``ProvenanceResult.overall_severity``
    is included explicitly even though it's a computed property — it's
    the field downstream tooling actually wants.
    """

    def encode(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        return str(obj)

    def result_to_dict(r: ProvenanceResult) -> dict[str, Any]:
        d = asdict(r)
        # Datetimes inside Notice objects need a manual pass — asdict
        # leaves them as datetime objects.
        for n in d.get("notices", []):
            nd = n.get("notice_date")
            if isinstance(nd, datetime):
                n["notice_date"] = nd.isoformat()
        d["overall_severity"] = r.overall_severity
        return d

    payload = {
        "count": len(results),
        "results": [result_to_dict(r) for r in results],
    }
    return json.dumps(payload, indent=2, default=encode) + "\n"


__all__ = ["View", "render_batch", "render_single"]
