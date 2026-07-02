"""The assembler — select + order modules into layer-tagged blocks.

``assemble(modules, ctx)`` is model-agnostic (ADR 0038 §3): it walks an
ordered module list, drops any whose ``applies_when`` predicate is false
or whose ``build`` yields nothing, and returns the surviving
:class:`Block` list in declaration order. An **adapter**
(:mod:`precis.utils.prompt.adapters`) then packages those blocks for one
runner. Model quirks never touch a module — they live in the adapter.

The assembler does *not* reorder by layer: module order is authored
intent (mechanics before contract; brief before body). The adapter is
what groups by layer for caching.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from precis.utils.prompt import predicates
from precis.utils.prompt.model import AssemblyContext, Block, Module

log = logging.getLogger(__name__)


def assemble(modules: Sequence[Module], ctx: AssemblyContext) -> list[Block]:
    """Resolve ``modules`` against ``ctx`` into an ordered block list.

    For each module, in order:

    1. If ``applies_when`` is set and its predicate is false → skip
       (capability *and* data gated together, ADR 0038 §8).
    2. Call ``build(ctx)``; a falsy result drops the module (the common
       "nothing to say this tick" case — e.g. no children yet).
    3. Otherwise emit a :class:`Block` carrying the module's id + layer.

    A builder that raises is logged and dropped, never fatal: one broken
    optional block must not sink the whole prompt (the planner runs
    unattended across the fleet). The exception is a ``required=True``
    module — one whose silent omission would corrupt a persisted artifact
    (a reviewer body digest); its failure re-raises so the caller aborts
    rather than shipping a truncated result."""
    blocks: list[Block] = []
    for mod in modules:
        if mod.applies_when is not None:
            try:
                if not predicates.evaluate(mod.applies_when, ctx):
                    continue
            except Exception:
                if mod.required:
                    raise
                log.exception(
                    "prompt.assemble: predicate %r failed for module %r",
                    mod.applies_when,
                    mod.id,
                )
                continue
        try:
            text = mod.build(ctx)
        except Exception:
            if mod.required:
                raise
            log.exception("prompt.assemble: module %r build failed", mod.id)
            continue
        if not text:
            continue
        blocks.append(Block(id=mod.id, layer=mod.layer, text=text.strip("\n")))
    return blocks


__all__ = ["assemble"]
