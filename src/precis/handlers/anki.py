"""AnkiHandler — spaced-repetition **cloze** cards (migration 0059).

Numeric-id ref kind addressed as ``anki``. Supersedes the thin ``flashcard``
kind: Anki owns scheduling, so there is no SM-2 state here. The body is cloze
markup (``{{c1::…}}``); ``ref.meta`` carries a **generic** Anki note shape so a
future non-cloze notetype needs no migration::

    meta = {
      "notetype": "Cloze",
      "deck":     "Precis",
      "fields":   {"Text": "<cloze markup>", "Back Extra": "<terse note>"},
      "anki":      {...},        # sync-state, written by slice 2 (AnkiWeb sync)
      "anki_stats":{...},        # decay signal, read back by slice 2
    }

Slice 1 (this handler) is the corpus half — author / store / search a cloze
card; there is **no** AnkiWeb dependency yet. On create it emits the shared
``card_combined`` chunk built from the *markup-stripped* text (+ Back Extra) so
the embed + chunk_keywords workers index it and ``search`` finds it. Full
design: ``docs/design/anki-integration.md``.

Authoring conventions (taught by ``precis-anki-help``):

- The body is one cloze sentence with at least one ``{{cN::hidden}}`` deletion
  (``{{c1::x}} {{c2::y}}`` → two cards; ``{{c1::x::hint}}`` → a hint).
- **Optional terse "Back Extra"**: a short answer-side note — a source, a
  mnemonic, a gotcha. Supply it after a lone ``---`` line at the end of the
  body. Used sparingly ("terse or omit").
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec

#: A cloze deletion: ``{{c1::answer}}`` or ``{{c1::answer::hint}}``. The answer
#: capture is non-greedy and the ``::hint`` tail optional, so the stripped form
#: keeps the answer and drops the hint.
_CLOZE_RE = re.compile(r"\{\{c\d+::(.+?)(?:::.+?)?\}\}", re.DOTALL)

#: A lone ``---`` line separating the cloze body from an optional Back Extra.
_EXTRA_SEP_RE = re.compile(r"\n[ \t]*---[ \t]*\n")

_DECK = "Precis"
_NOTETYPE = "Cloze"


def _split_extra(text: str) -> tuple[str, str]:
    """Split a body into ``(cloze_text, back_extra)`` on the first lone ``---``
    line. No separator → the whole body is the cloze text, extra is ``""``."""
    parts = _EXTRA_SEP_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        return parts[0].rstrip(), parts[1].strip()
    return text.rstrip(), ""


def _strip_cloze(text: str) -> str:
    """Replace every ``{{cN::answer::hint}}`` with just ``answer`` — the natural
    sentence, for the embedded/searchable ``card_combined`` chunk."""
    return _CLOZE_RE.sub(r"\1", text)


class AnkiHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="anki",
        title="Anki card",
        description=(
            "A spaced-repetition cloze card ({{c1::…}}) that lives in the "
            "corpus and syncs to AnkiWeb. Body is cloze markup; meta carries "
            "the generic Anki note shape. Anki owns scheduling — no SM-2 here."
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

    kind: ClassVar[str] = "anki"
    sense: ClassVar[str] = "anki card"

    #: The body is the card, so emit a `card_combined` search chunk (ord=-1) —
    #: same as memory. `_card_combined_text` strips the cloze markup.
    emits_card: ClassVar[bool] = True

    # ── create-time hooks (see NumericRefHandler) ──────────────────────

    def _validate_cloze(self, text: str) -> str:
        """Guard: a cloze card needs ≥1 ``{{cN::…}}`` deletion. Returns the
        cloze body (Back Extra split off)."""
        cloze_text, _extra = _split_extra(text)
        if not _CLOZE_RE.search(cloze_text):
            raise BadInput(
                "an anki card needs at least one cloze deletion, e.g. "
                "{{c1::the hidden answer}}",
                next=(
                    "put(kind='anki', text='Paris is the {{c1::capital}} of "
                    "France.') — see get(kind='skill', id='precis-anki-help')"
                ),
            )
        return cloze_text

    def _initial_meta(self, text: str) -> dict[str, Any]:
        cloze_text, extra = _split_extra(text)
        fields: dict[str, str] = {"Text": cloze_text}
        if extra:
            fields["Back Extra"] = extra
        return {"notetype": _NOTETYPE, "deck": _DECK, "fields": fields}

    def _card_combined_text(self, text: str) -> str:
        cloze_text, extra = _split_extra(text)
        stripped = _strip_cloze(cloze_text)
        return f"{stripped}\n\n{extra}".rstrip() if extra else stripped

    def _create(self, *, text: str | None, **kw: Any):  # type: ignore[override]
        # Reject a non-cloze body up front (before the insert tx) so a bad
        # card writes nothing — mirrors the atomic-create contract.
        if text is not None and text.strip():
            self._validate_cloze(text)
        return super()._create(text=text, **kw)
