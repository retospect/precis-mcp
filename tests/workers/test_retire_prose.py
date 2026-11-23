"""retire_prose — the retire flag's reword-in-place mode (gr693791 residual 2).

DB-backed (real ``refs``/``chunks`` via the ``store`` fixture); no LLM.
Covers the double gate, the two safe cite shapes (trailing cite, marker-own
sentence), the refused shapes (mid-sentence marker, prose after the
marker), idempotency, the worklist-tag drain, and the job glue's
``mode='retire-prose'`` loop.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.workers.hub_refine import (
    _META_REGROUND_VERDICT,
    _REGROUND_OK_NS,
    _REGROUND_OK_VALUE,
    _REGROUND_RETIRE_NS,
    _REGROUND_RETIRE_VALUE,
)
from precis.workers.retire_prose import (
    PROSE_PASS_REWORD_APPLIED,
    _reword_sentence,
    apply_reword_in_place,
)

_REWORD = "The supported claim, stated within what the evidence shows"

_DRAFT_N = 0


def _seed_retired_hub(store: Any, *, reword: str | None = _REWORD) -> int:
    hub = mint_hub(
        store, CanonicalClaim(sentence="The unsupported old claim.", scope={})
    )
    store.update_ref(
        hub,
        meta_patch={
            _META_REGROUND_VERDICT: {
                "verdict": "retire",
                "sha": "abc123",
                "at": "2026-11-23T00:00:00+00:00",
                "reword": reword,
            }
        },
    )
    store.add_tag(hub, Tag.closed(_REGROUND_RETIRE_NS, _REGROUND_RETIRE_VALUE))
    return hub


def _vet(store: Any, hub: int) -> None:
    store.add_tag(hub, Tag.closed(_REGROUND_OK_NS, _REGROUND_OK_VALUE))


def _draft_with(store: Any, text: str) -> tuple[int, str]:
    global _DRAFT_N
    _DRAFT_N += 1
    ref, _title = store.drafts.create_draft(
        name=f"retire-draft-{_DRAFT_N}", title="Retire draft"
    )
    chunk = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text=text, at={"last": True}
    )[0]
    return ref.id, chunk.handle


def _chunk_text_by_handle(store: Any, handle: str) -> str:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT text FROM chunks WHERE handle = %s ORDER BY chunk_id DESC LIMIT 1",
            (handle,),
        ).fetchone()[0]


def _verdict(store: Any, hub: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'reground_verdict' FROM refs WHERE ref_id = %s", (hub,)
        ).fetchone()
    return dict(row[0] or {})


def _has_tag(store: Any, hub: int, ns: str, value: str) -> bool:
    with store.pool.connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
                "WHERE rt.ref_id = %s AND t.namespace = %s AND t.value = %s",
                (hub, ns, value),
            ).fetchone()
            is not None
        )


# ── the splice, pure ────────────────────────────────────────────────────


def test_trailing_cite_shape() -> None:
    new, reason = _reword_sentence(
        "The old claim holds everywhere [fi42]. Next sentence stays.", 42, _REWORD
    )
    assert reason is None
    assert (
        new == "The supported claim, stated within what the evidence shows [fi42]. "
        "Next sentence stays."
    )


def test_marker_own_sentence_shape() -> None:
    new, reason = _reword_sentence(
        "The old claim holds everywhere. [fi42]. Tail sentence.", 42, _REWORD
    )
    assert reason is None
    assert (
        new == "The supported claim, stated within what the evidence shows. "
        "[fi42]. Tail sentence."
    )


def test_reword_with_own_punctuation_keeps_it() -> None:
    new, reason = _reword_sentence("Old claim [fi7].", 7, "Reworded claim.")
    assert reason is None
    assert new == "Reworded claim. [fi7]."


def test_mid_sentence_marker_is_refused() -> None:
    new, reason = _reword_sentence(
        "As shown [fi42], the value holds across samples.", 42, _REWORD
    )
    assert new is None
    assert "not sentence-final" in (reason or "")


def test_prose_after_marker_is_refused() -> None:
    new, reason = _reword_sentence(
        "Old claim [fi42] and further unsupported prose.", 42, _REWORD
    )
    assert new is None
    assert "not sentence-final" in (reason or "")


def test_marker_only_sentence_at_start_is_refused() -> None:
    new, reason = _reword_sentence("[fi42]. The old claim follows here.", 42, _REWORD)
    assert new is None
    assert "chunk start" in (reason or "")


@pytest.mark.parametrize("cite", ["[fi9]", "[dc9]", "[pc9]", "[pc9, fi88]"])
def test_other_cite_in_replaced_trailing_span_is_refused(cite: str) -> None:
    new, reason = _reword_sentence(
        f"Old claim, see {cite} and more [fi42].", 42, _REWORD
    )
    assert new is None
    assert "another cite" in (reason or "")


@pytest.mark.parametrize("cite", ["[fi9]", "[dc9]", "[pc9]", "[pc9, fi88]"])
def test_other_cite_in_replaced_previous_sentence_is_refused(cite: str) -> None:
    new, reason = _reword_sentence(f"Old claim {cite}. [fi42].", 42, _REWORD)
    assert new is None
    assert "another cite" in (reason or "")


def test_et_al_is_not_a_sentence_boundary() -> None:
    new, reason = _reword_sentence(
        "Smith et al. found the old claim [fi42]. Tail.", 42, "Reworded claim."
    )
    assert reason is None
    assert new == "Reworded claim. [fi42]. Tail."


def test_eg_and_fig_are_not_sentence_boundaries() -> None:
    new, reason = _reword_sentence(
        "Rates differ, e.g. in Fig. 3 and p. 5 [fi42].", 42, "Reworded claim."
    )
    assert reason is None
    assert new == "Reworded claim. [fi42]."


# ── the apply, DB-backed ────────────────────────────────────────────────


def test_apply_rewords_trailing_cite_and_drains_the_worklist(store: Any) -> None:
    hub = _seed_retired_hub(store)
    _vet(store, hub)
    _draft, handle = _draft_with(store, "The old claim holds everywhere [fiX].")
    # rewrite the placeholder into a real marker
    _cite_draft(store, handle, f"The old claim holds everywhere [fi{hub}].")

    result = apply_reword_in_place(store, hub, authorized=True, set_by="test")

    assert len(result.reworded) == 1
    assert not result.skipped
    assert (
        _chunk_text_by_handle(store, handle)
        == "The supported claim, stated within what the evidence shows "
        f"[fi{hub}]."
    )
    assert _verdict(store, hub)["prose_pass"] == PROSE_PASS_REWORD_APPLIED
    assert not _has_tag(store, hub, _REGROUND_RETIRE_NS, _REGROUND_RETIRE_VALUE)
    # The vetting tag stays — it is the human's, not the worklist's.
    assert _has_tag(store, hub, _REGROUND_OK_NS, _REGROUND_OK_VALUE)


def _cite_draft(store: Any, handle: str, text: str) -> None:
    with store.pool.connection() as conn:
        store.drafts.edit_text_in_tx(conn, handle, text, source={"kind": "test-seed"})
        conn.commit()


def test_apply_is_idempotent(store: Any) -> None:
    hub = _seed_retired_hub(store)
    _vet(store, hub)
    _draft, handle = _draft_with(store, f"Old claim [fi{hub}].")
    first = apply_reword_in_place(store, hub, authorized=True)
    assert len(first.reworded) == 1
    text_after_first = _chunk_text_by_handle(store, handle)
    second = apply_reword_in_place(store, hub, authorized=True)
    assert second.reworded == ()
    assert second.skipped[0].reason == "reword already applied"
    assert _chunk_text_by_handle(store, handle) == text_after_first


def test_apply_refused_without_run_authorization(store: Any) -> None:
    hub = _seed_retired_hub(store)
    _vet(store, hub)
    _draft_with(store, f"Old claim [fi{hub}].")
    with pytest.raises(BadInput, match="not authorized"):
        apply_reword_in_place(store, hub, authorized=False)


def test_apply_refused_without_vetting_tag(store: Any) -> None:
    hub = _seed_retired_hub(store)
    _draft_with(store, f"Old claim [fi{hub}].")
    with pytest.raises(BadInput, match="vetting tag"):
        apply_reword_in_place(store, hub, authorized=True)


def test_apply_refused_without_retire_verdict(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence="A supportable claim.", scope={}))
    _vet(store, hub)
    with pytest.raises(BadInput, match="no retire verdict"):
        apply_reword_in_place(store, hub, authorized=True)


def test_apply_refused_without_proposal(store: Any) -> None:
    hub = _seed_retired_hub(store, reword=None)
    _vet(store, hub)
    with pytest.raises(BadInput, match="no proposed reword"):
        apply_reword_in_place(store, hub, authorized=True)


def test_apply_skips_unsafe_shape_but_rewords_the_rest(store: Any) -> None:
    hub = _seed_retired_hub(store)
    _vet(store, hub)
    _draft_with(store, f"As shown [fi{hub}], values hold across samples.")
    _draft2, handle2 = _draft_with(store, f"Old claim [fi{hub}].")

    result = apply_reword_in_place(store, hub, authorized=True)

    assert len(result.reworded) == 1
    assert len(result.skipped) == 1
    assert "not sentence-final" in result.skipped[0].reason
    assert (
        _chunk_text_by_handle(store, handle2)
        == "The supported claim, stated within what the evidence shows "
        f"[fi{hub}]."
    )
    # The unsafe paragraph stays on the worklist; the pass is only partial.
    assert _verdict(store, hub)["prose_pass"] == "reword-partial"
    assert _has_tag(store, hub, _REGROUND_RETIRE_NS, _REGROUND_RETIRE_VALUE)


def test_apply_keeps_repeat_hub_citations_on_the_worklist(store: Any) -> None:
    hub = _seed_retired_hub(store)
    _vet(store, hub)
    text = f"Old claim [fi{hub}]. Another assertion [pc9, fi{hub}]."
    _draft, handle = _draft_with(store, text)

    result = apply_reword_in_place(store, hub, authorized=True)

    assert not result.reworded
    assert [skip.reason for skip in result.skipped] == [
        "multiple markers cite this hub in the chunk"
    ]
    assert _chunk_text_by_handle(store, handle) == text
    assert "prose_pass" not in _verdict(store, hub)
    assert _has_tag(store, hub, _REGROUND_RETIRE_NS, _REGROUND_RETIRE_VALUE)


def test_apply_writes_a_reground_log_entry(store: Any) -> None:
    hub = _seed_retired_hub(store)
    _vet(store, hub)
    _draft_with(store, f"Old claim [fi{hub}].")
    apply_reword_in_place(store, hub, authorized=True)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'reground_log' FROM refs WHERE ref_id = %s", (hub,)
        ).fetchone()
    entries = list(row[0] or [])
    assert any(e.get("action") == "reword-applied" for e in entries)
