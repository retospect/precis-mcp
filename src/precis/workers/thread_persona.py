"""Thread-type → persona registry (ADR 0051 §2, slice A2).

A **thread type** (``write-document``, ``review``, ``dream``, ``triage``,
…) is fronted by a **persona** — a skill carrying ``flavor: persona`` that
states *how the thread works*. Per ADR 0051 §2 the persona is the **first
block** of the prompt and a **floor**: it never ages out, never demotes,
and its cache segment only changes when the thread type does.

This module is the registry that generalizes two one-offs in
``planner_prompt.py``:

- the fixed ``_PINNED_SKILL_ID = "precis-tasks-help"`` head of the cached
  layer (the *write-document* persona), and
- the ``has_review``-gated reviewer persona injected in the variable layer.

into one table ``thread_type → PersonaSpec``. It also carries, per thread
type, the extra verbs the persona may reach beyond the synthesis base
(ADR 0051 §6c) — recorded here now, *enforced* later (the curated verb
surface lands with the fisheye phase).

**Scope of slice A2 (behavior-preserving).** Today the cached layer is
built thread-type-invariant (one fleet-wide prefix, ``planner_prompt.
_build_system_prompt`` uses a bare context) and the reviewer persona rides
the *variable* layer. A2 introduces this registry and routes the cached
**floor** persona through :func:`persona_for`, but the default resolution
(``write-document`` → ``precis-tasks-help``) reproduces today's bytes
exactly, and the reviewer's variable-layer path is untouched. Forking a
distinct cache prefix per thread type — the persona-first cache island of
ADR 0051 §5 — is deferred to the render-loop slice (B), where the cached
layer is re-assembled per tick and can legibly select the floor persona
from the tick's ``thread_type``. Converting a review into a *separately
spawned* thread (§2: a review is not a mid-thread persona swap) is deferred
to the delegation slice (E).
"""

from __future__ import annotations

from dataclasses import dataclass

#: The persona a thread runs under when no more-specific type is resolved.
#: Its persona skill is the current pinned operational manual, so the
#: cached floor is byte-identical to the pre-A2 ``_m_pinned`` output.
DEFAULT_THREAD_TYPE = "write-document"


@dataclass(frozen=True)
class PersonaSpec:
    """The floor persona + declared surface for one thread type.

    ``persona_skill_id`` — the ``flavor: persona`` (or, for
    ``write-document``, the operational-manual) skill loaded verbatim as the
    first, decay-exempt block.

    ``known_skills`` — skills this thread type is known to want, pre-loaded
    into the floor alongside the persona (ADR 0051 §2). Empty today; the
    write-document floor already carries the operational manual as its
    persona.

    ``extension_verbs`` — verbs this persona may reach beyond the synthesis
    base surface (ADR 0051 §6c: ``+link``/``+tag``/``+supersede``/
    ``+flag-claim``/…). Recorded now for the registry's completeness;
    enforcement arrives with the curated 7-verb surface (phase C/E). Until
    then the full verb kit stays exposed, so this is latent metadata and
    changes no rendered bytes.
    """

    persona_skill_id: str
    known_skills: tuple[str, ...] = ()
    extension_verbs: tuple[str, ...] = ()


#: The registry. Only thread types whose persona skill actually ships are
#: listed; add a row when its persona is authored (do not point at a
#: missing skill — the loader would fall back to an error stub).
#:
#: - ``write-document`` — the synthesis base; persona = the operational
#:   manual ``precis-tasks-help`` (identical to the pre-A2 pinned skill).
#: - ``review`` — the draft-reviewer persona (today injected in the variable
#:   layer via ``has_review``; registered here for the §2 floor promotion
#:   that lands with review-as-a-spawned-thread, phase E).
THREAD_PERSONAS: dict[str, PersonaSpec] = {
    "write-document": PersonaSpec(persona_skill_id="precis-tasks-help"),
    "review": PersonaSpec(
        persona_skill_id="precis-draft-reviewer",
        extension_verbs=("flag-claim", "request-evidence"),
    ),
}


def persona_for(thread_type: str | None) -> PersonaSpec:
    """The :class:`PersonaSpec` for ``thread_type``.

    Falls back to :data:`DEFAULT_THREAD_TYPE` for ``None`` or any
    unregistered type, so an unknown/absent thread type degrades to the
    write-document floor rather than erroring."""
    if thread_type and thread_type in THREAD_PERSONAS:
        return THREAD_PERSONAS[thread_type]
    return THREAD_PERSONAS[DEFAULT_THREAD_TYPE]


def resolve_thread_type(*, has_review: bool = False, is_dream: bool = False) -> str:
    """Classify a tick into a thread type from coarse signals (ADR 0051 §2).

    Pure and signal-based so it is unit-testable and decoupled from the
    store: the caller passes the signals it already computed (a
    ``meta.review`` present → ``has_review``; the dream loop → ``is_dream``).
    A review tick is a ``review`` thread; the dream loop is ``dream``;
    everything else is the default ``write-document``.

    This is the seam the render-loop (phase B) will call to stamp
    ``AssemblyContext.extras['thread_type']`` before the cached floor is
    assembled. In A2 nothing wires it into the live context yet (the cached
    layer stays thread-type-invariant), so it changes no rendered bytes."""
    if has_review:
        return "review"
    if is_dream:
        return "dream"
    return DEFAULT_THREAD_TYPE
