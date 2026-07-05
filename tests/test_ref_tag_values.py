"""Store-level test for the batched flag-state probe.

``ref_tag_values`` backs the reading-intent flag buttons on list views
(read-later / must-read / skim). One query maps a page of ref_ids to
the subset of requested OPEN values each carries — the N+1 avoidance.
"""

from __future__ import annotations

from precis.store import Tag


def test_ref_tag_values_batches_open_flag_state(store) -> None:
    a = store.insert_ref(kind="paper", slug="paper-a", title="A")
    b = store.insert_ref(kind="paper", slug="paper-b", title="B")
    c = store.insert_ref(kind="paper", slug="paper-c", title="C")

    store.add_tag(a.id, Tag.open("read-later"))
    store.add_tag(a.id, Tag.open("must-read"))
    store.add_tag(b.id, Tag.open("skim"))
    # A non-flag OPEN tag on b must not leak into the flag probe.
    store.add_tag(b.id, Tag.open("topic-something"))

    got = store.ref_tag_values(
        [a.id, b.id, c.id],
        "OPEN",
        ["read-later", "must-read", "skim"],
    )

    assert got[a.id] == {"read-later", "must-read"}
    assert got[b.id] == {"skim"}
    # c carries no flags → absent from the map (not an empty set).
    assert c.id not in got


def test_ref_tag_values_empty_inputs(store) -> None:
    assert store.ref_tag_values([], "OPEN", ["read-later"]) == {}
    ref = store.insert_ref(kind="paper", slug="paper-x", title="X")
    assert store.ref_tag_values([ref.id], "OPEN", []) == {}
