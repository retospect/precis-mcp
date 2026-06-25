"""Core types for the prompt assembler (ADR 0038).

A prompt is a list of **modules** grouped into two cache layers. Each
module yields zero or one **block** of text at assembly time; the
assembler orders them and an **adapter** packages the blocks for one
runner (system/user split, prefix ordering, …).

This module holds the value types only — no store access, no rendering
logic — so it imports cleanly from anywhere (``tables``, ``assembler``,
``adapters``, and the per-site module lists all depend on it).

Two module kinds (ADR 0038 §2):

* **Static** — body is a constant string (mechanics, the planner
  contract, a persona). Modelled as a :class:`Module` whose ``build``
  ignores the context and returns the literal.
* **Computed** — body is generated from live state and usually rendered
  as a TOON table (``doc_context``, ``tools``, ``kinds``, ``glossary``).
  Modelled as a :class:`Module` whose ``build`` queries the context.

The uniform ``build(ctx) -> str | None`` shape lets the assembler treat
both identically: it calls ``build``, drops the module when the result
is falsy, and tags the surviving text with the module's ``layer``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store


class Layer(StrEnum):
    """Cache-volatility layer — *is* the prompt-cache boundary (ADR 0038 §1).

    ``CACHED`` blocks are static across ticks (mechanics, tools, kinds,
    the skill menu, examples) → one long cache prefix. ``VARIABLE``
    blocks change per tick (the brief, doc_context, glossary, loaded
    skill bodies). The adapter maps these onto the runner's caching
    mechanism (system/user split + breakpoints for Claude; prefix-stable
    ordering for llama.cpp).
    """

    CACHED = "cached"
    VARIABLE = "variable"


class Profile(StrEnum):
    """Which module *set* a site emits (ADR 0038 §4).

    ``AGENT`` — autonomous, tools, multi-turn: persona + mechanics +
    tools + kinds + skill-menu + doc_context (+ glossary). The planner,
    the editor, the reviewers, the dreamer.

    ``HELPER`` — one-shot, no tools, structured output: persona + input +
    output-schema (+ examples, + one admonition). The summarizer, the
    chase judge, tex-fix.

    The profile is *which modules the assembler emits*, not two codebases;
    it maps onto the existing ``claude_agent`` vs ``claude_p`` choice.
    """

    AGENT = "agent"
    HELPER = "helper"


@dataclass
class AssemblyContext:
    """Per-assembly inputs + a memo scratchpad.

    Carries the live handles a builder needs (``store``, ``ref_id``,
    ``model``) plus an ``extras`` dict that builders and predicates use to
    share computed state within one assembly (e.g. the resolved anchor
    handle), so a value queried by a predicate isn't recomputed by the
    block it gates. ``store`` may be ``None`` for store-free assemblies
    (the cached-only system prompt is built that way in tests).
    """

    store: Store | None
    ref_id: int
    model: str
    profile: Profile = Profile.AGENT
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Module:
    """A unit that yields zero or one :class:`Block` at assembly time.

    ``build(ctx)`` returns the block text, or ``None`` / ``""`` to omit
    the module (the common "no anchor on this tick → no doc_context"
    case). ``layer`` tags the result. ``applies_when`` names an optional
    predicate (see :mod:`precis.utils.prompt.predicates`); when set and
    false, the module is skipped *without* calling ``build`` — the ADR §8
    "gate capability and data together" mechanism.
    """

    id: str
    layer: Layer
    build: Callable[[AssemblyContext], str | None]
    applies_when: str | None = None


@dataclass(frozen=True)
class Block:
    """A rendered prompt fragment: its text, cache layer, and provenance.

    ``id`` carries the originating module id so an assembled prompt stays
    inspectable (which block came from where) — the ``‹module · layer›``
    annotations in the validation shots are exactly this.
    """

    id: str
    layer: Layer
    text: str


__all__ = [
    "AssemblyContext",
    "Block",
    "Layer",
    "Module",
    "Profile",
]
