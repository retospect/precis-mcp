"""The allow-listed paper worker-retry clear door (gr514347)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.workers.retry_stamps import clearable_stamps
from tests.workers._helpers import seed_ref


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


META_FAILED_KEY = "s2_enrich_failed"
META_DONE_KEY = "s2_enriched_at"


def _stamp(store: Store, ref_id: int, key: str) -> None:
    store.update_ref(ref_id, meta_patch={key: True})


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
        for key in (META_FAILED_KEY, META_DONE_KEY):
            assert key in opts
        assert "abstract" in str(exc.value)

    @pytest.mark.parametrize(
        "key", ["context_sentence_failed", "context_sentence_no_abstract"]
    )
    def test_retired_stamp_is_refused(
        self, store: Store, handler: PaperHandler, key: str
    ) -> None:
        ref_id = seed_ref(store, title="Legacy stamped paper")
        _stamp(store, ref_id, key)
        with pytest.raises(BadInput, match=key) as exc:
            handler.edit(id=ref_id, args={"clear_stamp": key})
        assert key not in (exc.value.options or [])
        assert _meta(store, ref_id)[key] is True

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
        assert allowed[META_FAILED_KEY] == "stub_rank"
        assert allowed[META_DONE_KEY] == "stub_rank"


class TestClear:
    def test_a_list_of_stamps_clears_in_one_call(
        self, store: Store, handler: PaperHandler
    ) -> None:
        ref_id = seed_ref(store, title="A DFT study")
        _stamp(store, ref_id, META_FAILED_KEY)
        _stamp(store, ref_id, META_DONE_KEY)
        resp = handler.edit(
            id=ref_id,
            args={"clear_stamp": [META_FAILED_KEY, META_DONE_KEY]},
        )
        assert f"{META_FAILED_KEY} (stub_rank): 1 cleared" in resp.body
        assert f"{META_DONE_KEY} (stub_rank): 1 cleared" in resp.body

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
            _stamp(store, ref_id, META_DONE_KEY)
        ids: list[str | int] = [*stamped, clean]

        resp = handler.edit(id=ids, args={"clear_stamp": META_DONE_KEY})
        assert "2 cleared" in resp.body
        assert "1 already clear" in resp.body
        assert all(META_DONE_KEY not in _meta(store, rid) for rid in [*stamped, clean])
        # Events land per ref that actually cleared.
        assert [
            e for e in store.events_for(clean) if e.event == "metadata_edited"
        ] == []
        for ref_id in stamped:
            events = [
                e for e in store.events_for(ref_id) if e.event == "metadata_edited"
            ]
            assert len(events) == 1
            assert events[0].payload["cleared"] == [META_DONE_KEY]

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
