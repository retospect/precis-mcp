"""FigureHandler — the interactive SVG-canvas kind (migration 0057).

A ``figure`` is the **SVG instance** of the shared diagram core (ADR 0057): a
slug-addressed ref on the ``draft`` chunk-tree substrate (the DraftMixin ops,
parameterised ``kind='figure'``), never exported (``corpus_role='none'``). The
whole MCP surface (get / put / edit / delete / link, incl. the element→chunk
binding) lives in :class:`precis.diagram.handler.DiagramHandler`; this class is
the thin binding — it sets ``LANG = SVG_LANG`` (which carries the SVG mechanics
+ the figure handle scheme + the viewBox axis) and its ``KindSpec``. The
interactive draw-with-me turn loop is the shared :func:`precis.diagram.turn.run_turn`
driven by the ``/figure`` web editor.
"""

from __future__ import annotations

from typing import ClassVar

from precis.diagram.handler import DiagramHandler
from precis.figure.svg import SVG_LANG
from precis.protocol import KindSpec


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
            "depicts (element=<id>, target=<dc…/pc…/me…>). The interactive "
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
        role="artifact",
        corpus_role="none",
        # Compute-lane opt-in (ADR 0044): a diagram_propose job (ADR 0057
        # slice 5) parents on the figure it builds/verifies.
        can_own_jobs=True,
    )
