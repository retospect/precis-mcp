"""Contract tests for :class:`precis.handlers.anki.AnkiHandler`.

Slice 1 is the corpus half: put-create stores cloze markup in ``refs.title``,
the generic Anki note shape in ``ref.meta``, and emits a ``card_combined``
chunk built from the *markup-stripped* text so the embed + chunk_keywords
workers index the natural sentence (a query for "capital" matches a card that
hides ``{{c1::capital}}``). No AnkiWeb dependency yet.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Queue

import pytest
from psycopg import Connection

from precis.dispatch import Hub
from precis.errors import BadInput, Gone, NotFound
from precis.handlers.anki import AnkiHandler, _split_extra, _strip_cloze
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag


def _search(pattern: str, text: str) -> re.Match[str]:
    """``re.search`` narrowed for tests — asserts the pattern actually hit."""
    m = re.search(pattern, text)
    assert m is not None, f"pattern {pattern!r} not found in {text!r}"
    return m


def _make_handler(store):
    return AnkiHandler(hub=Hub(store=store))


class TestClozeHelpers:
    def test_strip_cloze_drops_markup_keeps_answer(self) -> None:
        assert (
            _strip_cloze("Paris is the {{c1::capital}} of France.")
            == "Paris is the capital of France."
        )

    def test_strip_cloze_drops_hint(self) -> None:
        assert _strip_cloze("The {{c1::heart::organ}} pumps blood.") == (
            "The heart pumps blood."
        )

    def test_strip_cloze_multiple_indices(self) -> None:
        assert _strip_cloze("The {{c1::heart}} pumps {{c2::blood}}.") == (
            "The heart pumps blood."
        )

    def test_split_extra_none(self) -> None:
        assert _split_extra("Just a {{c1::card}}.") == ("Just a {{c1::card}}.", "")

    def test_split_extra_present(self) -> None:
        body = "The {{c1::Krebs}} cycle.\n---\naka TCA cycle"
        assert _split_extra(body) == ("The {{c1::Krebs}} cycle.", "aka TCA cycle")


class TestAnkiCard:
    def test_search_retries_card_text_as_any_words(self, store: Store) -> None:
        handler = _make_handler(store)
        created = handler.put(
            text="A {{c1::foundation MLIP}} models interatomic forces.\n---\n"
            "Transferable across elements.",
            tags=["aixmat-2026"],
        )
        ref_id = int(_search(r"id=(\d+)", created.body).group(1))

        result = handler.search(
            q="aixmat transferable foundation potential",
            tags=["aixmat-2026"],
            mode="lexical",
            view="ids",
        )

        assert result.result_count == 1
        assert f"ak{ref_id}" in result.body.splitlines()
        assert "q ran as ANY" in result.body

    def test_put_image_through_dispatch(
        self,
        runtime_with_store: PrecisRuntime,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from precis.anki.notes import spec_from_ref

        url = "https://example.org/speaker.png"
        attachment = {"filename": "precis-portrait.png", "data": "aW1hZ2U="}

        def fetch_image(source: str) -> dict[str, str]:
            assert source == url
            return attachment

        monkeypatch.setattr("precis.handlers.anki.fetch_image", fetch_image)
        body = "Ask {{c1::Avery Chen}} (Example Lab) about replication."
        result = runtime_with_store.dispatch(
            "put", {"kind": "anki", "text": body, "__extras__": {"image": url}}
        )
        ref = store.get_ref(kind="anki", id=int(_search(r"id=(\d+)", result).group(1)))
        assert ref is not None
        spec = spec_from_ref(ref)
        assert spec is not None
        assert spec.fields == {"Text": body}
        assert spec.image == ("precis-portrait.png", b"image")
        for view, extras in (
            ("meta", {}),
            ("raw", {}),
            ("raw", {"meta_key": "anki_image"}),
        ):
            rendered = runtime_with_store.dispatch(
                "get",
                {"kind": "anki", "id": ref.id, "view": view, "__extras__": extras},
            )
            assert attachment["data"] not in rendered
            assert attachment["filename"] in rendered
            assert re.search(r'"size_bytes":\s*5', rendered)

    def test_put_emits_stripped_card_combined(self, store) -> None:
        h = _make_handler(store)
        resp = h.put(text="Paris is the {{c1::capital}} of France.")
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT chunk_kind, text FROM chunks WHERE ref_id = %s AND ord = -1",
                (ref_id,),
            ).fetchone()
        assert row is not None, "expected a card_combined chunk at ord=-1"
        assert row[0] == "card_combined"
        # The embedded/searchable card is the natural sentence — markup gone.
        assert row[1] == "Paris is the capital of France."

    def test_meta_carries_generic_note_shape(self, store) -> None:
        h = _make_handler(store)
        cloze = "The mitochondrion is the {{c1::powerhouse}} of the cell."
        resp = h.put(text=cloze)
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))

        ref = store.get_ref(kind="anki", id=ref_id)
        assert ref is not None
        assert ref.title == cloze  # raw markup preserved in the body
        assert ref.meta["notetype"] == "Cloze"
        assert ref.meta["deck"] == "Precis"
        assert ref.meta["fields"]["Text"] == cloze
        assert "Back Extra" not in ref.meta["fields"]

    def test_back_extra_split_and_stored(self, store) -> None:
        h = _make_handler(store)
        body = "The {{c1::Krebs}} cycle occurs in the matrix.\n---\naka TCA cycle"
        resp = h.put(text=body)
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))

        ref = store.get_ref(kind="anki", id=ref_id)
        assert ref.meta["fields"]["Text"] == (
            "The {{c1::Krebs}} cycle occurs in the matrix."
        )
        assert ref.meta["fields"]["Back Extra"] == "aka TCA cycle"

        with store.pool.connection() as conn:
            card_text = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = -1",
                (ref_id,),
            ).fetchone()[0]
        # Card text = stripped cloze + the Back Extra, both searchable.
        assert "Krebs cycle occurs in the matrix" in card_text
        assert "aka TCA cycle" in card_text

    def test_deck_tag_sets_subdeck(self, store) -> None:
        h = _make_handler(store)
        resp = h.put(
            text="Beijing is the {{c1::capital}} of China.", tags=["deck-chinese"]
        )
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))
        ref = store.get_ref(kind="anki", id=ref_id)
        assert ref.meta["deck"] == "Precis::chinese"

    def test_no_deck_tag_defaults_to_precis(self, store) -> None:
        h = _make_handler(store)
        resp = h.put(text="A {{c1::plain}} card.")
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))
        assert store.get_ref(kind="anki", id=ref_id).meta["deck"] == "Precis"

    def test_non_cloze_body_rejected(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput):
            h.put(text="This sentence has no cloze deletion.")

    def test_rejected_card_writes_nothing(self, store) -> None:
        """A non-cloze put leaves no ghost ref behind (atomic-create contract)."""
        h = _make_handler(store)
        before = len(store.list_refs(kind="anki", limit=1000))
        with pytest.raises(BadInput):
            h.put(text="no cloze here")
        after = len(store.list_refs(kind="anki", limit=1000))
        assert after == before

    @pytest.mark.parametrize(
        "body",
        [
            "Catpath {{c1::gap C18}} has no prior-art node.",
            "We {{c1::lack}} a multi-temperature sweep.",
            "Our spec has no {{c1::tolerances}}.",
            "Missing is {{c1::a sensitivity analysis}}.",
        ],
    )
    def test_rejects_work_item_clozes(self, store: Store, body: str) -> None:
        with pytest.raises(BadInput, match="tracks project work"):
            _make_handler(store).put(text=body)
        assert not store.list_refs(kind="anki")

    @pytest.mark.parametrize("tag", ["SPACE:repo-dev", "section:threads", None])
    def test_rejects_work_tracking_source(self, store: Store, tag: str | None) -> None:
        source = store.insert_ref(
            kind="memory",
            slug=None,
            title="Catpath notes" if tag else "Catpath gap list",
        )
        if tag:
            store.add_tag(source.id, Tag.parse(tag))
        with pytest.raises(BadInput, match="tracks project work"):
            _make_handler(store).put(
                text="A miscibility gap is a region of {{c1::phase separation}}.",
                link=f"me{source.id}",
                rel="derived-from",
            )
        assert not store.list_refs(kind="anki")

    def test_accepts_standalone_concept_from_factual_source(self, store: Store) -> None:
        source = store.insert_ref(kind="memory", slug=None, title="Phase equilibria")
        response = _make_handler(store).put(
            text="A miscibility gap is a region of {{c1::phase separation}}.",
            link=f"memory:{source.id}",
            rel="derived-from",
        )
        assert _search(r"id=(\d+)", response.body)


class TestDeckRetag:
    """gr735742: a ``deck-<topic>`` retag re-files the card — ``meta.deck``
    must follow the post-edit tag set, or the card keeps syncing to a deck
    its tags no longer name."""

    def _put(self, h, tags: list[str]) -> int:
        resp = h.put(text="A {{c1::test}} card.", tags=tags)
        return int(_search(r"id=(\d+)", resp.body).group(1))

    def test_retag_moves_deck(self, store) -> None:
        h = _make_handler(store)
        ref_id = self._put(h, ["deck-aixmat-2026"])
        h.tag(
            id=ref_id,
            add=["aixmat-2026", "deck-ml-potentials"],
            remove=["deck-aixmat-2026"],
        )
        from precis.anki.notes import spec_from_ref

        spec = spec_from_ref(store.get_ref(kind="anki", id=ref_id))
        assert spec is not None
        assert spec.deck == "Precis::ml-potentials"
        assert spec.pending_deck_revision is not None
        h.tag(id=ref_id, add=["deck-ml-potentials"])
        assert spec_from_ref(store.get_ref(kind="anki", id=ref_id)) == spec

    def test_removing_last_deck_tag_returns_to_base_deck(self, store) -> None:
        h = _make_handler(store)
        ref_id = self._put(h, ["deck-chinese"])
        h.tag(id=ref_id, remove=["deck-chinese"])
        assert store.get_ref(kind="anki", id=ref_id).meta["deck"] == "Precis"

    def test_adding_second_deck_tag_keeps_the_first(self, store) -> None:
        """ "First deck- tag wins" — the earlier tag keeps filing the card;
        the caller must remove it to move the card."""
        h = _make_handler(store)
        ref_id = self._put(h, ["deck-a"])
        h.tag(id=ref_id, add=["deck-b"])
        assert store.get_ref(kind="anki", id=ref_id).meta["deck"] == "Precis::a"
        h.tag(id=ref_id, remove=["deck-a"])
        assert store.get_ref(kind="anki", id=ref_id).meta["deck"] == "Precis::b"

    def test_noop_retag_keeps_the_filed_deck(self, store) -> None:
        """Create picks the winner by request order; ``tags_for`` orders by
        (ref_tags.created_at, global tag_id) — one tx shares its timestamp,
        so global first-use order wins, NOT request order. A no-op retag must
        not re-file the card onto a deck tag that merely sorts earlier."""
        h = _make_handler(store)
        # Seed deck-a's tag row first so its tag_id is lower than deck-z's:
        # on the second card tag order puts deck-a ahead even though the
        # put listed deck-z first.
        self._put(h, ["deck-a"])
        ref_id = self._put(h, ["deck-z", "deck-a"])
        assert store.get_ref(kind="anki", id=ref_id).meta["deck"] == "Precis::z"
        h.tag(id=ref_id, add=["deck-z"])  # no-op — deck-z already present
        assert store.get_ref(kind="anki", id=ref_id).meta["deck"] == "Precis::z"
        # Removing the winner re-files onto the surviving deck tag.
        h.tag(id=ref_id, remove=["deck-z"])
        assert store.get_ref(kind="anki", id=ref_id).meta["deck"] == "Precis::a"

    def test_non_deck_retag_leaves_deck(self, store) -> None:
        h = _make_handler(store)
        ref_id = self._put(h, ["deck-chinese"])
        h.tag(id=ref_id, add=["ml"])
        assert store.get_ref(kind="anki", id=ref_id).meta["deck"] == "Precis::chinese"

    def test_foreign_projection_deck_not_rewritten(self, store) -> None:
        """A read-only projection mirrors the real Anki deck; corpus tags
        must not rewrite it (the card is never pushed back)."""
        h = _make_handler(store)
        with store.tx() as conn:
            ref = store.insert_ref(
                kind="anki",
                slug=None,
                title="foreign card",
                meta={
                    "source": "anki-foreign",
                    "readonly": True,
                    "deck": "HandMade",
                },
                conn=conn,
            )
        h.tag(id=ref.id, add=["deck-ml"])
        assert store.get_ref(kind="anki", id=ref.id).meta["deck"] == "HandMade"


class TestLeeches:
    """`get(kind='anki', id='/leeches')` surfaces bad-recall cards (high lapses
    or collapsed ease) from `meta.anki_stats` — the fix-cloze-or-study loop."""

    def _seed(self, store, title, stats):
        with store.tx() as conn:
            ref = store.insert_ref(
                kind="anki",
                slug=None,
                title=title,
                meta={
                    "notetype": "Cloze",
                    "deck": "Precis",
                    "fields": {"Text": title},
                    "anki_stats": stats,
                },
                conn=conn,
            )
        return ref.id

    def test_leeches_lists_bad_recall_only(self, store) -> None:
        h = _make_handler(store)
        leech = self._seed(
            store,
            "hard {{c1::sesquipedalian}}",
            {"lapses_total": 6, "ease_min": 1.8, "reps_total": 20},
        )
        good = self._seed(
            store,
            "easy {{c1::cat}}",
            {"lapses_total": 0, "ease_min": 2.5, "reps_total": 3},
        )
        out = h.get(id="/leeches")
        assert f"ak{leech}" in out.body
        assert f"ak{good}" not in out.body
        assert "fix the cloze" in out.body


class TestAnkiDeleteSupersededBy:
    def _put_card(self, handler: AnkiHandler, text: str) -> int:
        return int(_search(r"id=(\d+)", handler.put(text=text).body).group(1))

    def test_delete_accepts_superseded_by(self, store: Store) -> None:
        handler = _make_handler(store)
        ids = [self._put_card(handler, f"A {{{{c1::dup{i}}}}} card.") for i in range(2)]
        handler.delete(id=ids[0], superseded_by=f"ak{ids[1]}", reason="duplicate")
        ref = store.get_ref(kind="anki", id=ids[0], include_deleted=True)
        assert ref is not None
        assert ref.meta["superseded_by"] == f"ak{ids[1]}"

    def test_delete_through_dispatch(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        """The gr780836 call shape: superseded_by + reason must clear the
        dispatch strictness gate, not just the handler's own signature."""
        handler = _make_handler(store)
        ids = [self._put_card(handler, f"A {{{{c1::dup{i}}}}} card.") for i in range(2)]
        out = runtime_with_store.dispatch(
            "delete",
            {
                "kind": "anki",
                "id": ids[0],
                "superseded_by": f"ak{ids[1]}",
                "reason": "duplicate",
            },
        )
        assert f"deleted anki card id={ids[0]}" in out
        ref = store.get_ref(kind="anki", id=ids[0], include_deleted=True)
        assert ref is not None
        assert ref.meta["superseded_by"] == f"ak{ids[1]}"

    def test_delete_through_dispatch_args_extras(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        handler = _make_handler(store)
        ids = [self._put_card(handler, f"A {{{{c1::dup{i}}}}} card.") for i in range(2)]
        out = runtime_with_store.dispatch(
            "delete",
            {
                "kind": "anki",
                "id": ids[0],
                "__extras__": {"superseded_by": f"ak{ids[1]}", "reason": "dup"},
            },
        )
        assert f"deleted anki card id={ids[0]}" in out

    def test_delete_refuses_self_successor(self, store: Store) -> None:
        handler = _make_handler(store)
        rid = self._put_card(handler, "A {{c1::solo}} card.")
        with pytest.raises(BadInput):
            handler.delete(id=rid, superseded_by=rid)

    def test_delete_dry_run_validates_successor(self, store: Store) -> None:
        """dry_run previews the real call: a dead successor refuses, a live
        one writes nothing."""
        handler = _make_handler(store)
        rid = self._put_card(handler, "A {{c1::solo}} card.")
        succ = self._put_card(handler, "A {{c1::other}} card.")
        with pytest.raises(NotFound):
            handler.delete(id=rid, superseded_by=2_000_000_000, dry_run=True)
        out = handler.delete(id=rid, superseded_by=f"ak{succ}", dry_run=True)
        assert "dry run" in out.body
        ref = store.get_ref(kind="anki", id=rid)
        assert ref is not None
        assert "superseded_by" not in ref.meta

    def test_delete_gone_target_not_stamped(self, store: Store) -> None:
        """The successor stamp must not outlive a refused delete: a Gone
        target fails before meta.superseded_by can be written."""
        handler = _make_handler(store)
        rid = self._put_card(handler, "A {{c1::solo}} card.")
        succ = self._put_card(handler, "A {{c1::other}} card.")
        handler.delete(id=rid)
        with pytest.raises(Gone):
            handler.delete(id=rid, superseded_by=f"ak{succ}")
        ref = store.get_ref(kind="anki", id=rid, include_deleted=True)
        assert ref is not None
        assert "superseded_by" not in ref.meta

    def test_delete_mutual_supersede_refuses_cycle(self, store: Store) -> None:
        """The state the losing racer re-reads under the row locks: A is
        already retired pointing at B, so ``delete B superseded_by A``
        must refuse and leave B live and unstamped — no two-card redirect
        cycle with no live survivor (review td781074)."""
        handler = _make_handler(store)
        a = self._put_card(handler, "A {{c1::first}} card.")
        b = self._put_card(handler, "A {{c1::second}} card.")
        handler.delete(id=a, superseded_by=f"ak{b}")
        with pytest.raises(Gone):
            handler.delete(id=b, superseded_by=f"ak{a}")
        ref = store.get_ref(kind="anki", id=b)
        assert ref is not None
        assert "superseded_by" not in ref.meta

    def test_delete_mutual_supersede_overlapped_txs(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The losing delete must wait at its row lock before rechecking liveness."""
        handler = _make_handler(store)
        a = self._put_card(handler, "A {{c1::first}} card.")
        b = self._put_card(handler, "A {{c1::second}} card.")

        loser_pid: Queue[int] = Queue()
        real_atomic = store.atomic

        @contextmanager
        def observe_loser() -> Iterator[Connection]:
            with real_atomic() as conn:
                conn.execute("SET LOCAL lock_timeout = '20s'")
                loser_pid.put(conn.info.backend_pid)
                yield conn

        def delete_loser() -> None:
            _make_handler(store).delete(id=b, superseded_by=f"ak{a}")

        with ThreadPoolExecutor(max_workers=1) as executor:
            with real_atomic() as winner:
                handler.delete(id=a, superseded_by=f"ak{b}")
                monkeypatch.setattr(store, "atomic", observe_loser)
                loser = executor.submit(delete_loser)
                pid = loser_pid.get(timeout=10)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if winner.execute(
                        "SELECT %s = ANY(pg_blocking_pids(%s))",
                        (winner.info.backend_pid, pid),
                    ).fetchone() == (True,):
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("the second delete never waited for the first")

                # Without the explicit lock the loser can instead block on
                # its successor stamp after passing the liveness checks.
                activity = winner.execute(
                    "SELECT query FROM pg_stat_activity WHERE pid = %s",
                    (pid,),
                ).fetchone()
                assert activity is not None
                assert "FOR NO KEY UPDATE" in activity[0], activity[0]
            with pytest.raises(Gone):
                loser.result(timeout=10)

        ref = store.get_ref(kind="anki", id=b)
        assert ref is not None
        assert "superseded_by" not in ref.meta
        tomb = store.get_ref(kind="anki", id=a, include_deleted=True)
        assert tomb is not None
        assert tomb.meta["superseded_by"] == f"ak{b}"
