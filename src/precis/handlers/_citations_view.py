"""``get(kind='draft', id=<slug>, view='citations')`` — the draft-citation
lifecycle view (to-fetch / re-ground / promote / done).

A draft cites corpus papers with three token forms: ``[pa<id>]`` (whole
paper), ``[pc<id>]`` (paper chunk), and the taproot end-state,
``[fi<hub>]``/``[<pub_id>]`` (the claim). This view partitions every such
citation in a draft into exactly one of four states — **to-fetch**,
**to-re-ground**, **to-promote**, **done** — so an author can work them
down (gripe 180155's "papers to fetch for this draft" worklist, and its
home for the whole lifecycle map).

**Purely derived — no LLM call, no new storage.** A cite's state is a
function of only its token kind (``pa``/``pc``/``fi``, or a base32
``[pub_id]`` placeholder) and, for ``pa``/``pc``, the cited paper's
body-block count (``Store.count_blocks``). Selectivity (is a ``[pc]``
cite-group actually a promotable claim, vs. background prose?) is
explicitly NOT decided here — that is ``taproot/canon.py::extract_claim``,
an LLM call that already runs inside ``backfill``'s promote dry-run; every
``[pc]`` with a fetched paper shows as to-promote regardless, and the
NO-CLAIM/skip determination surfaces at promote-time where the call
already happens (see the proposal's blocker-7 resolution).

Token scanning mirrors :func:`precis.utils.refeye._mine_claim_hub_ids`'s
interleave of the two citation grammars (:data:`~precis.utils.mentions.
BARE_BRACKET_REF_PATTERN` for handle-form cites, :data:`~precis.utils.
pub_id_lookup.PLACEHOLDER_RE` for base32 ``[pub_id]`` placeholders),
scanned in text order and span-deduped so a pub_id shaped like a handle
(e.g. ``fi2345``) is never double-counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.format import toon
from precis.response import Response
from precis.utils import handle_registry
from precis.utils.mentions import BARE_BRACKET_REF_PATTERN
from precis.utils.pub_id_lookup import PLACEHOLDER_RE, lookup_pub_id_finding

if TYPE_CHECKING:
    from precis.store import Ref, Store

#: The four partitions, in the view's stable render order.
_PARTITIONS: tuple[str, ...] = ("to-fetch", "to-re-ground", "to-promote", "done")

_PARTITION_TITLE: dict[str, str] = {
    "to-fetch": "TO FETCH",
    "to-re-ground": "TO RE-GROUND",
    "to-promote": "TO PROMOTE",
    "done": "DONE",
}

#: Next-action label per partition (display only — no action is taken by
#: this read-only view).
_ACTION_LABEL: dict[str, str] = {
    "to-fetch": "fetch",
    "to-re-ground": "re-ground [pa]→[pc]",
    "to-promote": "promote",
    "done": "—",
}

_TABLE_SCHEMA = ["dc", "token", "handle", "title", "doi", "action"]


@dataclass(frozen=True)
class _RawCite:
    """One resolved paper/finding citation mined from a draft chunk —
    pre-partition (block-count not yet consulted)."""

    dc: str  # the draft chunk handle carrying this cite, e.g. "dc1234"
    token: str  # the raw bracketed token as written, e.g. "[pc9911]"
    kind: str  # "paper" | "finding"
    ref_id: int  # the paper's (or finding's) ref_id — chunk cites resolved
    is_chunk: bool  # True for a [pc<id>] cite; irrelevant for kind="finding"


def _iter_chunk_tokens(text: str) -> list[tuple[str, str, str]]:
    """Every ``pa``/``pc``/``fi`` handle token or ``[pub_id]`` placeholder
    in ``text``, in appearance order, as ``(raw_token, tag, payload)``:

    - ``tag='pubid'`` — ``payload`` is the decoded 6-char base32 pub_id.
    - ``tag='handle'`` — ``payload`` is the bare handle (``'pa42'`` etc).

    Interleaves the two grammars by match position and skips a handle-form
    match that overlaps an already-claimed pub_id span (a pub_id shaped
    like a handle, e.g. ``fi2345``, would otherwise match both) — the same
    dedup :func:`precis.utils.refeye._mine_claim_hub_ids` does. The ``¶``/
    ``§`` sigil alternative of :data:`BARE_BRACKET_REF_PATTERN`'s ``bare``
    group is skipped (not a citable-kind handle).
    """
    pub_matches = list(PLACEHOLDER_RE.finditer(text))
    pub_spans = [m.span() for m in pub_matches]
    handle_matches = list(BARE_BRACKET_REF_PATTERN.finditer(text))
    events: list[tuple[int, str, Any]] = [
        (m.start(), "pubid", m) for m in pub_matches
    ] + [(m.start(), "handle", m) for m in handle_matches]
    events.sort(key=lambda e: e[0])

    out: list[tuple[str, str, str]] = []
    for _pos, tag, m in events:
        if tag == "pubid":
            out.append((m.group(0), "pubid", m.group(1)))
            continue
        bare = m.group("bare")
        if bare[0] in "¶§":
            continue
        if any(m.start() < pe and ps < m.end() for ps, pe in pub_spans):
            continue  # already claimed by the pub_id scan
        out.append((m.group(0), "handle", bare))
    return out


def _collect_raw_cites(store: Store, chunks: list[Any]) -> list[_RawCite]:
    """Walk a draft's chunks in reading order, mining every paper/finding
    citation. A ``[pc<id>]`` chunk cite is resolved to its owning paper via
    ``Store.resolve_handle`` (the chunk→ref join); tokens naming any other
    kind (memory, todo, …) — a well-formed handle but not a citable-kind
    one — are silently skipped, as are unresolvable pub_id placeholders
    (accidental base32-looking prose)."""
    out: list[_RawCite] = []
    for chunk in chunks:
        for raw, tag, payload in _iter_chunk_tokens(chunk.text or ""):
            if tag == "pubid":
                lookup = lookup_pub_id_finding(store, payload)
                if lookup is None:
                    continue
                out.append(
                    _RawCite(
                        dc=chunk.dc,
                        token=raw,
                        kind="finding",
                        ref_id=lookup["ref_id"],
                        is_chunk=False,
                    )
                )
                continue
            parsed = handle_registry.parse(payload)
            if parsed is None:
                continue
            kind, is_chunk, pk = parsed
            if kind == "finding":
                out.append(
                    _RawCite(
                        dc=chunk.dc,
                        token=raw,
                        kind="finding",
                        ref_id=pk,
                        is_chunk=False,
                    )
                )
            elif kind == "paper":
                if is_chunk:
                    resolved = store.resolve_handle(payload)
                    if resolved is None or resolved.kind != "paper":
                        continue  # dead/merged-away chunk — skip
                    out.append(
                        _RawCite(
                            dc=chunk.dc,
                            token=raw,
                            kind="paper",
                            ref_id=resolved.ref_id,
                            is_chunk=True,
                        )
                    )
                else:
                    out.append(
                        _RawCite(
                            dc=chunk.dc,
                            token=raw,
                            kind="paper",
                            ref_id=pk,
                            is_chunk=False,
                        )
                    )
            # else: a well-formed handle of some other kind (memory, todo,
            # …) cited bare — not a paper/claim citation, not this view's
            # business.
    return out


def _title_of(title: str | None, fallback: str) -> str:
    """First line of ``title``, capped at 100 chars; a title-less ref
    (or a live-but-title-less stub) shows its handle instead (proposal
    open-question 4)."""
    if not title or not title.strip():
        return fallback
    line = title.strip().splitlines()[0].strip()
    if len(line) > 100:
        return line[:100].rstrip() + "…"
    return line


def _partition_of(*, kind: str, is_chunk: bool, block_count: int) -> str:
    """The pure classifier — token kind + block-count, nothing else
    (proposal's whole partition rule)."""
    if kind == "finding":
        return "done"
    if block_count == 0:
        return "to-fetch"
    return "to-promote" if is_chunk else "to-re-ground"


def _build_rows(store: Store, raw: list[_RawCite]) -> dict[str, list[dict[str, str]]]:
    paper_ids = {c.ref_id for c in raw if c.kind == "paper"}
    finding_ids = {c.ref_id for c in raw if c.kind == "finding"}
    refs_by_id = store.fetch_refs_by_ids(paper_ids | finding_ids)
    dois = store.identifiers_for_refs(list(paper_ids))
    block_counts = {rid: store.count_blocks(rid) for rid in paper_ids}

    buckets: dict[str, list[dict[str, str]]] = {p: [] for p in _PARTITIONS}
    for c in raw:
        handle = handle_registry.format_handle(c.kind, c.ref_id)
        found_ref = refs_by_id.get(c.ref_id)
        title = _title_of(found_ref.title if found_ref else None, handle)
        if c.kind == "finding":
            partition = "done"
            doi = ""
        else:
            block_count = block_counts.get(c.ref_id, 0)
            partition = _partition_of(
                kind=c.kind, is_chunk=c.is_chunk, block_count=block_count
            )
            doi = dois.get(c.ref_id, {}).get("doi", "")
        buckets[partition].append(
            {
                "dc": c.dc,
                "token": c.token,
                "handle": handle,
                "title": title,
                "doi": doi,
                "action": _ACTION_LABEL[partition],
            }
        )
    return buckets


def _render(ref: Ref, buckets: dict[str, list[dict[str, str]]]) -> Response:
    lines = [f"# {ref.slug or ref.id} — citation lifecycle", ""]
    for partition in _PARTITIONS:
        rows = buckets[partition]
        lines.append(f"## {_PARTITION_TITLE[partition]} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.append("(none)")
        else:
            lines.append(toon.dump(rows, schema=_TABLE_SCHEMA))
        lines.append("")
    return Response(body="\n".join(lines).rstrip() + "\n")


def render_citations_view(store: Store, ref: Ref) -> Response:
    """Render ``view='citations'`` for a draft: every paper/claim cite in
    its chunks, partitioned into to-fetch / to-re-ground / to-promote /
    done. Read-only — no writes, no LLM call (see module docstring)."""
    chunks = store.drafts.reading_order(ref.id)
    raw = _collect_raw_cites(store, chunks)
    buckets = _build_rows(store, raw)
    return _render(ref, buckets)


def draft_fetch_ref_ids(store: Store, ref: Ref) -> list[int]:
    """Distinct paper ref_ids in this draft's **to-fetch** partition — cited
    but with zero body blocks (a stub). The papers-to-fetch worklist behind
    ``/drive?cited_by=<draft>`` (proposal AC5's draft-scoped acquisition
    queue). Reuses the citations view's own token scan + block-count
    derivation, so the drive scope and the ``view='citations'`` to-fetch
    partition can never diverge. Read-only, no LLM."""
    chunks = store.drafts.reading_order(ref.id)
    raw = _collect_raw_cites(store, chunks)
    paper_ids = {c.ref_id for c in raw if c.kind == "paper"}
    # Bulk "which of these have body chunks" (one query) minus set — the
    # to-fetch papers are those with none. Avoids an N+1 count per cited paper
    # (a lit-review draft cites 50–100+).
    return sorted(paper_ids - store.ref_ids_with_chunks(list(paper_ids)))


__all__ = ["draft_fetch_ref_ids", "render_citations_view"]
