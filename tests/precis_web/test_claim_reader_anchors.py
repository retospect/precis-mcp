"""Taproot claim-hub cites render as ``/claim/`` anchors inline in the
smartdraft reader — proves the ``claims=`` side-channel is actually threaded
through the reader route into its ``linkify_refs`` calls (not just
plumbed in ``claim_render.py``/``linkify.py``, which have their own unit
coverage). Uses the real DB-backed ``hub``/``runtime_with_store`` fixtures
(``tests/conftest.py``) rather than the readers' usual ``FakeStore`` — the
hub-cite resolution runs real SQL (``finding_cite_keys`` / ``derive_evidence``).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


@pytest.fixture
def reader_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _draft_citing_a_hub(hub: Hub) -> str:
    """Mint a claim hub with one corroborator, then a draft with a single
    paragraph citing it by ``[fi<id>]``. Returns the draft's slug."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="claim-orig", title="The original report", year=2001
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=originator, role="corroborates"
    )
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    ref, _title = store.create_draft(
        name="claimdraft", title="Claim draft", project_ref_id=proj
    )
    fi_handle = handle_registry.format_handle("finding", claim_hub)
    store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"Evidence shows this reaction proceeds well [{fi_handle}].",
        at={"last": True},
    )
    return str(ref.slug)


def test_smartdraft_reader_renders_claim_anchor(
    reader_client: TestClient, hub: Hub
) -> None:
    slug = _draft_citing_a_hub(hub)
    r = reader_client.get(f"/smartdraft/{slug}")
    assert r.status_code == 200
    assert "/claim/" in r.text
    assert _CLAIM.sentence in r.text  # the right-rail Claims panel
