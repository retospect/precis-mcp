"""Resolve a projected fold to the original mmCIF before pure chain import."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from precis_se.ops import OpError

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.types import Relation


def resolve_trace_source(store: Store, op: dict[str, Any]) -> dict[str, Any]:
    """Read the fold CIF through convergence's protein-to-structure link."""
    source = op.get("source")
    if not isinstance(source, str) or not source.lstrip().startswith("structure:"):
        return op
    slug = source.partition(":")[2].strip()
    structure = store.get_ref(kind="structure", id=slug)
    if structure is None:
        raise OpError(f"import_trace: unknown structure {slug!r}")
    models: list[str] = []
    links = store.links_for(
        structure.id, direction="in", relation=cast("Relation", "has-fold-structure")
    )
    protein_ids = {
        link.src_ref_id if link.dst_ref_id == structure.id else link.dst_ref_id
        for link in links
    }
    for protein_id in protein_ids:
        protein = store.get_ref(kind="protein", id=protein_id)
        fold = (protein.meta or {}).get("fold") if protein is not None else None
        cif = fold.get("cif") if isinstance(fold, dict) else None
        if isinstance(cif, str) and cif.strip():
            models.append(cif)
    if len(models) != 1:
        raise OpError(
            f"import_trace: structure {slug!r} needs one linked protein fold with mmCIF; "
            "pass the original PDB or mmCIF as source="
        )
    return {**op, "source": models[0]}
