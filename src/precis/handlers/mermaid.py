"""MermaidHandler — the mermaid diagram kind (migration 0066, ADR 0057).

The mermaid instance of the shared diagram core: a slug-addressed ref on the
``draft`` chunk-tree substrate (parameterised ``kind='mermaid'``), never
exported (``corpus_role='none'``). The whole MCP surface lives in
:class:`precis.diagram.handler.DiagramHandler`; this class sets ``LANG =
MERMAID_LANG`` (the mermaidx-backed source mechanics + the mermaid handle
scheme, auto-layout so no viewBox axis) and its ``KindSpec``. A first-class
kind (registered like ``figure``); the ``[mermaid]`` extra provides the engine.
"""

from __future__ import annotations

from typing import ClassVar

from precis.diagram.handler import DiagramHandler
from precis.mermaid import MERMAID_LANG
from precis.protocol import KindSpec


class MermaidHandler(DiagramHandler):
    LANG: ClassVar = MERMAID_LANG
    spec: ClassVar[KindSpec] = KindSpec(
        kind="mermaid",
        title="Mermaid",
        description=(
            "A mermaid diagram you draw *with* the model (flowchart / sequence "
            "/ state / class …). put creates one (id=<slug>, title=, optional "
            "project=<todo>, or text=<mermaid source>); get lists / renders "
            "(source + shared vocabulary + mn<id> handle + node→chunk bindings "
            "+ lints) / reads a node mn<id>; edit sets the source (text=), the "
            "shared vocabulary (vocab=), or the implementation notes (notes=); "
            "link binds a node to the chunk it depicts (element=<node id>, "
            "target=<dc…/pc…/me…>); delete soft-retires. The interactive "
            "draw-with-me chat is the /mermaid web editor. corpus_role=none "
            "(never exported). See precis-mermaid-help."
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
        # slice 5) parents on the mermaid it builds/verifies.
        can_own_jobs=True,
    )
