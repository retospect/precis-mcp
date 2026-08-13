"""Query-count regression for the smartdraft reader's Taproot claim-hub
derivation — the "``/smartdraft`` reader: O(all-hubs) claims derivation →
10-16 s TTFB" fix (OPEN-ITEMS.md, root-caused 2026-08-04).

Before the fix: ``claims``/``claims_evidence``/``hub_stats`` scanned the
WHOLE draft (including ``skel`` placeholder nodes never rendered in
full-document mode — scope defect A) and resolved each distinct hub cite
through ~16 individual round trips (batch defect B, plus the double-
derivation de-dup defect C) — ~1,800-2,000 DB round trips for a 121-hub
draft. This proves the fixed route's round-trip count stays near-constant
as the RENDERED hub-cite count scales 5 → 30 (all within the ±40 full-doc
window, so scope alone can't explain a flat count — only the batch/de-dup
fix can), and that ``?focus=`` doesn't materially change it either.

Uses the real DB-backed ``hub``/``runtime_with_store`` fixtures (mirrors
``tests/precis_web/test_claim_reader_anchors.py``) — the FakeStore reader
tests don't run real SQL, so they can't see this regression.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.claim_render import render_claim_evidence, render_claims_evidence
from precis_web.config import WebConfig


@pytest.fixture
def reader_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _mint_hub_with_evidence(hub: Hub, i: int) -> int:
    """One claim hub, cited by an originator + a corroborator (so both
    seniority groups + a grounding chunk are exercised), returns its
    ref_id."""
    store = hub.live_store
    claim = CanonicalClaim(
        sentence=f"Claim sentence number {i} about some reaction.",
        scope={"material": f"material-{i}"},
    )
    claim_hub = mint_hub(store, claim)
    originator = store.insert_ref(
        kind="paper", slug=f"qc-orig-{i}", title=f"Origin paper {i}", year=2000 + i % 20
    ).id
    follower = store.insert_ref(
        kind="paper",
        slug=f"qc-follow-{i}",
        title=f"Follower paper {i}",
        year=2010 + i % 10,
    ).id
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=originator,
        role="corroborates",
        meta={"source_handle": f"pc{900000 + i}"},
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    store.add_link(src_ref_id=follower, dst_ref_id=originator, relation="cites")
    return claim_hub


def _seed_draft(hub: Hub, n: int, *, name: str) -> tuple[str, list[str]]:
    """A draft with ``n`` paragraphs, each citing a DISTINCT freshly-minted
    claim hub by ``[fi<id>]`` — all within the ±40 full-doc render window
    (focus defaults to the first body chunk). Returns ``(slug, dcs)``."""
    store = hub.live_store
    proj = store.insert_ref(kind="todo", slug=None, title=f"QC project {name}").id
    ref, _title = store.drafts.create_draft(
        name=name, title="QC draft", project_ref_id=proj
    )
    dcs: list[str] = []
    for i in range(n):
        hub_ref_id = _mint_hub_with_evidence(hub, i)
        fi_handle = handle_registry.format_handle("finding", hub_ref_id)
        chunks = store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text=f"Paragraph {i} makes a claim [{fi_handle}].",
            at={"last": True},
        )
        dcs.append(chunks[0].dc)
    return str(ref.slug), dcs


@contextmanager
def _query_counter() -> Iterator[dict[str, int]]:
    """Count every ``psycopg.Connection.execute`` call for the duration of
    the ``with`` block — a simple, local statement counter (no fixture like
    this exists yet in the suite; see OPEN-ITEMS.md's test ask). Restores
    the original method on exit regardless of outcome."""
    counts = {"n": 0}
    original = psycopg.Connection.execute

    def counting_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        counts["n"] += 1
        return original(self, *args, **kwargs)

    psycopg.Connection.execute = counting_execute  # type: ignore[method-assign]
    try:
        yield counts
    finally:
        psycopg.Connection.execute = original  # type: ignore[method-assign]


def _get_query_count(client: TestClient, url: str) -> int:
    with _query_counter() as counts:
        r = client.get(url)
    assert r.status_code == 200
    return counts["n"]


def test_smartdraft_query_count_near_constant_across_hub_scale(
    reader_client: TestClient, hub: Hub
) -> None:
    """5 vs ~30 distinct, all-rendered ``TAPROOT:claim`` hub cites: the
    route's DB round-trip count must stay near-constant, not scale
    linearly (~6x) with the hub count — the load-bearing regression test
    for batch B + de-dup C."""
    slug_5, _ = _seed_draft(hub, 5, name="qc-draft-5")
    slug_30, _ = _seed_draft(hub, 30, name="qc-draft-30")

    n5 = _get_query_count(reader_client, f"/smartdraft/{slug_5}")
    n30 = _get_query_count(reader_client, f"/smartdraft/{slug_30}")

    # Before the fix this would be ~6x (linear in hub count, ~16
    # queries/hub); after it, resolving 25 MORE hubs costs a handful of
    # extra bulk queries, not 25 x 16.
    assert n30 <= n5 + 20, (
        f"query count scaled with hub count: 5 hubs={n5}, 30 hubs={n30} "
        "(expected near-constant, not linear)"
    )


def test_smartdraft_query_count_focus_does_not_materially_change_it(
    reader_client: TestClient, hub: Hub
) -> None:
    """``?focus=<dc>`` picks a different node as the fisheye/full-doc
    focus but must not re-widen the claims/hub-stats scan — the SAME
    rendered-window scoping applies regardless of which node is focused."""
    slug, dcs = _seed_draft(hub, 12, name="qc-draft-focus")

    n_default = _get_query_count(reader_client, f"/smartdraft/{slug}")
    n_focused = _get_query_count(reader_client, f"/smartdraft/{slug}?focus={dcs[6]}")

    assert n_focused <= n_default + 10, (
        f"?focus= materially changed the query count: default={n_default}, "
        f"focused={n_focused}"
    )


def test_render_claims_evidence_matches_singular_calls(hub: Hub) -> None:
    """Batch-vs-singular equivalence: ``render_claims_evidence`` over a
    handful of hubs returns the SAME order/content as calling
    ``render_claim_evidence`` once per head."""
    store = hub.live_store
    heads = []
    for i in range(3):
        hub_ref_id = _mint_hub_with_evidence(hub, 100 + i)
        heads.append(handle_registry.format_handle("finding", hub_ref_id))

    batched = render_claims_evidence(store, heads)
    singular = [render_claim_evidence(store, h) for h in heads]

    assert batched == singular
    assert len(batched) == 3


def test_render_claims_evidence_skips_non_hub_heads(hub: Hub) -> None:
    """A head that doesn't resolve to a live claim hub is silently dropped
    from the plural result, same as the singular function returning
    ``None`` for it."""
    store = hub.live_store
    hub_ref_id = _mint_hub_with_evidence(hub, 200)
    real_head = handle_registry.format_handle("finding", hub_ref_id)
    plain_finding = store.insert_ref(kind="finding", slug=None, title="Not a hub").id
    fake_head = handle_registry.format_handle("finding", plain_finding)

    out = render_claims_evidence(store, [real_head, fake_head])

    assert len(out) == 1
    assert out[0]["head"] == real_head
