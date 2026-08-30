"""Repairing evidence edges that assert support with no passage
(`src/precis/taproot/repair_evidence.py` + `precis taproot repair-evidence`),
per `docs/backlog/evidence-edges-assert-support-with-no-passage.md`.

DB-backed (real `refs`/`chunks`/`links` via the `store` fixture) but never
networked: every test injects `verify_batch_fn`, so the LLM verify step is a
local fake and the post-validation (quote verbatim in the claimed chunk,
unique across the paper) still runs for real -- that code path is the whole
anti-hallucination guarantee of the pass.

The load-bearing assertion repeated below is the **link count**: repair must
UPDATE the original row, never `attach_evidence` a second one (the conflict
key includes `src_chunk_id`, so an attach would leave the broken edge live).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.taproot.reground import AtomVerifyResult
from precis.taproot.repair_evidence import (
    repair_edge,
    select_broken_evidence_edges,
    select_prose_less_evidence_edges,
)
from precis.utils import handle_registry
from tests.conftest import _active_dsn
from tests.workers._helpers import seed_chunk, seed_ref

_CLAIM = "Graphene exhibits a tensile strength of 130 GPa."
_PASSAGE = "We measured that graphene exhibits a tensile strength of 130 GPa."

#: The exact broken meta the July batch left behind -- key present,
#: deliberately empty, with an affirmative verdict over it.
_BROKEN_META: dict[str, Any] = {"caveats": [], "support": "yes", "source_handle": None}


def _seed_broken_edge(
    store: Any,
    *,
    passage: str = _PASSAGE,
    claim: str = _CLAIM,
    relation: str = "corroborates",
) -> tuple[int, int, int, int]:
    """A hub + a source paper with one live body chunk + the broken edge.
    Returns ``(hub_ref_id, paper_ref_id, chunk_id, link_id)``."""
    paper = seed_ref(store, title="Lee 2008", kind="paper")
    chunk_id = seed_chunk(store, ref_id=paper, text=passage, ord=0)
    hub = mint_hub(store, CanonicalClaim(sentence=claim, scope={}))
    link = store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        relation=relation,
        meta=dict(_BROKEN_META),
    )
    return hub, paper, chunk_id, int(link.id)


def _link_row(store: Any, link_id: int) -> tuple[int | None, dict[str, Any]]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT src_chunk_id, meta FROM links WHERE link_id = %s", (link_id,)
        ).fetchone()
    assert row is not None
    return (int(row[0]) if row[0] is not None else None), dict(row[1] or {})


def _link_count(store: Any) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM links").fetchone()
    assert row is not None
    return int(row[0])


def _supported(chunk_ord: int, quote: str) -> Any:
    """Fake ``verify_batch_fn`` claiming support at ``chunk_ord``."""

    def _fn(atoms: Any, passages: Any) -> list[AtomVerifyResult]:
        return [AtomVerifyResult(0, True, chunk_ord, quote, None)]

    return _fn


def _rejects(atoms: Any, passages: Any) -> list[AtomVerifyResult]:
    """Fake ``verify_batch_fn`` judging every atom unsupported."""
    return [AtomVerifyResult(0, False, None, None, None)]


def _never_verify(atoms: Any, passages: Any) -> list[AtomVerifyResult]:
    raise AssertionError("verify_batch_fn should not have been called")


_FRONT_MATTER = (
    "Printed Touch Sensors Using Carbon NanoBud Material\n\n"
    "Anton S. Anisimov, David P. Brown, Bjorn F. Mikladal\n\n"
    "Canatu Oy, Helsinki, Finland"
)


def _seed_grounded_edge(
    store: Any, *, passage: str, claim: str = _CLAIM
) -> tuple[int, int, int, int]:
    """A hub + source paper + an edge that DOES anchor ``passage``.
    Returns ``(hub_ref_id, paper_ref_id, chunk_id, link_id)``."""
    paper = seed_ref(store, title="Anisimov 2018", kind="paper")
    chunk_id = seed_chunk(store, ref_id=paper, text=passage, ord=0)
    hub = mint_hub(store, CanonicalClaim(sentence=claim, scope={}))
    link = store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        relation="corroborates",
        src_pos=0,
        meta={"caveats": [], "support": "yes", "source_handle": f"pc{chunk_id}"},
    )
    return hub, paper, chunk_id, int(link.id)


# ── cohort B — grounded on a chunk that asserts nothing ─────────────────


def test_prose_less_cohort_selects_a_front_matter_grounding(store: Any) -> None:
    _hub, _paper, _chunk, link_id = _seed_grounded_edge(store, passage=_FRONT_MATTER)
    assert [e.link_id for e in select_prose_less_evidence_edges(store)] == [link_id]


def test_prose_less_cohort_maps_every_field_of_the_edge(store: Any) -> None:
    # link_id alone is not enough: a transposed column here would send
    # repair_edge at the wrong hub or the wrong source paper.
    hub, paper, _chunk, link_id = _seed_grounded_edge(store, passage=_FRONT_MATTER)
    (edge,) = select_prose_less_evidence_edges(store)
    assert (edge.link_id, edge.hub_ref_id, edge.source_ref_id) == (link_id, hub, paper)
    assert edge.source_kind == "paper"
    assert edge.relation == "corroborates"


def test_prose_less_cohort_skips_a_real_body_passage(store: Any) -> None:
    _seed_grounded_edge(store, passage=_PASSAGE)
    assert select_prose_less_evidence_edges(store) == []


def test_prose_less_cohort_excludes_cohort_a(store: Any) -> None:
    # Cohort A anchors NO passage; the two SQL predicates are disjoint on
    # src_chunk_id, so a --cohort both union needs no dedup.
    _seed_broken_edge(store)
    assert select_prose_less_evidence_edges(store) == []


def test_prose_less_cohort_catches_an_edge_anchored_on_a_retired_chunk(
    store: Any,
) -> None:
    # Between the two cohorts otherwise: cohort A wants src_chunk_id NULL, and
    # an inner join here would drop the row silently — leaving an edge anchored
    # on text no reader can reach with nothing that ever selects it.
    _hub, _paper, chunk_id, link_id = _seed_grounded_edge(store, passage=_PASSAGE)
    assert select_prose_less_evidence_edges(store) == []
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s", (chunk_id,)
        )
        conn.commit()
    assert [e.link_id for e in select_prose_less_evidence_edges(store)] == [link_id]


def test_prose_less_cohort_limit_applies_after_the_prose_filter(store: Any) -> None:
    # The limit is a prefix of the FILTERED cohort, not of the candidate scan
    # -- a SQL LIMIT would have burned the budget on good edges.
    _seed_grounded_edge(store, passage=_PASSAGE)
    a = _seed_grounded_edge(store, passage=_FRONT_MATTER)
    _seed_grounded_edge(store, passage=_PASSAGE)
    b = _seed_grounded_edge(store, passage=_FRONT_MATTER)
    got = [e.link_id for e in select_prose_less_evidence_edges(store, limit=2)]
    assert got == sorted([a[3], b[3]])


def test_repair_edge_repoints_a_front_matter_grounding(store: Any) -> None:
    # The end-to-end shape gripe 245842 leaves behind: repair_edge is
    # link_id-driven, so it moves an edge that was grounded on the WRONG
    # chunk, not only one that was grounded on nothing.
    paper = seed_ref(store, title="Anisimov 2018", kind="paper")
    fm = seed_chunk(store, ref_id=paper, text=_FRONT_MATTER, ord=0)
    body = seed_chunk(store, ref_id=paper, text=_PASSAGE, ord=1)
    hub = mint_hub(store, CanonicalClaim(sentence=_CLAIM, scope={}))
    link = store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        relation="corroborates",
        src_pos=0,
        meta={"caveats": [], "support": "yes", "source_handle": f"pc{fm}"},
    )
    result = repair_edge(
        store,
        hub,
        paper,
        int(link.id),
        apply=True,
        verify_batch_fn=_supported(1, "tensile strength of 130 GPa"),
    )
    assert result.status == "grounded"
    src_chunk_id, meta = _link_row(store, int(link.id))
    assert src_chunk_id == body
    assert meta["source_handle"] == f"pc{body}"


# ── repair_edge — the write path ────────────────────────────────────────


def test_repair_edge_grounds_and_updates_the_original_row(store: Any) -> None:
    """The whole point: `src_chunk_id` + `meta.source_handle` land on the
    SAME link_id, and no second edge appears."""
    hub, paper, chunk_id, link_id = _seed_broken_edge(store)
    before = _link_count(store)

    result = repair_edge(
        store,
        hub,
        paper,
        link_id,
        apply=True,
        verify_batch_fn=_supported(0, "tensile strength of 130 GPa"),
    )

    assert result.status == "grounded"
    assert result.applied is True
    assert result.chunk_id == chunk_id
    assert result.quote == "tensile strength of 130 GPa"

    src_chunk_id, meta = _link_row(store, link_id)
    assert src_chunk_id == chunk_id
    assert meta["source_handle"] == handle_registry.format_handle(
        "paper", chunk_id, chunk=True
    )
    # The verdict's other keys survive the `meta ||` patch.
    assert meta["support"] == "yes"
    assert meta["caveats"] == []
    # No second edge -- attach_evidence would have inserted one.
    assert _link_count(store) == before


def test_repair_edge_dry_run_writes_nothing(store: Any) -> None:
    hub, paper, chunk_id, link_id = _seed_broken_edge(store)
    before_row = _link_row(store, link_id)
    before_count = _link_count(store)

    result = repair_edge(
        store,
        hub,
        paper,
        link_id,
        verify_batch_fn=_supported(0, "tensile strength of 130 GPa"),
    )

    # The proposal is complete -- it just isn't written.
    assert result.status == "grounded"
    assert result.applied is False
    assert result.chunk_id == chunk_id
    assert result.source_handle is not None
    assert _link_row(store, link_id) == before_row
    assert _link_count(store) == before_count


def test_repair_edge_verify_rejected_writes_nothing(store: Any) -> None:
    """An empty verdict is a FINDING, not a failure: record it, touch
    neither the edge nor the claim."""
    hub, paper, _chunk_id, link_id = _seed_broken_edge(store)
    before_row = _link_row(store, link_id)
    before_title = _hub_title(store, hub)

    result = repair_edge(
        store, hub, paper, link_id, apply=True, verify_batch_fn=_rejects
    )

    assert result.status == "verify-rejected"
    assert result.applied is False
    assert result.chunk_id is None
    assert _link_row(store, link_id) == before_row
    # The claim sentence is never edited to match a source.
    assert _hub_title(store, hub) == before_title


def test_repair_edge_quote_validation_failure_writes_nothing(store: Any) -> None:
    """A hallucinated quote (not present in the claimed chunk) is rejected
    in code, distinctly from a clean rejection."""
    hub, paper, _chunk_id, link_id = _seed_broken_edge(store)
    before_row = _link_row(store, link_id)

    result = repair_edge(
        store,
        hub,
        paper,
        link_id,
        apply=True,
        verify_batch_fn=_supported(0, "a tensile strength of 999 TPa"),
    )

    assert result.status == "quote-validation-failed"
    assert result.applied is False
    assert _link_row(store, link_id) == before_row


def test_repair_edge_no_passage_never_calls_the_model(store: Any) -> None:
    hub, paper, _chunk_id, link_id = _seed_broken_edge(
        store, passage="An unrelated discussion of municipal water policy."
    )

    result = repair_edge(
        store, hub, paper, link_id, apply=True, verify_batch_fn=_never_verify
    )

    assert result.status == "no-passage"
    assert _link_row(store, link_id)[0] is None


def test_repair_edge_hearsay_only_reason(store: Any) -> None:
    """The claim's only matching text sits in a References section -- the
    paper cites the work, it didn't do it."""
    paper = seed_ref(store, title="A review", kind="paper")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, section_path) "
            "VALUES (%s, 'system', 0, 'paragraph', %s, %s)",
            (paper, _PASSAGE, ["References"]),
        )
        conn.commit()
    hub = mint_hub(store, CanonicalClaim(sentence=_CLAIM, scope={}))
    link = store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        relation="corroborates",
        meta=dict(_BROKEN_META),
    )

    result = repair_edge(
        store, hub, paper, int(link.id), apply=True, verify_batch_fn=_never_verify
    )

    assert result.status == "hearsay-only"
    assert _link_row(store, int(link.id))[0] is None


def test_repair_edge_duplicate_twin_is_reported_not_raised(store: Any) -> None:
    """A grounded twin already holding the conflict tuple makes the UPDATE
    raise UniqueViolation -- reported as `duplicate-exists`, and the run
    goes on."""
    hub, paper, chunk_id, link_id = _seed_broken_edge(store)
    store.add_link(  # the twin, already grounded at the same passage
        src_ref_id=paper,
        dst_ref_id=hub,
        src_pos=0,
        relation="corroborates",
        meta={
            "source_handle": handle_registry.format_handle(
                "paper", chunk_id, chunk=True
            )
        },
    )
    before_count = _link_count(store)

    result = repair_edge(
        store,
        hub,
        paper,
        link_id,
        apply=True,
        verify_batch_fn=_supported(0, "tensile strength of 130 GPa"),
    )

    assert result.status == "duplicate-exists"
    assert result.applied is False
    # The broken row survives untouched -- deleting it is a human call.
    assert _link_row(store, link_id)[0] is None
    assert _link_count(store) == before_count


def test_repair_edge_missing_hub_is_a_status_not_a_crash(store: Any) -> None:
    hub, paper, _chunk_id, link_id = _seed_broken_edge(store)
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (hub,))
        conn.commit()

    result = repair_edge(
        store, hub, paper, link_id, apply=True, verify_batch_fn=_never_verify
    )

    assert result.status == "hub-missing"
    assert _link_row(store, link_id)[0] is None


def test_repair_edge_searches_only_the_attached_source(store: Any) -> None:
    """A better-matching OTHER paper on the same hub must not be used --
    the edge asserts this source, so the passage must be in this source."""
    hub, paper, chunk_id, link_id = _seed_broken_edge(store)
    other = seed_ref(store, title="A different paper", kind="paper")
    seed_chunk(store, ref_id=other, text=_PASSAGE, ord=0)
    store.add_link(
        src_ref_id=other,
        dst_ref_id=hub,
        relation="corroborates",
        meta=dict(_BROKEN_META),
    )

    seen: list[int] = []

    def _fetch(_store: Any, paper_ref_id: int) -> list[Any]:
        from precis.taproot.reground import _fetch_body_chunks

        seen.append(paper_ref_id)
        return _fetch_body_chunks(_store, paper_ref_id)

    result = repair_edge(
        store,
        hub,
        paper,
        link_id,
        apply=True,
        verify_batch_fn=_supported(0, "tensile strength of 130 GPa"),
        fetch_body_chunks_fn=_fetch,
    )

    assert seen == [paper]  # only the edge's own source was read
    assert result.chunk_id == chunk_id


def _hub_title(store: Any, hub_ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (hub_ref_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


# ── select_broken_evidence_edges — the cohort ───────────────────────────


def test_select_cohort_finds_the_broken_shape(store: Any) -> None:
    hub, paper, _chunk_id, link_id = _seed_broken_edge(store)

    edges = select_broken_evidence_edges(store)

    assert [e.link_id for e in edges] == [link_id]
    assert edges[0].hub_ref_id == hub
    assert edges[0].source_ref_id == paper
    assert edges[0].source_kind == "paper"
    assert edges[0].relation == "corroborates"


def test_select_cohort_skips_edges_that_carry_a_real_source_handle(
    store: Any,
) -> None:
    """The population `_backfill_grounding` Part B already covers stays
    its job -- this pass is only for the jsonb-null shape."""
    paper = seed_ref(store, title="Lee 2008", kind="paper")
    chunk_id = seed_chunk(store, ref_id=paper, text=_PASSAGE, ord=0)
    hub = mint_hub(store, CanonicalClaim(sentence=_CLAIM, scope={}))
    store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        relation="corroborates",
        meta={
            "support": "yes",
            "source_handle": handle_registry.format_handle(
                "paper", chunk_id, chunk=True
            ),
        },
    )

    assert select_broken_evidence_edges(store) == []


def test_select_cohort_skips_sources_with_no_live_body_chunks(store: Any) -> None:
    """The 'acquire + ingest first' bucket -- nothing to ground against."""
    paper = seed_ref(store, title="Never ingested", kind="paper")
    hub = mint_hub(store, CanonicalClaim(sentence=_CLAIM, scope={}))
    store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        relation="corroborates",
        meta=dict(_BROKEN_META),
    )

    assert select_broken_evidence_edges(store) == []


def test_select_cohort_skips_already_grounded_edges(store: Any) -> None:
    paper = seed_ref(store, title="Lee 2008", kind="paper")
    seed_chunk(store, ref_id=paper, text=_PASSAGE, ord=0)
    hub = mint_hub(store, CanonicalClaim(sentence=_CLAIM, scope={}))
    store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        src_pos=0,
        relation="corroborates",
        meta=dict(_BROKEN_META),
    )

    assert select_broken_evidence_edges(store) == []


#: A second, *different* claim -- mint_hub converges on the claim sha, so
#: two seeds sharing a sentence land on ONE hub (and one draft cite would
#: then pull both edges in).
_OTHER_CLAIM = "Silicon carbide sublimes at 2700 degrees Celsius."


def test_select_cohort_restricts_to_a_drafts_cited_hubs(store: Any) -> None:
    in_hub, _paper, _chunk, in_link = _seed_broken_edge(store)
    # A second broken edge, on a hub the draft never cites.
    _seed_broken_edge(store, claim=_OTHER_CLAIM, passage=_OTHER_CLAIM)
    draft = seed_ref(store, title="The draft", kind="draft")
    store.add_link(src_ref_id=draft, dst_ref_id=in_hub, relation="cites")

    assert len(select_broken_evidence_edges(store)) == 2
    scoped = select_broken_evidence_edges(store, draft_ref_id=draft)
    assert [e.link_id for e in scoped] == [in_link]


def test_select_cohort_limit_is_a_stable_prefix(store: Any) -> None:
    _hub_a, _pa, _ca, first = _seed_broken_edge(store)
    _seed_broken_edge(store, claim=_OTHER_CLAIM, passage=_OTHER_CLAIM)

    assert [e.link_id for e in select_broken_evidence_edges(store, limit=1)] == [first]


# ── `precis taproot repair-evidence` ────────────────────────────────────


def _cli_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "taproot_cmd": "repair-evidence",
        "dry_run": False,
        "apply": False,
        "cohort": "no-passage",
        "draft": None,
        "limit": None,
        "tier": "medium",
        "top_k": 6,
        "out": None,
        "database_url": _active_dsn(),
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _patch_verify(monkeypatch: Any, quote: str) -> None:
    """Bind the module-level `verify_atoms_batch` the CLI's default
    tier-bound verify fn closes over -- no network, no router."""
    from precis.taproot import repair_evidence

    def _fake(atoms: Any, passages: Any, *, tier: Any = None) -> list[AtomVerifyResult]:
        return [AtomVerifyResult(0, True, 0, quote, None)]

    monkeypatch.setattr(repair_evidence, "verify_atoms_batch", _fake)


def test_cli_dry_run_is_the_default_and_writes_nothing(
    store: Any, monkeypatch: Any, tmp_path: Any, capsys: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    _hub, _paper, chunk_id, link_id = _seed_broken_edge(store)
    before_row = _link_row(store, link_id)
    _patch_verify(monkeypatch, "tensile strength of 130 GPa")
    out = tmp_path / "proposal.jsonl"

    taproot_cli.run(_cli_args(out=str(out)))

    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["link_id"] == link_id
    assert rows[0]["chunk_id"] == chunk_id
    assert rows[0]["quote"] == "tensile strength of 130 GPa"
    assert rows[0]["reason"] == "grounded"
    assert rows[0]["applied"] is False
    assert "DRY-RUN" in capsys.readouterr().err
    # Nothing written -- the edge is still passage-less.
    assert _link_row(store, link_id) == before_row


def test_cli_cohort_prose_less_selects_only_the_front_matter_edge(
    store: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    _h, _p, _c, passage_less_link = _seed_broken_edge(store)  # cohort A
    _h2, _p2, _c2, front_matter_link = _seed_grounded_edge(store, passage=_FRONT_MATTER)
    _patch_verify(monkeypatch, "tensile strength of 130 GPa")
    out = tmp_path / "proposal.jsonl"

    taproot_cli.run(_cli_args(out=str(out), cohort="prose-less"))

    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r["link_id"] for r in rows] == [front_matter_link]
    assert passage_less_link not in [r["link_id"] for r in rows]


def test_cli_cohort_both_unions_the_two_shapes(
    store: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    _h, _p, _c, passage_less_link = _seed_broken_edge(store)
    _h2, _p2, _c2, front_matter_link = _seed_grounded_edge(store, passage=_FRONT_MATTER)
    _patch_verify(monkeypatch, "tensile strength of 130 GPa")
    out = tmp_path / "proposal.jsonl"

    taproot_cli.run(_cli_args(out=str(out), cohort="both"))

    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sorted(r["link_id"] for r in rows) == sorted(
        [passage_less_link, front_matter_link]
    )


def test_cli_apply_repairs_the_original_row(
    store: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    _hub, _paper, chunk_id, link_id = _seed_broken_edge(store)
    before_count = _link_count(store)
    _patch_verify(monkeypatch, "tensile strength of 130 GPa")
    out = tmp_path / "applied.jsonl"

    taproot_cli.run(_cli_args(apply=True, out=str(out)))

    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["applied"] is True
    src_chunk_id, meta = _link_row(store, link_id)
    assert src_chunk_id == chunk_id
    assert meta["source_handle"] == handle_registry.format_handle(
        "paper", chunk_id, chunk=True
    )
    assert _link_count(store) == before_count


def test_cli_reports_a_dead_dispatch_as_an_error_row_and_exits_nonzero(
    store: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    """`RegroundingUnavailable` means the model never ran -- it must never
    be recorded as a grounding reason."""
    from precis.cli import taproot as taproot_cli
    from precis.taproot import repair_evidence
    from precis.taproot.reground import RegroundingUnavailable

    _hub, _paper, _chunk_id, link_id = _seed_broken_edge(store)

    def _dead(atoms: Any, passages: Any, *, tier: Any = None) -> list[AtomVerifyResult]:
        raise RegroundingUnavailable("dispatch timed out")

    monkeypatch.setattr(repair_evidence, "verify_atoms_batch", _dead)
    out = tmp_path / "errors.jsonl"

    with pytest.raises(SystemExit) as exc:
        taproot_cli.run(_cli_args(apply=True, out=str(out)))
    assert exc.value.code == 1

    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["link_id"] == link_id
    assert rows[0]["reason"] is None
    assert "dispatch timed out" in rows[0]["error"]
    assert _link_row(store, link_id)[0] is None


def test_cli_bad_draft_handle_exits_nonzero(store: Any) -> None:
    from precis.cli import taproot as taproot_cli

    paper = seed_ref(store, title="not a draft", kind="paper")
    with pytest.raises(SystemExit) as exc:
        taproot_cli.run(_cli_args(draft=handle_registry.format_handle("paper", paper)))
    assert exc.value.code == 1
