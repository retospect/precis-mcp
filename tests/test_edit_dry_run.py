"""``edit(..., dry_run=True)`` must never write — the tool-level contract.

Regression for a data-loss class found in the 2026-07-04 editable-kinds
audit: seven editable kinds (todo, folder, finding, paper, cfp,
datasheet, structure) accepted ``dry_run`` via ``**_kw`` and silently
discarded it, then wrote anyway — so a caller "previewing" a change
actually mutated the ref. The file/chunk kinds (plaintext family, draft)
already honoured it.

Fix (2026-07-04): todo + folder honour dry_run (cheap preview, no write);
finding / paper / structure reject it loudly instead of applying it.

Fix (td48769, 2026-08-05): paper / cfp / datasheet now honour dry_run
too — a real ``field: old → new`` preview, no write — bringing them to
parity with the file kinds. ``finding`` and ``structure`` still reject:
finding's ``edit`` surface (pick_candidate / title / unacquirable_note)
has no faithful preview and was reworked by the trust/retitle ship, so
its dry_run-preview arm is deferred (see OPEN-ITEMS); structure ops
mutate the cell/bond graph and may dispatch compute.
"""

from __future__ import annotations

import json

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.cfp import CfpHandler
from precis.handlers.datasheet import DatasheetHandler
from precis.handlers.folder import FolderHandler
from precis.handlers.paper import PaperHandler
from precis.handlers.structure import StructureHandler
from precis.handlers.todo import TodoHandler
from precis.store.types import ChunkInsert

# ── honour: no write, returns a preview ──────────────────────────────


def test_todo_edit_dry_run_does_not_write(hub: Hub) -> None:
    h = TodoHandler(hub=hub)
    h.put(text="original title", body="original body")
    tid = h.store.list_refs(kind="todo", limit=1)[0].id

    resp = h.edit(
        id=tid, mode="replace", text="rewritten", body="new body", dry_run=True
    )
    assert "dry-run" in resp.body.lower()

    # Task line unchanged.
    detail = h.get(id=tid).body
    assert "original title" in detail
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


def _seed_paper_ref(
    hub: Hub, *, kind: str = "paper", slug: str = "wang2020state", **kw
) -> int:
    """Insert a minimal ``kind`` ref (paper / cfp / datasheet all share
    the ``refs`` shape) + one body chunk. Returns the ref_id."""
    ref = hub.live_store.insert_ref(
        kind=kind,
        slug=slug,
        title=kw.pop("title", "Original title"),
        authors=kw.pop("authors", [{"name": "Wang, Q."}]),
        year=kw.pop("year", 2020),
        meta=kw.pop("meta", {"abstract": "Original abstract."}),
    )
    hub.live_store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="Body chunk.", meta={})]
    )
    return ref.id


def test_paper_edit_dry_run_previews_field_patch_and_does_not_write(hub: Hub) -> None:
    h = PaperHandler(hub=hub)
    ref_id = _seed_paper_ref(hub, kind="paper", slug="wang2020state", year=2020)

    resp = h.edit(id=ref_id, year=2024, title="New title", dry_run=True)
    assert "dry run" in resp.body.lower()
    assert "2020" in resp.body and "2024" in resp.body
    assert "New title" in resp.body

    ref = hub.live_store.fetch_refs_by_ids([ref_id])[ref_id]
    assert ref.year == 2020
    assert ref.title == "Original title"


def test_cfp_edit_dry_run_reuses_papers_preview(hub: Hub) -> None:
    """``CfpHandler`` doesn't override ``edit`` — it inherits
    ``PaperHandler.edit`` verbatim (see ``test_cfp_subclasses_paper_for_dry_reuse``
    in ``test_cfp_handler.py``), so the dry_run preview proven above for
    paper applies to cfp unchanged."""
    assert CfpHandler.edit is PaperHandler.edit
    h = CfpHandler(hub=hub)
    assert h.spec.kind == "cfp"


def test_cfp_insert_ref_round_trips_on_fresh_db(hub: Hub) -> None:
    """``insert_ref(kind='cfp', ...)`` must succeed against a DB built
    from the migration chain — regression for gr194088, where the
    baseline snapshot's ``kinds`` COPY block was missing the ``cfp`` row
    (never migration-seeded; only upserted at boot via
    ``precis.store._kinds_ops.upsert_kinds``), so a fresh/test DB raised
    ``BadInput: unknown kind: 'cfp'`` on the very first live cfp ref even
    though ``CfpHandler`` was fully wired. See
    ``test_cfp_edit_dry_run_reuses_papers_preview`` above for the
    dry-run-only workaround this replaces."""
    ref_id = _seed_paper_ref(hub, kind="cfp", slug="nsf-2026-call")

    ref = hub.live_store.fetch_refs_by_ids([ref_id])[ref_id]
    assert ref.kind == "cfp"
    assert ref.title == "Original title"


def test_datasheet_edit_dry_run_previews_meta_patch_and_does_not_write(
    hub: Hub,
) -> None:
    h = DatasheetHandler(hub=hub)
    ref_id = _seed_paper_ref(hub, kind="datasheet", slug="ds2026")

    resp = h.edit(id=ref_id, vendor="Espressif", dry_run=True)
    assert "dry run" in resp.body.lower()
    assert "Espressif" in resp.body

    ref = hub.live_store.fetch_refs_by_ids([ref_id])[ref_id]
    assert (ref.meta or {}).get("vendor") is None


def test_datasheet_edit_dry_run_previews_both_meta_and_bib_and_does_not_write(
    hub: Hub,
) -> None:
    h = DatasheetHandler(hub=hub)
    ref_id = _seed_paper_ref(hub, kind="datasheet", slug="ds2026combo", year=2020)

    resp = h.edit(id=ref_id, vendor="Espressif", year=2025, dry_run=True)
    assert "dry run" in resp.body.lower()
    assert "Espressif" in resp.body
    assert "2020" in resp.body and "2025" in resp.body

    ref = hub.live_store.fetch_refs_by_ids([ref_id])[ref_id]
    assert ref.year == 2020
    assert (ref.meta or {}).get("vendor") is None


# ── reject: loud error, no write ─────────────────────────────────────


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
