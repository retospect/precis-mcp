"""The run-cube "Relax" button — POST /structure/{slug}/relax (ADR 0044).

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
