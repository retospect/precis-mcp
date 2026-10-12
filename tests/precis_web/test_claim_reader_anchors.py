"""Taproot claim-hub cites render as ``/claim/`` anchors inline in the
smartdraft reader — proves the ``claims=`` side-channel is actually threaded
through the reader route into its ``linkify_refs`` calls (not just
plumbed in ``claim_render.py``/``linkify.py``, which have their own unit
coverage). Uses the real DB-backed ``hub``/``runtime_with_store`` fixtures
(``tests/conftest.py``) rather than the readers' usual ``FakeStore`` — the
hub-cite resolution runs real SQL (``finding_cite_keys`` / ``derive_evidence``).
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.linkify import _CLAIM_ANCHOR_CLS

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
    store = hub.live_store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="claim-orig", title="The original report", year=2001
    ).id
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=originator, role="corroborates"
    )
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    ref, _title = store.drafts.create_draft(
        name="claimdraft", title="Claim draft", project_ref_id=proj
    )
    fi_handle = handle_registry.format_handle("finding", claim_hub)
    store.drafts.add_chunks(
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


def _flag_rail_only(hub: Hub, slug: str, meta: dict[str, str]) -> None:
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    with hub.live_store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = COALESCE(meta, '{}'::jsonb)"
            " || %s::jsonb"
            " WHERE ref_id = %s",
            (Jsonb(meta), ref.id),
        )


@pytest.mark.parametrize(
    "meta",
    [{"cite_render": "rail"}, {"cast": "reading"}],
    ids=["explicit", "legacy-cast"],
)
def test_rail_only_draft_hides_inline_anchor_but_rail_and_editor_keep_cite(
    reader_client: TestClient, hub: Hub, meta: dict[str, str]
) -> None:
    from precis_web import smartdraft

    slug = _draft_citing_a_hub(hub)
    normal = reader_client.get(f"/smartdraft/{slug}").text
    _flag_rail_only(hub, slug, meta)
    store = hub.live_store
    ref = store.get_ref(kind="draft", id=slug)
    assert ref is not None
    nodes = [n for n in smartdraft.build_nodes(store, ref.id) if "Evidence" in n.text]
    assert len(nodes) == 1
    cite = re.search(r"\[fi\d+\]", nodes[0].text)
    assert cite is not None
    stored_cite = cite.group(0)

    r = reader_client.get(f"/smartdraft/{slug}")
    assert r.status_code == 200
    # Claims rail still lists the evidence.
    assert _CLAIM.sentence in r.text
    # Exactly the one inline anchor is gone; the rail row's own head stays.
    assert _CLAIM_ANCHOR_CLS in normal and _CLAIM_ANCHOR_CLS not in r.text
    assert (
        r.text.count('data-claim-head="fi') == normal.count('data-claim-head="fi') - 1
    )
    # Editor / raw text still carries the stored cite.
    assert stored_cite in r.text

    # blocks() hydration: display stripped, no anchor.
    rb = reader_client.get(f"/smartdraft/{slug}/blocks", params={"dcs": nodes[0].dc})
    assert rb.status_code == 200
    assert "Evidence shows this reaction proceeds well." in rb.text
    assert "data-claim-head" not in rb.text and "◆" not in rb.text
