"""ConceptHandler — nodes in the learner's personal knowledge graph (migration
0062, reading-prep loop).

A numeric-id ref (like ``memory``/``anki``): ``refs.title`` = the concept name;
``refs.meta`` carries the continuous **mastery** field + derived ``state`` +
canonical ``definition``/``aliases``. On create it emits the reused
``card_combined`` chunk (ord=-1) built from name+definition so the concept
**is a vector** in the corpus manifold (frontier distance / routing get this for
free). Objectives are concepts, not todos (supersedes decision 7). Full design:
docs/design/reading-prep-loop.md.

``put`` text is ``"<name> — <definition>"`` (em-dash / newline separated). The
**promotion pass** (slice 2c) writes richer nodes — aliases, provenance links,
graph edges — directly via the store, reusing the same
``precis.reading.concepts`` helpers so manual and promoted nodes are identical.
Bodies are immutable (the numeric-ref contract): delete+put to reword. The
mastery field mutates out-of-band (the mastery pass), never via ``put``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.reading.concepts import (
    concept_card_text,
    initial_concept_meta,
    split_name_def,
)


class ConceptHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="concept",
        title="Concept",
        description=(
            "A node in your personal knowledge graph: a term/idea with a "
            "continuous mastery field, derived state, an embeddable definition, "
            "and typed edges (has-prerequisite / analogy-of / contrasts-with) to "
            "other concepts. Body is '<name> — <definition>'. Objectives are "
            "concepts. See docs/design/reading-prep-loop.md."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "concept"
    sense: ClassVar[str] = "concept"

    #: Emit an embeddable `card_combined` (name + definition) so the concept is
    #: a vector — the substrate for frontier distance + review routing.
    emits_card: ClassVar[bool] = True

    def _initial_meta(self, text: str, tags: list[str]) -> dict[str, Any]:
        name, definition = split_name_def(text)
        return initial_concept_meta(name, definition)

    def _card_combined_text(self, text: str) -> str:
        name, definition = split_name_def(text)
        return concept_card_text(name, definition)

    def _render_one(self, ref: Any, tags: Any) -> str:
        meta = ref.meta or {}
        name = meta.get("name") or ref.title
        out = [f"# concept {ref.id}: {name}"]
        if meta.get("definition"):
            out += ["", meta["definition"]]
        if meta.get("aliases"):
            out += ["", "aka: " + ", ".join(meta["aliases"])]
        out += [
            "",
            f"mastery: {meta.get('mastery', 0.0):.2f}  state: {meta.get('state', '?')}",
        ]
        if tags:
            out += ["", "tags: " + " ".join(str(t) for t in tags)]
        return "\n".join(out)


__all__ = ["ConceptHandler"]
