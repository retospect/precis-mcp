"""FigureHandler — the interactive SVG-canvas kind (migration 0057).

A ``figure`` is the **SVG instance** of the shared diagram core: a
slug-addressed ref on the ``draft`` chunk-tree substrate (the DraftStore ops,
reached as ``store.drafts``, parameterised ``kind='figure'``), never exported
(``corpus_role='none'``). The
shared MCP surface lives in :class:`precis.diagram.handler.DiagramHandler`.
This class adds draft attachment and binds the SVG language, handle scheme
and viewBox to the shared core. The ``/figure`` editor drives the turn loop.
"""

from __future__ import annotations

from typing import ClassVar

from precis.diagram.handler import DiagramHandler
from precis.errors import BadInput
from precis.figure.svg import SVG_LANG
from precis.protocol import KindSpec, Response


class FigureHandler(DiagramHandler):
    LANG: ClassVar = SVG_LANG
    spec: ClassVar[KindSpec] = KindSpec(
        kind="figure",
        title="Figure",
        description=(
            "An interactive SVG canvas you draw *with* the model. put creates "
            "a figure (id=<slug>, title=, optional project=<todo>, optional "
            "viewbox='0 0 W H' or text=<svg>); get lists / renders the figure "
            "(assembled SVG + shared vocabulary + fn<id> source handle + "
            "lints) / reads a node fn<id>; edit sets the SVG source (text=), "
            "the shared vocabulary (vocab=), the implementation notes (notes="
            "— the model's private design log), or the viewBox (viewbox=); delete "
            "soft-retires the figure. link binds an element to the chunk it "
            "depicts (element=<id>, target=<dc…/pc…/me…>) or attaches the canvas "
            "to a draft figure (rel='figure-in', target=<dc…>). The interactive "
            "draw-with-me chat is in the /figure web editor. corpus_role=none "
            "(never exported). See precis-figure-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        note_like=True,
        placement="artifact",
        corpus_role="none",
        views=("links", "log", "raw", "lints"),
        # A figure's addressable chunks are fn<chunk_id> node handles —
        # the dispatcher's generic slug~ord rewrite must leave them alone
        # (gr793934 for the mermaid twin; same class as plan's gr495745).
        supports_chunk_selectors=False,
        # Compute-lane opt-in: a diagram_propose job parents on the figure it builds/verifies.
        can_own_jobs=True,
    )

    def link(
        self,
        *,
        id: str | int = "",
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        element: str | None = None,
        **kwargs: object,
    ) -> Response:
        if rel != "figure-in" or element is not None:
            return super().link(
                id=id, target=target, mode=mode, rel=rel, element=element, **kwargs
            )
        anchor = self.store.drafts.get_draft_chunk(target) if target else None
        if (
            mode != "add"
            or anchor is None
            or anchor.retired
            or anchor.chunk_kind != "figure"
        ):
            raise BadInput(
                "figure-in needs a live draft figure chunk as its target",
                next="link(kind='figure', id='<slug>', rel='figure-in', target='dc42')",
            )
        ref = self._resolve_any(id)
        self.store.drafts.link_figure_canvas(anchor.chunk_id, ref.id)
        return Response(body=f"embedded {ref.slug} at {anchor.dc}")
