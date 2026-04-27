"""FlashcardHandler — Q/A pairs with spaced-repetition metadata.

Numeric-id ref kind addressed as ``fc``. The body is the *knowledge
statement*; the agent generates a quiz dynamically from that statement
at review time (per v1 design — see grimoire/agents/flashcard-review.md).

SM-2 review state lives in ``ref.meta`` JSON:

    {
      "easiness": 2.5,
      "interval": 1,
      "reps": 0,
      "next_review": "2026-04-28T10:00:00Z",
      "last_reviewed": null,
      "review_log": []
    }

Phase 5 ships a thin handler with create / read / search / list-due.
The full SM-2 grader (grade=0..5 → next interval) lands in a follow-up
once the agent surface for review feedback is finalised.

List views:
    /due     — cards whose next_review is in the past
    /recent  — most recent (default)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.utils.next_block import render_next_section


class FlashcardHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="fc",
        title="Flashcard",
        description=(
            "Spaced-repetition knowledge card. Body is the knowledge "
            "statement; agent generates the quiz format at review time. "
            "SM-2 schedule kept in ref.meta."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        is_numeric=True,
        id_required=False,
    )

    kind: ClassVar[str] = "fc"
    sense: ClassVar[str] = "flashcard"

    def _list_view(self, view: str) -> Response | None:
        if view == "due":
            return self._render_due()
        return super()._list_view(view)

    def _render_due(self) -> Response:
        refs = self.store.list_refs(kind=self.kind, limit=200)
        now = datetime.now(UTC)
        due: list[tuple[int, str]] = []
        upcoming: list[tuple[int, str, datetime]] = []  # within 3 days
        for r in refs:
            meta = r.meta or {}
            nxt_raw = meta.get("next_review")
            if not nxt_raw:
                # Untouched cards count as due.
                due.append((r.id, r.title))
                continue
            try:
                nxt = datetime.fromisoformat(str(nxt_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if nxt <= now:
                due.append((r.id, r.title))
            elif (nxt - now).days <= 3:
                upcoming.append((r.id, r.title, nxt))

        if not due and not upcoming:
            return Response(body="no flashcards due")

        lines: list[str] = []
        if due:
            lines.append(f"# {len(due)} flashcard(s) due")
            for ref_id, title in due:
                preview = (title[:80] + "…") if len(title) > 80 else title
                lines.append(f"  {ref_id:>4}  {preview}")
        if upcoming:
            lines.append("")
            lines.append(f"## {len(upcoming)} due within 3 days")
            for ref_id, title, nxt in upcoming:
                preview = (title[:60] + "…") if len(title) > 60 else title
                when = nxt.date().isoformat()
                lines.append(f"  {ref_id:>4}  ({when})  {preview}")

        body = "\n".join(lines)
        body += render_next_section(
            [
                ("get(kind='fc', id=N)", "read the card to quiz yourself"),
                (
                    "put(kind='fc', id=N, tags=['REVIEW:good'])",
                    "log a review (SM-2 grade)",
                ),
                ("put(kind='fc', text='knowledge statement')", "create a new card"),
            ]
        )
        return Response(body=body)
