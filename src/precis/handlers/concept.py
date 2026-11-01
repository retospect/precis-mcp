"""ConceptHandler — nodes in the learner's personal knowledge graph (migration
0062, reading-prep loop).

A numeric-id ref (like ``memory``/``anki``): ``refs.title`` = the concept name;
``refs.meta`` carries the continuous **mastery** field + derived ``state`` +
canonical ``definition``/``aliases``. On create it emits the reused
``card_combined`` chunk (ord=-1) built from name+definition so the concept
**is a vector** in the corpus manifold (frontier distance / routing get this for
free). Objectives are concepts, not todos (supersedes decision 7). Full design:
docs/backlog/reading-prep-loop.md.

``put`` text is ``"<name> — <definition>"`` (em-dash / newline separated). The
**promotion pass** (slice 2c) writes richer nodes — aliases, provenance links,
graph edges — directly via the store, reusing the same
``precis.reading.concepts`` helpers so manual and promoted nodes are identical.
Bodies are immutable (the numeric-ref contract): delete+put to reword. The
mastery field mutates out-of-band (the mastery pass), never via ``put``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.reading.concepts import (
    concept_card_text,
    initial_concept_meta,
    split_name_def,
)
from precis.response import Response
from precis.store import Tag
from precis.utils.edit_resolve import normalize_dry_run


class ConceptHandler(NumericRefHandler):
    #: Agent-authored assertions (graph-node-trust-state slice 1).
    trust_origin: ClassVar[str | None] = "generated"
    spec: ClassVar[KindSpec] = KindSpec(
        kind="concept",
        title="Concept",
        description=(
            "A node in your personal knowledge graph: a term/idea with a "
            "continuous mastery field, derived state, an embeddable definition, "
            "and typed edges (has-prerequisite / analogy-of / contrasts-with) to "
            "other concepts. Body is '<name> — <definition>'. Objectives are "
            "concepts. See docs/backlog/reading-prep-loop.md."
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

    def put(
        self,
        *,
        text: str | None = None,
        tags: list[str] | None = None,
        auto_refresh_days: int | None = None,
        dry_run: bool | str = False,
        **_kw: Any,
    ) -> Response:
        """Create a concept; ``dry_run=True`` writes nothing and lists the
        nearest concepts to its card text (name — definition) by embedding
        distance (:mod:`precis.handlers._put_preview`)."""
        if normalize_dry_run(dry_run) is None:
            return super().put(
                text=text, tags=tags, auto_refresh_days=auto_refresh_days, **_kw
            )
        from precis.handlers import _put_preview

        self._reject_mutating_put(
            id=_kw.get("id"),
            mode=_kw.get("mode"),
            untags=_kw.get("untags"),
            unlink=_kw.get("unlink"),
            rel=_kw.get("rel"),
            link=_kw.get("link"),
        )
        if text is None or not text.strip():
            raise BadInput(
                "creating a concept requires text='<name> — <definition>'",
                next="put(kind='concept', text='overpotential — the extra "
                "voltage beyond equilibrium', dry_run=True)",
            )
        parsed = [Tag.parse_strict(t, kind=self.kind) for t in tags or []]
        space = next(
            (f"SPACE:{t.value}" for t in reversed(parsed) if t.prefix == "SPACE"),
            None,
        )
        dups = _put_preview.nearest_duplicates(
            self, self._card_combined_text(text), kind=self.kind, space=space
        )
        return _put_preview.render_preview(
            self.kind,
            dups=dups,
            space=space,
            next_rows=_put_preview.dup_read_rows(self.kind, dups),
        )

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
