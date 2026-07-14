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
from precis.response import Response

#: A cloze deletion: ``{{c1::answer}}`` or ``{{c1::answer::hint}}``. The answer
#: capture is non-greedy and the ``::hint`` tail optional, so the stripped form
#: keeps the answer and drops the hint.
_CLOZE_RE = re.compile(r"\{\{c\d+::(.+?)(?:::.+?)?\}\}", re.DOTALL)

#: A lone ``---`` line separating the cloze body from an optional Back Extra.
_EXTRA_SEP_RE = re.compile(r"\n[ \t]*---[ \t]*\n")

_DECK = "Precis"
_NOTETYPE = "Cloze"

#: A ``deck-<topic>`` tag files the card under the ``Precis::<topic>`` sub-deck
#: (Anki auto-creates it on sync). No deck tag → the base ``Precis`` deck. The
#: topic is a slug; keeps model-authored cards under one namespace, away from
#: hand-made decks. Only the first deck- tag wins.
_DECK_TAG_RE = re.compile(r"^deck-([a-z0-9][a-z0-9-]*)$", re.IGNORECASE)


def _deck_from_tags(tags: list[str]) -> str:
    for t in tags:
        m = _DECK_TAG_RE.match(t.strip())
        if m:
            return f"{_DECK}::{m.group(1).lower()}"
    return _DECK


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

    def _initial_meta(self, text: str, tags: list[str]) -> dict[str, Any]:
        cloze_text, extra = _split_extra(text)
        fields: dict[str, str] = {"Text": cloze_text}
        if extra:
            fields["Back Extra"] = extra
        return {
            "notetype": _NOTETYPE,
            "deck": _deck_from_tags(tags),
            "fields": fields,
        }

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

    # ── list views ─────────────────────────────────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        return ("recent", "leeches")

    def _list_view(self, view: str) -> Response | None:
        if view == "leeches":
            return self._render_leeches()
        return super()._list_view(view)

    def _render_leeches(self, limit: int = 40) -> Response:
        """Cards with bad recall — high lapses or collapsed ease — across the
        whole synced collection (authored + projected). The retention loop's
        entry point: fix the cloze (tag it `precis-fix` in Anki) or study more.

        Stats come from `meta.anki_stats`, refreshed each sync. A card is a
        *leech* at ≥4 lapses or ease ≤ 2.0 (dropped below Anki's 250% default)."""
        sql = """
            SELECT ref_id, title,
                   (meta->'anki_stats'->>'lapses_total')::int   AS lapses,
                   (meta->'anki_stats'->>'ease_min')::float      AS ease,
                   (meta->'anki_stats'->>'reps_total')::int      AS reps
            FROM refs
            WHERE kind = 'anki' AND deleted_at IS NULL
              AND meta ? 'anki_stats'
              AND ( (meta->'anki_stats'->>'lapses_total')::int >= 4
                 OR (meta->'anki_stats'->>'ease_min')::float <= 2.0 )
            ORDER BY (meta->'anki_stats'->>'lapses_total')::int DESC NULLS LAST,
                     (meta->'anki_stats'->>'ease_min')::float ASC NULLS LAST
            LIMIT %s
        """
        with self.store.pool.connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        if not rows:
            return Response(
                body=(
                    "no bad-recall anki cards yet — needs review history from a "
                    "sync (get(kind='anki', id='/recent') to browse instead)"
                )
            )
        lines = [
            f"# {len(rows)} bad-recall card(s) — fix the cloze "
            "(tag `precis-fix` in Anki) or study more"
        ]
        for ref_id, title, lapses, ease, reps in rows:
            preview = (title[:70] + "…") if len(title) > 70 else title
            lines.append(
                f"  ak{ref_id:<7} lapses={lapses or 0} ease={ease if ease is not None else '?'} "
                f"reps={reps or 0}  {preview}"
            )
        return Response(body="\n".join(lines))
