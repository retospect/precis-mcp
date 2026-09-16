"""Regression for gr341499: ``dry_run='full'`` false-greening a DOI edit.

``PaperHandler.edit(..., doi=..., dry_run='full')`` previewed a DOI change
as if it would apply, but the real write (``Store.set_ref_identifier``)
can fail atomically when ``ref_identifiers`` already holds that
``(id_kind, id_value)`` pair for a *different* live ref — the 0049
BEFORE-UPDATE trigger lowercases stored DOIs, so the collision is
case-insensitive. The dry-run path never ran that check, so it reported
success on a commit that would actually raise ``BadInput``.

These tests assert dry_run and the real write now agree: both raise the
same collision error, for both an exact-case and a different-case DOI
clash.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store.types import ChunkInsert


def _seed_paper_ref(
    hub: Hub, *, slug: str, doi: str | None = None, **kw: object
) -> int:
    """Insert a minimal paper ref (+ one body chunk), optionally owning
    ``doi``. Mirrors ``test_edit_dry_run._seed_paper_ref``."""
    ref = hub.live_store.insert_ref(
        kind="paper",
        slug=slug,
        title=kw.pop("title", "Original title"),  # type: ignore[arg-type]
        authors=kw.pop("authors", [{"name": "Wang, Q."}]),  # type: ignore[arg-type]
        year=kw.pop("year", 2020),  # type: ignore[arg-type]
        meta=kw.pop("meta", {"abstract": "Original abstract."}),  # type: ignore[arg-type]
    )
    hub.live_store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="Body chunk.", meta={})]
    )
    if doi:
        hub.live_store.insert_ref_identifiers(ref.id, [("doi", doi, "test-seed")])
    return ref.id


def test_paper_edit_dry_run_reports_doi_collision(hub: Hub) -> None:
    h = PaperHandler(hub=hub)
    owner_id = _seed_paper_ref(
        hub, slug="owner2020doi", doi="10.1234/OWNED.DOI", title="Owner paper"
    )
    victim_id = _seed_paper_ref(hub, slug="victim2020doi", title="Victim paper")

    # Different case from the stored value — the 0049 trigger lowercases
    # at write time, so this must still collide.
    with pytest.raises(BadInput, match=rf"belongs to ref id={owner_id}"):
        h.edit(id=victim_id, doi="10.1234/owned.doi", dry_run="full")

    # dry_run must never write, collision or not.
    ids = hub.live_store.identifiers_for_refs([victim_id])[victim_id]
    assert "doi" not in ids


def test_paper_edit_real_write_fails_the_same_way(hub: Hub) -> None:
    h = PaperHandler(hub=hub)
    owner_id = _seed_paper_ref(
        hub, slug="owner2020doi2", doi="10.5678/OWNED.DOI", title="Owner paper"
    )
    victim_id = _seed_paper_ref(hub, slug="victim2020doi2", title="Victim paper")

    with pytest.raises(BadInput, match=rf"belongs to ref id={owner_id}"):
        h.edit(id=victim_id, doi="10.5678/owned.doi", dry_run="full")

    with pytest.raises(BadInput, match=rf"belongs to ref id={owner_id}"):
        h.edit(id=victim_id, doi="10.5678/owned.doi")

    ids = hub.live_store.identifiers_for_refs([victim_id])[victim_id]
    assert "doi" not in ids


def test_paper_edit_dry_run_reports_arxiv_collision(hub: Hub) -> None:
    h = PaperHandler(hub=hub)
    owner_id = _seed_paper_ref(hub, slug="owner2020arxiv", title="Owner paper")
    hub.live_store.insert_ref_identifiers(
        owner_id, [("arxiv", "2401.00001", "test-seed")]
    )
    victim_id = _seed_paper_ref(hub, slug="victim2020arxiv", title="Victim paper")

    with pytest.raises(BadInput, match=rf"belongs to ref id={owner_id}"):
        h.edit(id=victim_id, arxiv="2401.00001", dry_run="full")


def test_paper_edit_dry_run_no_collision_still_previews(hub: Hub) -> None:
    h = PaperHandler(hub=hub)
    victim_id = _seed_paper_ref(hub, slug="lonely2020doi", title="Lonely paper")

    resp = h.edit(id=victim_id, doi="10.9999/free.doi", dry_run="full")
    assert "dry run" in resp.body.lower()
    assert "10.9999/free.doi" in resp.body

    ids = hub.live_store.identifiers_for_refs([victim_id])[victim_id]
    assert "doi" not in ids
