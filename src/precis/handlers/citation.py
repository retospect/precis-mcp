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

The ``source_handle`` is a chunk address (``slug~N`` or ``slug~A..B``)
into either a ``paper`` or a ``patent`` (docs/backlog/
patent-evidence-parity.md Phase 3) — a bare/unprefixed handle defaults
to ``paper`` for backward compatibility; an explicit ``patent:<slug>``
prefix (or a ``pk<id>`` universal chunk handle) points at a patent. The
verifier can revisit the exact span at any time, and
``view='bibliography'`` will format it as a citation in its rendered
output.

Storage details:

* ``kind='citation'`` is seeded in ``0001_initial.sql`` (originally
  added in the archived ``0007_citation_kind.sql``).
* The claim (``text=`` on put) is stored **in full** in ``refs.title``
  — truncation is a *display* concern (the Drive list caps it via
  ``_display_title``), never a storage one.
* The full record sits in ``refs.meta`` as a JSON object.
* ``link='paper:<slug>'`` (or ``link='patent:<slug>'``) + ``rel='cites'``
  connects each citation to its source via the existing ``links``
  machinery, so ``links_for(paper|patent)`` surfaces "who cites me"
  lookups for free.

The verifier itself is **client-side** (a subagent the writing
thread spawns); this handler only owns the storage door.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers._link_tag_ops import apply_tag_ops
from precis.handlers._numeric_ref import NumericRefHandler
from precis.handlers._patent_family import family_representative
from precis.handlers._patent_ingest import FAMILY_STUB_META_KEY
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import Ref
from precis.utils import handle_registry


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

    def put(
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
        self._reject_mutating_put(
            id=id,
            mode=mode,
            untags=untags,
            unlink=unlink,
            rel=rel,
            link=link,
            id_note="citations are write-once; re-verification creates a new one",
        )
        if not text or not text.strip():
            raise BadInput(
                "put(kind='citation') requires text=<claim summary>",
                next=(
                    "put(kind='citation', text='claim summary', "
                    "source_handle='collins06~7', source_quote='...', "
                    "verifier_confidence=0.95, link='pa5', rel='cites')"
                ),
            )
        if not source_handle or not str(source_handle).strip():
            raise BadInput(
                "put(kind='citation') requires source_handle=<chunk address>",
                next=(
                    "source_handle is the paper- or patent-side chunk "
                    "handle, e.g. 'collins06~7' or 'ep1234567b1~5..8'"
                ),
            )
        # Accept a universal chunk handle (``pc<id>`` / ``pk<id>``)
        # — the form search output now emits — and normalize it to the
        # canonical ``slug~ord`` form so downstream validation and storage
        # stay unchanged. ``resolved.kind`` is carried separately (not
        # embedded in ``sh``, which stays a bare, kind-less slug~ord string
        # for storage compat) so a patent chunk handle doesn't silently
        # default to "paper" below.
        sh = str(source_handle).strip()
        resolved_kind: str | None = None
        if handle_registry.parse(sh) is not None:
            resolved = self.store.resolve_handle(sh)
            if resolved is not None and resolved.chunk_ord is not None:
                sh = f"{resolved.public_id}~{resolved.chunk_ord}"
                resolved_kind = resolved.kind
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
        # Resolve the source_handle's (kind, slug) and verify that ref
        # exists in the corpus. A citation that doesn't point at a real
        # paper or patent is by definition not a citation — it's a guess.
        # Before this check, the LLM could mint citations against
        # fabricated bib keys ("smith2024quantum") that downstream broke
        # bibtex generation at compile time. Now they fail at put time
        # with a next-hint that routes to the right recovery. Runs after
        # the shape checks so callers that get the shape wrong still see
        # the shape error first.
        extracted = _extract_source_slug(sh, default_kind=resolved_kind or "paper")
        if extracted is None:
            raise BadInput(
                f"put(kind='citation') source_handle={source_handle!r} names "
                "an unsupported source kind - citations may only point at "
                "'paper' or 'patent' sources",
                next="source_handle='paper:<slug>~N' or 'patent:<slug>~N'",
            )
        source_kind, source_slug = extracted
        source_ref = self.store.get_ref(kind=source_kind, id=source_slug)
        if source_ref is None:
            # The source may have been merged away by dedup — follow the
            # ``meta.superseded_by`` tombstone to the live survivor before
            # declaring it missing (mirrors the link-target redirect).
            dead = self.store.get_ref(
                kind=source_kind, id=source_slug, include_deleted=True
            )
            if dead is not None:
                surv = self.store.follow_supersede(dead.id)
                if surv is not None:
                    source_ref = self.store.get_ref(kind=source_kind, id=surv)
        if source_ref is None:
            if source_kind == "paper":
                next_hint = (
                    f"put(kind='finding', body='<claim>', "
                    f"cited_in='paper:{source_slug}', "
                    "verifier_confidence=0.5)"
                )
            else:
                next_hint = (
                    f"get(kind='patent', id='{source_slug}')  # ingest it from EPO OPS"
                )
            raise BadInput(
                f"source_handle={source_handle!r} references "
                f"{source_kind} {source_slug!r}, but no such {source_kind} "
                "exists in the corpus. Citations must point at real papers "
                "or patents; "
                + (
                    "mint a kind='finding' first to start the chase, then "
                    "write the citation once it lands."
                    if source_kind == "paper"
                    else "ingest the patent first."
                ),
                next=next_hint,
            )
        # A simple-family stub (docs/backlog/patent-evidence-parity.md
        # Phase 2) carries biblio meta only — no description/claims blocks
        # to hold a chunk-addressed quote. Point the caller at the family's
        # actual full member instead of failing opaquely on a chunk lookup.
        if source_kind == "patent" and (source_ref.meta or {}).get(
            FAMILY_STUB_META_KEY
        ):
            rep = family_representative(
                self.store, (source_ref.meta or {}).get("family_id")
            )
            rep_slug = rep.slug if rep is not None and rep.slug else None
            raise BadInput(
                f"source_handle={source_handle!r} points at {source_slug!r}, "
                "a simple-family stub (biblio only, no description/claims "
                "blocks to cite)"
                + (
                    f" - cite the family representative {rep_slug!r} instead"
                    if rep_slug
                    else " - no full family member is ingested yet"
                ),
                next=(
                    f"put(kind='citation', ..., source_handle='{rep_slug}~N', ...)"
                    if rep_slug
                    else f"get(kind='patent', id='{source_slug}')  # re-check the family"
                ),
            )

        verified_iso = verified_at or datetime.now(UTC).isoformat()

        record: dict[str, Any] = {
            "claim": text.strip(),
            "source_handle": sh,
            "source_quote": str(source_quote).strip(),
            "char_offset": int(char_offset) if char_offset is not None else None,
            "verifier_confidence": (
                float(verifier_confidence) if verifier_confidence is not None else None
            ),
            "verifier_caveats": verifier_caveats,
            "verified_at": verified_iso,
        }

        # Tag + link plumbing — same shape as other numeric-ref puts.
        # The link target resolves before the tx so an unknown target
        # fails before we touch the row; user tags go through the shared
        # ``apply_tag_ops`` inside the tx (a bad tag rolls the create
        # back atomically).
        target = None
        relation_slug = rel or "cites"
        if link is not None:
            from precis.handlers._link_target import parse_link_target

            target = parse_link_target(link, store=self.store)

        with self.store.tx() as conn:
            ref = self.store.insert_ref(
                kind=self.kind,
                slug=None,
                title=text.strip(),  # full claim — truncation is display-only
                meta=record,
                conn=conn,
            )
            # The claim is novel, agent-authored prose. refs.title now
            # holds it in full, but the title column isn't embedded and
            # refs.meta isn't indexed at all. Mirror the claim into a
            # card_combined chunk (ord=-1) so the embed + chunk_keywords
            # workers index it — citations become semantically searchable,
            # not just a lexical match on the title. Citations are
            # write-once, so the card never needs re-syncing.
            #
            # source_quote is deliberately NOT chunked: it's a verbatim
            # copy of the span at source_handle (paper:<slug>~N), which is
            # already an embedded chunk — re-embedding it would just
            # duplicate that vector.
            self.store.upsert_card_combined(ref.id, text.strip(), conn=conn)
            apply_tag_ops(
                self.store, self.kind, ref.id, tags=tags, untags=None, conn=conn
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

    def _render_one(self, ref: Ref, tags: Any) -> str:
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


#: The only source kinds a citation may validate against (docs/backlog/
#: patent-evidence-parity.md Phase 3). Anything else named by an explicit
#: ``kind:`` prefix is rejected outright, rather than silently skipped.
_ACCEPTED_SOURCE_KINDS = ("paper", "patent")


def _extract_source_slug(
    source_handle: str, *, default_kind: str = "paper"
) -> tuple[str, str] | None:
    """Return ``(kind, slug)`` embedded in a ``source_handle``, or ``None``.

    Accepts the kind-qualified form (``paper:tsmc2024iedm`` /
    ``patent:ep1234567b1``) and the bare-slug form (``tsmc2024iedm``, with
    or without a ``~N`` chunk suffix or ``~A..B`` range) — the latter
    defaults to ``default_kind``. ``default_kind`` lets a caller that
    already resolved a universal chunk handle (``pc<id>`` / ``pk<id>``, ADR
    0036) to a specific ref kind pass that kind through: normalization
    reduces such a handle to a bare, kind-less ``slug~ord`` string, so
    without this override a patent chunk handle would silently re-guess
    "paper".

    Returns ``None`` when the resolved kind — whether from an explicit
    ``kind:`` prefix or from ``default_kind`` (a bare handle, including one
    the caller already resolved from a universal chunk handle of a
    non-paper/non-patent kind, e.g. ``ec5``) — falls outside
    :data:`_ACCEPTED_SOURCE_KINDS`. The allowlist is enforced on every path,
    not just the explicit-prefix one — a bare handle must not bypass it by
    inheriting an unaccepted ``default_kind``.
    """
    h = source_handle.strip()
    kind = default_kind
    if ":" in h:
        prefix, _, rest = h.partition(":")
        kind, h = prefix.lower(), rest
    if kind not in _ACCEPTED_SOURCE_KINDS:
        return None
    # Strip chunk address (``~N`` or ``~A..B``)
    h = h.split("~", 1)[0]
    slug = h.strip()
    return (kind, slug) if slug else None


def _one_line(text: str, limit: int) -> str:
    """Single-line truncation for the create-ack one-liner."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


__all__ = ["CitationHandler"]
