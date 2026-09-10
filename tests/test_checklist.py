"""Contract tests for :class:`precis.handlers.checklist.ChecklistHandler`
(``checklist`` kind, docs/backlog/checklist-kind.md slice 1).

Covers every slice-1 acceptance-criteria bullet: put creates a local
checklist + items; edit's add_item/retire_item/verdict/add_note/
remove_note/assign/unassign ops; the shipped-rev edit rejection; the
append-only verdict ledger; the three-valued per-target status view
(not checked / stale / current); "no checklist assigned" honesty.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.handlers.checklist import ChecklistHandler
from precis.store import Store


def _handler(store: Any) -> ChecklistHandler:
    return ChecklistHandler(hub=Hub(store=store))


def _make_target(store: Store, slug: str = "target-board") -> str:
    """A generic slug-addressed ref to use as a checklist target —
    checklist targets are kind-agnostic, so any existing slug kind works."""
    store.insert_ref(kind="paper", slug=slug, title="Target board")
    return f"paper:{slug}"


_ITEM = {
    "name": "item-one",
    "phase": "setup",
    "severity": "blocking",
    "decidability": "judgment",
    "prevents": "the thing breaks if this is skipped",
}


class TestInit:
    def test_missing_store_raises_init_error(self) -> None:
        with pytest.raises(InitError):
            ChecklistHandler(hub=Hub(store=None))


# ── put ──────────────────────────────────────────────────────────────


class TestPut:
    def test_put_with_no_id_is_rejected(self, store: Store) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.put(items=[_ITEM])

    def test_put_creates_local_checklist_with_items(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="my-checklist", items=[_ITEM])
        checklist = store.checklist_get("my-checklist")
        assert checklist is not None
        assert checklist["origin"] == "local"
        item = store.checklist_item_current(checklist["id"], "item-one")
        assert item is not None
        assert item["rev"] == 1
        assert item["origin"] == "local"
        assert item["prevents"] == _ITEM["prevents"]

    def test_put_with_empty_items_creates_empty_checklist(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="empty-checklist")
        checklist = store.checklist_get("empty-checklist")
        assert checklist is not None
        assert store.checklist_items_current(checklist["id"]) == []

    def test_put_duplicate_name_rejected(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="dupe-checklist", items=[_ITEM])
        with pytest.raises(BadInput):
            h.put(id="dupe-checklist", items=[_ITEM])

    def test_put_item_without_prevents_is_rejected(self, store: Store) -> None:
        h = _handler(store)
        bad_item = {"name": "no-prevents"}
        with pytest.raises(BadInput):
            h.put(id="cargo-cult-checklist", items=[bad_item])
        assert store.checklist_get("cargo-cult-checklist") is None

    def test_put_item_with_bad_severity_is_rejected(self, store: Store) -> None:
        h = _handler(store)
        bad_item = {**_ITEM, "severity": "urgent"}
        with pytest.raises(BadInput):
            h.put(id="bad-severity-checklist", items=[bad_item])


# ── edit: add_item / retire_item / shipped rejection ───────────────────


class TestEditItems:
    def test_add_item_creates_new_item(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[])
        h.edit(
            id="cl",
            op="add_item",
            item="new-item",
            prevents="a failure this catches",
        )
        checklist = store.checklist_get("cl")
        assert checklist is not None
        item = store.checklist_item_current(checklist["id"], "new-item")
        assert item is not None
        assert item["rev"] == 1
        assert item["origin"] == "local"

    def test_add_item_revs_existing_local_item(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        h.edit(id="cl", op="add_item", item="item-one", body="new instructions")
        checklist = store.checklist_get("cl")
        assert checklist is not None
        item = store.checklist_item_current(checklist["id"], "item-one")
        assert item is not None
        assert item["rev"] == 2
        assert item["body"] == "new instructions"
        # unspecified fields carried forward from the prior rev
        assert item["prevents"] == _ITEM["prevents"]

    def test_add_item_missing_prevents_on_brand_new_item_rejected(
        self, store: Store
    ) -> None:
        h = _handler(store)
        h.put(id="cl", items=[])
        with pytest.raises(BadInput):
            h.edit(id="cl", op="add_item", item="brand-new")

    def test_retire_item(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        h.edit(id="cl", op="retire_item", item="item-one")
        checklist = store.checklist_get("cl")
        assert checklist is not None
        assert store.checklist_item_current(checklist["id"], "item-one") is None

    def test_retire_nonexistent_item_raises_not_found(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[])
        with pytest.raises(NotFound):
            h.edit(id="cl", op="retire_item", item="ghost")

    def test_edit_on_shipped_item_rejected_with_hint(self, store: Store) -> None:
        h = _handler(store)
        checklist = store.checklist_create(name="shipped-cl", origin="shipped")
        store.checklist_item_add_rev(
            checklist_id=checklist["id"],
            name="shipped-item",
            rev=1,
            phase=None,
            severity="blocking",
            decidability="judgment",
            prevents="a shipped failure",
            applies=None,
            body=None,
            origin="shipped",
        )
        with pytest.raises(BadInput, match="shipped"):
            h.edit(
                id="shipped-cl",
                op="add_item",
                item="shipped-item",
                body="trying to rewrite it",
            )
        with pytest.raises(BadInput, match="shipped"):
            h.edit(id="shipped-cl", op="retire_item", item="shipped-item")
        # unchanged
        current = store.checklist_item_current(checklist["id"], "shipped-item")
        assert current is not None
        assert current["rev"] == 1

    def test_edit_unknown_op_rejected(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[])
        with pytest.raises(BadInput):
            h.edit(id="cl", op="bogus")

    def test_edit_unknown_checklist_raises_not_found(self, store: Store) -> None:
        h = _handler(store)
        with pytest.raises(NotFound):
            h.edit(id="does-not-exist", op="add_item", item="x", prevents="y")


# ── edit: verdict (append-only ledger) ─────────────────────────────────


class TestVerdict:
    def test_record_verdict_pins_rev_and_fingerprint(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        h.edit(
            id="cl",
            op="verdict",
            target=target,
            item="item-one",
            verdict="pass",
            fingerprint="fp-1",
            checked_by="agent-x",
        )
        checklist = store.checklist_get("cl")
        assert checklist is not None
        ref = store.get_ref(kind="paper", id="target-board")
        assert ref is not None
        v = store.checklist_verdict_latest(
            target_ref_id=ref.id, checklist_id=checklist["id"], item_name="item-one"
        )
        assert v is not None
        assert v["item_rev"] == 1
        assert v["fingerprint"] == "fp-1"
        assert v["verdict"] == "pass"
        assert v["checked_by"] == "agent-x"

    def test_verdict_bad_value_rejected(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        with pytest.raises(BadInput):
            h.edit(
                id="cl", op="verdict", target=target, item="item-one", verdict="maybe"
            )

    def test_second_verdict_is_append_only_old_row_retained(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        ref = store.get_ref(kind="paper", id="target-board")
        assert ref is not None
        checklist = store.checklist_get("cl")
        assert checklist is not None

        h.edit(id="cl", op="verdict", target=target, item="item-one", verdict="fail")
        h.edit(id="cl", op="verdict", target=target, item="item-one", verdict="pass")

        latest = store.checklist_verdict_latest(
            target_ref_id=ref.id, checklist_id=checklist["id"], item_name="item-one"
        )
        assert latest is not None
        assert latest["verdict"] == "pass"

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT verdict FROM checklist_verdicts WHERE target_ref_id = %s "
                "AND checklist_id = %s AND item_name = %s ORDER BY id",
                (ref.id, checklist["id"], "item-one"),
            ).fetchall()
        assert [r[0] for r in rows] == ["fail", "pass"]


# ── get: three-valued status rendering ─────────────────────────────────


class TestStatusView:
    def test_unassigned_target_renders_no_checklist_assigned(
        self, store: Store
    ) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        resp = h.get(id="cl", target=target)
        assert "no checklist assigned" in resp.body

    def test_assigned_no_verdict_renders_not_checked(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        h.edit(id="cl", op="assign", target=target)
        resp = h.get(id="cl", target=target)
        assert "not checked" in resp.body

    def test_stale_when_item_rev_behind_current(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        h.edit(id="cl", op="assign", target=target)
        h.edit(id="cl", op="verdict", target=target, item="item-one", verdict="pass")
        # revise the item after the verdict was recorded
        h.edit(id="cl", op="add_item", item="item-one", body="revised instructions")
        resp = h.get(id="cl", target=target)
        assert "stale" in resp.body
        assert "item revised" in resp.body

    def test_stale_when_fingerprint_disagrees(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        h.edit(id="cl", op="assign", target=target)
        h.edit(
            id="cl",
            op="verdict",
            target=target,
            item="item-one",
            verdict="pass",
            fingerprint="fp-old",
        )
        resp = h.get(id="cl", target=target, fingerprint="fp-new")
        assert "stale" in resp.body
        assert "target changed" in resp.body

    def test_current_verdict_renders_when_rev_and_fingerprint_match(
        self, store: Store
    ) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        h.edit(id="cl", op="assign", target=target)
        h.edit(
            id="cl",
            op="verdict",
            target=target,
            item="item-one",
            verdict="pass",
            fingerprint="fp-1",
        )
        resp = h.get(id="cl", target=target, fingerprint="fp-1")
        assert "not checked" not in resp.body
        assert "stale" not in resp.body
        assert "pass" in resp.body

    def test_no_fingerprint_supplied_never_forces_stale(self, store: Store) -> None:
        """Fingerprints are opaque and comparison-optional in slice 1: a
        caller that never supplies one must never see a spurious stale."""
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        h.edit(id="cl", op="assign", target=target)
        h.edit(
            id="cl",
            op="verdict",
            target=target,
            item="item-one",
            verdict="pass",
            fingerprint="fp-1",
        )
        resp = h.get(id="cl", target=target)
        assert "stale" not in resp.body

    def test_unassign_then_get_reverts_to_no_checklist_assigned(
        self, store: Store
    ) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        h.edit(id="cl", op="assign", target=target)
        h.edit(id="cl", op="unassign", target=target)
        resp = h.get(id="cl", target=target)
        assert "no checklist assigned" in resp.body

    def test_definition_view_without_target(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        resp = h.get(id="cl")
        assert "item-one" in resp.body

    def test_unknown_checklist_raises_not_found(self, store: Store) -> None:
        h = _handler(store)
        with pytest.raises(NotFound):
            h.get(id="ghost-checklist")

    def test_list_view_with_no_id(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl-a", items=[])
        resp = h.get()
        assert "cl-a" in resp.body


# ── notes ────────────────────────────────────────────────────────────


class TestNotes:
    def test_add_and_remove_note(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        ref = store.get_ref(kind="paper", id="target-board")
        assert ref is not None
        checklist = store.checklist_get("cl")
        assert checklist is not None

        h.edit(
            id="cl",
            op="add_note",
            target=target,
            name="q1",
            note_kind="question",
            body="what bore?",
            item="item-one",
        )
        notes = store.checklist_notes_list(
            target_ref_id=ref.id, checklist_id=checklist["id"]
        )
        assert len(notes) == 1
        assert notes[0]["name"] == "q1"
        assert notes[0]["kind"] == "question"

        h.edit(id="cl", op="remove_note", target=target, name="q1")
        notes_after = store.checklist_notes_list(
            target_ref_id=ref.id, checklist_id=checklist["id"]
        )
        assert notes_after == []

    def test_add_note_bad_kind_rejected(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        with pytest.raises(BadInput):
            h.edit(
                id="cl",
                op="add_note",
                target=target,
                name="q1",
                note_kind="opinion",
                body="body",
            )

    def test_remove_note_nonexistent_raises_not_found(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[_ITEM])
        target = _make_target(store)
        with pytest.raises(NotFound):
            h.edit(id="cl", op="remove_note", target=target, name="ghost")


# ── assign / unassign ────────────────────────────────────────────────


class TestAssign:
    def test_assign_idempotent(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[])
        target = _make_target(store)
        r1 = h.edit(id="cl", op="assign", target=target)
        r2 = h.edit(id="cl", op="assign", target=target)
        assert "assigned" in r1.body
        assert "already assigned" in r2.body

    def test_unassign_not_assigned_raises_not_found(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[])
        target = _make_target(store)
        with pytest.raises(NotFound):
            h.edit(id="cl", op="unassign", target=target)

    def test_bare_ref_id_target_resolves(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[])
        ref = store.insert_ref(kind="paper", slug="bare-id-target", title="Bare")
        h.edit(id="cl", op="assign", target=str(ref.id))
        resp = h.get(id="cl", target=str(ref.id))
        assert "no checklist assigned" not in resp.body

    def test_unknown_target_raises_not_found(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="cl", items=[])
        with pytest.raises(NotFound):
            h.edit(id="cl", op="assign", target="paper:does-not-exist")


# ── search ───────────────────────────────────────────────────────────


class TestSearch:
    def test_search_requires_q(self, store: Store) -> None:
        h = _handler(store)
        with pytest.raises(BadInput):
            h.search()

    def test_search_matches_name(self, store: Store) -> None:
        h = _handler(store)
        h.put(id="tapeout-review", items=[])
        resp = h.search(q="tapeout")
        assert "tapeout-review" in resp.body

    def test_search_no_matches(self, store: Store) -> None:
        h = _handler(store)
        resp = h.search(q="nonexistent-xyz")
        assert "no checklist matches" in resp.body
