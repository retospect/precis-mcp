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
will be a second instance. ``precis.figure.turn`` / ``precis.figure.context``
are thin shims that bind ``SVG_LANG`` to the generic core here, so the figure
handler / web route / tests are untouched by the factoring.
"""
