"""Prompt assembler + module library.

One assembler + one module library, so the editor/planner, the
reviewers, the summarizer, and the judges share a cacheable, inspectable
prompt surface instead of ~8 hand-rolled concatenation sites.

Pipeline::

    assemble(modules, ctx) -> [Block]        # model-agnostic
    adapter.render([Block]) -> messages|prompt  # model-specific, owns caching

Migration is "build one first, then fold in": step 1
is ``workers/planner_prompt.py`` (the agent profile, ``claude_agent``
adapter); the summarizer, reviewers, and the rest fold in afterwards.

Public surface:

* :class:`Layer`, :class:`Profile`, :class:`Module`, :class:`Block`,
  :class:`AssemblyContext` — the value types.
* :func:`assemble` — select + order modules into blocks.
* :class:`ClaudeAgentAdapter` — render blocks to ``(system, user)``.
* :class:`LiteLLMAdapter` — render blocks to an OpenAI ``messages`` list.
* the computed table builders (``tools_table``, ``kinds_table``,
  ``doc_context_table``, ``glossary_table``).
* :func:`persist_assembled_context` — the input-side twin of
  ``meta.transcript``: write the assembled block list onto a ref's meta so a
  debugging surface can render "what the LLM actually saw last time".
"""

from __future__ import annotations

from precis.utils.prompt.adapters import ClaudeAgentAdapter, LiteLLMAdapter
from precis.utils.prompt.assembler import assemble
from precis.utils.prompt.capture import persist_assembled_context
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
    section_review_block,
    tools_table,
)

__all__ = [
    "AssemblyContext",
    "Block",
    "ClaudeAgentAdapter",
    "Layer",
    "LiteLLMAdapter",
    "Module",
    "Profile",
    "assemble",
    "doc_context_table",
    "glossary_table",
    "kinds_table",
    "persist_assembled_context",
    "section_review_block",
    "tools_table",
]
