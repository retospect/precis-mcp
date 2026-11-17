"""The memory fingerprint bridge (gr677471): ``memory:me<id>`` checklist
targets whose verdicts go stale when the root's ``part-of`` subtree
moves, and the skill text that documents the bridge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers.checklist import ChecklistHandler
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.workers.auto_check_evaluators import checklist_clean


def _handler(store: Store) -> ChecklistHandler:
    return ChecklistHandler(hub=Hub(store=store))


_ITEM = {
    "name": "kill-line",
    "phase": "gate",
    "severity": "blocking",
    "decidability": "judgment",
    "prevents": "the plan tests without a pass/pivot/kill line",
}


def _memory(store: Store, title: str, body: str) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title)
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})],
    )
    return int(ref.id)


def _append_body(store: Store, ref_id: int, text: str) -> None:
    store.chunks.insert_chunks(
        ref_id,
        [ChunkInsert(ord=1, text=text, meta={"chunk_kind": "memory_body"})],
    )


def _record_pass(h: ChecklistHandler, target: str) -> None:
    h.edit(
        id="venture-gate",
        op="verdict",
        target=target,
        item="kill-line",
        verdict="pass",
        evidence={"reasoning": "the plan states its kill line before testing"},
    )


def test_memory_fingerprint_goes_stale_on_subtree_edit(store: Store) -> None:
    h = _handler(store)
    h.put(id="venture-gate", items=[_ITEM])
    root = _memory(store, "Plan root", "the plan\n")
    child = _memory(store, "Band index", "bands\n")
    store.add_link(src_ref_id=child, dst_ref_id=root, relation="part-of")
    target = f"memory:me{root}"
    h.edit(id="venture-gate", op="assign", target=target)
    _record_pass(h, target)
    body = h.get(id="venture-gate", target=target).body
    assert "pass" in body
    assert "stale" not in body

    # An edit anywhere in the part-of subtree stales the verdict.
    _append_body(store, child, "more bands\n")
    body = h.get(id="venture-gate", target=target).body
    assert "stale (target changed)" in body

    # Re-recorded, the verdict is current again…
    _record_pass(h, target)
    body = h.get(id="venture-gate", target=target).body
    assert "pass" in body
    assert "stale" not in body

    # …and an edit to an UNLINKED memory leaves it current.
    other = _memory(store, "Unrelated note", "scratch\n")
    _append_body(store, other, "edit\n")
    body = h.get(id="venture-gate", target=target).body
    assert "pass" in body
    assert "stale" not in body


def test_memory_verdict_refuses_anchors(store: Store) -> None:
    """Memory has no named sub-objects: ``anchors=`` is refused, never
    silently ignored — a supplied ``fingerprint=`` does not buy an
    anchor past the bridge either (review td705325)."""
    h = _handler(store)
    h.put(id="venture-gate", items=[_ITEM])
    root = _memory(store, "Plan root", "the plan\n")
    target = f"memory:me{root}"
    h.edit(id="venture-gate", op="assign", target=target)
    with pytest.raises(NotFound, match="anchors not found"):
        h.edit(
            id="venture-gate",
            op="verdict",
            target=target,
            item="kill-line",
            verdict="pass",
            anchors=["band-1"],
        )
    with pytest.raises(NotFound, match="anchors not found"):
        h.edit(
            id="venture-gate",
            op="verdict",
            target=target,
            item="kill-line",
            verdict="pass",
            anchors=["band-1"],
            fingerprint="opaque-caller-digest",
        )


def test_checklist_clean_resolves_memory_handle(store: Store) -> None:
    """The auto-check evaluator resolves ``memory:me<id>`` too — the
    gate and the handler's status view must agree (review td705325)."""
    h = _handler(store)
    h.put(id="venture-gate", items=[_ITEM])
    root = _memory(store, "Plan root", "the plan\n")
    child = _memory(store, "Band index", "bands\n")
    store.add_link(src_ref_id=child, dst_ref_id=root, relation="part-of")
    target = f"memory:me{root}"
    spec = {
        "type": "checklist_clean",
        "checklist": "venture-gate",
        "target": target,
    }
    assert checklist_clean.evaluate(store, spec) is None  # not assigned
    h.edit(id="venture-gate", op="assign", target=target)
    assert checklist_clean.evaluate(store, spec) is None  # nothing checked
    _record_pass(h, target)
    assert checklist_clean.evaluate(store, spec) is True
    # A subtree edit re-opens the gate exactly as it stales the view.
    _append_body(store, child, "more bands\n")
    assert checklist_clean.evaluate(store, spec) is None


def test_checklist_skill_names_memory_bridge() -> None:
    text = (
        Path(__file__).parent.parent
        / "src"
        / "precis"
        / "data"
        / "skills"
        / "precis-checklist-help.md"
    ).read_text(encoding="utf-8")
    assert "memory:me" in text
    assert "part-of" in text
