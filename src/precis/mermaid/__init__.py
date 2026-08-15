"""The ``mermaid`` diagram kind — a second instance of the shared diagram core.

A mermaid diagram is edited through the identical draw-with-me loop as a
``figure`` (``precis.diagram.turn``), with its nodes bindable to the corpus
chunks they depict. Only the *language* differs: the source is mermaid text,
validated + rendered + exported by the pure-Python ``mermaidx`` engine (an
embedded QuickJS running the real mermaid.js + resvg — no Node, no Chromium,
no container). ``MERMAID_LANG`` is the :class:`~precis.diagram.lang.DiagramLang`
instance; ``mermaidx`` is lazy-imported so nothing loads it unless a mermaid
turn actually validates/renders. The kind is first-class (registered like
``figure``); ``mermaidx`` is a core dependency, so validation/render work
out of the box.
"""

from precis.mermaid.mermaid import MERMAID_LANG, MermaidLang, render_svg

__all__ = ["MERMAID_LANG", "MermaidLang", "render_svg"]
