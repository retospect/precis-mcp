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

#: The reasons a brief session may refuse a source todo: it does not say
#: what should change (``rewrite``), it is more than one reviewable change
#: (``split``), it needs a decision only a person can make (``decide``),
#: the code already does it (``built``, with commit or ``file:line``
#: evidence), it is not a code change (``not-code``: a run, a measurement,
#: a backfill, a production step).
REFUSAL_REASONS: tuple[str, ...] = ("rewrite", "split", "decide", "built", "not-code")

#: What a brief session ends with — the one place the closing section in
#: ``run_slice`` and the verdict parser share. Either outcome rides the
#: FIRST line of the final message; any other ending is a failed attempt.
BRIEF_GRAMMAR = (
    "`brief: td<build id>` as the FIRST line of the final message, the brief "
    "text on the following lines; or "
    f"`refuse: <{'|'.join(REFUSAL_REASONS)}> — <what is missing, or the "
    "evidence>` as the FIRST line"
)

__all__ = [
    "BRIEF_GRAMMAR",
    "FACTORY_BUILD_TAG",
    "NEEDS_SPEC_TAG",
    "REFUSAL_REASONS",
    "SOURCE_TODO_KEY",
]
