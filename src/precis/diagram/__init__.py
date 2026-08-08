"""The shared diagram-editing core (ADR 0057, slice 3).

A *diagram* is a model-owned source document (SVG for ``figure``, mermaid for
``mermaid``) edited *with* a human through a draw-with-me turn loop, with its
elements bindable to the corpus chunks they depict. The loop — the three-doc
model (source / shared vocabulary / private notes), the JSON reply contract,
the bounded auto-heal, the element→chunk binding reconcile, and the
prepared-context assembly — is identical across languages; only the *source
language* differs (compile / sanitize / lint / extract-elements / render).

That language surface is the :class:`~precis.diagram.lang.DiagramLang` port.
``figure`` is the SVG instance (``precis.figure.svg.SVG_LANG``); ``mermaid``
is the second (``precis.mermaid.mermaid.MERMAID_LANG``). ``precis.figure.turn``
/ ``precis.figure.context`` are thin shims that bind ``SVG_LANG`` to the
generic core here, so the figure handler / web route / tests are untouched by
the factoring.

Element→chunk bindings: a node (by its stable id) binds to the chunk it
depicts via a chunk-level ``depicts`` link (element id in
``links.meta.elements``); the prepared context lists each node + topology +
the linked chunk body, and a ``[binding]`` lint catches drift.

Autonomous tick: the ``diagram_propose`` job_type (ADR 0057 slice 5,
``precis.workers.job_types.diagram_propose``) runs **one** turn from an
instruction + seed chunk handles, mutating the diagram in place and
reconciling bindings — owned by the diagram itself (compute lane;
figure/mermaid set ``KindSpec.can_own_jobs``).
"""
