"""Shared notice wording and revision-scoped citation decisions."""

from __future__ import annotations

from typing import Any

from precis.export.sources import _resolve_source_ref
from precis.store._draft_ops import content_sha
from precis.store._retraction_ops import RetractionNotice, RetractionVerdict

#: Appended to the label of a status with no recorded notice.
LEGACY_HINT = " (no notice recorded: run a forced retraction check)"


class RetractionMarks:
    """Memoize notice and verdict reads for one viewer window or export."""

    def __init__(self) -> None:
        self.notices: dict[int, list[RetractionNotice]] = {}
        self.verdicts: dict[
            tuple[int, str], dict[tuple[int, int], RetractionVerdict]
        ] = {}

    def for_paper(self, store: Any, paper: Any) -> list[RetractionNotice]:
        if paper.id not in self.notices:
            load = getattr(store, "retraction_notices", None)
            self.notices[paper.id] = load(paper.id) if load else []
        return self.notices[paper.id]

    def open_at(self, store: Any, paper: Any, dc: str, text: str) -> list[str]:
        """Labels of the notices still open for ``paper`` at block ``dc``
        whose current text is ``text``.

        A notice is open until its verdict for this block revision is
        closed. Legacy fallback: a paper whose ``refs.retraction_status`` is
        set but has no linked notice (an older correction, or a status set
        by enrichment without links) gets a plain status label. It has no
        review row, so it cannot be judged or closed: ``LEGACY_HINT`` says
        to run a forced retraction check, which records the notice.
        """
        notices = self.for_paper(store, paper)
        verdicts: dict[tuple[int, int], RetractionVerdict] = {}
        if notices and dc.startswith("dc") and dc[2:].isdigit():
            key = int(dc[2:]), content_sha(text)
            if key not in self.verdicts:
                self.verdicts[key] = store.retraction_verdicts(*key)
            verdicts = self.verdicts[key]
        result = []
        for notice in notices:
            verdict = verdicts.get((paper.id, notice.id))
            if verdict and verdict.closed:
                continue
            label = f"{notice.status.replace('_', ' ')} (pa{notice.id})"
            if verdict:
                label += " — affects this claim"
            result.append(label)
        legacy = getattr(paper, "retraction_status", None)
        if legacy and not any(n.status == legacy for n in notices):
            result.append(legacy.replace("_", " ") + LEGACY_HINT)
        return result

    def reference_label(self, store: Any, paper: Any) -> str:
        notices = self.for_paper(store, paper)
        full = [n for n in notices if n.full_retraction]
        legacy = getattr(paper, "retraction_status", None) == "retracted" and not any(
            n.status == "retracted" for n in notices
        )
        if not full and not legacy:
            return ""
        years = [n.date.year for n in full if n.date is not None]
        old_date = getattr(paper, "retracted_at", None)
        if (
            not years
            and old_date is not None
            and (legacy or any(n.nature is None for n in full))
        ):
            years.append(old_date.year)
        return f" (retracted {min(years)})" if years else " (retracted)"

    def citation_labels(
        self, store: Any, keys: tuple[str, ...], dc: str, text: str
    ) -> list[str]:
        if not hasattr(store, "get_ref"):
            return []
        labels = []
        for key in keys:
            paper = _resolve_source_ref(store, key)
            if paper is not None:
                labels.extend(self.open_at(store, paper, dc, text))
        return list(dict.fromkeys(labels))
