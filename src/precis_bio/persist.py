"""Store write-back for fold results — shared by the handler's inline
slice-0 path and the ``fold`` worker dispatch.

Kept out of both ``protein.py`` and ``jobs.py`` so neither imports the other:
the handler mints/awaits a job; the job runs the engine; **both** land the
resulting :class:`~precis_bio.ir.ProteinFold` on the protein ref through this
one function, so the write shape (``meta.fold`` + the ``card_combined`` search
chunk) is defined once. Mirrors ``precis_chem.persist.apply_route_result``.
"""

from __future__ import annotations

from typing import Any

from precis_bio.ir import ProteinFold


def apply_fold_result(
    store: Any,
    protein_ref_id: int,
    fold: ProteinFold,
    *,
    cache_key: str,
) -> None:
    """Persist a fold onto its ref.

    Writes the normalized structure to ``meta.fold``, stamps the content-address
    (``meta.cache_key``) + status + the scalar confidences (so a list/render
    needn't load the whole CIF blob), and (re-)emits the embeddable
    ``card_combined`` chunk so the protein is searchable by sequence/name.
    Idempotent — a re-run with the same ``cache_key`` overwrites in place (the
    DELETE+INSERT card keeps the embedding cascade clean).
    """
    with store.tx() as conn:
        store.stamp_ref_meta(
            protein_ref_id,
            {
                "fold": fold.to_json(),
                "cache_key": cache_key,
                "engine": fold.engine,
                "engine_version": fold.engine_version,
                "mode": fold.mode,
                "status": "folded" if fold.folded else "no-model",
                "plddt_mean": fold.plddt_mean,
                "ptm": fold.ptm,
                "iptm": fold.iptm,
            },
            conn=conn,
        )
        store.upsert_card_combined(protein_ref_id, fold.card_text(), conn=conn)
