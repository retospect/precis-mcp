"""Taproot Phase-3 slice W1 — the chase forward bridge.

``precis.workers.chase._taproot_bridge`` (reached from ``advance_finding``'s
established-terminal path, gated by ``taproot_enabled=`` /
``PRECIS_TAPROOT_CHASE_ENABLED``) mints/attaches a taproot claim hub off the
chase LLM verifier's terminal verdict. DB-backed (real ``store`` fixture);
the chase verifier hook (``_verify_support_with_caveats``) is always mocked
here (no live model) — the canonicalizer's own ``dedup_judge`` dispatch only
fires once a candidate hub exists, so most scenarios below never reach it:
the "no candidates yet -> mint a new hub" path exercises the real
``block``/``place`` logic against a deterministic ``MockEmbedder``.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

from precis.dispatch import Hub
from precis.handlers.finding import FindingHandler
from precis.store.types import BlockInsert, Tag
from precis.taproot.canon import TAPROOT_CLAIM, TAPROOT_NAMESPACE
from precis.workers.chase import FindingRow, advance_finding, run_finding_chase_pass
from tests.workers._helpers import make_mock_bge_m3

_VERIFY_PATH = "precis.workers.chase._verify_support_with_caveats"
_DEDUP_PATH = "precis.workers.chase.dedup_judge"


# ── seeding helpers (mirrors tests/workers/test_chase.py) ──────────────


def _seed_paper(
    store: Any,
    *,
    cite_key: str,
    blocks: list[str] | None = None,
) -> int:
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=f"Test paper {cite_key}", meta={}
    )
    if blocks:
        store.insert_blocks(
            ref.id, [BlockInsert(pos=i, text=t, meta={}) for i, t in enumerate(blocks)]
        )
    return ref.id


def _seed_finding(
    store: Any,
    *,
    cite_key: str,
    title: str = "The device sustains 2.4 kV without breakdown.",
    scope: dict[str, str] | None = None,
) -> FindingRow:
    h = FindingHandler(hub=Hub(store=store))
    resp = h.put(
        title=title,
        body="claim body",
        scope=scope or {"electrode": "Cu"},
        # Pin the frontier ord explicitly (~0) so _select_target_chunk
        # takes the deterministic "frontier_ord is set" branch and never
        # reaches the with_llm _locate_chunk_in_target hook — this test
        # module exercises the taproot bridge, not that unrelated hook.
        cited_in=f"{cite_key}~0",
    )
    fid = int(re.search(r"id=(\d+)", resp.body).group(1))
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title, meta FROM refs WHERE ref_id = %s", (fid,)
        ).fetchone()
    return FindingRow(ref_id=fid, title=row[0], meta=dict(row[1] or {}))


def _advance(store: Any, finding: FindingRow, **kwargs: Any) -> tuple[str, Any]:
    with store.pool.connection() as conn:
        outcome, ev = advance_finding(conn, store, finding, **kwargs)
        conn.commit()
    return outcome, ev


def _status(store: Any, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if getattr(t, "namespace", None) == "closed" and t.prefix == "STATUS":
            return t.value
    return None


def _hub_ref_ids(store: Any) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT rt.ref_id FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE t.namespace = %s AND t.value = %s",
            (TAPROOT_NAMESPACE, TAPROOT_CLAIM),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _edges_from(store: Any, src: int) -> list[tuple[int, str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id, relation, meta FROM links WHERE src_ref_id = %s",
            (src,),
        ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2] or {})) for r in rows]


_VERIFY_YES = {
    "supports": "yes",
    "support_reason": "direct measurement statement",
    "caveats": ["only tested at room temperature"],
    "cited_others": [],
    "terminal": True,
}

_VERIFY_NO = {
    "supports": "no",
    "support_reason": "chunk does not corroborate the claim",
    "caveats": [],
    "cited_others": [],
    "terminal": True,
}


# ── verification -> edge meta mapping, new-hub path ─────────────────────


def test_established_finding_mints_hub_and_attaches_evidence_with_mapped_meta(
    store: Any,
) -> None:
    paper = _seed_paper(
        store,
        cite_key="primary",
        blocks=["A direct measurement statement with no citations."],
    )
    finding = _seed_finding(store, cite_key="primary")

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        outcome, _ev = _advance(
            store,
            finding,
            with_llm=True,
            taproot_enabled=True,
            taproot_embedder=make_mock_bge_m3(),
        )
    assert outcome == "terminated"
    assert _status(store, finding.ref_id) == "established"

    hubs = _hub_ref_ids(store)
    assert len(hubs) == 1
    hub = hubs[0]

    edges = _edges_from(store, paper)
    assert len(edges) == 1
    dst, relation, meta = edges[0]
    assert dst == hub
    assert relation == "corroborates"  # apply_placement's default role
    assert meta == {
        "support": "yes",
        "support_reason": "direct measurement statement",
        "caveats": ["only tested at room temperature"],
        "char_offset": None,
        "source_handle": "primary~0",
    }


# ── NO-SUPPORT / NO-CLAIM skips ──────────────────────────────────────────


def test_no_support_verdict_skips_the_bridge(store: Any) -> None:
    paper = _seed_paper(
        store, cite_key="nosupport", blocks=["An unrelated measurement."]
    )
    finding = _seed_finding(store, cite_key="nosupport")

    with patch(_VERIFY_PATH, return_value=_VERIFY_NO):
        outcome, _ev = _advance(
            store,
            finding,
            with_llm=True,
            taproot_enabled=True,
            taproot_embedder=make_mock_bge_m3(),
        )
    # The deterministic chase outcome is untouched by the taproot skip.
    assert outcome == "terminated"
    assert _status(store, finding.ref_id) == "established"

    assert _hub_ref_ids(store) == []
    assert _edges_from(store, paper) == []


def test_empty_claim_title_skips_the_bridge(store: Any) -> None:
    """The NO-CLAIM equivalent here: an (unrealistic, defensive) blank
    finding title has nothing to canonicalize into a claim sentence."""
    paper = _seed_paper(store, cite_key="blanktitle", blocks=["A measurement."])
    finding = _seed_finding(store, cite_key="blanktitle")
    store.update_ref(finding.ref_id, title="")
    finding = FindingRow(ref_id=finding.ref_id, title="", meta=finding.meta)

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        outcome, _ev = _advance(
            store,
            finding,
            with_llm=True,
            taproot_enabled=True,
            taproot_embedder=make_mock_bge_m3(),
        )
    assert outcome == "terminated"
    assert _status(store, finding.ref_id) == "established"

    assert _hub_ref_ids(store) == []
    assert _edges_from(store, paper) == []


# ── flag gating — independent of PRECIS_CHASE_LLM ───────────────────────


def test_flag_off_writes_nothing_even_with_a_verdict_present(store: Any) -> None:
    paper = _seed_paper(store, cite_key="flagoff", blocks=["A measurement."])
    finding = _seed_finding(store, cite_key="flagoff")

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        outcome, _ev = _advance(
            store,
            finding,
            with_llm=True,
            taproot_enabled=False,  # the gate under test
            taproot_embedder=make_mock_bge_m3(),
        )
    assert outcome == "terminated"
    assert _status(store, finding.ref_id) == "established"

    assert _hub_ref_ids(store) == []
    assert _edges_from(store, paper) == []


def test_flag_on_without_llm_verdict_writes_nothing(store: Any) -> None:
    """``taproot_enabled=True`` alone is not enough — the bridge also
    needs the chase LLM verdict (``with_llm=True``); deterministic chase
    runs (no ``verification``) never reach it."""
    paper = _seed_paper(store, cite_key="nollm", blocks=["A direct statement."])
    finding = _seed_finding(store, cite_key="nollm")

    outcome, _ev = _advance(
        store,
        finding,
        with_llm=False,
        taproot_enabled=True,
        taproot_embedder=make_mock_bge_m3(),
    )
    assert outcome == "terminated"
    assert _status(store, finding.ref_id) == "established"

    assert _hub_ref_ids(store) == []
    assert _edges_from(store, paper) == []


def test_no_embedder_degrades_to_a_no_op_instead_of_crashing(store: Any) -> None:
    paper = _seed_paper(store, cite_key="noembedder", blocks=["A measurement."])
    finding = _seed_finding(store, cite_key="noembedder")

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        outcome, _ev = _advance(
            store,
            finding,
            with_llm=True,
            taproot_enabled=True,
            taproot_embedder=None,
        )
    assert outcome == "terminated"
    assert _status(store, finding.ref_id) == "established"
    assert _hub_ref_ids(store) == []
    assert _edges_from(store, paper) == []


# ── idempotency — a re-established finding doesn't duplicate the hub/edge ──


def test_reestablished_finding_does_not_duplicate_hub_or_edge(store: Any) -> None:
    embedder = make_mock_bge_m3()
    paper = _seed_paper(
        store, cite_key="reestablish", blocks=["A direct measurement statement."]
    )
    finding = _seed_finding(store, cite_key="reestablish")

    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        result = run_finding_chase_pass(
            store,
            limit=10,
            with_llm=True,
            taproot_enabled=True,
            taproot_embedder=embedder,
        )
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    hubs = _hub_ref_ids(store)
    assert len(hubs) == 1
    hub = hubs[0]
    edges = _edges_from(store, paper)
    assert len(edges) == 1

    # Simulate the async card_forge + embed passes that would, in
    # production, run some time after mint_hub before a redispatch: the
    # hub's card_combined chunk exists and carries an embedding (the same
    # deterministic vector the query re-embeds to, since the claim
    # sentence — the finding title — hasn't changed).
    with store.pool.connection() as conn:
        card = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, -1, 'card_combined', %s) RETURNING chunk_id",
            (hub, finding.title),
        ).fetchone()
        assert card is not None
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
            "VALUES (%s, %s, %s, 'ok')",
            (card[0], "bge-m3", embedder.embed_one(finding.title)),
        )
        conn.commit()

    # Simulate a redispatch: STATUS flips back to tracing so the finding
    # re-enters the claim pool.
    store.add_tag(
        finding.ref_id,
        Tag.closed("STATUS", "tracing"),
        set_by="chase",
        replace_prefix=True,
    )

    with (
        patch(_VERIFY_PATH, return_value=_VERIFY_YES),
        patch(
            _DEDUP_PATH,
            return_value={
                "verdict": "same",
                "confidence": 0.99,
                "rationale": "identical claim text",
            },
        ),
    ):
        result = run_finding_chase_pass(
            store,
            limit=10,
            with_llm=True,
            taproot_enabled=True,
            taproot_embedder=embedder,
        )
    # Not asserting claimed==1 here: the freshly-minted hub is itself a
    # `kind='finding'` ref carrying STATUS:tracing (hub.py's own
    # "no resolved originators yet" tag), so this pass's claim also picks
    # it up — a harmless empty-chain dead_chain for the hub, orthogonal to
    # what this test is pinning down (see the gripe filed for this
    # tracing-vocabulary overlap). failed==0 is what matters: no crash.
    assert result["failed"] == 0
    assert _status(store, finding.ref_id) == "established"

    # Still exactly one hub, one paper->hub edge — the second pass
    # attached to the existing hub rather than minting a duplicate.
    assert _hub_ref_ids(store) == [hub]
    edges_after = _edges_from(store, paper)
    assert len(edges_after) == 1
    assert edges_after[0][0] == hub


# ── the uncovered race: mint collision must converge, not drop ──────────


def test_two_findings_same_claim_without_pre_embedding_converge_to_one_hub(
    store: Any,
) -> None:
    """The real (previously data-losing) race: finding A's bridge mints a
    hub for claim X. That hub's ``card_combined`` chunk gets embedded only
    later, async, by card_forge/embed (ADR 0007) — deliberately NOT
    simulated here (contrast ``test_reestablished_finding_does_not_duplicate_
    hub_or_edge`` above, which inserts the embedding by hand). So when
    finding B's bridge runs for the SAME claim X before that embed lands,
    ``canon.block`` ANN-joins against ``chunk_embeddings`` and finds zero
    rows for X's hub -> ``judged == []`` -> ``place()`` returns ``"new"``
    for B too, and both bridges call ``mint_hub`` for the identical
    deterministic pub_id.

    Before the fix: B's ``mint_hub`` raised ``UniqueViolation`` on the
    ``ref_identifiers`` PK, caught by ``_taproot_bridge``'s blanket
    ``except``, and B's evidence edge was silently, permanently lost.
    After the fix: B's ``mint_hub`` converges to A's hub (savepoint-
    isolated rollback of B's partial write + a pub_id lookup), so both
    papers end up attached as evidence on the SAME single hub.
    """
    same_title = "Pd/C catalyzes Suzuki coupling at room temperature."
    paper_a = _seed_paper(store, cite_key="racea", blocks=["A direct statement."])
    paper_b = _seed_paper(store, cite_key="raceb", blocks=["Another direct statement."])
    finding_a = _seed_finding(store, cite_key="racea", title=same_title)
    finding_b = _seed_finding(store, cite_key="raceb", title=same_title)

    embedder = make_mock_bge_m3()
    with patch(_VERIFY_PATH, return_value=_VERIFY_YES):
        outcome_a, _ = _advance(
            store,
            finding_a,
            with_llm=True,
            taproot_enabled=True,
            taproot_embedder=embedder,
        )
        # No card_combined embedding inserted for A's hub here — the
        # window this test targets is exactly the gap before that
        # async embed lands.
        outcome_b, _ = _advance(
            store,
            finding_b,
            with_llm=True,
            taproot_enabled=True,
            taproot_embedder=embedder,
        )

    assert outcome_a == "terminated"
    assert outcome_b == "terminated"
    assert _status(store, finding_a.ref_id) == "established"
    assert _status(store, finding_b.ref_id) == "established"

    hubs = _hub_ref_ids(store)
    assert len(hubs) == 1  # no duplicate hub minted
    hub = hubs[0]

    edges_a = _edges_from(store, paper_a)
    edges_b = _edges_from(store, paper_b)
    assert len(edges_a) == 1 and edges_a[0][0] == hub
    assert len(edges_b) == 1 and edges_b[0][0] == hub  # not dropped
