"""The todo-sourced build chain: the names the dispatcher, the claim
path and the rescue pass share.

A chain minted from a plain todo (not a gripe) hangs off its source the
way a gripe chain does: the plan root is ``related-to`` the source todo
and carries ``meta.source_todo`` = the source's integer ref id. The
``related-to`` edge alone never makes a todo chain — any other todo a
root links to stays a plain relation — so the key is what the chain
inventory (``workers.gripe_dispatch``) and the source holds
(``workers.executors._common.source_todo_holds``) match on.
"""

from __future__ import annotations

#: Plan-root ``meta`` key holding the source todo's ref id (an integer).
SOURCE_TODO_KEY = "source_todo"
#: Open tag that makes a todo eligible for the factory; withdrawing it
#: holds the chain built from it.
FACTORY_BUILD_TAG = "factory:build"
#: Open tag on a todo the factory refused to build: it holds the source
#: until a human rewrites or rules on it.
NEEDS_SPEC_TAG = "needs-spec"

__all__ = ["FACTORY_BUILD_TAG", "NEEDS_SPEC_TAG", "SOURCE_TODO_KEY"]
