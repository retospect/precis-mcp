"""CitationHandler — verified claim → source pointer.

Numeric-ref kind written by the **citation-fill workflow**: an
agent drafts a claim, a verifier subagent confirms the source
quote precisely supports it, and the result lands here as a
durable, queryable record. Reads support assembling a
bibliography (``get(kind='citation', id='/recent')`` and the
future ``get(kind='paper', id=<slug>, view='bibliography')``
aggregator).

Record shape (stored in ``refs.meta``):

::

    {
      "claim": "MOF X achieves 12% FE for CO2 reduction",
      "source_handle": "collins06~7",
      "source_quote": "we observed 12% Faradaic efficiency for ...",
      "char_offset": 142,
      "verifier_confidence": 0.95,
      "verifier_caveats": null,
      "verified_at": "2026-05-31T14:23:00Z"
    }

The ``source_handle`` is a paper-side chunk address (``slug~N`` or
``slug~A..B``) — the verifier can revisit the exact span at any
time, and ``view='bibliography'`` will format it as a citation in
its rendered output.

Storage details:

* ``kind='citation'`` is seeded in ``0001_initial.sql`` (originally
  added in the archived ``0007_citation_kind.sql``).
* The claim summary (``text=`` on put) lives in ``refs.title`` for
  list-view scannability.
* The full record sits in ``refs.meta`` as a JSON object.
* ``link='paper:<slug>'`` + ``rel='cites'`` connects each citation
  to its source paper via the existing ``links`` machinery, so
  ``links_for(paper)`` surfaces "papers I cite" lookups for free.

The verifier itself is **client-side** (a subagent the writing
thread spawns); this handler only owns the storage door.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import Ref, Tag


class CitationHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="citation",
        title="Citation",
        description=(
            "Verified claim → source pointer. Written by the citation-fill "
            "workflow after the verifier confirms the source quote precisely "
            "supports the claim. Stores claim text, source chunk handle, "
            "verbatim quote, verifier confidence, and verified_at timestamp."
        ),
        supports_put=True,
        supports_get=True,
        supports_search=True,
        supports_search_hits=False,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=False,
    )
    kind: ClassVar[str] = "citation"
    sense: ClassVar[str] = "citation"

    # ──────────────────────────────────────────────────────────────────
    # put — create a verified citation
    # ──────────────────────────────────────────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        source_handle: str | None = None,
        source_quote: str | None = None,
        char_offset: int | None = None,
        verifier_confidence: float | None = None,
        verifier_caveats: str | None = None,
        verified_at: str | None = None,
        tags: list[str] | None = None,
        link: str | None = None,
        rel: str | None = None,
        mode: str | None = None,
        untags: list[str] | None = None,
        unlink: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a citation record.

        Required: ``text`` (claim summary), ``source_handle`` (chunk
        address like ``"collins06~7"``), ``source_quote`` (verbatim
        text supporting the claim).

        Recommended: ``verifier_confidence`` (0..1, the verifier
        subagent's confidence), ``verified_at`` (ISO-8601 timestamp;
        defaults to now). ``link='paper:<slug>'`` + ``rel='cites'``
        wires the citation to the source paper for graph queries.

        Existing-id ``put`` is rejected — citations are write-once
        (re-verification creates a new citation referencing the
        same source).
        """
        if id is not None:
            raise BadInput(
                f"put on existing citation id={id!r} is not supported "
                "(citations are write-once; re-verification creates a new one)",
                next=f"put(kind={self.kind!r}, text=..., source_handle=..., ...)",
            )
        if mode is not None or untags is not None or unlink is not None:
            raise BadInput(
                f"only id-less create is supported on kind={self.kind!r}",
                next="put creates a new citation; use tag/link/delete on existing",
            )
        if not text or not text.strip():
            raise BadInput(
                "put(kind='citation') requires text=<claim summary>",
                next=(
                    "put(kind='citation', text='claim summary', "
                    "source_handle='collins06~7', source_quote='...', "
                    "verifier_confidence=0.95, link='paper:collins06', rel='cites')"
                ),
            )
        if not source_handle or not str(source_handle).strip():
            raise BadInput(
                "put(kind='citation') requires source_handle=<chunk address>",
                next=(
                    "source_handle is the paper-side chunk handle, e.g. "
                    "'collins06~7' or 'collins06~5..8'"
                ),
            )
        if not source_quote or not str(source_quote).strip():
            raise BadInput(
                "put(kind='citation') requires source_quote=<verbatim text>",
                next=(
                    "source_quote is the exact wording from the source "
                    "chunk that the verifier confirmed supports the claim"
                ),
            )
        if (
            verifier_confidence is not None
            and not 0.0 <= float(verifier_confidence) <= 1.0
        ):
            raise BadInput(
                "verifier_confidence must be between 0.0 and 1.0",
                next=f"verifier_confidence={verifier_confidence!r}",
            )

        verified_iso = verified_at or datetime.now(UTC).isoformat()

        record: dict[str, Any] = {
            "claim": text.strip(),
            "source_handle": str(source_handle).strip(),
            "source_quote": str(source_quote).strip(),
            "char_offset": int(char_offset) if char_offset is not None else None,
            "verifier_confidence": (
                float(verifier_confidence) if verifier_confidence is not None else None
            ),
            "verifier_caveats": verifier_caveats,
            "verified_at": verified_iso,
        }

        # Tag + link plumbing — same shape as other numeric-ref puts.
        # Validation happens before any DB write so a bad tag or
        # unknown link target fails before we touch the row.
        parsed_tags: list[Tag] = []
        if tags:
            parsed_tags = [Tag.parse_strict(t, kind=self.kind) for t in tags]
        target = None
        relation_slug = rel or "cites"
        if link is not None:
            from precis.handlers._link_target import parse_link_target

            target = parse_link_target(link, store=self.store)

        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=text.strip()[:200],  # cap title at sane scannable length
                meta=record,
                conn=conn,
            )
            for tag in parsed_tags:
                self.store.add_tag(
                    ref.id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            if target is not None:
                self.store.add_link(
                    src_ref_id=ref.id,
                    dst_ref_id=target.ref_id,
                    dst_pos=target.pos,
                    relation=relation_slug,
                    conn=conn,
                )

        return Response(
            body=(
                f"created citation id={ref.id} "
                f"({_one_line(text.strip(), 60)})\n"
                f"source: {record['source_handle']}\n"
                f"verifier_confidence: {record['verifier_confidence']}\n"
                f"verified_at: {record['verified_at']}"
            )
        )

    # ──────────────────────────────────────────────────────────────────
    # get — render the stored citation
    # ──────────────────────────────────────────────────────────────────

    def _render_one(self, ref: Ref, tags: Any) -> str:  # type: ignore[override]
        """Render one citation record.

        Pulls the claim / source / verifier fields out of ``ref.meta``
        and formats them in a stable, scannable order. Tags (if any)
        ride along on the trailing ``tags:`` line like every other
        numeric-ref kind.
        """
        meta = ref.meta or {}
        lines = [f"# citation {ref.id}"]
        claim = meta.get("claim") or ref.title or ""
        lines.append(f"_{claim}_")
        lines.append("")
        lines.append(f"source: `{meta.get('source_handle') or '?'}`")
        quote = meta.get("source_quote")
        if quote:
            lines.append(f'quote: "{quote}"')
        if meta.get("char_offset") is not None:
            lines.append(f"char_offset: {meta['char_offset']}")
        conf = meta.get("verifier_confidence")
        if conf is not None:
            lines.append(f"verifier_confidence: {conf}")
        caveats = meta.get("verifier_caveats")
        if caveats:
            lines.append(f"verifier_caveats: {caveats}")
        verified_at = meta.get("verified_at")
        if verified_at:
            lines.append(f"verified_at: {verified_at}")
        if tags:
            lines.append("")
            lines.append("tags: " + " ".join(str(t) for t in tags))
        return "\n".join(lines)


def _one_line(text: str, limit: int) -> str:
    """Single-line truncation for the create-ack one-liner."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


__all__ = ["CitationHandler"]
