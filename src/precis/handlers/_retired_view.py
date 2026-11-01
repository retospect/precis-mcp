"""``view='retired'`` — the opt-in tombstone read (gr548660).

A retired handle's default read redirects to the survivor:
``Store.resolve_handle`` follows ``meta.superseded_by`` and the
dispatcher rewrites ``id=`` to the survivor's public id before the
handler runs. An auditor after a gardener merge needs the tombstone
itself — its remaining identifiers, its own body chunks, the link set
it had at retirement, the ``superseded_by`` pointer and the merge
record. :func:`render_retired_ref` renders exactly those rows off the
retired ref; the view is read-only and never redirects.

Two merge shapes leave different evidence behind, and the render says
which it found:

- **garden merges** (``meta.garden_merge``) record the full link
  snapshot the apply repointed — the "links at retirement" list is the
  merge's own reversal record.
- **dedup merges** (``meta.dedup`` + ``meta.rehomed_cite_keys`` /
  ``meta.rehomed_cite_key_aliases``) migrate identifiers, body and
  links to the survivor with no link snapshot — the view shows what
  stayed on the tombstone plus the re-homed key record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.response import Response
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section
from precis.utils.timeutil import as_utc

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import Link, Ref


def _handle(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _ts(value: Any) -> str:
    dt = as_utc(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt is not None else "?"


def _endpoint(kind_of: dict[int, str], ref_id: int | None, chunk_id: int | None) -> str:
    """``pa42`` / ``ref 42`` / ``pa42~chunk7`` for a recorded link end."""
    if ref_id is None:
        return "?"
    h = _handle(kind_of.get(ref_id, "ref"), ref_id)
    return f"{h}~chunk{chunk_id}" if chunk_id is not None else h


def _live_link_line(kind_of: dict[int, str], link: Link) -> str:
    left = _endpoint(kind_of, link.src_ref_id, link.src_chunk_id)
    right = _endpoint(kind_of, link.dst_ref_id, link.dst_chunk_id)
    return f"  {left} —{link.relation}→ {right}"


def render_retired_ref(store: Store, ref: Ref) -> Response:
    """Render the retired ref itself — never the survivor it points at.

    Raises :class:`BadInput` on a live ref: the view answers "what did
    this ref look like when it folded", and a live row has no
    retirement to show. A merged alias (a slug the loser's key
    redirects through) resolves live too — that lands here as the
    survivor, which the same refusal names.
    """
    handle = _handle(ref.kind, ref.id)
    if ref.retired_at is None:
        raise BadInput(
            f"{handle} is live — view='retired' reads retired "
            "(merged/superseded) refs only; a merge loser's re-homed "
            "cite key resolves to the survivor, so address the "
            "tombstone by its own handle (e.g. pa<id>)",
            next=f"get(kind={ref.kind!r}, id={handle!r})",
        )
    meta = ref.meta or {}
    sup_id = meta.get("superseded_by")
    sup_ref = None
    if sup_id is not None:
        try:
            sup_ref = store.fetch_refs_by_ids([int(sup_id)], include_deleted=True).get(
                int(sup_id)
            )
        except (TypeError, ValueError):
            sup_ref = None

    body: list[str] = [f"# {handle} — retired {_ts(ref.retired_at)}", ""]
    body.append(f"title: {ref.title}")
    body.append(f"kind: {ref.kind}")

    if sup_id is not None:
        sup_h = _handle(sup_ref.kind, int(sup_id)) if sup_ref else f"ref {sup_id}"
        sup_title = f" — {sup_ref.title}" if sup_ref is not None else ""
        body.append(f"superseded_by: {sup_h}{sup_title}")
    else:
        body.append("superseded_by: (none)")

    garden = meta.get("garden_merge")
    if isinstance(garden, dict):
        body.append(
            f"merge: {garden.get('merge_id', '?')} (garden merge, "
            f"recorded {garden.get('at', '?')})"
        )
    elif meta.get("dedup") is not None:
        body.append(f"merge: dedup — {meta['dedup']}")
    else:
        body.append("merge: (no merge record — a plain retire)")

    if ref.pdf_sha256:
        pages = f", pages {ref.pdf_pages}" if ref.pdf_pages else ""
        role = f", role {ref.pdf_role}" if ref.pdf_role else ""
        body.append(f"pdf: {ref.pdf_sha256}{pages}{role}")

    idents = store.list_ref_identifiers(ref.id)
    if idents:
        body.append("identifiers (still on the retired row):")
        body.extend(
            f"  {scheme}={value} ({source})" for scheme, value, source in idents
        )
    else:
        body.append("identifiers: (none left on the row)")
    moved_keys = meta.get("rehomed_cite_keys") or []
    moved_aliases = meta.get("rehomed_cite_key_aliases") or []
    if moved_keys or moved_aliases:
        bits = [f"cite_key {k}" for k in moved_keys]
        bits += [f"cite_key_alias {a}" for a in moved_aliases]
        body.append(
            "rehomed to the survivor (recorded): " + ", ".join(str(b) for b in bits)
        )

    chunks = store.chunks.list_chunks_for_ref(ref.id)
    body.append(f"body chunks: {len(chunks)}")
    if chunks:
        first = " ".join((chunks[0].text or "").split())[:240]
        body.append(f"first chunk (ord {chunks[0].ord}): {first}")

    other_meta = sorted(
        k
        for k in meta
        if k
        not in {
            "superseded_by",
            "garden_merge",
            "dedup",
            "rehomed_cite_keys",
            "rehomed_cite_key_aliases",
        }
    )
    if other_meta:
        body.append("other meta keys: " + ", ".join(other_meta))

    endpoint_kinds = {ref.id: ref.kind}
    if sup_ref is not None:
        endpoint_kinds[sup_ref.id] = sup_ref.kind
    if isinstance(garden, dict) and isinstance(garden.get("links"), list):
        recorded = garden["links"]
        body.append(f"links at retirement ({len(recorded)}, recorded by the merge):")
        for orig in recorded:
            body.append(
                "  "
                + _endpoint(
                    endpoint_kinds, orig.get("src_ref_id"), orig.get("src_chunk_id")
                )
                + f" —{orig.get('relation', '?')}→ "
                + _endpoint(
                    endpoint_kinds, orig.get("dst_ref_id"), orig.get("dst_chunk_id")
                )
                + f"  [{orig.get('disposition', '?')}]"
            )
    else:
        live_links = store.links_for(ref.id)
        endpoint_kinds.update(
            {
                rid: r.kind
                for rid, r in store.fetch_refs_by_ids(
                    list(
                        {
                            rid
                            for lk in live_links
                            for rid in (lk.src_ref_id, lk.dst_ref_id)
                        }
                    ),
                    include_deleted=True,
                ).items()
            }
        )
        body.append(
            f"links on the row now ({len(live_links)} — most moved to the "
            "survivor at merge):"
        )
        for lk in live_links:
            body.append(_live_link_line(endpoint_kinds, lk))

    rendered = "\n".join(body)
    if sup_ref is not None:
        rendered += render_next_section(
            [
                (
                    f"get(kind={sup_ref.kind!r}, id={_handle(sup_ref.kind, sup_ref.id)!r})",
                    "read the survivor this ref merged into",
                ),
                (
                    "get(kind='job', id='/garden/merges')",
                    "the garden merge ledger (verdict, evidence, undone)",
                ),
            ]
        )
    return Response(body=rendered, result_count=1)


__all__ = ["render_retired_ref"]
