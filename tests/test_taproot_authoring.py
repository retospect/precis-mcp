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
from precis.handlers.finding import FindingHandler
from precis.taproot.authoring import resolve_paper_ref_id, seed_claim_hub
from precis.taproot.seniority import derive_evidence
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
