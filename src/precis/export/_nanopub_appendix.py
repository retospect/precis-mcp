"""Shared "Published claim artifacts" bookkeeping for the draft
exporters (docx / latex).

Once a cited claim hub's nanopub publish row reaches ``signed`` (artifact
bytes exist and are frozen), the export can point at the verifiable
artifact — the frozen AIDA sentence, the trusty URI, and a date — without
any draft edit: hubs without a minted artifact simply don't appear, so
the section grows as signing proceeds. This module resolves the entries
once, in first-citation order, with the status wording shared so the two
output formats never drift; each exporter renders its own markup.

The cited-finding list comes from the export's
:class:`~precis.export._trust_marks.TrustTracker` — every finding-backed
cite already resolves trust exactly once there, so its cache *is* the
cited set and no second accumulator threads through the render pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Publish states whose artifact exists and is byte-frozen — the
#: frozen-ness ladder's "artifact" rungs (``reviewed`` freezes only the
#: string; nothing citable-as-artifact exists yet).
MINTED_STATES = ("signed", "anchored", "published")

#: Section heading, identical in both exporters.
SECTION_TITLE = "Published claim artifacts"


@dataclass(frozen=True, slots=True)
class PublishedClaimEntry:
    """One appendix entry: the frozen claim sentence, the artifact's
    trusty URI, and the shared status wording (``published <date>`` vs
    the visually distinct ``signed/anchored <date> — under embargo``)."""

    sentence: str
    trusty_uri: str
    status_text: str


def published_claim_entries(store: Any, trust: Any) -> list[PublishedClaimEntry]:
    """The appendix entries for one export — one per cited claim hub
    whose live publish row is in :data:`MINTED_STATES`, in first-citation
    order (a hub cited many times yields exactly one entry). Empty when
    nothing cited is minted, when the export resolved no finding cites,
    or when ``store`` lacks the nanopub mixin (hand-rolled test fakes) —
    so nanopub-free exports are byte-identical to before."""
    if trust is None:
        return []
    row_of = getattr(store, "nanopub_publish_row", None)
    if row_of is None:
        return []
    entries: list[PublishedClaimEntry] = []
    for fid in trust.cited:
        row = row_of(fid)
        if row is None or row.state not in MINTED_STATES or not row.trusty_uri:
            continue
        entries.append(
            PublishedClaimEntry(
                sentence=row.approved_title or f"claim fi{fid}",
                trusty_uri=str(row.trusty_uri),
                status_text=_status_text(store, row),
            )
        )
    return entries


def _status_text(store: Any, row: Any) -> str:
    if row.state == "published" and row.published_at is not None:
        return f"published {row.published_at.date().isoformat()}"
    signed = ""
    artifact = (
        store.nanopub_artifact(row.artifact_id) if row.artifact_id is not None else None
    )
    if artifact is not None:
        signed = f" {artifact.created_at.date().isoformat()}"
    return f"{row.state}{signed} — under embargo, not yet public"


__all__ = [
    "MINTED_STATES",
    "SECTION_TITLE",
    "PublishedClaimEntry",
    "published_claim_entries",
]
