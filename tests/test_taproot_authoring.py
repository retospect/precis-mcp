"""Cite-seeded claim-hub authoring (`src/precis/taproot/authoring.py`) +
`precis taproot mint` CLI (`src/precis/cli/taproot.py`).

DB-backed (real `refs`/`chunks`/`ref_tags`/`links`/`ref_identifiers` via the
`store` fixture); no LLM -- `seed_claim_hub` writes through the existing
hub primitives (`precis.taproot.hub.mint_hub` / `attach_evidence`) directly.
Mirrors the setup style of `tests/test_taproot_hub.py`.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.handlers.finding import FindingHandler
from precis.taproot.authoring import (
    resolve_hub_ref_id,
    resolve_paper_ref_id,
    seed_claim_hub,
)
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.taproot.seniority import derive_evidence
from precis.utils import handle_registry
from tests.conftest import _active_dsn
from tests.workers._helpers import seed_ref


def _ref_tag(store: Any, ref_id: int, ns: str) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = %s",
            (ref_id, ns),
        ).fetchone()
    return row[0] if row else None


def _pub_id_row(store: Any, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'pub_id'",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def _edges(store: Any, *, src: int, dst: int) -> list[tuple[str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT relation, meta FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (src, dst),
        ).fetchall()
    return [(r[0], r[1] or {}) for r in rows]


# ── happy path ──────────────────────────────────────────────────────────


def test_seed_claim_hub_happy_path(store: Any) -> None:
    paper = seed_ref(store, title="Collins 2006", kind="paper")

    out = seed_claim_hub(
        store,
        sentence="Pd/C catalyzes Suzuki coupling at room temperature.",
        scope={"material": "Pd/C"},
        supporters=[
            {
                "paper": paper,
                "role": "corroborates",
                "source_handle": "pc123",
            }
        ],
    )

    assert out["attached"] == 1
    assert out["already"] == 0
    hub_ref_id = out["hub_ref_id"]

    # A pub_id row exists in ref_identifiers for the hub.
    assert _pub_id_row(store, hub_ref_id) == out["pub_id"]

    # The hub is a TAPROOT:claim finding.
    assert _ref_tag(store, hub_ref_id, "TAPROOT") == "claim"

    # derive_evidence surfaces the paper as a corroborator.
    evidence = derive_evidence(store, hub_ref_id)
    corroborator_ids = [e.paper_ref_id for e in evidence.corroborators]
    assert paper in corroborator_ids
    matching = [e for e in evidence.corroborators if e.paper_ref_id == paper]
    assert matching[0].source_handle == "pc123"


# ── scope_lint advisory key (wired 2026-08-20) ──────────────────────────


def test_seed_claim_hub_scope_lint_fires_for_prose_scope(store: Any) -> None:
    """A prose scope value (>4 words) trips ``lint_scope`` -- the mint
    result carries the warning under ``scope_lint`` -- but the hub is
    minted and the evidence edge attached regardless: advisory means
    advisory, never blocking."""
    paper = seed_ref(store, title="Sensor Paper 2022", kind="paper")

    out = seed_claim_hub(
        store,
        sentence="Amine loading raises CO2 capacity in aqueous solvents.",
        scope={"method": "engineered into printed touch sensors"},
        supporters=[{"paper": paper}],
    )

    assert out["scope_lint"]
    assert any("scope-free-text" in w for w in out["scope_lint"])

    # Critical assertion: the mint is NOT blocked.
    assert out["attached"] == 1
    assert _ref_tag(store, out["hub_ref_id"], "TAPROOT") == "claim"
    assert _pub_id_row(store, out["hub_ref_id"]) == out["pub_id"]


def test_seed_claim_hub_scope_lint_empty_for_clean_scope(store: Any) -> None:
    """A clean, controlled-term scope value produces no ``scope_lint``
    warnings."""
    paper = seed_ref(store, title="Clean Scope Paper 2022", kind="paper")

    out = seed_claim_hub(
        store,
        sentence="Pd/C catalyzes Suzuki coupling at room temperature.",
        scope={"material": "Pd/C"},
        supporters=[{"paper": paper}],
    )

    assert out["scope_lint"] == []
    assert out["attached"] == 1


# ── chunk grounding (source_handle → src_chunk_id) ────────────────────────


def _src_chunk_id(store: Any, *, src: int, dst: int, relation: str) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT src_chunk_id FROM links WHERE src_ref_id = %s "
            "AND dst_ref_id = %s AND relation = %s",
            (src, dst, relation),
        ).fetchone()
    return None if row is None else row[0]


def test_source_handle_grounds_edge_at_paper_chunk(store: Any) -> None:
    """A supporter whose ``source_handle`` resolves to a real paper chunk
    lands on the edge as ``src_chunk_id`` — the edge cites the passage
    (``pc<id>``), not just the paper (ref-level ``pa<id>``)."""
    from precis.store.types import ChunkInsert

    paper = seed_ref(store, title="Wu 2022", kind="paper")
    store.chunks.insert_chunks(
        paper, [ChunkInsert(ord=0, text="Rotaxane passage.", meta={})]
    )
    with store.pool.connection() as conn:
        chunk_id = int(
            conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord LIMIT 1",
                (paper,),
            ).fetchone()[0]
        )
    pc = handle_registry.format_handle("paper", chunk_id, chunk=True)

    out = seed_claim_hub(
        store,
        sentence="Rotaxanes act as molecular machines.",
        scope={},
        supporters=[{"paper": paper, "role": "corroborates", "source_handle": pc}],
    )
    assert out["attached"] == 1
    assert (
        _src_chunk_id(store, src=paper, dst=out["hub_ref_id"], relation="corroborates")
        == chunk_id
    )


def test_two_passages_of_one_paper_are_two_edges(store: Any) -> None:
    """Two supporters, same paper, different grounding chunks → two edges
    (the ``set of chunks that support this point``), not one collapsed
    ref-level edge. The dedup key now includes the grounding chunk."""
    from precis.store.types import ChunkInsert

    paper = seed_ref(store, title="Wu 2022", kind="paper")
    store.chunks.insert_chunks(
        paper,
        [
            ChunkInsert(ord=0, text="First supporting passage.", meta={}),
            ChunkInsert(ord=1, text="Second supporting passage.", meta={}),
        ],
    )
    with store.pool.connection() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord", (paper,)
            ).fetchall()
        ]
    pc0 = handle_registry.format_handle("paper", ids[0], chunk=True)
    pc1 = handle_registry.format_handle("paper", ids[1], chunk=True)

    out = seed_claim_hub(
        store,
        sentence="Rotaxanes act as molecular machines.",
        scope={},
        supporters=[
            {"paper": paper, "role": "corroborates", "source_handle": pc0},
            {"paper": paper, "role": "corroborates", "source_handle": pc1},
        ],
    )
    assert out["attached"] == 2
    assert not out["collapsed"]
    with store.pool.connection() as conn:
        src_chunks = {
            r[0]
            for r in conn.execute(
                "SELECT src_chunk_id FROM links WHERE src_ref_id = %s "
                "AND dst_ref_id = %s AND relation = 'corroborates'",
                (paper, out["hub_ref_id"]),
            ).fetchall()
        }
    assert src_chunks == {ids[0], ids[1]}


# ── support is a verdict, never a default ────────────────────────────────


def test_supporter_without_verified_trio_is_born_withheld(store: Any) -> None:
    """A supporter that attests no verification (no support/support_reason/
    verified_by trio) mints an edge with NO ``support`` key at all — born
    withheld behind ``nanopub.preflight.withheld_edges`` — never the old
    default ``support: "yes"``. A partial trio (a bare ``support`` with no
    reason/verifier) degrades the same way."""
    paper = seed_ref(store, title="Bare 2024", kind="paper")
    partial = seed_ref(store, title="Partial 2024", kind="paper")

    out = seed_claim_hub(
        store,
        sentence="Zeolite frameworks stabilize isolated Cu sites.",
        scope={},
        supporters=[
            {"paper": paper, "role": "corroborates"},
            # `support` alone is not a verdict — nobody stands behind it.
            {"paper": partial, "role": "corroborates", "support": "yes"},
        ],
    )
    assert out["attached"] == 2

    for src in (paper, partial):
        edges = _edges(store, src=src, dst=out["hub_ref_id"])
        assert len(edges) == 1
        _relation, meta = edges[0]
        assert "support" not in meta
        assert "caveats" not in meta
        assert "verified_by" not in meta


def test_supporter_with_verified_trio_lands_the_six_key_stamp(store: Any) -> None:
    """A supporter carrying the full trio (support + support_reason +
    verified_by — a caller attesting a real verification) lands the same
    six-key stamp shape ``taproot.verify_edges`` writes, with
    ``verified_at``/``verified_claim_sha`` stamped at mint time."""
    from precis.taproot.canon import claim_sha

    paper = seed_ref(store, title="Verified 2024", kind="paper")
    sentence = "Cu-CHA converts methane to methanol stepwise."

    out = seed_claim_hub(
        store,
        sentence=sentence,
        scope={},
        supporters=[
            {
                "paper": paper,
                "role": "corroborates",
                "support": "yes",
                "support_reason": "the passage reports the stepwise conversion",
                "verified_by": "reto",
                "caveats": ["stoichiometric, not catalytic"],
            }
        ],
    )
    assert out["attached"] == 1

    edges = _edges(store, src=paper, dst=out["hub_ref_id"])
    assert len(edges) == 1
    _relation, meta = edges[0]
    assert meta["support"] == "yes"
    assert meta["support_reason"] == "the passage reports the stepwise conversion"
    assert meta["caveats"] == ["stoichiometric, not catalytic"]
    assert meta["verified_by"] == "reto"
    assert meta["verified_at"]
    assert meta["verified_claim_sha"] == claim_sha(sentence)


# ── idempotency ──────────────────────────────────────────────────────────


def test_seed_claim_hub_is_idempotent(store: Any) -> None:
    paper = seed_ref(store, title="Collins 2006", kind="paper")
    spec: dict[str, Any] = {
        "sentence": "Pd/C catalyzes Suzuki coupling at room temperature.",
        "scope": {"material": "Pd/C"},
        "supporters": [{"paper": paper, "role": "corroborates"}],
    }

    first = seed_claim_hub(store, **spec)
    second = seed_claim_hub(store, **spec)

    assert second["pub_id"] == first["pub_id"]
    assert second["hub_ref_id"] == first["hub_ref_id"]
    assert first["attached"] == 1
    assert second["attached"] == 0
    assert second["already"] == 1

    # No duplicate hub, no duplicate links row.
    with store.pool.connection() as conn:
        hub_count = conn.execute(
            "SELECT count(*) FROM ref_identifiers WHERE id_kind = 'pub_id' "
            "AND id_value = %s",
            (first["pub_id"],),
        ).fetchone()[0]
    assert hub_count == 1
    assert len(_edges(store, src=paper, dst=first["hub_ref_id"])) == 1


def test_seed_claim_hub_multi_supporter_idempotent_via_shared_connection(
    store: Any,
) -> None:
    """``seed_claim_hub``'s ``_evidence_edge_exists`` check now reuses one
    connection across the supporter loop (efficiency-only change) — a
    multi-supporter mint must still attach each new edge exactly once and
    detect every one of them as already-present on a re-run, same as
    before the connection was threaded through."""
    papers = [
        seed_ref(store, title=f"Multi-supporter {i}", kind="paper") for i in range(3)
    ]
    spec: dict[str, Any] = {
        "sentence": "Grubbs catalysts enable ring-closing metathesis.",
        "scope": {},
        "supporters": [{"paper": p, "role": "corroborates"} for p in papers],
    }

    first = seed_claim_hub(store, **spec)
    assert first["attached"] == 3
    assert first["already"] == 0
    assert not first["collapsed"]

    second = seed_claim_hub(store, **spec)
    assert second["hub_ref_id"] == first["hub_ref_id"]
    assert second["attached"] == 0
    assert second["already"] == 3

    for paper in papers:
        assert len(_edges(store, src=paper, dst=first["hub_ref_id"])) == 1


# ── many-to-many (load-bearing) ─────────────────────────────────────────


def test_seed_claim_hub_many_to_many_one_paper_two_hubs(store: Any) -> None:
    """One fixture paper supports TWO different claims -> two distinct
    hubs, and the paper carries a distinct evidence edge to each, each with
    its own source_handle."""
    paper = seed_ref(store, title="Shared Corroborator 2012", kind="paper")

    out_a = seed_claim_hub(
        store,
        sentence="Pd/C catalyzes Suzuki coupling at room temperature.",
        scope={"material": "Pd/C", "method": "Suzuki coupling"},
        supporters=[{"paper": paper, "source_handle": "pc111"}],
    )
    out_b = seed_claim_hub(
        store,
        sentence="Nickel foam electrodes reduce overpotential in alkaline OER.",
        scope={"material": "Ni foam", "method": "OER"},
        supporters=[{"paper": paper, "source_handle": "pc222"}],
    )

    assert out_a["pub_id"] != out_b["pub_id"]
    assert out_a["hub_ref_id"] != out_b["hub_ref_id"]

    # Both hubs resolve as live TAPROOT:claim findings.
    assert _ref_tag(store, out_a["hub_ref_id"], "TAPROOT") == "claim"
    assert _ref_tag(store, out_b["hub_ref_id"], "TAPROOT") == "claim"

    # The paper has an evidence edge to BOTH hubs, each with its own
    # source_handle.
    edges_a = _edges(store, src=paper, dst=out_a["hub_ref_id"])
    edges_b = _edges(store, src=paper, dst=out_b["hub_ref_id"])
    assert len(edges_a) == 1
    assert len(edges_b) == 1
    assert edges_a[0][1].get("source_handle") == "pc111"
    assert edges_b[0][1].get("source_handle") == "pc222"

    evidence_a = derive_evidence(store, out_a["hub_ref_id"])
    evidence_b = derive_evidence(store, out_b["hub_ref_id"])
    assert paper in [e.paper_ref_id for e in evidence_a.corroborators]
    assert paper in [e.paper_ref_id for e in evidence_b.corroborators]


# ── resolver variety: ref_id / handle / cite_key ────────────────────────


def test_seed_claim_hub_resolves_paper_handle_and_cite_key(store: Any) -> None:
    ref = store.insert_ref(
        kind="paper", slug="authoring-test-2020", title="Slugged Paper"
    )
    paper = ref.id

    out_handle = seed_claim_hub(
        store,
        sentence="A handle-addressed supporter attaches fine.",
        scope={},
        supporters=[{"paper": f"pa{paper}"}],
    )
    out_slug = seed_claim_hub(
        store,
        sentence="A slug-addressed supporter attaches fine too.",
        scope={},
        supporters=[{"paper": "authoring-test-2020"}],
    )

    assert _edges(store, src=paper, dst=out_handle["hub_ref_id"])
    assert _edges(store, src=paper, dst=out_slug["hub_ref_id"])


# ── F1: non-paper supporters are rejected ───────────────────────────────


def test_resolve_paper_ref_id_rejects_non_paper_kind(store: Any) -> None:
    todo = seed_ref(store, title="a todo", kind="todo")

    with pytest.raises(BadInput):
        resolve_paper_ref_id(store, todo)


def test_resolve_paper_ref_id_accepts_patent(store: Any) -> None:
    patent = seed_ref(store, title="A patent", kind="patent")

    assert resolve_paper_ref_id(store, patent) == patent


def test_seed_claim_hub_rejects_non_paper_supporter(store: Any) -> None:
    memory = seed_ref(store, title="a memory", kind="memory")

    with pytest.raises(BadInput):
        seed_claim_hub(
            store,
            sentence="A claim wrongly sourced from a memory ref.",
            scope={},
            supporters=[{"paper": memory}],
        )


def test_seed_claim_hub_rejects_contradicts_supporter_role(store: Any) -> None:
    # Migration 0150 split (D4): `contradicts` is adjudication-derived —
    # the mint-time supporters= door must refuse it like every other
    # agent-facing door, and must not mint a half-attached hub first.
    paper = seed_ref(store, title="a contrary paper", kind="paper")

    with pytest.raises(BadInput, match="adjudication-derived"):
        seed_claim_hub(
            store,
            sentence="A claim whose supporter tries to pre-contradict it.",
            scope={},
            supporters=[
                {"paper": paper, "role": "contradicts", "source_handle": "pc9"}
            ],
        )
    # No contradicts edge landed from the refused supporter.
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND relation = 'contradicts'",
            (paper,),
        ).fetchall()
    assert rows == []


# ── gr263195: validate-first + atomicity ─────────────────────────────


def test_seed_claim_hub_invalid_supporter_writes_nothing(store: Any) -> None:
    """A batch with one valid and one invalid (wrong-kind) supporter fails
    closed -- no hub ref, no chunks, no tags, no evidence edge for EITHER
    supporter. Before the fix, ``mint_hub`` committed before the
    supporter loop ever resolved kinds, so an invalid supporter left a
    durable zero-evidence orphan hub behind (observed: fi263178)."""
    good_paper = seed_ref(store, title="A good paper", kind="paper")
    bad_ref = seed_ref(store, title="Not evidence-sourced", kind="memory")

    with store.pool.connection() as conn:
        before = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding'"
        ).fetchone()[0]

    with pytest.raises(BadInput):
        seed_claim_hub(
            store,
            sentence="A claim with one bad supporter among the good.",
            scope={},
            supporters=[
                {"paper": good_paper, "role": "corroborates"},
                {"paper": bad_ref, "role": "corroborates"},
            ],
        )

    with store.pool.connection() as conn:
        after = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding'"
        ).fetchone()[0]
        chunk_count = conn.execute(
            "SELECT count(*) FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
            "WHERE r.kind = 'finding'"
        ).fetchone()[0]
        tag_count = conn.execute(
            "SELECT count(*) FROM ref_tags rt JOIN refs r ON r.ref_id = rt.ref_id "
            "WHERE r.kind = 'finding'"
        ).fetchone()[0]
        edge_count = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id IN (%s, %s)",
            (good_paper, bad_ref),
        ).fetchone()[0]

    # No orphan hub -- the finding count is unchanged, and neither
    # supporter's paper got an evidence edge.
    assert after == before
    assert chunk_count == 0
    assert tag_count == 0
    assert edge_count == 0


def test_seed_claim_hub_all_valid_supporters_still_mints_with_edges(
    store: Any,
) -> None:
    """The validate-first pass doesn't change the happy path: an all-valid
    multi-supporter batch still mints the hub and attaches every edge."""
    paper_a = seed_ref(store, title="Supporter A", kind="paper")
    paper_b = seed_ref(store, title="Supporter B", kind="paper")

    out = seed_claim_hub(
        store,
        sentence="Every supporter here resolves cleanly.",
        scope={},
        supporters=[
            {"paper": paper_a, "role": "corroborates"},
            {"paper": paper_b, "role": "establishes"},
        ],
    )

    assert out["attached"] == 2
    assert out["already"] == 0
    assert _ref_tag(store, out["hub_ref_id"], "TAPROOT") == "claim"
    assert _edges(store, src=paper_a, dst=out["hub_ref_id"])
    assert _edges(store, src=paper_b, dst=out["hub_ref_id"])


def test_seed_claim_hub_accepts_patent_supporter(store: Any) -> None:
    patent = seed_ref(store, title="A patent", kind="patent")

    out = seed_claim_hub(
        store,
        sentence="A claim correctly sourced from a patent.",
        scope={},
        supporters=[{"paper": patent}],
    )

    assert out["attached"] == 1
    assert _edges(store, src=patent, dst=out["hub_ref_id"])


# ── F3: a collapsed duplicate supporter is surfaced, not silent ─────────


def test_seed_claim_hub_surfaces_collapsed_duplicate_supporter(store: Any) -> None:
    paper = seed_ref(store, title="Collins 2006", kind="paper")

    out = seed_claim_hub(
        store,
        sentence="Two supporters differing only by source_handle.",
        scope={},
        supporters=[
            {"paper": paper, "role": "corroborates", "source_handle": "pc1"},
            {"paper": paper, "role": "corroborates", "source_handle": "pc2"},
        ],
    )

    assert out["attached"] == 1
    assert out["already"] == 0
    assert len(out["collapsed"]) == 1
    assert out["collapsed"][0]["source_handle"] == "pc2"

    # Only one edge, carrying the FIRST supporter's meta.
    edges = _edges(store, src=paper, dst=out["hub_ref_id"])
    assert len(edges) == 1
    assert edges[0][1].get("source_handle") == "pc1"


def test_seed_claim_hub_finding_search_surfaces_hub(store: Any) -> None:
    paper = seed_ref(store, title="Collins 2006", kind="paper")
    out = seed_claim_hub(
        store,
        sentence="Pd/C catalyzes Suzuki coupling at room temperature.",
        scope={},
        supporters=[{"paper": paper}],
    )
    handler = FindingHandler(hub=Hub(store=store))
    result = handler.search(tags=["TAPROOT:claim"])
    assert str(out["hub_ref_id"]) in result.body


# ── CLI smoke ────────────────────────────────────────────────────────────


def _cli_args(**overrides: Any) -> argparse.Namespace:
    base = {
        "spec": None,
        "json_spec": None,
        "dry_run": False,
        "format": "text",
        "set_by": "agent",
        "database_url": _active_dsn(),
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_mint_smoke(store: Any, capsys: Any) -> None:
    from precis.cli import taproot as taproot_cli

    paper = seed_ref(store, title="Collins 2006", kind="paper")
    spec = json.dumps(
        [
            {
                "sentence": "Pd/C catalyzes Suzuki coupling at room temperature.",
                "scope": {"material": "Pd/C"},
                "supporters": [
                    {"paper": paper, "role": "corroborates", "source_handle": "pc1"}
                ],
            }
        ]
    )

    args = _cli_args(json_spec=spec)
    args.taproot_cmd = "mint"
    taproot_cli.run(args)

    out = capsys.readouterr().out
    assert "+1 evidence" in out
    assert "0 already" in out

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding'"
        ).fetchone()
    assert row[0] == 1


def test_cli_mint_prints_lint_warning_to_stderr(store: Any, capsys: Any) -> None:
    """``_print_results`` collects ``notation``/``scope_lint`` per result
    and prints each to stderr as ``lint: <pub_id>  <warning>`` -- advisory,
    doesn't block the mint (stdout still reports the attach)."""
    from precis.cli import taproot as taproot_cli

    paper = seed_ref(store, title="Sensor Paper 2022", kind="paper")
    spec = json.dumps(
        [
            {
                "sentence": ("Amine loading raises CO2 capacity in aqueous solvents."),
                "scope": {"method": "engineered into printed touch sensors"},
                "supporters": [{"paper": paper, "role": "corroborates"}],
            }
        ]
    )

    args = _cli_args(json_spec=spec)
    args.taproot_cmd = "mint"
    taproot_cli.run(args)

    captured = capsys.readouterr()
    assert "+1 evidence" in captured.out
    assert "lint: " in captured.err
    assert "scope-free-text" in captured.err

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding'"
        ).fetchone()
    assert row[0] == 1


def test_cli_mint_no_lint_line_for_clean_spec(store: Any, capsys: Any) -> None:
    """A clean spec (no notation/scope_lint warnings) prints no ``lint:``
    line to stderr."""
    from precis.cli import taproot as taproot_cli

    paper = seed_ref(store, title="Clean Paper 2022", kind="paper")
    spec = json.dumps(
        [
            {
                "sentence": "Pd/C catalyzes Suzuki coupling at room temperature.",
                "scope": {"material": "Pd/C"},
                "supporters": [
                    {"paper": paper, "role": "corroborates", "source_handle": "pc1"}
                ],
            }
        ]
    )

    args = _cli_args(json_spec=spec)
    args.taproot_cmd = "mint"
    taproot_cli.run(args)

    captured = capsys.readouterr()
    assert "+1 evidence" in captured.out
    assert "lint: " not in captured.err


def test_cli_mint_dry_run_writes_nothing(store: Any, capsys: Any) -> None:
    from precis.cli import taproot as taproot_cli

    paper = seed_ref(store, title="Collins 2006", kind="paper")
    spec = json.dumps(
        [
            {
                "sentence": "A brand-new dry-run-only claim.",
                "scope": {},
                "supporters": [{"paper": paper}],
            }
        ]
    )

    args = _cli_args(json_spec=spec, dry_run=True)
    args.taproot_cmd = "mint"
    taproot_cli.run(args)

    out = capsys.readouterr().out
    assert "DRY-RUN" in out

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding'"
        ).fetchone()
    assert row[0] == 0


# ── F2: pre-flight resolution blocks any partial write ──────────────────


def test_cli_mint_bad_second_claim_writes_nothing(store: Any, capsys: Any) -> None:
    from precis.cli import taproot as taproot_cli

    paper = seed_ref(store, title="Good Paper 2006", kind="paper")
    spec = json.dumps(
        [
            {
                "sentence": "Claim one has a perfectly good supporter.",
                "scope": {},
                "supporters": [{"paper": paper}],
            },
            {
                "sentence": "Claim two has an unresolvable supporter.",
                "scope": {},
                "supporters": [{"paper": "no-such-handle-zzz"}],
            },
        ]
    )

    args = _cli_args(json_spec=spec)
    args.taproot_cmd = "mint"
    with pytest.raises(SystemExit) as exc_info:
        taproot_cli.run(args)
    assert exc_info.value.code != 0

    # Nothing minted -- not even the hub for the FIRST (valid) claim.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding'"
        ).fetchone()
    assert row[0] == 0


# ── F4: dry-run reports true attached vs. already per supporter ─────────


def test_cli_mint_dry_run_new_supporter_on_existing_hub_reports_attached(
    store: Any, capsys: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    paper1 = seed_ref(store, title="Collins 2006", kind="paper")
    paper2 = seed_ref(store, title="A Second Corroborator", kind="paper")
    sentence = "Pd/C catalyzes Suzuki coupling at room temperature."

    existing = seed_claim_hub(
        store, sentence=sentence, scope={}, supporters=[{"paper": paper1}]
    )

    spec = json.dumps(
        [{"sentence": sentence, "scope": {}, "supporters": [{"paper": paper2}]}]
    )
    args = _cli_args(json_spec=spec, dry_run=True)
    args.taproot_cmd = "mint"
    taproot_cli.run(args)

    out = capsys.readouterr().out
    assert "+1 evidence" in out
    assert "0 already" in out

    # Still a dry-run: no second hub, no new edge for paper2.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding'"
        ).fetchone()
    assert row[0] == 1
    assert not _edges(store, src=paper2, dst=existing["hub_ref_id"])


# ── resolve_hub_ref_id + `precis taproot refine` (migration 0100) ───────

_CLAIM_A = CanonicalClaim(sentence="Claim A: the original wording.", scope={})
_CLAIM_B = CanonicalClaim(sentence="Claim B: a sharper wording.", scope={})


def test_resolve_hub_ref_id_by_handle_and_int(store: Any) -> None:
    hub = mint_hub(store, _CLAIM_A)
    handle = handle_registry.format_handle("finding", hub)

    assert resolve_hub_ref_id(store, hub) == hub  # bare ref_id
    assert resolve_hub_ref_id(store, handle) == hub  # fi<id> handle


def test_resolve_hub_ref_id_rejects_non_hub(store: Any) -> None:
    paper = seed_ref(store, title="Not a hub", kind="paper")
    with pytest.raises(BadInput):
        resolve_hub_ref_id(store, paper)


def _cli_refine_args(**overrides: Any) -> argparse.Namespace:
    base = {
        "from_hub": None,
        "to_hub": None,
        "dry_run": False,
        "set_by": "agent",
        "database_url": _active_dsn(),
        "taproot_cmd": "refine",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_refine_writes_a_refines_edge(store: Any, capsys: Any) -> None:
    from precis.cli import taproot as taproot_cli

    original = mint_hub(store, _CLAIM_A)
    sharper = mint_hub(store, _CLAIM_B)
    from_h = handle_registry.format_handle("finding", sharper)
    to_h = handle_registry.format_handle("finding", original)

    taproot_cli.run(_cli_refine_args(from_hub=from_h, to_hub=to_h))

    out = capsys.readouterr().out
    assert "linked" in out
    assert _edges(store, src=sharper, dst=original)  # edge written


def test_cli_refine_dry_run_writes_nothing(store: Any, capsys: Any) -> None:
    from precis.cli import taproot as taproot_cli

    original = mint_hub(store, _CLAIM_A)
    sharper = mint_hub(store, _CLAIM_B)
    from_h = handle_registry.format_handle("finding", sharper)
    to_h = handle_registry.format_handle("finding", original)

    taproot_cli.run(_cli_refine_args(from_hub=from_h, to_hub=to_h, dry_run=True))

    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert not _edges(store, src=sharper, dst=original)  # nothing written


def test_cli_refine_bad_hub_exits_nonzero(store: Any) -> None:
    from precis.cli import taproot as taproot_cli

    original = mint_hub(store, _CLAIM_A)
    paper = seed_ref(store, title="Not a hub", kind="paper")
    to_h = handle_registry.format_handle("finding", original)
    paper_h = handle_registry.format_handle("paper", paper)

    with pytest.raises(SystemExit) as exc:
        taproot_cli.run(_cli_refine_args(from_hub=paper_h, to_hub=to_h))
    assert exc.value.code == 1


# ── `precis taproot backfill-grounding` ─────────────────────────────────


def test_backfill_grounding_part_b_grounds_paper_evidence_edge(store: Any) -> None:
    """A ref-level ``corroborates`` edge carrying a resolvable
    ``source_handle`` gets its ``src_chunk_id`` set to the referenced
    chunk."""
    from precis.cli.taproot import _backfill_grounding
    from precis.store.types import ChunkInsert

    paper = seed_ref(store, title="Wu 2022", kind="paper")
    store.chunks.insert_chunks(
        paper, [ChunkInsert(ord=0, text="Rotaxane passage.", meta={})]
    )
    with store.pool.connection() as conn:
        chunk_id = int(
            conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord LIMIT 1",
                (paper,),
            ).fetchone()[0]
        )
    pc = handle_registry.format_handle("paper", chunk_id, chunk=True)

    hub_ref_id = mint_hub(
        store, CanonicalClaim(sentence="A backfillable claim.", scope={})
    )
    # Ref-level edge (no src_pos) -- the pre-fix shape this backfills.
    store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub_ref_id,
        relation="corroborates",
        meta={"source_handle": pc},
    )
    assert (
        _src_chunk_id(store, src=paper, dst=hub_ref_id, relation="corroborates") is None
    )

    result = _backfill_grounding(store, dry_run=False)

    assert result["paper_candidates"] == 1
    assert result["paper_edges_grounded"] == 1
    assert result["unresolved"] == 0
    assert result["skipped_collision"] == 0
    assert (
        _src_chunk_id(store, src=paper, dst=hub_ref_id, relation="corroborates")
        == chunk_id
    )

    # Idempotent: nothing left to ground on a second pass.
    second = _backfill_grounding(store, dry_run=False)
    assert second["paper_candidates"] == 0
    assert second["paper_edges_grounded"] == 0


def test_backfill_grounding_part_a_resyncs_draft_cites_edge(store: Any) -> None:
    """A draft paragraph that cites a paper chunk already produces a
    chunk-grounded ``cites`` edge -- simulate the pre-fix ref-level shape
    by nulling ``src_chunk_id``, then confirm the backfill's resync
    restores the grounding at the citing paragraph."""
    from precis.cli.taproot import _backfill_grounding
    from precis.store.types import ChunkInsert

    paper = seed_ref(store, title="Wu 2022", kind="paper")
    store.chunks.insert_chunks(
        paper, [ChunkInsert(ord=0, text="Rotaxane nanomachines.", meta={})]
    )
    with store.pool.connection() as conn:
        chunk_id = int(
            conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord LIMIT 1",
                (paper,),
            ).fetchone()[0]
        )
    pc = handle_registry.format_handle("paper", chunk_id, chunk=True)

    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft = DraftHandler(hub=Hub(store=store))
    draft.put(id="bg-nt", title="T", project=proj)
    ref = store.get_ref(kind="draft", id="bg-nt")
    title_h = store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id="bg-nt",
        chunk_kind="paragraph",
        text=f"the effect holds [{pc}]",
        at={"after": "¶" + title_h},
    )
    para = store.drafts.reading_order(ref.id)[1]

    def _cites() -> list[Any]:
        return [
            link
            for link in store.links_for(ref.id, direction="out", relation="cites")
            if (link.meta or {}).get("auto") == "mention"
        ]

    cites = _cites()
    assert len(cites) == 1
    assert cites[0].src_chunk_id == para.chunk_id  # already grounded

    # Simulate the legacy ref-level shape this backfill exists to fix.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET src_chunk_id = NULL WHERE link_id = %s",
            (cites[0].id,),
        )
    assert _cites()[0].src_chunk_id is None

    result = _backfill_grounding(store, dry_run=False)

    assert result["drafts_found"] == 1
    assert result["drafts_resynced"] == 1
    assert result["draft_edges_before"] == 1
    assert result["draft_edges_after"] == 0

    cites_after = _cites()
    assert len(cites_after) == 1
    assert cites_after[0].src_chunk_id == para.chunk_id


def test_backfill_grounding_dry_run_writes_nothing(store: Any) -> None:
    from precis.cli.taproot import _backfill_grounding
    from precis.store.types import ChunkInsert

    paper = seed_ref(store, title="Wu 2022", kind="paper")
    store.chunks.insert_chunks(
        paper, [ChunkInsert(ord=0, text="Rotaxane passage.", meta={})]
    )
    with store.pool.connection() as conn:
        chunk_id = int(
            conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord LIMIT 1",
                (paper,),
            ).fetchone()[0]
        )
    pc = handle_registry.format_handle("paper", chunk_id, chunk=True)
    hub_ref_id = mint_hub(store, CanonicalClaim(sentence="A dry-run claim.", scope={}))
    store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub_ref_id,
        relation="corroborates",
        meta={"source_handle": pc},
    )

    result = _backfill_grounding(store, dry_run=True)

    assert result["dry_run"] is True
    assert result["paper_edges_grounded"] >= 1
    # Nothing written -- the edge is still ref-level.
    assert (
        _src_chunk_id(store, src=paper, dst=hub_ref_id, relation="corroborates") is None
    )


def _cli_backfill_args(**overrides: Any) -> argparse.Namespace:
    base = {
        "dry_run": False,
        "format": "text",
        "database_url": _active_dsn(),
        "taproot_cmd": "backfill-grounding",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_backfill_grounding_smoke(store: Any, capsys: Any) -> None:
    from precis.cli import taproot as taproot_cli

    taproot_cli.run(_cli_backfill_args())

    out = capsys.readouterr().out
    assert "drafts:" in out
    assert "paper/patent evidence:" in out
