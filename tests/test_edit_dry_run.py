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
parity with the file kinds. ``structure`` still rejects: its ops mutate
the cell/bond graph and may dispatch compute.

Finding candidate picks, retitles, hypothesis sharpening and unacquirable
overrides preview their changes. The self-pick close-out still rejects dry_run.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._finding_hypothesis import META_PROPOSED_PAYLOAD
from precis.handlers.cfp import CfpHandler
from precis.handlers.datasheet import DatasheetHandler
from precis.handlers.finding import FindingHandler
from precis.handlers.folder import FolderHandler
from precis.handlers.paper import PaperHandler
from precis.handlers.structure import StructureHandler
from precis.handlers.todo import TodoHandler
from precis.identity import make_pub_id, make_taproot_hub_paper_id
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from tests.test_finding import _seed_paper
from tests.test_finding_hypothesis_edit import _approved_row, _mint_hypothesis

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


def _snapshot_finding(store: Store, ref_id: int) -> tuple[tuple[str, ...], ...]:
    with store.pool.connection() as conn:
        return tuple(
            tuple(row[0] for row in conn.execute(query, (ref_id,)).fetchall())
            for query in (
                "SELECT jsonb_build_array(title, meta)::text FROM refs WHERE ref_id = %s",
                "SELECT to_jsonb(t)::text FROM ref_tags t WHERE ref_id = %s ORDER BY 1",
                "SELECT to_jsonb(l)::text FROM links l WHERE src_ref_id = %s ORDER BY 1",
                "SELECT to_jsonb(i)::text FROM ref_identifiers i WHERE ref_id = %s ORDER BY 1",
                "SELECT to_jsonb(c)::text FROM chunks c WHERE ref_id = %s ORDER BY 1",
                "SELECT to_jsonb(p)::text FROM nanopub_publish p WHERE claim_ref_id = %s ORDER BY 1",
            )
        )


def test_finding_retitle_dry_run_previews_and_does_not_write(store: Store) -> None:
    old = "Pd/C catalyzes Suzuki coupling at room temperature."
    new = "Pd/C catalyzes Suzuki coupling of aryl halides at room temperature."
    scope = {"material": "Pd/C"}
    ref_id = mint_hub(store, CanonicalClaim(sentence=old, scope=scope))
    h = FindingHandler(hub=Hub(store=store))
    before = _snapshot_finding(store, ref_id)

    body = h.edit(id=f"fi{ref_id}", title=new, dry_run=True).body

    assert body.splitlines()[0] == (
        f"dry-run: would retitle claim hub fi{ref_id} (nothing written)"
    )
    assert f"old: {old}\nnew: {new}" in body
    old_pub_id = make_pub_id(make_taproot_hub_paper_id(old, scope))
    new_pub_id = make_pub_id(make_taproot_hub_paper_id(new, scope))
    assert f"pub_id: {old_pub_id} -> {new_pub_id}" in body
    assert (
        f"apply: edit(kind='finding', id='fi{ref_id}', title={new!r})"
        in body.splitlines()
    )
    assert _snapshot_finding(store, ref_id) == before


def test_finding_sharpen_dry_run_previews_and_does_not_write(store: Store) -> None:
    ref_id = _mint_hypothesis(store)
    _approved_row(store, ref_id)
    h = FindingHandler(hub=Hub(store=store))
    payload = store.fetch_refs_by_ids([ref_id])[ref_id].meta[META_PROPOSED_PAYLOAD]
    before = _snapshot_finding(store, ref_id)

    body = h.edit(
        id=ref_id,
        testable_by="Compare films by nanoindentation.",
        motivation="Both sources implicate interfacial stiffness.",
        dry_run=True,
    ).body

    assert body.splitlines()[0] == (
        f"dry-run: would sharpen hypothesis fi{ref_id} (nothing written)"
    )
    assert (
        f"testable_by: {payload['testable_by']!r} -> 'Compare films by nanoindentation.'"
        in body
    )
    assert (
        f"motivation: {payload['motivation']!r} -> 'Both sources implicate interfacial stiffness.'"
        in body
    )
    assert "meta.testable_by_history" in body
    assert "meta.motivation_history" in body
    assert "would reopen any unsigned reviews" in body
    assert (
        "no change"
        in h.edit(id=ref_id, testable_by=payload["testable_by"], dry_run=True).body
    )
    assert _snapshot_finding(store, ref_id) == before


def _seed_plain_finding(h: FindingHandler) -> int:
    _seed_paper(h.store, cite_key="source")
    h.put(
        title="Claim in a cited source", body="Claim body.", scope={}, cited_in="source"
    )
    return h.store.list_refs(kind="finding", limit=1)[0].id


def test_finding_unacquirable_note_dry_run_previews_and_does_not_write(
    store: Store,
) -> None:
    h = FindingHandler(hub=Hub(store=store))
    ref_id = _seed_plain_finding(h)
    h.edit(id=ref_id, unacquirable_note="Original print-only monograph")
    before = _snapshot_finding(store, ref_id)

    body = h.edit(
        id=ref_id,
        unacquirable_note="Abstract supports the claim",
        unacquirable_mode="abstract",
        dry_run=True,
    ).body

    assert body.splitlines()[0] == (
        f"dry-run: would record unacquirable override on finding id={ref_id} "
        "(nothing written)"
    )
    assert "mode: abstract" in body
    assert "note: Abstract supports the claim" in body
    assert "would replace:" in body and "Original print-only monograph" in body
    assert "abstract-backs-it" in body
    assert _snapshot_finding(store, ref_id) == before


def test_finding_pick_candidate_dry_run_does_not_write(store: Store) -> None:
    h = FindingHandler(hub=Hub(store=store))
    ref_id = _seed_plain_finding(h)
    for key in ("fischer13", "miller23a", "wang2020"):
        candidate_id = _seed_paper(store, cite_key=key)
        store.add_link(
            src_ref_id=ref_id,
            dst_ref_id=candidate_id,
            relation="derived-from",
            meta={"candidate": True},
        )
    store.add_tag(
        ref_id,
        Tag.closed("STATUS", "multi_candidate"),
        set_by="chase",
        replace_prefix=True,
    )
    before = _snapshot_finding(store, ref_id)

    body = h.edit(id=ref_id, pick_candidate="fischer13", dry_run=True).body

    assert body.splitlines()[0] == (
        f"dry-run: would pick candidate fischer13 on finding id={ref_id} (nothing written)"
    )
    dropped = body.splitlines()[1]
    assert dropped.startswith("would drop 2 other candidate(s)")
    assert "miller23a" in dropped and "wang2020" in dropped
    assert "status: STATUS:multi_candidate -> STATUS:tracing" in body
    assert _snapshot_finding(store, ref_id) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_finding_retitle_refuses_plain_finding(store: Store, dry_run: bool) -> None:
    h = FindingHandler(hub=Hub(store=store))
    ref_id = _seed_plain_finding(h)

    with pytest.raises(BadInput, match="only retitles a TAPROOT:claim hub"):
        h.edit(id=ref_id, title="New title", dry_run=dry_run)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "replace", "text": "Whole new body."},
        {"title": "Reworded claim sentence."},
        {"meta": {"scope": {"material": "C60"}}},
        {"testable_by": "Compare films by nanoindentation."},
        {"unacquirable_note": "print-only 1962 monograph"},
        {"pick_candidate": "fischer13"},
    ],
    ids=["body", "retitle", "rescope", "sharpen", "override", "pick"],
)
def test_finding_edit_rejects_non_mode_dry_run(
    store: Store, kwargs: dict[str, Any]
) -> None:
    """A truthy non-mode ``dry_run`` (``'bogus'``) raises the documented
    ``BadInput`` on every op — previews included — instead of silently
    previewing on a ``bool()`` coercion."""
    h = FindingHandler(hub=Hub(store=store))
    ref_id = _seed_plain_finding(h)

    with pytest.raises(BadInput, match="dry_run must be bool"):
        h.edit(id=ref_id, dry_run="bogus", **kwargs)


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
