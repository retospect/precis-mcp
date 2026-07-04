"""``edit(..., dry_run=True)`` must never write — the tool-level contract.

Regression for a data-loss class found in the 2026-07-04 editable-kinds
audit: seven editable kinds (todo, folder, finding, paper, cfp,
datasheet, structure) accepted ``dry_run`` via ``**_kw`` and silently
discarded it, then wrote anyway — so a caller "previewing" a change
actually mutated the ref. The file/chunk kinds (plaintext family, draft)
already honoured it.

Fix: todo + folder now *honour* dry_run (cheap preview, no write); the
kinds whose edit is a bespoke op (finding candidate-pick, paper metadata
patch, structure graph ops) *reject* dry_run loudly instead of applying
it. Either way, no editable kind silently writes on dry_run.
"""

from __future__ import annotations

import json

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.handlers.folder import FolderHandler
from precis.handlers.paper import PaperHandler
from precis.handlers.structure import StructureHandler
from precis.handlers.todo import TodoHandler

# ── honour: no write, returns a preview ──────────────────────────────


def test_todo_edit_dry_run_does_not_write(hub: Hub) -> None:
    h = TodoHandler(hub=hub)
    h.put(text="original task line", body="original body")
    tid = h.store.list_refs(kind="todo", limit=1)[0].id

    resp = h.edit(
        id=tid, mode="replace", text="rewritten", body="new body", dry_run=True
    )
    assert "dry-run" in resp.body.lower()

    # Task line unchanged.
    detail = h.get(id=tid).body
    assert "original task line" in detail
    assert "rewritten" not in detail
    # Body chunk unchanged.
    with h.store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0", (tid,)
        ).fetchall()
    assert rows == [("original body",)]


def test_folder_edit_dry_run_does_not_rename(hub: Hub) -> None:
    h = FolderHandler(hub=hub)
    h.put(text="Original name")
    fid = h.store.list_refs(kind="folder", limit=1)[0].id

    resp = h.edit(id=fid, text="New name", dry_run=True)
    assert "dry-run" in resp.body.lower()
    assert h.store.list_refs(kind="folder", limit=1)[0].title == "Original name"


# ── reject: loud error, no write ─────────────────────────────────────


def test_finding_edit_dry_run_rejected(hub: Hub) -> None:
    h = FindingHandler(hub=hub)
    with pytest.raises(BadInput, match="dry_run"):
        h.edit(id=1, pick_candidate="miller23a", dry_run=True)


def test_paper_edit_dry_run_rejected(hub: Hub) -> None:
    h = PaperHandler(hub=hub)
    with pytest.raises(BadInput, match="dry_run"):
        h.edit(id="somepaper", year=2024, dry_run=True)


def test_structure_edit_dry_run_rejected(hub: Hub) -> None:
    h = StructureHandler(hub=hub)
    with pytest.raises(BadInput, match="dry_run"):
        h.edit(
            id="pd111",
            ops=json.loads(
                '[{"op": "add_atom", "element": "O", "frac": [0.3, 0.3, 0.5]}]'
            ),
            dry_run=True,
        )
