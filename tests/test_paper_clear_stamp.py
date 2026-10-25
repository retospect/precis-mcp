"""``edit(args={'clear_stamp': ...})`` — the worker-retry re-arm door
(gr514347).

The context_sentence pass converges a paper by stamping
``refs.meta['context_sentence_failed']`` / ``['context_sentence_no_abstract']``;
until this door, un-stamping a cohort needed raw
``UPDATE refs SET meta = meta - key`` prod SQL. These tests pin: the
allow-list refusal naming allowed keys, the cleared/already-clear count
shape on one ref and on a batch, the ``metadata_edited`` ref_event, and —
the acceptance criterion — that clearing the stamp makes the worker's own
selection query pick the ref up again.

Since gr514381 the stamps are not permanent gates: ``_claim`` keeps
selecting ``no_abstract`` rows (self-heal) and any ``failed`` stamp older
than the current pass version (a legacy bare ``true`` included) earns one
re-trial. Only a ``failed`` stamp AT the current version gates — that is
the shape the reclaim tests below stamp.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.workers.context_sentence import (
    _PASS_VERSION,
    META_FAILED_KEY,
    META_NO_ABSTRACT_KEY,
    _claim,
)
from precis.workers.retry_stamps import clearable_stamps
from tests.workers._helpers import seed_ref


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _claim_ids(store: Store, ref_id: int) -> list[int]:
    """The worker's own selection query, restricted to this ref."""
    with store.pool.connection() as conn:
        return [row[0] for row in _claim(conn, limit=5, ref_ids=[ref_id])]


def _stamp(store: Store, ref_id: int, key: str) -> None:
    """A stamp that actually GATES ``_claim``: a bare ``true`` reads as
    version 0 and earns a re-trial either way (gr514381) — only a
    current-version stamp gates, and for ``no_abstract`` that means
    ``{"v", "at"}`` with ``at`` newer than the ref's ``updated_at``."""
    value: object = {"v": _PASS_VERSION}
    if key == META_NO_ABSTRACT_KEY:
        value = {"v": _PASS_VERSION, "at": "9999-12-31T00:00:00Z"}
    store.update_ref(ref_id, meta_patch={key: value})


def _meta(store: Store, ref_id: int) -> dict:
    return dict(store.fetch_refs_by_ids([ref_id])[ref_id].meta or {})


class TestAllowList:
    def test_a_disallowed_key_is_refused_naming_the_allowed_set(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = seed_ref(store, title="stamped paper")
        with pytest.raises(BadInput) as exc:
            handler.edit(id=ref_id, args={"clear_stamp": "abstract"})
        opts = exc.value.options or []
        for key in (META_FAILED_KEY, META_NO_ABSTRACT_KEY):
            assert key in opts
        assert "abstract" in str(exc.value)
        # Nothing written.
        assert "context_sentence_failed" not in _meta(store, ref_id)

    def test_an_allowlisted_key_not_written_by_any_worker_is_still_refused(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """The door is not a generic meta-delete: an arbitrary ``refs.meta``
        key name (even a plausible-looking one) is refused."""
        ref_id = seed_ref(store, title="stamped paper")
        with pytest.raises(BadInput):
            handler.edit(id=ref_id, args={"clear_stamp": "fetch_oa_attempts"})

    def test_the_registered_allow_list_is_what_the_worker_declares(self) -> None:
        allowed = clearable_stamps()
        assert allowed[META_FAILED_KEY] == "context_sentence"
        assert allowed[META_NO_ABSTRACT_KEY] == "context_sentence"


class TestClearAndReclaim:
    def test_clearing_no_abstract_stamp_makes_the_ref_claimable_again(
        self, store: Store, handler: PaperHandler
    ) -> None:
        """A current-version no_abstract stamp gates ``_claim`` until the
        ref changes (gr514381) — clearing it re-arms the selection."""
        ref_id = seed_ref(store, title="A DFT study")
        _stamp(store, ref_id, META_NO_ABSTRACT_KEY)
        assert _claim_ids(store, ref_id) == []

        resp = handler.edit(id=ref_id, args={"clear_stamp": META_NO_ABSTRACT_KEY})
        assert "1 cleared" in resp.body
        assert _claim_ids(store, ref_id) == [ref_id]
        assert META_NO_ABSTRACT_KEY not in _meta(store, ref_id)
        events = [e for e in store.events_for(ref_id) if e.event == "metadata_edited"]
        assert len(events) == 1
        assert events[0].payload["cleared"] == [META_NO_ABSTRACT_KEY]

    def test_clearing_failed_stamp_reclaims(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = seed_ref(store, title="A DFT study")
        _stamp(store, ref_id, META_FAILED_KEY)
        assert _claim_ids(store, ref_id) == []
        handler.edit(id=ref_id, args={"clear_stamp": META_FAILED_KEY})
        assert _claim_ids(store, ref_id) == [ref_id]

    def test_a_list_of_stamps_clears_in_one_call(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = seed_ref(store, title="A DFT study")
        _stamp(store, ref_id, META_FAILED_KEY)
        _stamp(store, ref_id, META_NO_ABSTRACT_KEY)
        resp = handler.edit(
            id=ref_id,
            args={"clear_stamp": [META_FAILED_KEY, META_NO_ABSTRACT_KEY]},
        )
        assert f"{META_FAILED_KEY} (context_sentence): 1 cleared" in resp.body
        assert f"{META_NO_ABSTRACT_KEY} (context_sentence): 1 cleared" in resp.body
        assert _claim_ids(store, ref_id) == [ref_id]

    def test_clearing_a_stamp_that_was_never_set_is_an_honest_noop(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = seed_ref(store, title="A DFT study")
        resp = handler.edit(id=ref_id, args={"clear_stamp": META_FAILED_KEY})
        assert "0 cleared" in resp.body
        assert "1 already clear" in resp.body
        # No fabricated edit event for a no-op clear.
        assert [
            e for e in store.events_for(ref_id) if e.event == "metadata_edited"
        ] == []


class TestBatch:
    def test_id_list_counts_cleared_and_already_clear(
        self, store: Store, handler: PaperHandler
    ) -> None:
        stamped = [seed_ref(store, title=f"paper {i}") for i in range(2)]
        clean = seed_ref(store, title="clean paper")
        for ref_id in stamped:
            _stamp(store, ref_id, META_NO_ABSTRACT_KEY)
        ids: list[str | int] = [*stamped, clean]

        resp = handler.edit(id=ids, args={"clear_stamp": META_NO_ABSTRACT_KEY})
        assert "2 cleared" in resp.body
        assert "1 already clear" in resp.body
        for rid in [*stamped, clean]:
            assert _claim_ids(store, rid) == [rid]
        # Events land per ref that actually cleared.
        assert [
            e for e in store.events_for(clean) if e.event == "metadata_edited"
        ] == []
        for ref_id in stamped:
            events = [
                e for e in store.events_for(ref_id) if e.event == "metadata_edited"
            ]
            assert len(events) == 1
            assert events[0].payload["cleared"] == [META_NO_ABSTRACT_KEY]

    def test_dry_run_reports_but_writes_nothing(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = seed_ref(store, title="A DFT study")
        _stamp(store, ref_id, META_FAILED_KEY)
        resp = handler.edit(
            id=ref_id, args={"clear_stamp": META_FAILED_KEY}, dry_run=True
        )
        assert "DRY RUN" in resp.body
        assert "1 would clear" in resp.body
        assert META_FAILED_KEY in _meta(store, ref_id)
        assert _claim_ids(store, ref_id) == []
        assert [
            e for e in store.events_for(ref_id) if e.event == "metadata_edited"
        ] == []


class TestRefusals:
    def test_clear_stamp_refuses_combination_with_other_edit_fields(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = seed_ref(store, title="A DFT study")
        _stamp(store, ref_id, META_FAILED_KEY)
        with pytest.raises(BadInput):
            handler.edit(
                id=ref_id,
                title="renamed",
                args={"clear_stamp": META_FAILED_KEY},
            )
        assert META_FAILED_KEY in _meta(store, ref_id)

    def test_clear_stamp_with_a_missing_id_is_refused(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        out = runtime_with_store.dispatch(
            "edit",
            {"kind": "paper", "__extras__": {"clear_stamp": META_FAILED_KEY}},
        )
        assert "[error:BadInput]" in out

    def test_other_edit_fields_still_refuse_a_list_id(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = seed_ref(store, title="A DFT study")
        with pytest.raises(BadInput):
            handler.edit(id=[ref_id], title="renamed")
