"""Finding occurrence counts through the draft get door."""

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store

pytestmark = pytest.mark.db


def test_finding_counts_split_by_heading(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_AGENT_TABLE_FORMAT", "toon")
    ref, _ = store.drafts.create_draft(name="census", title="Census")
    [section] = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="Selected [fi12]"
    )
    [nested] = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="Nested", at={"into": section.dc}
    )
    [paragraph] = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="[fi12] [fi12] [fi123] fi12 [fi12x]",
        at={"into": nested.dc},
    )
    [retired] = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="[fi12]", at={"into": section.dc}
    )
    store.drafts.retire_chunk(retired.dc)
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="[fi12] [fi123]"
    )
    request = {"kind": "draft", "id": "census", "view": "cites"}
    handles = ["fi12", "fi123", "fi999", "fi12"]
    split = runtime_with_store.dispatch(
        "get", {**request, "args": {"handles": handles, "by_section": section.dc}}
    )
    assert "handle\ttotal\tin_section\telsewhere" in split
    assert "fi12\t4\t3\t1" in split
    assert "fi123\t2\t1\t1" in split
    assert "fi999\t0\t0\t0" in split
    assert split.count("fi12\t") == 1

    totals = runtime_with_store.dispatch(
        "get", {**request, "args": {"handles": handles}}
    )
    assert "handle\ttotal" in totals
    assert "fi12\t4" in totals
    assert "in_section" not in totals

    _, foreign = store.drafts.create_draft(name="other-census", title="Other")
    for invalid in (paragraph.dc, retired.dc, foreign.dc):
        error, is_error = runtime_with_store.dispatch_with_status(
            "get", {**request, "args": {"handles": handles, "by_section": invalid}}
        )
        assert is_error
        assert "live heading in this draft" in error


@pytest.mark.parametrize("args", [{}, {"handles": []}, {"handles": ["[fi12]"]}])
def test_finding_counts_validate_handles(
    runtime_with_store: PrecisRuntime, store: Store, args: dict[str, list[str]]
) -> None:
    store.drafts.create_draft(name="census", title="Census")
    error, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "draft", "id": "census", "view": "cites", "args": args}
    )
    assert is_error
    assert "nonempty handles list" in error
