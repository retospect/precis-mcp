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
the embed + chunk_keywords workers index it and ``search`` finds it. The
AnkiWeb sync half lives in :mod:`precis.anki`.

Authoring conventions (taught by ``precis-anki-help``):

- The body is one cloze sentence with at least one ``{{cN::hidden}}`` deletion
  (``{{c1::x}} {{c2::y}}`` → two cards; ``{{c1::x::hint}}`` → a hint).
- **Optional terse "Back Extra"**: a short answer-side note — a source, a
  mnemonic, a gotcha. Supply it after a lone ``---`` line at the end of the
  body. Terse; omitted only when the card carries no symbol, variable or
  acronym, because each one is defined there (``precis-cloze`` rule 7).
- **Put-time lint (non-fatal)**: when the words just before a cloze already
  announce its hint (*"The term {{c1::…::term}}"*), the ack carries a
  cut-the-lead-in note pointing at ``precis-cloze`` rule 4.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import replace
from typing import Any, ClassVar
from uuid import uuid4

from precis.anki.cloze import (
    acronym_hint_leaks,
    cloze_hints,
    has_cloze,
    space_adjacent_braces,
    strip_cloze,
)
from precis.anki.media import fetch_image
from precis.anki.work_tracking import (
    has_work_source_tag,
    is_work_item_text,
    is_work_title,
)
from precis.errors import BadInput
from precis.format import render_agent_table
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Ref, Tag
from precis.utils import handle_registry
from precis.utils.link_target import parse_link_target

#: A lone ``---`` line separating the cloze body from an optional Back Extra.
_EXTRA_SEP_RE = re.compile(r"\n[ \t]*---[ \t]*\n")

_DECK = "Precis"
_NOTETYPE = "Cloze"

#: A ``deck-<topic>`` tag files the card under the ``Precis::<topic>`` sub-deck
#: (Anki auto-creates it on sync). No deck tag → the base ``Precis`` deck. The
#: topic is a slug; keeps model-authored cards under one namespace, away from
#: hand-made decks. With several deck- tags the *filed* deck wins while its tag
#: remains (at create, request order picks it — ``tags_for`` order would not be
#: stable); remove the tag to re-file onto a surviving one.
_DECK_TAG_RE = re.compile(r"^deck-([a-z0-9][a-z0-9-]*)$", re.IGNORECASE)


def _deck_from_tags(tags: list[str]) -> str:
    for t in tags:
        m = _DECK_TAG_RE.match(t.strip())
        if m:
            return f"{_DECK}::{m.group(1).lower()}"
    return _DECK


#: How many words before a cloze count as its lead-in (gr743086).
_HINT_LEAD_WORDS = 3

#: Lead-in words that announce what a hint already names — checked alongside
#: the hint itself in the words before a cloze at put time. Keyed by hint;
#: values are the lead-in words that name the same thing. Tiny on purpose:
#: it is a nudge, not a parser of meaning.
_HINT_LEAD_SYNONYMS = {
    "term": {"word", "name"},
    "kanji": {"character", "letter", "glyph"},
    "pronunciation": {"pronounced"},
    "meaning": {"means"},
}


def _hint_lead_dupes(text: str) -> list[str]:
    """One message per cloze whose hint is already announced by the ≤3 words
    before it (gr743086) — *"The term {{c1::available::term}} …"* wastes the
    lead-in on every sibling card and on the revealed sentence, since the
    hint shows on its own card anyway. Lint only: the put still succeeds."""
    out: list[str] = []
    for start, hint in cloze_hints(text):
        hint_l = hint.strip().lower()
        if not hint_l:
            continue
        lead = strip_cloze(text[:start])  # earlier clozes' hints aren't prose
        words = re.findall(r"[A-Za-zÀ-ÿ]+", lead)[-_HINT_LEAD_WORDS:]
        named = {hint_l, *_HINT_LEAD_SYNONYMS.get(hint_l, set())}
        hit = next((w for w in words if w.lower() in named), None)
        if hit:
            out.append(
                f"hint '{hint}' is announced by the preceding word "
                f"'{hit}' — cut the lead-in scaffolding "
                "(get(kind='skill', id='precis-cloze'), rule 4)"
            )
    return out


def _split_extra(text: str) -> tuple[str, str]:
    """Split a body into ``(cloze_text, back_extra)`` on the first lone ``---``
    line. No separator → the whole body is the cloze text, extra is ``""``."""
    parts = _EXTRA_SEP_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        return parts[0].rstrip(), parts[1].strip()
    return text.rstrip(), ""


def _strip_cloze(text: str) -> str:
    """Replace every ``{{cN::answer::hint}}`` with just ``answer`` — the natural
    sentence, for the embedded/searchable ``card_combined`` chunk. Brace-aware
    (:mod:`precis.anki.cloze`), so nested LaTeX braces stay in the answer."""
    return strip_cloze(text)


def _space_cloze_braces(text: str) -> tuple[str, int]:
    """Normalise the cloze part of a body (Back Extra untouched) so Anki's
    first-``}}`` scan finds the real end of each cloze. Returns
    ``(text, spaces_inserted)``; idempotent."""
    m = _EXTRA_SEP_RE.search(text)
    head, tail = (text, "") if m is None else (text[: m.start()], text[m.start() :])
    head, n = space_adjacent_braces(head)
    return head + tail, n


class AnkiHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="anki",
        title="Anki card",
        description=(
            "A spaced-repetition cloze card ({{c1::…}}) that lives in the "
            "corpus and syncs to AnkiWeb. Body is cloze markup; meta carries "
            "the generic Anki note shape. Before put, search Anki for the "
            "concept and fact to avoid duplicates. Use factual sources and "
            "refuse repo work tracking, including gap and backlog lists."
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
    search_card_kinds: ClassVar[tuple[str, ...]] = ("card_combined",)
    accepts_put_meta: ClassVar[bool] = True

    def put(
        self,
        *,
        image: str | None = None,
        auto_refresh_days: int | None = None,
        **kw: Any,
    ) -> Response:
        # Only the typed attachment option feeds the numeric create hook.
        kw["meta"] = {"image": image} if image is not None else None
        return super().put(auto_refresh_days=auto_refresh_days, **kw)

    def delete(
        self,
        *,
        id: str | int | None = None,
        reason: str | None = None,
        superseded_by: str | int | None = None,
        dry_run: bool = False,
        **_kw: Any,
    ) -> Response:
        """Soft-delete a card; ``superseded_by=`` names the replacing card.

        The successor (``ak<N>`` or a bare id) is validated up front —
        on ``dry_run`` too, so the preview refuses what the real call
        would — and recorded as ``meta.superseded_by`` inside the delete
        transaction, so a duplicate fold keeps its trail.
        """
        succ = None
        if superseded_by is not None:
            if id is None:
                raise BadInput(
                    "delete requires id=",
                    next=f"delete(kind={self.kind!r}, id=N)",
                )
            ref_id = self._coerce_id(id)
            self._resolve_ref_for_delete(ref_id)
            raw = str(superseded_by).strip()
            parsed = handle_registry.parse(handle_registry.normalize(raw))
            if parsed is not None:
                succ_kind, is_chunk, succ_id = parsed
                if succ_kind != self.kind or is_chunk:
                    raise BadInput(
                        "superseded_by must name an anki card",
                        next="superseded_by='ak<N>'",
                    )
                succ = succ_id
            else:
                succ = self._coerce_id(raw)
            if succ == ref_id:
                raise BadInput(
                    "superseded_by must name a different card",
                    next="superseded_by='ak<other card>'",
                )
            self._resolve_ref_for_delete(succ)
        if succ is None or dry_run:
            return self._delete(id, reason=reason, dry_run=dry_run)
        # ``store.atomic()`` pins the connection, so the stamp, the liveness
        # recheck and the retire tx inside ``_delete`` commit or roll back
        # as one — a failed delete can't leave a lone ``superseded_by``
        # tombstone. Both rows lock in ref_id order first: ``delete A
        # superseded_by B`` racing ``delete B superseded_by A`` serialises
        # on the lower row instead of both passing an unlocked check (or
        # deadlocking), so the loser re-reads the winner's committed retire
        # and refuses instead of minting a redirect cycle with no live
        # survivor (review td781074). NO KEY UPDATE, matching ``retire_ref``
        # — it conflicts with a concurrent retire/stamp yet stays compatible
        # with the FOR KEY SHARE a ref_tags FK check holds on the card
        # (``_after_tag_mutation`` explains the upgrade deadlock).
        with self.store.atomic() as conn:
            for rid in sorted((ref_id, succ)):
                conn.execute(
                    "SELECT 1 FROM refs WHERE ref_id = %s FOR NO KEY UPDATE",
                    (rid,),
                )
            self._resolve_ref_for_delete(ref_id)
            self._resolve_ref_for_delete(succ)
            self.store.stamp_ref_meta(
                ref_id,
                {"superseded_by": handle_registry.format_handle(self.kind, succ)},
                conn=conn,
            )
            return self._delete(id, reason=reason, dry_run=False)

    def _merge_put_meta(
        self, text: str, initial_meta: dict[str, Any], put_meta: dict[str, Any]
    ) -> dict[str, Any]:
        return {**initial_meta, "anki_image": fetch_image(put_meta["image"])}

    #: A deck move can re-file a set of cards in one tag call.
    tag_batch_ids: ClassVar[bool] = True

    @staticmethod
    def _without_image_data(ref: Ref) -> Ref:
        meta = ref.meta or {}
        image = meta.get("anki_image")
        if not image:
            return ref
        data = image["data"]
        return replace(
            ref,
            meta={
                **meta,
                "anki_image": {
                    "filename": image["filename"],
                    "size_bytes": len(data) * 3 // 4
                    - (len(data) - len(data.rstrip("="))),
                },
            },
        )

    def _render_meta_view(self, ref: Ref) -> str:
        return super()._render_meta_view(self._without_image_data(ref))

    def _render_raw(
        self, ref: Ref, tags: list[Tag], *, meta_key: str | None = None
    ) -> str:
        return super()._render_raw(
            self._without_image_data(ref), tags, meta_key=meta_key
        )

    # ── create-time hooks (see NumericRefHandler) ──────────────────────

    def _validate_cloze(self, text: str) -> str:
        """Guard: a cloze card needs ≥1 ``{{cN::…}}`` deletion. Returns the
        cloze body (Back Extra split off)."""
        cloze_text, _extra = _split_extra(text)
        if not has_cloze(cloze_text):
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

    def _after_tag_mutation(
        self,
        ref_id: int,
        added: list[Tag],
        removed: list[Tag],
        *,
        conn: Any,
    ) -> None:
        """Keep ``meta.deck`` following the ``deck-<topic>`` tag (gr735742).

        ``_initial_meta`` stamps the deck only at put-create, so a retag used
        to leave the card syncing to a deck its tags no longer name. Runs
        inside the tag tx: any ``deck-`` add or remove recomputes the deck
        from the ref's whole post-edit tag set and patches meta only when it
        differs.

        Two order hazards shape the recompute:

        * ``FOR NO KEY UPDATE`` serialises the recalculation per ref — a
          concurrent tag tx on the same card blocks here until this one
          commits, then reads a fresh snapshot that sees the earlier tx's
          committed tag writes. Without it a ``remove deck-a`` racing an
          ``add deck-b`` can commit last while having computed over a
          snapshot where deck-b was absent, leaving the tag set ``{deck-b}``
          with ``meta.deck = Precis``. NO KEY UPDATE, not UPDATE: the FK
          check on this tx's own ``ref_tags`` inserts holds FOR KEY SHARE on
          the refs row, and FOR UPDATE conflicts with that — two tag txs
          would deadlock on the upgrade. NO KEY UPDATE is compatible with
          KEY SHARE yet still serialises against other meta writers.
        * The filed deck is *preserved* while its backing tag remains.
          ``tags_for`` orders by ``(ref_tags.created_at, tags.tag_id)`` and
          every tag one tx adds shares the tx-start timestamp, so the order
          degenerates to the global tag id — first-ever-use order, not the
          request order ``_initial_meta`` won by. Recomputing blind could
          re-file a card onto a deck tag that merely sorts earlier, even on
          a no-op retag.

        A read-only foreign projection's deck mirrors the real Anki deck
        (``project_cards``); corpus tags can't move a note precis doesn't
        own, so leave those alone."""
        if not any(_DECK_TAG_RE.match(str(t)) for t in (*added, *removed)):
            return
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s FOR NO KEY UPDATE",
            (ref_id,),
        ).fetchone()
        meta = dict(row[0]) if row and row[0] else {}
        if meta.get("source") == "anki-foreign" or meta.get("readonly"):
            return
        prefix = f"{_DECK}::"
        topics = [
            m.group(1).lower()
            for t in self.store.tags_for(ref_id, conn=conn)
            if (m := _DECK_TAG_RE.match(str(t).strip()))
        ]
        current = meta.get("deck")
        if (
            isinstance(current, str)
            and current.startswith(prefix)
            and current[len(prefix) :] in topics
        ):
            return  # the filed deck's tag survives — keep it
        deck = f"{prefix}{topics[0]}" if topics else _DECK
        if current != deck:
            self.store.update_ref(
                ref_id,
                meta_patch={"deck": deck, "anki_deck_revision": uuid4().hex},
                conn=conn,
            )

    def _guard_work_tracking(
        self, cloze_text: str, tags: list[str] | None, link: str | None
    ) -> None:
        # Check the revealed sentence so a cloze around the work-item wording
        # cannot hide it.
        is_work_tracking = is_work_item_text(
            _strip_cloze(cloze_text)
        ) or has_work_source_tag(tags or [])
        if link is not None and not is_work_tracking:
            target = parse_link_target(link, store=self.store)
            is_work_tracking = target.kind in {"todo", "gripe"}
            if target.kind == "memory":
                ref = self.store.get_ref(kind="memory", id=target.ref_id)
                is_work_tracking = bool(
                    has_work_source_tag(self.store.tags_for(target.ref_id))
                    or (ref is not None and is_work_title(ref.title))
                )
        if is_work_tracking:
            raise BadInput(
                "Anki cards need a durable fact. This card or its source "
                "tracks project work.",
                next=(
                    "Use a factual source for any underlying concept, write "
                    "the concept in isolation, and run search(kind='anki', "
                    "q='<concept and fact>') before put."
                ),
            )

    def _create(
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        **kw: Any,
    ) -> Response:
        # Reject a non-cloze body up front (before the insert tx) so a bad
        # card writes nothing — mirrors the atomic-create contract.
        spaced = 0
        leaks: list[str] = []
        dupes: list[str] = []
        if text is not None and text.strip():
            cloze_text = self._validate_cloze(text)
            self._guard_work_tracking(cloze_text, tags, link)
            leaks = acronym_hint_leaks(cloze_text)
            text, spaced = _space_cloze_braces(text)
            dupes = _hint_lead_dupes(text)
        ack = super()._create(text=text, tags=tags, link=link, **kw)
        body = ack.body
        if spaced:
            body += (
                f"\nspaced {spaced} `}}}}` inside cloze answers so Anki does not "
                "cut the answer."
            )
        if leaks:
            body += (
                f"\nWarning ({', '.join(leaks)}): the hint's initials spell the "
                "answer. Show the short form and cloze its expansion or meaning."
            )
        if dupes:
            body += "\n" + "\n".join(dupes)
        return dataclasses.replace(ack, body=body) if body != ack.body else ack

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
            WHERE kind = 'anki' AND retired_at IS NULL
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
        lines.append(
            render_agent_table(
                [
                    {
                        "id": f"ak{ref_id}",
                        "lapses": lapses or 0,
                        "ease": ease if ease is not None else "?",
                        "reps": reps or 0,
                        "preview": (title[:70] + "…") if len(title) > 70 else title,
                    }
                    for ref_id, title, lapses, ease, reps in rows
                ],
                schema=["id", "lapses", "ease", "reps", "preview"],
            )
        )
        return Response(body="\n".join(lines))
