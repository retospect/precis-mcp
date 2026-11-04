"""Re-extract a misfiled paper as slides without changing its PDF.

The caller locks/rechecks the paper and owns the transaction. Unlike the
datasheet kind flip, slide boundaries need new chunks, so the paper becomes
a tombstone and its original chunks/derived rows remain untouched. The new
presentation receives every identifier and compatible ref links. Any link
anchored to an original chunk, including an evidence pin, refuses conversion:
Marker's chunk page estimates cannot identify the corresponding PDF slide.
Other stored references to old pc handles are not rewritten or redirected.
Changed links receive the usual computed/hunch restamp.

Ref tags follow the datasheet policy: copy open/shared namespaces, omit
paper-only axes, their CASCADE markers and DREAM, then add subtype:slides.
The tombstone retains its historical tags. Ref metadata, authors and year
carry; old chunk tags and derived rows remain with the old chunks. New
embeddings are left to the worker. The existing superseded_by redirect plus
converted_from_kind on the survivor keeps the retired pa handle readable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.errors import BadInput
from precis.ingest.kind_convert import paper_only_tag_namespaces
from precis.ingest.pres import PresToWrite, PresWriteResult, write_pres
from precis.store import Store, Tag
from precis.store.revision_context import trust_door
from precis.utils import handle_registry


@dataclass(frozen=True)
class PlannedLink:
    """A compatible ref link whose paper endpoint can move to the deck."""

    link_id: int
    src: int
    dst: int


def plan_links(conn: Connection, ref_id: int) -> list[PlannedLink]:
    """Refuse all source chunk anchors/pins, then plan ref endpoint transfers.

    The ref lock held by the writer pins incoming FK-backed link inserts;
    lock existing links too before this probe on the write path.
    """
    rows = conn.execute(
        "SELECT l.link_id, l.src_ref_id, l.src_chunk_id, "
        "l.dst_ref_id, l.dst_chunk_id, l.relation, l.meta "
        "FROM links l "
        "WHERE l.src_ref_id = %s OR l.dst_ref_id = %s "
        "OR l.meta ? 'evidence' ORDER BY l.link_id",
        (ref_id, ref_id),
    ).fetchall()
    chunks = {
        int(row[0])
        for row in conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s",
            (ref_id,),
        ).fetchall()
    }
    planned = []
    blocked = []
    for row in rows:
        link_id, src, src_chunk, dst, dst_chunk, relation, meta = row
        anchors = {
            handle_registry.format_handle("paper", chunk, chunk=True)
            for chunk in (src_chunk, dst_chunk)
            if chunk in chunks
        }
        evidence = (meta or {}).get("evidence", [])
        pins = [evidence] if isinstance(evidence, str) else evidence
        if isinstance(pins, list):
            for pin in pins:
                parsed = handle_registry.parse(pin) if isinstance(pin, str) else None
                if parsed and parsed[:2] == ("paper", True) and parsed[2] in chunks:
                    anchors.add(pin)
        if anchors:
            blocked.append(
                f"link:{link_id} rel='{relation}' anchors {', '.join(sorted(anchors))}"
            )
        if src == ref_id or dst == ref_id:
            planned.append(PlannedLink(link_id, src, dst))
    if blocked:
        raise BadInput(
            "chunk-anchored links or evidence pins prevent conversion: "
            + "; ".join(blocked),
            next="remove or move each listed link off the paper's chunks, "
            "then retry convert_to='pres'",
        )
    return planned


def require_pdf_owner(
    conn: Connection, ref_id: int, sha: str | None, *, lock: bool = False
) -> None:
    """A duplicate outside this conversion must not keep owning the PDF hash."""
    owner = conn.execute(
        "SELECT ref_id FROM ref_identifiers "
        "WHERE id_kind = 'pdf_sha256' AND id_value = %s"
        + (" FOR UPDATE" if lock else ""),
        (sha,),
    ).fetchone()
    if owner is not None and owner[0] != ref_id:
        raise BadInput(
            "the PDF identifier belongs to another ref",
            next="resolve the duplicate PDF ownership before retrying "
            "convert_to='pres'",
        )


@trust_door("computed")
def apply_pres_convert(
    store: Store,
    conn: Connection,
    *,
    ref_id: int,
    pres: PresToWrite,
) -> PresWriteResult:
    """Write the deck and transfer ownership, within the caller's transaction."""
    conn.execute(
        "SELECT link_id FROM links WHERE src_ref_id = %s OR dst_ref_id = %s "
        "ORDER BY link_id FOR UPDATE",
        (ref_id, ref_id),
    ).fetchall()
    links = plan_links(conn, ref_id)
    require_pdf_owner(conn, ref_id, pres.pdf_sha256, lock=True)
    result = write_pres(pres, conn=conn)
    # write_pres reserves a suffixed slug while the paper still owns its
    # cite_key. Replace that key only when the paper has one to transfer;
    # otherwise keep the writer's stable fallback (including any suffix).
    original_key = conn.execute(
        "SELECT id_value FROM ref_identifiers "
        "WHERE ref_id = %s AND id_kind = 'cite_key' ORDER BY id_value LIMIT 1",
        (ref_id,),
    ).fetchone()
    if original_key is not None:
        conn.execute(
            "DELETE FROM ref_identifiers WHERE ref_id = %s AND id_kind = 'cite_key'",
            (result.ref_id,),
        )
        result = replace(result, slug=original_key[0], slug_suffixed=False)
    conn.execute(
        "UPDATE ref_identifiers SET ref_id = %s WHERE ref_id = %s",
        (result.ref_id, ref_id),
    )
    conn.execute(
        "UPDATE refs n SET title = o.title, authors = o.authors, year = o.year, "
        "meta = o.meta || n.meta || %s::jsonb "
        "FROM refs o WHERE n.ref_id = %s AND o.ref_id = %s",
        (
            Jsonb({"converted_from_kind": "paper", "subtype": "slides"}),
            result.ref_id,
            ref_id,
        ),
    )
    for link in links:
        conn.execute(
            "UPDATE links SET src_ref_id = %s, dst_ref_id = %s WHERE link_id = %s",
            (
                result.ref_id if link.src == ref_id else link.src,
                result.ref_id if link.dst == ref_id else link.dst,
                link.link_id,
            ),
        )
    conn.execute(
        "INSERT INTO ref_tags (ref_id, tag_id, set_by, created_at, expires_at) "
        "SELECT %s, rt.tag_id, rt.set_by, rt.created_at, rt.expires_at "
        "FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "WHERE rt.ref_id = %s AND NOT (t.namespace = ANY(%s))",
        (result.ref_id, ref_id, sorted(paper_only_tag_namespaces("pres"))),
    )
    store.add_tag(result.ref_id, Tag.open("subtype:slides"), conn=conn)
    # This lifecycle edge records the conversion itself, not a model's
    # evidence claim. Write it inside the lifecycle transaction;
    # add_link's machine-evidence admission does not apply here.
    conn.execute(
        "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
        "VALUES (%s, %s, 'supersedes', 'system')",
        (result.ref_id, ref_id),
    )
    store.stamp_ref_meta(ref_id, {"superseded_by": result.ref_id}, conn=conn)
    store.retire_ref(ref_id, conn=conn)
    return result
