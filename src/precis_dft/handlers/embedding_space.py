"""``kind='embedding_space'`` — versioned vector-index header.

Carries the encoder, dimensionality, reducer (PCA / UMAP), and the
list of supported proposal strategies (``nearest`` / ``direction`` /
``bo_acquisition``). Vectors themselves live in the
``dft_embeddings`` table (one row per (space_id, structure_id));
bulk matrices and PCA loadings live on NFS at
``/shared/dft/embeddings/<space_id>/``.

Identity: ``embedding_space:<name>``, e.g. ``embedding_space:catalysts_v1``.
Version is in ``meta.version`` (not in the id) so the same space can
be re-indexed without breaking references.
"""

from __future__ import annotations

from typing import Any

from precis.protocol import Handler, KindSpec
from precis.response import Response


class EmbeddingSpaceHandler(Handler):
    spec = KindSpec(
        kind="embedding_space",
        title="Embedding-space index header",
        description=(
            "Vector index over structures: encoder (MACE-MP-0 or CHGNet "
            "latents), dimensionality (after optional PCA), reducer, "
            "supported proposal strategies. Vectors live in the "
            "dft_embeddings table; this ref is the index manifest."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=True,
        supports_edit=True,
        supports_tag=True,
        is_numeric=False,
        id_required=True,
        views=("toc", "header", "loadings", "directions", "anchors"),
    )

    def __init__(self, *, hub: Any) -> None:
        self.hub = hub

    def get(self, **kw: Any) -> Response:
        raise NotImplementedError(
            "EmbeddingSpaceHandler.get — Phase 0 wiring not landed"
        )

    def search(self, **kw: Any) -> Response:
        raise NotImplementedError(
            "EmbeddingSpaceHandler.search — Phase 0 wiring not landed"
        )

    def put(self, **kw: Any) -> Response:
        raise NotImplementedError(
            "EmbeddingSpaceHandler.put — Phase 0 wiring not landed"
        )

    def edit(self, **kw: Any) -> Response:
        raise NotImplementedError(
            "EmbeddingSpaceHandler.edit — Phase 0 wiring not landed"
        )

    def tag(self, **kw: Any) -> Response:
        raise NotImplementedError(
            "EmbeddingSpaceHandler.tag — Phase 0 wiring not landed"
        )
