"""``kind='material'`` — established-knowledge aggregate.

A material record bundles:

- Identity: composition + phase + prototype + spacegroup.
- Canonical references: which bulk / surface structures + functional
  + reference scheme + solvation model count as official.
- Linked calculations: list of ``calc:<id>`` slugs.
- Derived: ``E_form``, ``bulk_modulus``, ``E_ads`` summary,
  ``reactions[<network_id>]`` (overpotentials by conditions hash).
- ``reaction_eval`` chunks: per-network full evaluation records
  (kept in chunks rather than meta to avoid MCP frame bloat).
"""

from __future__ import annotations

import json
from typing import Any

from precis.protocol import Handler, KindSpec
from precis.response import Response


class MaterialHandler(Handler):
    spec = KindSpec(
        kind="material",
        title="Catalyst material",
        description=(
            "Established catalyst aggregate: composition + phase + linked "
            "structures + linked dft_calculations + canonical refs + "
            "derived properties (eform, bulk_modulus, eads_summary, "
            "reactions[network_id]). The thing volcano_compute and "
            "pourbaix_*_compute read from."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        supports_edit=True,
        supports_tag=True,
        supports_link=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=True,
        views=(
            "toc",
            "header",
            "canonical",
            "derived",
            "reactions",
            "structures",
            "calculations",
        ),
    )

    def __init__(self, *, hub: Any) -> None:
        self.hub = hub

    def put(self, **kw: Any) -> Response:
        """Create a material aggregate.

        Required: ``id='material:<canonical_name>'``, ``composition={...}``,
        ``phase=<str>``. Optional: ``prototype``, ``aliases``,
        ``canonical={...}``, ``calculations=[<calc:id>, ...]``.

        Re-puts on the same id update the meta in place (composition,
        canonical refs, etc. evolve as more calcs land).
        """
        id_ = kw.get("id")
        if not id_ or not id_.startswith("material:"):
            return Response(body="material put requires id='material:<name>'")
        composition = kw.get("composition")
        if not isinstance(composition, dict) or not composition:
            return Response(
                body="material put requires composition={element: count, ...}"
            )
        phase = kw.get("phase")
        if not phase:
            return Response(
                body="material put requires phase=<str> (e.g. 'fcc', 'rutile')"
            )

        store = self._store()
        existing = store.fetch_ref_by_slug("material", id_)
        if existing is not None:
            # Update in place.
            updates = {
                k: v
                for k, v in kw.items()
                if k
                in (
                    "composition",
                    "phase",
                    "prototype",
                    "aliases",
                    "canonical",
                    "calculations",
                    "structures",
                    "derived",
                )
            }
            store.set_meta(existing.ref_id, **updates)
            return Response(body=f"updated material {id_}")

        meta = {
            "composition": composition,
            "phase": phase,
            "prototype": kw.get("prototype"),
            "aliases": kw.get("aliases", []),
            "canonical": kw.get("canonical", {}),
            "calculations": kw.get("calculations", []),
            "structures": kw.get("structures", []),
            "derived": kw.get("derived", {}),
        }
        title = _formula_string(composition)
        ref = store.insert_ref(
            kind="material",
            slug=id_,
            title=title,
            meta=meta,
        )
        return Response(
            body=(
                f"created material id={id_} (composition={title}, "
                f"phase={phase}, ref_id={ref.ref_id})"
            )
        )

    def get(self, **kw: Any) -> Response:
        id_ = kw.get("id")
        if not id_:
            return Response(body="material get requires id='material:<name>'")
        store = self._store()
        ref = store.fetch_ref_by_slug("material", id_)
        if ref is None:
            return Response(body=f"material not found: {id_}")

        view = kw.get("view")
        if view in (None, "summary", "toc", "header"):
            return Response(body=_format_summary(ref.meta, id_, store, ref))
        if view in ("canonical", "derived", "calculations", "structures"):
            return Response(body=str(ref.meta.get(view, {})))
        if view == "reactions":
            chunks = store.list_blocks_for_ref(ref.ref_id)
            reaction_chunks = [c for c in chunks if c.chunk_kind == "reaction_eval"]
            return Response(
                body=(
                    f"{len(reaction_chunks)} reaction evaluation(s):\n"
                    + "\n".join(
                        f"  {c.meta.get('network_id', '?')} (hash={c.meta.get('conditions_hash', '?')[:8]})"
                        for c in reaction_chunks
                    )
                )
            )
        return Response(body=f"unknown view {view!r}")

    def edit(self, **kw: Any) -> Response:
        """Two modes:

        - Default: merge fields into ``meta`` (think ``set_meta``).
        - ``mode='write_reaction_eval'`` with ``record=<dict>``:
          append a ``reaction_eval`` chunk carrying the full
          evaluation result.
        """
        id_ = kw.get("id")
        if not id_:
            return Response(body="material edit requires id='material:<name>'")
        store = self._store()
        ref = store.fetch_ref_by_slug("material", id_)
        if ref is None:
            return Response(body=f"material not found: {id_}")

        mode = kw.get("mode")
        if mode == "write_reaction_eval":
            record = kw.get("record")
            if not isinstance(record, dict):
                return Response(body="record={...} required for write_reaction_eval")
            return self._write_reaction_eval(store, ref, record)

        # Default: merge fields. Restrict to known keys.
        updates = {
            k: v
            for k, v in kw.items()
            if k
            in (
                "composition",
                "phase",
                "prototype",
                "aliases",
                "canonical",
                "calculations",
                "structures",
                "derived",
            )
        }
        if not updates:
            return Response(body="no recognised fields to update")
        store.set_meta(ref.ref_id, **updates)
        return Response(body=f"updated material {id_}: {sorted(updates)}")

    def search(self, **kw: Any) -> Response:
        raise NotImplementedError("material search — wiring not landed")

    def tag(self, **kw: Any) -> Response:
        raise NotImplementedError("material tag — wiring not landed")

    def link(self, **kw: Any) -> Response:
        raise NotImplementedError("material link — wiring not landed")

    # ── helpers ──────────────────────────────────────────────────

    def _write_reaction_eval(
        self, store: Any, ref: Any, record: dict[str, Any]
    ) -> Response:
        network_id = record.get("network_id", "?")
        conditions_hash = record.get("conditions_hash", "?")
        # Replace any prior chunk for the same (network, conditions).
        existing = [
            c
            for c in store.list_blocks_for_ref(ref.ref_id)
            if c.chunk_kind == "reaction_eval"
            and c.meta.get("network_id") == network_id
            and c.meta.get("conditions_hash") == conditions_hash
        ]
        # The mock store has delete_blocks_for_ref but not selective
        # delete by meta — we go through a full re-insert of the
        # rest. For real precis-mcp this becomes one DELETE/INSERT.
        if existing:
            keep = [
                c
                for c in store.list_blocks_for_ref(ref.ref_id)
                if not (
                    c.chunk_kind == "reaction_eval"
                    and c.meta.get("network_id") == network_id
                    and c.meta.get("conditions_hash") == conditions_hash
                )
            ]
            store.delete_blocks_for_ref(ref.ref_id)
            for c in keep:
                store.insert_blocks(
                    ref.ref_id,
                    [{"chunk_kind": c.chunk_kind, "text": c.text, "meta": c.meta}],
                )

        store.insert_blocks(
            ref.ref_id,
            [
                {
                    "chunk_kind": "reaction_eval",
                    "text": json.dumps(record, default=str),
                    "meta": {
                        "network_id": network_id,
                        "conditions_hash": conditions_hash,
                        "overpotential": record.get("overpotential"),
                        "rate_determining_step": record.get("rate_determining_step"),
                    },
                }
            ],
        )

        # Mirror the headline number into derived.reactions for
        # cheap volcano queries.
        derived = dict(ref.meta.get("derived", {}))
        reactions = dict(derived.get("reactions", {}))
        reactions[network_id] = {
            "overpotential": record.get("overpotential"),
            "rate_determining_step": record.get("rate_determining_step"),
            "conditions_hash": conditions_hash,
        }
        derived["reactions"] = reactions
        store.set_meta(ref.ref_id, derived=derived)

        return Response(
            body=(
                f"wrote reaction_eval on {ref.slug}: network={network_id} "
                f"η={record.get('overpotential', '?'):.3f} V "
                f"(RDS step {record.get('rate_determining_step', '?')})"
            )
        )

    def _store(self) -> Any:
        if self.hub is not None and hasattr(self.hub, "store"):
            return self.hub.store
        return self.store  # type: ignore[attr-defined]


def _formula_string(composition: dict[str, int]) -> str:
    """Render ``{Pt:3, Ni:1}`` as ``Pt3Ni`` for the ref title."""
    parts = []
    for element, count in sorted(composition.items()):
        parts.append(f"{element}{count if count > 1 else ''}")
    return "".join(parts)


def _format_summary(
    meta: dict[str, Any],
    id_: str,
    store: Any,
    ref: Any,
) -> str:
    composition = meta.get("composition", {})
    derived = meta.get("derived", {})
    reactions_summary = derived.get("reactions", {})
    chunks = store.list_blocks_for_ref(ref.ref_id)
    n_evals = sum(1 for c in chunks if c.chunk_kind == "reaction_eval")
    return (
        f"# {id_}\n"
        f"composition:    {_formula_string(composition)}\n"
        f"phase:          {meta.get('phase', '?')}\n"
        f"prototype:      {meta.get('prototype', '?')}\n"
        f"n_calcs:        {len(meta.get('calculations', []))}\n"
        f"n_structures:   {len(meta.get('structures', []))}\n"
        f"reactions:      {sorted(reactions_summary)}\n"
        f"reaction_evals: {n_evals} chunk(s)\n"
    )
