"""``kind='special_site'`` — derived navigation surface.

One per named surface / interstitial site identified by the annotator,
scoped to a parent structure. Read-only from the agent's perspective —
populated by the view_worker as a side effect of annotation. Lets
``search(kind='special_site', parent='structure:<sha>', filter=...)``
answer questions like "bridge sites near a Cu atom".

Identity: ``site:<structure_sha>:<name>`` where ``name`` is the
annotator-assigned label (e.g. ``bridge_12_13``, ``octahedral_3``).
"""

from __future__ import annotations

from typing import Any

from precis.protocol import Handler, KindSpec
from precis.response import Response


class SpecialSiteHandler(Handler):
    spec = KindSpec(
        kind="special_site",
        title="Annotator-identified site",
        description=(
            "Named site identified by the annotator on a structure. "
            "Surface sites: top, bridge, fcc/hcp hollow. Bulk sites: "
            "octahedral / tetrahedral interstitial. Substitutional Wyckoff "
            "positions for known prototypes. Read-only; populated by the "
            "view_worker."
        ),
        supports_get=True,
        supports_search=True,
        is_numeric=False,
        id_required=False,  # search without id is the natural access pattern
    )

    def __init__(self, *, hub: Any) -> None:
        self.hub = hub

    def get(self, **kw: Any) -> Response:
        raise NotImplementedError("SpecialSiteHandler.get — Phase 0 wiring not landed")

    def search(self, **kw: Any) -> Response:
        raise NotImplementedError(
            "SpecialSiteHandler.search — Phase 0 wiring not landed"
        )
