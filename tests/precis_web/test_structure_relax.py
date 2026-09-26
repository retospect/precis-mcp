"""The run-cube "Relax" button — POST /structure/{slug}/relax.

The dispatch itself (a struct_relax job parented on the structure, no todo)
is covered at the handler level in ``tests/test_structure_handler.py``. Here
we only guard the endpoint's own logic: it rejects an unknown fidelity rung
before touching the store, and offers exactly the documented ladder.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from precis_web.routes.structure import _RELAX_RUNGS


def test_relax_button_offers_the_documented_ladder() -> None:
    assert _RELAX_RUNGS == ("clean", "ml", "dft")


def test_relax_rejects_unknown_fidelity(client: TestClient) -> None:
    resp = client.post(
        "/structure/whatever/relax",
        data={"fidelity": "bogus"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "unknown fidelity" in resp.text


def test_structure_index_redirects_to_drive_kind_structure(
    client: TestClient,
) -> None:
    """``/structure`` (the list) is retired into the unified Drive surface —
    it redirects to the ``kind=structure`` facet preset, the same target
    ``_drive_back.html.j2``'s back-link uses. The workbench
    (``/structure/{slug}``) is unaffected — ``test_structure_detail_404``
    below proves the ``{slug}`` route still resolves and isn't swallowed by
    this redirect."""
    r = client.get("/structure", follow_redirects=False)
    assert r.status_code in (302, 307, 308)
    assert r.headers["location"] == "/drive?k=structure&folder=*&sort=recency"


def test_structure_detail_404(client: TestClient) -> None:
    r = client.get("/structure/nope")
    assert r.status_code == 404
    assert "not found" in r.text.lower()
