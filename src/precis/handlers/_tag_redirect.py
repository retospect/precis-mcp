"""Compatibility shim — the yield-redirect moved to the store layer.

``precis.store._tag_redirect`` is the single chokepoint for
``ask-user:<question>`` / ``halt:<reason>`` long-yield tag redirects
(see its docstring for the whole design). It moved out of handlers for
gr652333: the ``asks_triage`` worker swaps a kept rewrite's
``ask-user:<question>`` tag through the same path, and the
workers→handlers import contract forbids ``precis.workers`` importing
``precis.handlers``. The handler call sites keep importing these names
from here unchanged.
"""

from precis.store._tag_redirect import (
    REDIRECTABLE_NAMESPACES,
    TAG_VALUE_REDIRECT_THRESHOLD,
    _see_chunk_backed,
    _value_needs_redirect,
    redirect_long_tag_values,
)

__all__ = [
    "REDIRECTABLE_NAMESPACES",
    "TAG_VALUE_REDIRECT_THRESHOLD",
    "_see_chunk_backed",
    "_value_needs_redirect",
    "redirect_long_tag_values",
]
