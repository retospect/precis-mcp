"""Store ops for the ``cad`` kind (the CAD analytic IR + Amendment 1).

Storage splits by what is actually a search target:

- the **design** is a slug-addressed ``refs`` row (``kind='cad'``);
  design-level metadata (units, tolerances) lives on ``refs.meta``;
- the design keeps **one** ``card_combined`` chunk — an auto-built
  summary (title + component + node names + shapes + bbox) — so
  ``search(kind='cad', q=…)`` works on intent and joins the cross-kind
  embedding search. One vector per design;
- the **nodes** live in the dedicated ``cad_nodes`` table — structured
  geometry, never embedded. Re-authoring retires the old node rows and
  the old card, then writes the new set (soft-delete model). A node's
  ``blend:`` (smooth-min width, :attr:`~precis.cad.scene.NodeSpec.blend`)
  has no column of its own: it rides on ``refs.meta['blends']`` as
  ``{node_name: metres}`` — written from the nodes on every save
  (:func:`_blends_meta`), re-attached to the nodes on load — the same
  design-level home ``materials``/``dims`` already use, so the rounding
  slice shipped without a migration;
- a **sampled-field grid** (a ``field:<sha256>`` leaf,
  :class:`~precis.cad.primitives.Field`) is a ``chunk_blobs`` payload
  (ADR 0034: chunk-keyed bytea, TOASTed, content-addressed) on a dedicated
  ``chunk_kind='field'`` chunk of the ref that produced it — ``text`` is
  the one-line human summary (shape, pitch, origin, source), ``meta.field``
  the same JSON header the payload starts with, so shape/pitch reads never
  de-TOAST the bytes. The DSL carries the payload's sha256, so a copied
  design keeps working and an identical grid (same samples, same header)
  is stored once; :meth:`CadMixin.put_field` returns the existing address
  instead of writing a twin. A changed grid is a **new** chunk + blob —
  the old row is never updated (the chunks rule), which is what keeps an
  older design's ``field:`` reference valid. :meth:`CadMixin.cad_load`
  binds a store-backed :data:`~precis.cad.dsl.FieldLoader` onto the spec.

Mixin assumes the concrete Store provides ``self.pool`` / ``self.tx`` /
``self.insert_ref`` / ``self.get_ref``.
"""

from __future__ import annotations

import hashlib
from typing import Any

from psycopg.types.json import Jsonb

from precis.cad.dsl import FIELD_REF_MIN, FieldLoader
from precis.cad.fieldops import (
    FIELD_MIME,
    decode_field,
    encode_field,
    payload_sha256,
)
from precis.cad.fieldops import field_header as _wire_header
from precis.cad.primitives import Field
from precis.cad.scene import NodeSpec, SceneSpec, coerce_pattern, spec_to_source
from precis.cad.vec import as_float3
from precis.errors import NotFound
from precis.utils.units import format_quantity


class FieldNotFound(NotFound, LookupError):
    """No stored field grid matches the reference — a ``NotFound`` for the
    dispatcher and a ``LookupError`` for :func:`precis.cad.dsl.build`,
    which turns it into a ``DslError`` naming the reference."""


class FieldAmbiguous(NotFound, LookupError):
    """A ``field:`` prefix matches more than one stored grid."""


#: ``pg_advisory_xact_lock`` namespace for :meth:`CadMixin.put_field`'s
#: per-ref critical section (its own int, distinct from
#: ``precis_se.persist.TREE_LOCK_NAMESPACE``'s — the two-int form keys
#: ``(namespace, ref_id)``, so different namespaces never contend). The
#: chunk ``ord`` is minted by ``MAX(ord) + 1`` under ``chunks_ref_id_ord_key``
#: (UNIQUE ``(ref_id, ord)``): two concurrent puts on one ref — the
#: ``se_simp`` job is a real concurrent caller — would otherwise read the
#: same max and one would die on the unique violation. The lock also
#: serialises the content-address dedup check, so two identical grids in
#: flight land as one row.
FIELD_PUT_LOCK_NAMESPACE = 0xF1E1D  # "field"


def _field_summary(fld: Field, provenance: dict[str, Any]) -> str:
    """The field chunk's ``text`` — the one line a human (or search) sees."""
    nx, ny, nz = fld.shape
    o = fld.origin
    src = provenance.get("source") or provenance.get("kind") or ""
    tail = f", source: {src}" if src else ""
    return (
        f"sdf field {nx}×{ny}×{nz} @ {format_quantity(fld.pitch, 'length')} pitch, "
        f"origin ({format_quantity(float(o[0]), 'length')}, "
        f"{format_quantity(float(o[1]), 'length')}, "
        f"{format_quantity(float(o[2]), 'length')}), "
        f"{'exact' if fld.exact else 'sign-correct'}{tail}"
    )


def cad_source_sha(spec: SceneSpec) -> str:
    """Content fingerprint of a design — sha256 of its canonical source.

    The version anchor for attached analyses (`analyzed-by` links pin it
    in ``links.meta``, ``cad_save`` records it in the ``ref_events`` row):
    content-derived, so a no-op re-save does not change it and cannot
    false-flag an analysis as stale.
    """
    return hashlib.sha256(spec_to_source(spec).encode("utf-8")).hexdigest()[:16]


def _blends_meta(spec: SceneSpec) -> dict[str, Any]:
    """``spec.meta`` with ``blends`` derived from the nodes (dropped when no
    node blends) — the nodes are the source of truth, the meta key only
    the persistence vehicle."""
    meta = dict(spec.meta)
    blends = {n.name: float(n.blend) for n in spec.nodes if n.blend > 0.0}
    if blends:
        meta["blends"] = blends
    else:
        meta.pop("blends", None)
    return meta


class CadMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any
    chunks: Any  # ChunkStore sub-store — the shared card_combined write
    append_event: Any  # EventsMixin — the cad-saved version-anchor row

    def cad_save(
        self,
        *,
        slug: str,
        title: str,
        spec: SceneSpec,
        card_text: str,
    ) -> tuple[Any, bool, int]:
        """Create-or-replace a design. Returns ``(ref, created, n_nodes)``."""
        existing = self.get_ref(kind="cad", id=slug)
        created = existing is None
        meta = _blends_meta(spec)
        with self.tx() as conn:
            if created:
                ref = self.insert_ref(
                    kind="cad",
                    slug=slug,
                    title=title,
                    meta=meta,
                    conn=conn,
                )
            else:
                ref = existing
                conn.execute(
                    "UPDATE cad_nodes SET retired_at = now() "
                    "WHERE ref_id = %s AND retired_at IS NULL",
                    (ref.id,),
                )
                conn.execute(
                    "UPDATE refs SET title = %s, meta = %s, updated_at = now() "
                    "WHERE ref_id = %s",
                    (title, Jsonb(meta), ref.id),
                )
            n = 0
            for ordi, node in enumerate(spec.nodes):
                conn.execute(
                    """
                    INSERT INTO cad_nodes
                        (ref_id, ord, name, component, op, config,
                         loc, rot, pattern)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        ref.id,
                        ordi,
                        node.name,
                        node.component,
                        node.op,
                        node.config,
                        list(node.loc),
                        list(node.rot),
                        Jsonb(node.pattern) if node.pattern is not None else None,
                    ),
                )
                n += 1
            self.chunks._replace_card_combined(conn, ref_id=ref.id, card_text=card_text)
            # Version anchor: one ref_events row per save carrying the
            # content sha, so `analyzed-by` attachments (which pin the sha
            # they analyzed into links.meta) can be compared for staleness
            # in SQL — and exports gain the same audit trail.
            self.append_event(
                ref.id,
                source="cad",
                event="saved",
                payload={"sha": cad_source_sha(spec), "n_nodes": n},
                conn=conn,
            )
        return ref, created, n

    # -- read ------------------------------------------------------------
    def cad_load(self, ref_id: int) -> tuple[SceneSpec, dict[str, int]]:
        """Reconstruct a design's :class:`SceneSpec` from ``cad_nodes``.

        Returns the spec plus a ``{node_name: node_id}`` map so the handler
        can render the ``ca<node_id>`` node handles.
        """
        ref = self.get_ref(kind="cad", id=ref_id)
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT node_id, name, component, op, config, loc, rot, pattern "
                "FROM cad_nodes WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY ord ASC",
                (ref_id,),
            ).fetchall()
        spec = SceneSpec()
        if ref is not None and ref.meta:
            spec.meta = dict(ref.meta)
        blends_raw = spec.meta.pop("blends", None) or {}
        blends = {str(k): float(v) for k, v in dict(blends_raw).items()}
        handles: dict[str, int] = {}
        components: list[str] = []
        for node_id, name, component, op, config, loc, rot, pattern in rows:
            spec.nodes.append(
                NodeSpec(
                    name=str(name),
                    op=str(op),
                    config=str(config),
                    component=str(component),
                    loc=as_float3(loc),
                    rot=as_float3(rot),
                    pattern=coerce_pattern(pattern),
                    blend=blends.get(str(name), 0.0),
                )
            )
            handles[str(name)] = int(node_id)
            if component not in components:
                components.append(str(component))
        spec.components = components or ["part"]
        spec.field_loader = self.field_loader()
        return spec, handles

    # -- sampled-field grids (chunk_blobs) --------------------------------
    def put_field(
        self,
        ref_id: int,
        fld: Field,
        *,
        provenance: dict[str, Any] | None = None,
    ) -> str:
        """Store a :class:`~precis.cad.primitives.Field` grid as a
        ``chunk_kind='field'`` chunk + ``chunk_blobs`` payload on ``ref_id``
        (the cad ref it belongs to, or the ref whose run produced it — the
        se design a SIMP result came from — when the cad design does not
        exist yet; lookup is by content address, not by ref). Returns the
        payload's sha256, the ``field:<sha256>`` the DSL carries.

        Content-addressed: if a payload with this sha is already stored
        (same samples, same header — ``provenance`` included) nothing is
        written and the existing address comes back. A different grid is
        always a new chunk; an existing one is never updated in place."""
        prov = dict(provenance or {})
        payload = encode_field(fld, prov)
        sha = payload_sha256(payload)
        header = _wire_header(fld, prov)
        with self.tx() as conn:
            # Held to commit: the ord mint + the dedup check below are one
            # critical section per ref (FIELD_PUT_LOCK_NAMESPACE).
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (FIELD_PUT_LOCK_NAMESPACE, int(ref_id)),
            )
            hit = conn.execute(
                "SELECT 1 FROM chunk_blobs WHERE sha256 = %s AND mime = %s",
                (sha, FIELD_MIME),
            ).fetchone()
            if hit is not None:
                return sha
            row = conn.execute(
                """
                INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta)
                VALUES (
                    %s, 'agent',
                    (SELECT COALESCE(MAX(ord), -1) + 1 FROM chunks WHERE ref_id = %s),
                    'field', %s, %s
                )
                RETURNING chunk_id
                """,
                (ref_id, ref_id, _field_summary(fld, prov), Jsonb({"field": header})),
            ).fetchone()
            assert row is not None
            conn.execute(
                "INSERT INTO chunk_blobs (chunk_id, bytes, mime, sha256, size_bytes) "
                "VALUES (%s, %s, %s, %s, %s)",
                (int(row[0]), payload, FIELD_MIME, sha, len(payload)),
            )
        return sha

    @staticmethod
    def _field_ref(ref: str) -> str:
        r = str(ref).strip().lower()
        if (
            len(r) < FIELD_REF_MIN
            or len(r) > 64
            or any(c not in "0123456789abcdef" for c in r)
        ):
            raise FieldNotFound(
                f"field reference {ref!r} is not a sha256 or a >= {FIELD_REF_MIN}-char "
                "hex prefix of one"
            )
        return r

    def field_sha(self, ref: str) -> str:
        """The full sha256 of the one stored field grid ``ref`` (a sha256 or
        a ``>= 12``-char prefix) names — how a boundary prefix becomes the
        canonical ``field:<sha256>`` the design stores. Raises
        :class:`FieldNotFound` / :class:`FieldAmbiguous` naming ``ref``."""
        r = self._field_ref(ref)
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT sha256 FROM chunk_blobs "
                "WHERE mime = %s AND sha256 LIKE %s LIMIT 2",
                (FIELD_MIME, r + "%"),
            ).fetchall()
        if not rows:
            raise FieldNotFound(
                f"no stored field grid matches {ref!r} — put_field it first "
                "(the design references a grid by content address)"
            )
        if len(rows) > 1:
            raise FieldAmbiguous(
                f"field reference {ref!r} matches more than one stored grid — "
                "give more of the sha256"
            )
        return str(rows[0][0])

    def field_header(self, ref: str) -> dict[str, Any] | None:
        """The stored grid's JSON header (shape, pitch_m, origin_m, exact,
        provenance) without de-TOASTing the samples — ``None`` when ``ref``
        names nothing. Ambiguous prefixes also read as ``None``."""
        try:
            sha = self.field_sha(ref)
        except NotFound:
            return None
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT c.meta FROM chunk_blobs b JOIN chunks c ON c.chunk_id = b.chunk_id"
                " WHERE b.sha256 = %s AND b.mime = %s LIMIT 1",
                (sha, FIELD_MIME),
            ).fetchone()
        if row is None:
            return None
        header = dict((row[0] or {}).get("field") or {})
        header.setdefault("sha256", sha)
        return header

    def get_field(self, ref: str) -> tuple[dict[str, Any], Field]:
        """Load a stored grid → ``(header, field)``; the field's samples are
        byte-identical to what :meth:`put_field` stored. ``ref`` as in
        :meth:`field_sha`, whose errors this raises."""
        sha = self.field_sha(ref)
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT bytes FROM chunk_blobs WHERE sha256 = %s AND mime = %s LIMIT 1",
                (sha, FIELD_MIME),
            ).fetchone()
        if row is None:  # pragma: no cover - field_sha just saw it
            raise FieldNotFound(f"no stored field grid matches {ref!r}")
        header, fld = decode_field(bytes(row[0]))
        header["sha256"] = sha
        return header, fld

    def field_loader(self) -> FieldLoader:
        """A :data:`~precis.cad.dsl.FieldLoader` bound to this store —
        what :meth:`cad_load` attaches to every spec it returns."""

        def _load(ref: str) -> Field:
            return self.get_field(ref)[1]

        return _load

    def cad_node(self, node_id: int) -> tuple[int, str, dict[str, Any]] | None:
        """A single live cad node by node_id → (ref_id, name, meta-dict)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id, name, component, op, config, loc, rot, pattern "
                "FROM cad_nodes WHERE node_id = %s AND retired_at IS NULL",
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        ref_id, name, component, op, config, loc, rot, pattern = row
        meta = {
            "component": component,
            "op": op,
            "config": config,
            "loc": list(loc or []),
            "rot": list(rot or []),
        }
        if pattern:
            meta["pattern"] = dict(pattern)
        ref = self.get_ref(kind="cad", id=int(ref_id))
        blend = ((ref.meta or {}).get("blends") or {}).get(str(name)) if ref else None
        if blend:
            meta["blend"] = float(blend)
        return int(ref_id), str(name), meta

    # -- delete ----------------------------------------------------------
    def cad_delete(self, ref_id: int) -> int:
        """Soft-delete a design: mark the ref deleted, retire its nodes,
        drop its search card — atomically. Returns nodes retired."""
        with self.tx() as conn:
            conn.execute(
                "UPDATE refs SET retired_at = now() "
                "WHERE ref_id = %s AND kind = 'cad' AND retired_at IS NULL",
                (ref_id,),
            )
            n = conn.execute(
                "UPDATE cad_nodes SET retired_at = now() "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            ).rowcount
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
                (ref_id,),
            )
        return int(n)
