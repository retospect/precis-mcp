"""gr595551: the gripe's current-state summary over a WORM comment ledger.

A gripe's ``gripe_comment`` chunks are append-only; the mutable
``meta.summary`` block records the current state plus the comment ord
it was rebuilt through (a reader can tell ``current`` from ``stale``).
``put(id=N, text=…, args={'stage': '<slug>'})`` appends a stage-marked
entry and rebuilds via the small model (``route_small``, injectable);
``edit(mode='summary')`` rebuilds on demand, writes a vetted text with
``args={'summary': …}`` (``args={'through': N}`` pins coverage, ``''``
clears); the same gate runs either way — anchors (``an<N>``/``co<N>``)
and ``entry N`` pointers must exist in the ledger, and a raw
``path``/``path::qual`` refuses (a stage ``put`` carrying one refuses
too — the entry's contract is handles only). The bare card carries
the block, ``view='summary'`` reads it uncapped, ``view='full'`` leads
with it, and ``get(id='/match', args={'trace': …})`` ranks open gripes
by which trace file/symbol tokens their ledgers name.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _gripe_summary as gs
from precis.handlers.alias import AliasHandler
from precis.handlers.gripe import GripeHandler
from tests._refs import insert_gripe


@pytest.fixture
def handler(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _gripe(
    handler: GripeHandler,
    title: str = "Area: the flux capacitor leaks",
    status: str = "open",
) -> int:
    return int(insert_gripe(handler.store, title, status=status).id)


def _alias(handler: GripeHandler, handle: str, location: str) -> None:
    """Mint the code-anchor pair — the ``alias`` kind's
    ``handle ⇄ <path>::<symbol>`` map is where ``an<N>``/``co<N>``
    ledger handles resolve until gr595549's mechanism lands."""
    handler.store.upsert_kinds([AliasHandler.spec])
    handler.store.insert_ref(
        kind="alias",
        slug=None,
        title=f"{handle} ⇄ {location}",
        meta={"alias": {"a": handle, "b": location, "rel": "short-for"}},
    )


def _fake_llm(reply: str):
    def _call(_prompt: str) -> str:
        return reply

    return _call


def _fail_llm(_prompt: str) -> str:
    raise RuntimeError("the small model is on fire")


# ── stage-labelled appends ──────────────────────────────────────────


class TestStageAppend:
    def test_stage_slug_rejected_on_create(self, handler: GripeHandler) -> None:
        with pytest.raises(BadInput, match="no ledger to append"):
            handler.put(text="new filing", stage="triage")

    def test_stage_slug_shape(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        with pytest.raises(BadInput, match="not a slug"):
            handler.put(id=rid, text="entry", stage="Not A Slug!")

    def test_stage_needs_text(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        with pytest.raises(BadInput, match="text is required|IS the text"):
            handler.put(id=rid, stage="triage")

    def test_stage_entry_refuses_raw_locations(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stage-entry contract is ``an<N>`` handles only — a raw
        ``path`` or ``path::qual`` refuses naming the mint (review of
        td655156)."""
        rid = _gripe(handler)
        with pytest.raises(BadInput, match="handles only.*would land raw"):
            handler.put(
                id=rid, text="suspect src/a.py::run drops frames", stage="triage"
            )
        with pytest.raises(BadInput, match="src/b.py"):
            handler.put(id=rid, text="touched files: src/b.py", stage="code")
        # …while a handle-only entry lands and rebuilds
        monkeypatch.setattr(gs, "route_small", _fake_llm("state: an3"))
        out = handler.put(id=rid, text="suspect an3", stage="triage").body
        assert "summary rebuilt through comment 1" in out

    def test_stage_entry_without_stage_still_takes_paths(
        self, handler: GripeHandler
    ) -> None:
        """A plain comment stays prose — the raw-location rule binds
        stage entries only (older entries predate it either way)."""
        rid = _gripe(handler)
        handler.put(id=rid, text="looked at src/a.py — seems fine")
        view = handler.get(id=rid, view="comments").body
        assert "src/a.py" in view

    def test_stage_meta_persisted(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gs, "route_small", _fake_llm("root cause at an7"))
        rid = _gripe(handler)
        handler.put(id=rid, text="suspect the flux coil, see an7", stage="triage")
        chunks = handler.store.chunks.list_chunks_for_ref(rid)
        entry = next(c for c in chunks if c.chunk_kind == "gripe_comment")
        assert gs.stage_of(entry) == "triage"

    def test_stage_append_rebuilds(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gs, "route_small", _fake_llm("state: suspect an7"))
        rid = _gripe(handler)
        out = handler.put(
            id=rid, text="suspect the flux coil, see an7", stage="triage"
        ).body
        assert "summary rebuilt through comment 1" in out
        card = handler.get(id=rid).body
        assert "summary (through comment 1 · current):" in card
        assert "state: suspect an7" in card

    def test_rebuild_failure_keeps_comment(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ledger write commits first — a model fault reports on the
        ack and names the retry; it never eats the entry."""
        monkeypatch.setattr(gs, "route_small", _fail_llm)
        rid = _gripe(handler)
        out = handler.put(id=rid, text="dead end: not the coil", stage="test").body
        assert "summary not rebuilt" in out
        assert f"edit(kind='gripe', id={rid}, mode='summary')" in out
        view = handler.get(id=rid, view="comments").body
        assert "dead end: not the coil" in view
        # and the retry works once the model is back
        monkeypatch.setattr(
            gs, "route_small", _fake_llm("ruled out: coil, see entry 1")
        )
        out = handler.edit(id=rid, mode="summary").body
        assert "summary rebuilt through comment 1" in out


# ── edit(mode='summary') ─────────────────────────────────────────────


class TestEditSummary:
    def test_rebuild_no_ledger(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        with pytest.raises(BadInput, match="no ledger entries"):
            handler.edit(id=rid, mode="summary")

    def test_rebuild(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gs, "route_small", _fake_llm("suspect an3, see entry 1"))
        rid = _gripe(handler)
        handler.put(id=rid, text="first look: coil reads an3")
        handler.put(id=rid, text="coil fine — still leaking")
        out = handler.edit(id=rid, mode="summary").body
        assert "summary rebuilt through comment 2" in out
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None
        assert (s := gs.summary_of(ref.meta)) is not None
        assert s.through == 2

    def test_rebuild_idempotent_when_current(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gs, "route_small", _fake_llm("state"))
        rid = _gripe(handler)
        handler.put(id=rid, text="one entry")
        handler.edit(id=rid, mode="summary")
        out = handler.edit(id=rid, mode="summary").body
        assert "already current" in out

    def test_manual_write(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="coil fine, see an5")
        handler.put(id=rid, text="still leaking")
        out = handler.edit(
            id=rid,
            mode="summary",
            summary="state: coil ruled out, see entry 1; an5 in play",
            through=1,
        ).body
        assert "summary written (through comment 1)" in out
        card = handler.get(id=rid).body
        # through=1 with the ledger at 2 → the stale mark names the rebuild
        assert "STALE" in card
        assert "edit(kind='gripe', id=" in card

    def test_manual_write_defaults_through(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="entry one")
        handler.put(id=rid, text="entry two")
        handler.edit(id=rid, mode="summary", summary="two entries stand")
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None
        assert (s := gs.summary_of(ref.meta)) is not None
        assert s.through == 2

    def test_through_without_summary_refused(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="entry")
        with pytest.raises(BadInput, match="rides with"):
            handler.edit(id=rid, mode="summary", through=1)

    def test_through_out_of_range(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="entry")
        with pytest.raises(BadInput, match="outside the ledger"):
            handler.edit(id=rid, mode="summary", summary="x", through=9)

    def test_clear(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="entry")
        handler.edit(id=rid, mode="summary", summary="state")
        out = handler.edit(id=rid, mode="summary", summary="").body
        assert "summary cleared" in out
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None
        assert gs.summary_of(ref.meta) is None
        assert "summary" not in handler.get(id=rid).body

    def test_prose_keys_refused(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        with pytest.raises(BadInput, match="takes no title"):
            handler.edit(id=rid, mode="summary", title="new title")
        with pytest.raises(BadInput, match="takes no text"):
            handler.edit(id=rid, mode="summary", text="prose")

    def test_fix_keys_refused(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        with pytest.raises(BadInput, match="does not take"):
            handler.edit(id=rid, mode="summary", batch="b1")

    def test_summary_keys_need_mode(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        with pytest.raises(BadInput, match="need mode='summary'"):
            handler.edit(id=rid, title="Area: retitled", summary="sneaky")

    def test_dry_run_folds_nothing(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="one")
        handler.put(id=rid, text="two")
        out = handler.edit(id=rid, mode="summary", dry_run=True).body
        assert "dry run" in out
        assert "nothing written" in out
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None
        assert gs.summary_of(ref.meta) is None


# ── the fold's coverage honesty ─────────────────────────────────────


class TestFoldCoverage:
    def test_long_entry_folds_as_pieces(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry past the piece budget is fed as sequential parts —
        the ``through`` stamp may never claim coverage of text the
        model did not see (review: truncation skipped dead ends)."""
        calls: list[str] = []

        def _recorder(prompt: str) -> str:
            calls.append(prompt)
            return f"state after piece {len(calls)}"

        monkeypatch.setattr(gs, "route_small", _recorder)
        rid = _gripe(handler)
        handler.put(id=rid, text="x" * (gs._ENTRY_CAP + 50), stage="code")
        assert len(calls) == 2
        assert "part 1/2" in calls[0]
        assert "part 2/2" in calls[1]
        # the tail reached the model — nothing silently dropped
        assert calls[1].rstrip().endswith("x" * 50)
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None
        assert (s := gs.summary_of(ref.meta)) is not None
        assert s.through == 1

    def test_fold_persists_per_entry(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mid-rebuild gate refusal leaves an honest (text, through)
        pair — entry 1's fold stands, and the retry resumes at entry 2
        instead of replaying it."""
        replies = iter(["after entry one", "invents an404"])
        monkeypatch.setattr(gs, "route_small", lambda _p: next(replies))
        rid = _gripe(handler)
        handler.put(id=rid, text="first entry")
        handler.put(id=rid, text="second entry")
        with pytest.raises(BadInput, match="an404"):
            handler.edit(id=rid, mode="summary")
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None
        assert (s := gs.summary_of(ref.meta)) is not None
        assert s.through == 1
        assert s.text == "after entry one"
        monkeypatch.setattr(gs, "route_small", _fake_llm("after entry two"))
        out = handler.edit(id=rid, mode="summary").body
        assert "summary rebuilt through comment 2" in out

    def test_entries_fold_one_call_each(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each new entry is its own fold — old summary + new entry →
        new summary."""
        calls: list[str] = []

        def _route(p: str) -> str:
            calls.append(p)
            return "s"

        monkeypatch.setattr(gs, "route_small", _route)
        rid = _gripe(handler)
        handler.put(id=rid, text="one")
        handler.put(id=rid, text="two")
        handler.put(id=rid, text="three")
        out = handler.edit(id=rid, mode="summary").body
        assert "folded 3 entries 1-3" in out
        assert len(calls) == 3
        assert "--- entry 2 ---" in calls[1]

    def test_ord_zero_entry_folds(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (review of td655156): worker writers mint the
        first ledger ord as 0 (``gripe_triage._next_ord`` /
        ``executors._common.append_chunk`` on a chunkless gripe), so
        the 'nothing folded yet' floor is -1 — a 0 floor skipped the
        ord-0 entry forever."""
        from precis.store.types import ChunkInsert

        calls: list[str] = []

        def _route(p: str) -> str:
            calls.append(p)
            return "state: first look in"

        monkeypatch.setattr(gs, "route_small", _route)
        rid = _gripe(handler)
        with handler.store.tx() as conn:
            handler.store.chunks.insert_chunks(
                rid,
                [
                    ChunkInsert(
                        ord=0,
                        text="triage suspects the coil",
                        meta={"chunk_kind": "gripe_comment", "stage": "triage"},
                    )
                ],
                conn=conn,
            )
        out = handler.edit(id=rid, mode="summary").body
        assert "summary rebuilt through comment 0" in out
        assert len(calls) == 1
        assert "--- entry 0 (triage) ---" in calls[0]
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None
        assert (s := gs.summary_of(ref.meta)) is not None
        assert s.through == 0
        # and the card reads it as covered, not stale
        assert "through comment 0 · current" in handler.get(id=rid).body


# ── the scrub: each claim once, under the right label (gr673911) ────


class TestSummaryScrub:
    def _comments(self, handler: GripeHandler, rid: int) -> list:
        return gs.ledger(handler.store.chunks.list_chunks_for_ref(rid))

    def test_relabels_recommend_entries(self, handler: GripeHandler) -> None:
        """gr673911: a ``recommend:`` entry cited as ``ruled out:``
        takes the ENTRY's label — the ledger's word beats the
        model's."""
        rid = _gripe(handler)
        handler.put(id=rid, text="recommend: Bulk-close the batch")
        handler.put(id=rid, text="recommend: PRIO:high")
        out = gs.scrub_summary(
            "ruled out: Bulk-close the batch, see entry 1\n"
            "ruled out: PRIO:high, see entry 2",
            self._comments(handler, rid),
        )
        assert "recommend: Bulk-close the batch, see entry 1" in out
        assert "recommend: PRIO:high, see entry 2" in out
        assert "ruled out" not in out

    def test_inner_label_behind_a_stage_tag(self, handler: GripeHandler) -> None:
        """``triage: suspected cause - …`` carries its claim label one
        level in — a tag alone is provenance, not a label."""
        rid = _gripe(handler)
        handler.put(id=rid, text="triage: suspected cause - devin first")
        out = gs.scrub_summary(
            "ruled out: devin first, see entry 1",
            self._comments(handler, rid),
        )
        assert out == "suspected cause: devin first, see entry 1"

    def test_dedupe_merges_entry_pointers(self, handler: GripeHandler) -> None:
        """gr673911's ~50 alternating lines: a claim listed once per
        citing entry collapses to one line citing them all."""
        rid = _gripe(handler)
        handler.put(id=rid, text="triage: suspected cause - devin first")
        handler.put(
            id=rid,
            text="triage: memory gripe on me477673 — routed to the gardener",
        )
        handler.put(id=rid, text="devin first")
        out = gs.scrub_summary(
            "ruled out: devin first, see entry 1\n"
            "ruled out: memory me477673, see entry 2\n"
            "ruled out: devin first, see entry 3",
            self._comments(handler, rid),
        )
        assert out.count("devin first") == 1
        # the merged claim relabels from entry 1's inner label and
        # cites every entry that stated it
        assert "suspected cause: devin first, see entries 1, 3" in out
        # a tag-only entry with no inner claim label keeps the model's
        # label — there is no ledger word to take
        assert "ruled out: memory me477673, see entry 2" in out

    def test_later_unlabelled_verdict_keeps_ruled_out(
        self, handler: GripeHandler
    ) -> None:
        """Regression (review td674237): ``recommend: retry`` then
        ``test: retry failed`` — the disproof is the ledger's latest
        verdict on the claim. The scrub must not resurrect the older
        ``recommend:`` just because the test entry's label is not a
        claim label."""
        rid = _gripe(handler)
        handler.put(id=rid, text="recommend: retry")
        handler.put(id=rid, text="test: retry failed")
        out = gs.scrub_summary(
            "ruled out: retry, see entries 1, 2",
            self._comments(handler, rid),
        )
        assert out == "ruled out: retry, see entries 1, 2"

    def test_merge_label_follows_newest_cited_entry(
        self, handler: GripeHandler
    ) -> None:
        """The merged line keeps the label of the occurrence citing
        the NEWEST entry — a ``recommend:`` re-listed after the
        ``ruled out:`` disproof cannot restore it (review td674237)."""
        rid = _gripe(handler)
        handler.put(id=rid, text="recommend: retry")
        handler.put(id=rid, text="test: retry failed")
        out = gs.scrub_summary(
            "ruled out: retry, see entry 2\nrecommend: retry, see entry 1",
            self._comments(handler, rid),
        )
        assert out == "ruled out: retry, see entries 1, 2"

    def test_restatement_needs_the_whole_entry(self, handler: GripeHandler) -> None:
        """Regression (review td674237): a verdict entry whose FIRST
        line restates the claim but whose body goes on (``test: retry``
        + ``retry failed``) is not a bare restatement — the disproof
        stands and the older ``recommend:`` is not resurrected."""
        rid = _gripe(handler)
        handler.put(id=rid, text="recommend: retry")
        handler.put(id=rid, text="test: retry\nretry failed")
        out = gs.scrub_summary(
            "ruled out: retry, see entries 1, 2",
            self._comments(handler, rid),
        )
        assert out == "ruled out: retry, see entries 1, 2"

    def test_bare_restatement_defers_to_older_label(
        self, handler: GripeHandler
    ) -> None:
        """A citing entry that only REPEATS the claim carries no
        verdict — the older labelled entry still supplies the
        label."""
        rid = _gripe(handler)
        handler.put(id=rid, text="recommend: retry the coil swap")
        handler.put(id=rid, text="retry the coil swap")
        out = gs.scrub_summary(
            "suspect: retry the coil swap, see entries 1, 2",
            self._comments(handler, rid),
        )
        assert out == "recommend: retry the coil swap, see entries 1, 2"

    def test_label_scopes_to_the_head_claim(self, handler: GripeHandler) -> None:
        """Regression (review td674237): an entry whose label heads a
        DIFFERENT claim does not label this one — ``recommend: replace
        coil`` with ``retry failed`` in the body is a disproof of
        ``retry``, so a ``ruled out: retry`` line citing it keeps its
        label instead of resurrecting entry 1's ``recommend:``."""
        rid = _gripe(handler)
        handler.put(id=rid, text="recommend: retry")
        handler.put(id=rid, text="recommend: replace coil\nretry failed")
        out = gs.scrub_summary(
            "ruled out: retry, see entries 1, 2",
            self._comments(handler, rid),
        )
        assert out == "ruled out: retry, see entries 1, 2"

    def test_label_applies_when_head_claim_matches(self, handler: GripeHandler) -> None:
        """The scope check's positive arm: the head claim containing the
        claim (``recommend: retry the coil swap`` for ``retry the coil
        swap``) still takes the entry's label, even alongside a
        disproof of something else in the body."""
        rid = _gripe(handler)
        handler.put(id=rid, text="recommend: retry the coil swap\nreplace failed")
        out = gs.scrub_summary(
            "ruled out: retry the coil swap, see entry 1",
            self._comments(handler, rid),
        )
        assert out == "recommend: retry the coil swap, see entry 1"

    def test_unlabelled_claim_lines_dedupe(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="one")
        handler.put(id=rid, text="two")
        out = gs.scrub_summary(
            "still suspect an7, see entry 1\nstill suspect an7, see entry 2",
            self._comments(handler, rid),
        )
        assert out == "still suspect an7, see entries 1, 2"

    def test_real_ruled_out_keeps_its_label(self, handler: GripeHandler) -> None:
        """A tag-only entry is not a claim label — a dead end the model
        reports stays ``ruled out:``."""
        rid = _gripe(handler)
        handler.put(id=rid, text="test: coil eliminated under load")
        out = gs.scrub_summary(
            "ruled out: coil, see entry 1", self._comments(handler, rid)
        )
        assert out == "ruled out: coil, see entry 1"

    def test_fold_scrubs_the_reply(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The persisted text is the scrubbed one — mislabels and
        repeats never land even when the model emits them."""
        monkeypatch.setattr(
            gs,
            "route_small",
            _fake_llm(
                "ruled out: Bulk-close the batch, see entry 1\n"
                "ruled out: PRIO:high, see entry 2\n"
                "ruled out: Bulk-close the batch, see entry 1"
            ),
        )
        rid = _gripe(handler)
        handler.put(id=rid, text="recommend: Bulk-close the batch")
        handler.put(id=rid, text="recommend: PRIO:high")
        out = handler.edit(id=rid, mode="summary").body
        assert "summary rebuilt through comment 2" in out
        view = handler.get(id=rid, view="summary").body
        assert "recommend: Bulk-close the batch, see entry 1" in view
        assert "recommend: PRIO:high, see entry 2" in view
        assert "ruled out" not in view

    def test_manual_write_is_not_scrubbed(self, handler: GripeHandler) -> None:
        """``args={'summary': …}`` is the writer's vetted text — the
        scrub binds model output only."""
        rid = _gripe(handler)
        handler.put(id=rid, text="recommend: keep it")
        handler.edit(
            id=rid,
            mode="summary",
            summary="ruled out: keep it, see entry 1",
        )
        assert "ruled out: keep it" in handler.get(id=rid, view="summary").body


# ── the gate ─────────────────────────────────────────────────────────


class TestGate:
    def test_bad_anchor_refused(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="the ledger only names an2")
        with pytest.raises(BadInput, match="anchor an99 is not in the ledger"):
            handler.edit(id=rid, mode="summary", summary="suspect an99")

    def test_bad_entry_ref_refused(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="one entry")
        with pytest.raises(BadInput, match="entry 7 is not a ledger comment"):
            handler.edit(id=rid, mode="summary", summary="ruled out: X, see entry 7")

    def test_co_anchor_spelling_accepted(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="the report spelled it co12")
        handler.edit(id=rid, mode="summary", summary="suspect co12, see entry 1")
        assert "co12" in handler.get(id=rid, view="summary").body

    def test_model_output_gated(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rebuild that invents an anchor is refused — nothing written."""
        monkeypatch.setattr(gs, "route_small", _fake_llm("suspect an404"))
        rid = _gripe(handler)
        handler.put(id=rid, text="the ledger names an4 only")
        with pytest.raises(BadInput, match="an404"):
            handler.edit(id=rid, mode="summary")

    def test_gate_summary_pure(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        handler.put(id=rid, text="suspect an1")
        handler.put(id=rid, text="ruled out coil")
        comments = gs.ledger(handler.store.chunks.list_chunks_for_ref(rid))
        assert gs.gate_summary("state: an1 live, see entry 2", comments) == []
        problems = gs.gate_summary("see entry 9, maybe an8", comments)
        assert any("entry 9" in p for p in problems)
        assert any("anchor an8" in p for p in problems)

    def test_gate_catches_plural_entry_refs(self, handler: GripeHandler) -> None:
        """``see entries 1, 9`` cites ords the same as the singular —
        a dead pointer may not hide behind the plural spelling (the
        scrub's merged pointers emit it)."""
        rid = _gripe(handler)
        handler.put(id=rid, text="one entry")
        comments = gs.ledger(handler.store.chunks.list_chunks_for_ref(rid))
        problems = gs.gate_summary("ruled out: X, see entries 1, 9", comments)
        assert any("entry 9" in p for p in problems)
        assert gs.gate_summary("ruled out: X, see entries 1", comments) == []

    def test_gate_refuses_raw_locations(self, handler: GripeHandler) -> None:
        """A summary naming a raw ``path``/``path::qual`` refuses even
        when the ledger legitimately carries that path — the summary's
        contract is anchors only, same as the entries' (review of
        td655156)."""
        rid = _gripe(handler)
        handler.put(id=rid, text="the pre-rule entry names src/a.py")
        comments = gs.ledger(handler.store.chunks.list_chunks_for_ref(rid))
        problems = gs.gate_summary("suspect src/a.py, see entry 1", comments)
        assert any("raw location src/a.py" in p for p in problems)
        with pytest.raises(BadInput, match="raw location src/a.py"):
            handler.edit(id=rid, mode="summary", summary="suspect src/a.py")
        with pytest.raises(BadInput, match="raw location src/a.py::run"):
            handler.edit(id=rid, mode="summary", summary="suspect src/a.py::run")

    def test_gate_refuses_model_raw_location(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fold's reply is gated the same way — a model that echoes
        a raw path is refused."""
        monkeypatch.setattr(
            gs, "route_small", _fake_llm("suspect src/a.py, see entry 1")
        )
        rid = _gripe(handler)
        handler.put(id=rid, text="the pre-rule entry names src/a.py")
        with pytest.raises(BadInput, match="raw location src/a.py"):
            handler.edit(id=rid, mode="summary")


# ── reads ────────────────────────────────────────────────────────────


class TestReads:
    def _summarised(self, handler: GripeHandler) -> int:
        rid = _gripe(handler)
        handler.put(id=rid, text="first: coil fine")
        handler.edit(id=rid, mode="summary", summary="state: coil ruled out")
        return rid

    def test_summary_view(self, handler: GripeHandler) -> None:
        rid = self._summarised(handler)
        out = handler.get(id=rid, view="summary").body
        assert "state: coil ruled out" in out
        assert "through comment 1 · current" in out

    def test_summary_view_none(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        out = handler.get(id=rid, view="summary").body
        assert "summary: none" in out

    def test_full_view_leads_with_summary(self, handler: GripeHandler) -> None:
        rid = self._summarised(handler)
        out = handler.get(id=rid, view="full").body
        assert "state: coil ruled out" in out

    def test_card_marks_stale(self, handler: GripeHandler) -> None:
        rid = self._summarised(handler)
        handler.put(id=rid, text="new development")
        card = handler.get(id=rid).body
        assert "STALE" in card


# ── /match ───────────────────────────────────────────────────────────


class TestMatch:
    def test_requires_trace(self, handler: GripeHandler) -> None:
        with pytest.raises(BadInput, match="needs args=\{'trace'"):
            handler.get(id="/match")
        with pytest.raises(BadInput, match="needs args=\{'trace'"):
            handler.get(id="/match", trace="   ")

    def test_view_refused(self, handler: GripeHandler) -> None:
        with pytest.raises(BadInput, match="takes no view"):
            handler.get(id="/match", view="full", trace="x")

    def test_trace_elsewhere_refused(self, handler: GripeHandler) -> None:
        rid = _gripe(handler)
        with pytest.raises(BadInput, match="belongs to the '/match'"):
            handler.get(id=rid, trace="Traceback")

    def test_ranks_and_scopes(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The documented ledger shape is anchor-only — the match must
        resolve ``an<N>`` handles to their locations, not substring
        the literal text (review of td655156)."""
        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        _alias(handler, "an7", "src/precis/flux/parse.py::deflux_core")
        hit = _gripe(handler, "Area: flux parse dies")
        handler.put(id=hit, text="touched files: an7 — deflux rewired", stage="code")
        _gripe(handler, "Area: unrelated slack ping")
        closed = _gripe(handler, "Area: old flux bug", status="done")
        handler.put(id=closed, text="an7 was the fix")
        out = handler.get(
            id="/match",
            trace=(
                "Traceback (most recent call last):\n"
                '  File "src/precis/flux/parse.py", line 42, in deflux_core\n'
                "ValueError: flux"
            ),
        ).body
        assert f"gr{hit}" in out
        assert f"gr{closed}" not in out  # closed gripes don't rank
        assert "unrelated slack ping" not in out
        assert "last files entry 1: src/precis/flux/parse.py" in out

    def test_literal_paths_still_match(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anchor resolution ADDS to the literal match — a raw path in
        the ledger keeps working (older entries predate the rule)."""
        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        hit = _gripe(handler, "Area: flux parse dies")
        # a plain comment — no stage= — is the pre-rule entry shape:
        # stage entries refuse raw locations outright now.
        handler.put(
            id=hit,
            text="touched files: src/precis/flux/parse.py — deflux_core rewired",
        )
        out = handler.get(
            id="/match",
            trace='File "src/precis/flux/parse.py", line 42, in deflux_core',
        ).body
        assert f"gr{hit}" in out

    def test_files_from_latest_entry_only(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row's file list is the NEWEST touched-files entry's own
        files — not the union over the ledger (the label says 'last
        files entry')."""
        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        _alias(handler, "an8", "src/precis/old/one.py")
        _alias(handler, "an9", "src/precis/new/two.py::build_thing")
        rid = _gripe(handler, "Area: churn")
        handler.put(id=rid, text="touched files: an8", stage="code")
        handler.put(id=rid, text="touched files: an9", stage="code")
        out = handler.get(
            id="/match", trace='File "src/precis/new/two.py", line 5, in build_thing'
        ).body
        assert "last files entry 2: src/precis/new/two.py" in out
        assert "one.py" not in out

    def test_later_diagnostic_does_not_replace_touched_files(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A later triage/test entry that names another file is not the
        ownership list — the latest ``touched files:`` entry stays."""
        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        _alias(handler, "an8", "src/precis/new/two.py")
        _alias(handler, "an9", "src/precis/other/three.py::build_thing")
        rid = _gripe(handler, "Area: churn")
        handler.put(id=rid, text="touched files: an8", stage="code")
        handler.put(id=rid, text="suspected cause in an9", stage="triage")
        out = handler.get(
            id="/match",
            trace='File "src/precis/other/three.py", line 5, in build_thing',
        ).body
        assert f"gr{rid}" in out
        assert "last files entry 1: src/precis/new/two.py" in out
        assert "three.py," not in out.split("last files")[1].split("\n")[0]

    def test_unresolved_anchor_scores_nothing(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An anchor with no alias pair resolves to nothing — it
        cannot phantom-hit a trace."""
        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        rid = _gripe(handler, "Area: anchorless")
        handler.put(id=rid, text="touched files: an404", stage="code")
        out = handler.get(
            id="/match", trace='File "src/precis/flux/parse.py", line 1'
        ).body
        assert "no open gripe's ledger" in out

    def test_resolved_locations_fold_case(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (review FIX of td655156): a resolved anchor
        location joins the same case-folded surface the trace tokens
        and ledger prose score against — a ledger whose only code
        location is ``an1 ⇄ src/App.tsx`` still hits a trace naming
        ``File "src/App.tsx"`` (the unresolved join kept the minted
        case, so ``src/app.tsx`` never matched)."""
        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        _alias(handler, "an1", "src/App.tsx")
        rid = _gripe(handler, "Area: App render dies")
        handler.put(id=rid, text="touched files: an1", stage="code")
        out = handler.get(
            id="/match", trace='File "src/App.tsx", line 9, in render'
        ).body
        assert f"gr{rid}" in out
        # ownership reads the folded location back off the pair
        assert "last files entry 1: src/app.tsx" in out

    def test_tsx_jsx_locations_round_trip(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``.tsx``/``.jsx`` are locations end to end (review of
        td655156): they extract out of prose, mint to ``an<N>``
        handles, and resolve back onto a matching trace — every
        trace token lands, case-fold included."""
        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        handler.store.upsert_kinds([AliasHandler.spec])
        rid = _gripe(handler, "Area: widget render dies")
        text = gs.anchorize_text(
            handler.store,
            "suspect src/ui/App.tsx::RenderWidget leaks into src/ui/util.jsx",
            gripe=rid,
        )
        assert text == "suspect an1 leaks into an2"
        handler.put(id=rid, text=text, stage="triage")
        out = handler.get(
            id="/match",
            trace=(
                'File "src/ui/App.tsx", line 9, in RenderWidget\n'
                'File "src/ui/util.jsx", line 4, in <module>'
            ),
        ).body
        assert f"gr{rid}" in out
        # 4 file tokens + renderwidget — a case-leaking resolve drops 3
        assert "hits 5" in out

    def test_no_hits(self, handler: GripeHandler) -> None:
        _gripe(handler, "Area: something else")
        out = handler.get(id="/match", trace='File "zzz/q.py", line 1').body
        assert "no open gripe's ledger" in out

    def test_trace_tokens(self) -> None:
        files, syms = gs.trace_tokens(
            'Traceback:\n  File "a/b/thing.py", line 9, in do_thing\n'
            "  at MakeIt.so (make/it.py:3)\n"
            "RuntimeError: boom in WidgetFactory"
        )
        assert "thing.py" in files
        assert "a/b/thing.py" in files
        assert "do_thing" in syms
        assert "widgetfactory" in syms
        # english noise is not a symbol
        assert "boom" not in syms


# ── the pipeline writers are wired to the fold ───────────────────────


class TestPipelineWiring:
    def test_add_comment_marks_stage(self, handler: GripeHandler) -> None:
        """``gripe_triage``'s append stamps the stage the entry came
        from — the ledger records which pipeline stage wrote it."""
        from precis.workers import gripe_triage as gt

        rid = _gripe(handler)
        with handler.store.tx() as conn:
            gt._add_comment(
                conn,
                handler.store,
                rid,
                "the verdict",
                gt._next_ord(conn, rid),
                stage="triage",
            )
        chunks = handler.store.chunks.list_chunks_for_ref(rid)
        entry = next(c for c in chunks if c.chunk_kind == "gripe_comment")
        assert gs.stage_of(entry) == "triage"

    def test_triage_fold_rebuilds(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stage's post-commit fold calls the rebuild on the gripe
        it just wrote — the summary tracks the ledger."""
        from precis.workers import gripe_triage as gt

        rid = _gripe(handler)
        seen: list[int] = []

        def _rebuild(_store: Any, ref: Any) -> str:
            seen.append(ref.id)
            return "ok"

        monkeypatch.setattr(gs, "rebuild_summary", _rebuild)
        gt._fold_summary(handler.store, rid)
        assert seen == [rid]

    def test_triage_fold_swallows_faults(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-soft: a rebuild fault after the ledger write can never
        fail the tick."""
        from precis.workers import gripe_triage as gt

        rid = _gripe(handler)

        def _boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("model on fire")

        monkeypatch.setattr(gs, "rebuild_summary", _boom)
        gt._fold_summary(handler.store, rid)  # must not raise

    def test_fixer_writeback_marks_stage_and_folds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``fixer/writeback`` stamps ``meta.stage='code'`` on its
        ledger entries and runs the fold after they land."""
        from precis.fixer import writeback as wb
        from precis.handlers import _gripe_summary as gs_mod

        calls: dict[str, object] = {}

        class _FakeStore:
            def get_ref(self, *, kind: str, id: int) -> object:
                calls["get_ref"] = (kind, id)
                return object()  # any non-None ref feeds the fold

            def close(self) -> None:
                pass

        fake = _FakeStore()

        import precis.store.store as store_mod

        monkeypatch.setattr(store_mod.Store, "connect", lambda *a, **k: fake)

        import precis.workers.executors._common as common_mod

        def _fake_append(
            store: object,
            ref_id: int,
            kind: str,
            text: str,
            meta: dict[str, str] | None = None,
        ) -> None:
            calls["append_chunk"] = (ref_id, kind, text, meta)

        monkeypatch.setattr(common_mod, "append_chunk", _fake_append)
        monkeypatch.setattr(
            gs_mod,
            "rebuild_summary",
            lambda store, ref: calls.setdefault("rebuild", ref) or "ok",
        )
        ok = wb.gripe_writeback("postgresql://example/db", 42, "FIXER (auto): built it")
        assert ok is True
        appended = calls["append_chunk"]
        assert isinstance(appended, tuple)
        _ref_id, kind, _text, meta = appended
        assert kind == "gripe_comment"
        assert meta == {"stage": "code"}
        assert "rebuild" in calls


# ── the touched-files marker is literal ────────────────────────────


class TestTouchedMarker:
    def test_bare_touched_line_is_not_an_ownership_entry(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (review of td655156): a line merely *beginning*
        ``touched`` — ``touched nothing in diagnostic.py`` — is not a
        touched-files entry. The marker is the literal
        ``touched files:``."""
        assert not gs.is_touched_entry("touched nothing in src/diag.py")
        assert not gs.is_touched_entry("touched: src/diag.py")
        assert gs.is_touched_entry("touched files: src/a.py")
        assert gs.is_touched_entry("FIXER (auto): touched files: src/a.py")

        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        _alias(handler, "an7", "src/diag.py")
        rid = _gripe(handler, "Area: churn")
        handler.put(id=rid, text="touched nothing in an7", stage="triage")
        out = handler.get(id="/match", trace='File "src/diag.py", line 1, in run').body
        # the named file scores the hit, but no ownership is claimed —
        # the row pattern is ``last files entry <N>:`` (the footer's
        # prose legitimately carries the bare phrase)
        assert f"gr{rid}" in out
        assert not re.search(r"last files entry \d+:", out)

    def test_ownership_reads_only_the_marker_line(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``files`` comes from the ``touched files:`` line's payload
        alone — another line in the same comment naming a file is hit
        evidence, never ownership (review of td655156)."""
        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        _alias(handler, "an8", "src/precis/new/two.py")
        _alias(handler, "an9", "src/precis/diag.py")
        rid = _gripe(handler, "Area: churn")
        handler.put(
            id=rid,
            text="touched files: an8\nalso examined an9",
            stage="code",
        )
        out = handler.get(
            id="/match", trace='File "src/precis/diag.py", line 5, in probe'
        ).body
        assert f"gr{rid}" in out
        assert "last files entry 1: src/precis/new/two.py" in out
        assert "src/precis/diag.py" not in out

    def test_touched_payload_shape(self) -> None:
        assert gs.touched_payload("touched files: src/a.py, an7") == ("src/a.py, an7")
        assert gs.touched_payload("head\ntouched files: src/a.py\nnext") == "src/a.py"
        assert gs.touched_payload("touched nothing here") is None

    def test_payload_must_share_the_marker_line(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (review of td655156): ``touched files:`` followed
        by a newline then a file claims NOTHING — a newline-eating
        ``\\s*`` after the colon used to hand the next line's file to
        the ownership list."""
        assert gs.touched_payload("touched files:\nsrc/diag.py") == ""
        assert gs.touched_payload("touched files:   src/a.py") == "src/a.py"
        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        _alias(handler, "an7", "src/precis/diag.py")
        rid = _gripe(handler, "Area: churn")
        handler.put(
            id=rid,
            text="touched files:\nan7 — next line, not payload",
            stage="code",
        )
        out = handler.get(
            id="/match", trace='File "src/precis/diag.py", line 1, in run'
        ).body
        # the file still scores the hit — but ownership resolves to
        # nothing, never to the next line's file
        assert f"gr{rid}" in out
        assert "last files entry 1: (none resolved)" in out
        assert "diag.py" not in out

    def test_newest_marker_wins_even_when_payload_resolves_nothing(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (review FIX of td655156): the NEWEST touched-files
        marker owns the answer even when its payload yields no files —
        an empty or unresolved payload means "no files on record"; a
        fall-through would restore ownership the newest entry
        superseded."""
        monkeypatch.setattr(gs, "route_small", _fake_llm("irrelevant"))
        _alias(handler, "an8", "src/precis/old/one.py")
        rid = _gripe(handler, "Area: churn")
        handler.put(id=rid, text="touched files: an8", stage="code")
        handler.put(id=rid, text="touched files: an404", stage="code")
        out = handler.get(
            id="/match", trace='File "src/precis/old/one.py", line 5, in probe'
        ).body
        assert f"gr{rid}" in out
        assert "last files entry 2: (none resolved)" in out
        assert "one.py" not in out


# ── mint_anchors: the producer's handle mint ───────────────────────


class TestMintAnchors:
    def test_mint_find_or_mint_and_resolve(self, handler: GripeHandler) -> None:
        """``an<N>`` allocates in order, a repeat mint reuses the live
        pair, and ``resolve_anchors`` round-trips the location."""
        handler.store.upsert_kinds([AliasHandler.spec])
        out = gs.mint_anchors(
            handler.store,
            ["src/a.py", "src/a.py::do_thing", "src/a.py"],
        )
        assert out == {"src/a.py": "an1", "src/a.py::do_thing": "an2"}
        assert gs.mint_anchors(handler.store, ["src/a.py"]) == {"src/a.py": "an1"}
        # a new location allocates past the used numbers
        assert gs.mint_anchors(handler.store, ["src/b.py"]) == {"src/b.py": "an3"}
        assert gs.resolve_anchors(handler.store, {"an1", "an2"}) == {
            "an1": "src/a.py",
            "an2": "src/a.py::do_thing",
        }

    def test_mint_links_the_gripe(self, handler: GripeHandler) -> None:
        """``gripe=`` stamps the provisional pair + link ``put`` adds —
        the alias lands on ``/stale`` once the gripe closes."""
        from precis.store.types import Tag

        handler.store.upsert_kinds([AliasHandler.spec])
        rid = _gripe(handler)
        out = gs.mint_anchors(handler.store, ["src/a.py"], gripe=rid)
        assert out == {"src/a.py": "an1"}
        rows = AliasHandler(hub=handler.hub).lookup("an1")
        assert len(rows) == 1
        payload = rows[0].meta["alias"]
        assert payload["gripe"] == rid
        links = handler.store.links_for(rows[0].id)
        assert any(l.dst_ref_id == rid and l.relation == "related-to" for l in links)
        # closing the gripe surfaces the pair on /stale
        handler.store.add_tag(
            rid, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
        )
        assert "an1" in AliasHandler(hub=handler.hub).get(id="/stale").body

    def test_mint_empty(self, handler: GripeHandler) -> None:
        assert gs.mint_anchors(handler.store, []) == {}
        assert gs.mint_anchors(handler.store, ["", "  "]) == {}

    def test_mint_never_reuses_a_dead_handle(self, handler: GripeHandler) -> None:
        """Regression (review of td655156): allocation is over ALL
        pairs — a retired or expired pair's ``an<N>`` is history, never
        re-minted onto a different location (its ledger cites would
        silently retarget)."""
        handler.store.upsert_kinds([AliasHandler.spec])
        assert gs.mint_anchors(handler.store, ["src/a.py"]) == {"src/a.py": "an1"}
        # Retire an1: its number is spent even though the pair is dead.
        rows = AliasHandler(hub=handler.hub).lookup("an1")
        assert len(rows) == 1
        handler.store.retire_ref(rows[0].id)
        assert gs.mint_anchors(handler.store, ["src/b.py"]) == {"src/b.py": "an2"}
        # An already-expired pair's number is spent the same way.
        handler.store.insert_ref(
            kind="alias",
            slug=None,
            title="an9 ⇄ src/gone.py",
            meta={
                "alias": {
                    "a": "an9",
                    "b": "src/gone.py",
                    "rel": "short-for",
                    "expires_at": "2000-01-01T00:00:00+00:00",
                }
            },
        )
        assert gs.mint_anchors(handler.store, ["src/c.py"]) == {"src/c.py": "an10"}
        # …and a retired pair's LOCATION re-mints fresh, on a new number —
        # resolve still reads live pairs only.
        assert gs.mint_anchors(handler.store, ["src/a.py"]) == {"src/a.py": "an11"}

    def test_mint_allocates_under_concurrency(self, handler: GripeHandler) -> None:
        """Regression (review of td655156): the max-scan and the inserts
        are ONE critical section behind a transaction-scoped advisory
        lock — concurrent minters can never mint the same handle."""
        from concurrent.futures import ThreadPoolExecutor

        handler.store.upsert_kinds([AliasHandler.spec])
        locs = [f"src/mod{i}.py" for i in range(12)]
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(
                ex.map(lambda loc: gs.mint_anchors(handler.store, [loc]), locs)
            )
        handles = [h for r in results for h in r.values()]
        assert len(handles) == len(set(handles)) == len(locs)
        assert {int(h[2:]) for h in handles} == set(range(1, len(locs) + 1))


# ── the fixer's producer path writes handles only ──────────────────


class TestWritebackAnchors:
    def test_writeback_mints_handles_into_the_ledger(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Producer path (review of td655156): ``gripe_writeback``'s
        ``touched=`` mints ``an<N>`` handles first — the ledger entry
        carries handles only, never the raw paths/symbols — and
        ``/match`` resolves them back to the file."""
        import precis.store.store as store_mod
        from precis.fixer import writeback as wb

        monkeypatch.setattr(gs, "route_small", _fake_llm("state: fix landed"))
        handler.store.upsert_kinds([AliasHandler.spec])
        rid = _gripe(handler, "Area: flux parse dies")
        monkeypatch.setattr(store_mod.Store, "connect", lambda *a, **k: handler.store)
        monkeypatch.setattr(handler.store, "close", lambda: None)

        ok = wb.gripe_writeback(
            "postgresql://example/db",
            rid,
            "FIXER (auto): candidate fix built on branch fix/gr7",
            "in_review",
            touched=(
                ["src/precis/flux/parse.py"],
                ["src/precis/flux/parse.py::deflux_core"],
            ),
        )
        assert ok is True
        chunks = handler.store.chunks.list_chunks_for_ref(rid)
        entry = next(c for c in chunks if c.chunk_kind == "gripe_comment")
        assert "\ntouched files: an1" in entry.text
        assert "\nchanged symbols: an2" in entry.text
        assert "src/precis/flux/parse.py" not in entry.text
        assert "deflux_core" not in entry.text
        assert gs.is_touched_entry(entry.text)
        assert gs.stage_of(entry) == "code"
        # the minted pair is linked provisional and resolves
        resolved = gs.resolve_anchors(handler.store, {"an1", "an2"})
        assert resolved["an1"] == "src/precis/flux/parse.py"
        assert resolved["an2"] == "src/precis/flux/parse.py::deflux_core"
        # and /match's ownership list reads the resolved file
        out = handler.get(
            id="/match",
            trace='File "src/precis/flux/parse.py", line 42, in deflux_core',
        ).body
        assert f"gr{rid}" in out
        assert re.search(r"last files entry \d+: src/precis/flux/parse\.py", out)

    def test_writeback_anchorizes_the_outcome_prose(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The outcome head can name locations too (a failed gate's
        tail carries paths) — they mint to ``an<N>`` before the entry
        lands, never raw (review of td655156)."""
        import precis.store.store as store_mod
        from precis.fixer import writeback as wb

        monkeypatch.setattr(gs, "route_small", _fake_llm("state: gate red"))
        handler.store.upsert_kinds([AliasHandler.spec])
        rid = _gripe(handler, "Area: lint gripes")
        monkeypatch.setattr(store_mod.Store, "connect", lambda *a, **k: handler.store)
        monkeypatch.setattr(handler.store, "close", lambda: None)

        ok = wb.gripe_writeback(
            "postgresql://example/db",
            rid,
            "FIXER (auto): build attempt did not land — gate failed:\n"
            "src/precis/flux/parse.py:12: error: bad flux",
        )
        assert ok is True
        chunks = handler.store.chunks.list_chunks_for_ref(rid)
        entry = next(c for c in chunks if c.chunk_kind == "gripe_comment")
        assert "src/precis/flux/parse.py" not in entry.text
        assert re.search(r"\ban\d+\b", entry.text) is not None
        assert gs.resolve_anchors(handler.store, {"an1"}) == {
            "an1": "src/precis/flux/parse.py"
        }


# ── anchorize: free text → handles ─────────────────────────────────


class TestAnchorize:
    def test_extract_locations(self) -> None:
        """``path::qual`` tokens and bare paths are locations; a bare
        symbol or an existing handle is prose."""
        assert gs.extract_locations(
            "suspect src/a.py::run drops frames; also src/b.py, "
            "then src/a.py::run again."
        ) == ["src/a.py::run", "src/b.py"]
        assert gs.extract_locations("off-by-one in page_slice") == []
        assert gs.extract_locations("see an7, entry 3") == []

    def test_extract_locations_covers_repo_extensions(self) -> None:
        """``_FILE_RE`` names the repo's own code/doc shapes too —
        ``.j2``/``.sh``/``.plist``/``.cad`` paths are locations, or a
        stage entry naming one would land it raw (review of td655156)."""
        out = gs.extract_locations(
            "broke deploy/roles/x/tasks/main.j2, scripts/run.sh, "
            "a/b.service, app/Main.plist and geom/case.cad — "
            "also docs/roadmap.md and data/x.trig"
        )
        assert out == [
            "deploy/roles/x/tasks/main.j2",
            "scripts/run.sh",
            "a/b.service",
            "app/Main.plist",
            "geom/case.cad",
            "docs/roadmap.md",
            "data/x.trig",
        ]

    def test_anchorize_mints_and_substitutes(self, handler: GripeHandler) -> None:
        """Free text's locations mint ``an<N>`` handles and land as
        handles — the raw path never reaches the ledger (review FIX of
        td655156)."""
        handler.store.upsert_kinds([AliasHandler.spec])
        rid = _gripe(handler)
        out = gs.anchorize_text(
            handler.store,
            "suspect src/a.py::run drops frames in src/b.py.",
            gripe=rid,
        )
        assert out == "suspect an1 drops frames in an2."
        assert gs.resolve_anchors(handler.store, {"an1", "an2"}) == {
            "an1": "src/a.py::run",
            "an2": "src/b.py",
        }
        # find-or-mint: a repeat mention reuses the live pair, and the
        # anchored text is itself location-free
        assert gs.anchorize_text(handler.store, out) == out

    def test_anchorize_redacts_what_the_mint_missed(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A location with no minted handle redacts — a raw path may
        NEVER substitute."""
        handler.store.upsert_kinds([AliasHandler.spec])
        monkeypatch.setattr(gs, "mint_anchors", lambda *a, **k: {})
        out = gs.anchorize_text(handler.store, "see src/a.py::run and src/b.py")
        assert "src/a.py" not in out and "src/b.py" not in out
        assert out == (f"see {gs.UNMINTED_LOCATION} and {gs.UNMINTED_LOCATION}")

    def test_utils_seam_redacts_on_mint_fault(
        self, handler: GripeHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pipeline seam is fail-soft: a mint fault redacts the
        locations rather than landing raw text."""
        from precis.utils import gripe_summary as ugs

        def _boom(*_a: object, **_k: object) -> str:
            raise RuntimeError("mint is down")

        monkeypatch.setattr(gs, "anchorize_text", _boom)
        out = ugs.anchorize(handler.store, "suspect src/a.py::run")
        assert out == f"suspect {gs.UNMINTED_LOCATION}"
