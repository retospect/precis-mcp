"""Prompt assembler + module library (ADR 0038).

One assembler + one module library, so the editor/planner, the
reviewers, the summarizer, and the judges share a cacheable, inspectable
prompt surface instead of ~8 hand-rolled concatenation sites.

Pipeline (ADR 0038 §3)::

    assemble(modules, ctx) -> [Block]        # model-agnostic
    adapter.render([Block]) -> messages|prompt  # model-specific, owns caching

Migration is "build one first, then fold in" (ADR 0038 §Migration): step 1
is ``workers/planner_prompt.py`` (the agent profile, ``claude_agent``
adapter); the summarizer, reviewers, and the rest fold in afterwards.

Public surface:

* :class:`Layer`, :class:`Profile`, :class:`Module`, :class:`Block`,
  :class:`AssemblyContext` — the value types.
* :func:`assemble` — select + order modules into blocks.
* :class:`ClaudeAgentAdapter` — render blocks to ``(system, user)``.
* the computed table builders (``tools_table``, ``kinds_table``,
  ``doc_context_table``, ``glossary_table``).
"""

from __future__ import annotations

from precis.utils.prompt.adapters import ClaudeAgentAdapter
from precis.utils.prompt.assembler import assemble
from precis.utils.prompt.model import (
    AssemblyContext,
    Block,
    Layer,
    Module,
    Profile,
)
from precis.utils.prompt.tables import (
    doc_context_table,
    glossary_table,
    kinds_table,
    tools_table,
)

__all__ = [
    "AssemblyContext",
    "Block",
    "ClaudeAgentAdapter",
    "Layer",
    "Module",
    "Profile",
    "assemble",
    "doc_context_table",
    "glossary_table",
    "kinds_table",
    "tools_table",
]
