"""Q12 (d) cleanup scripts — selection queries + write paths on fixtures.

The three one-off scripts in ``scripts/`` (``_conventions_findings_to_memory``,
``_taproot_review_orphans_rehome``, ``_merge_fi501221_fi501223``) carry
their prod selection as code. These tests pin the selection shape — and
the ``--apply`` writes — against test-DB fixtures shaped like the prod
sets the audit named. Nothing here runs against prod: the scripts are
``--dry-run`` by default and only ever connect through
``PRECIS_DATABASE_URL``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from precis.store.types import ChunkInsert, Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from tests.workers._helpers import seed_ref

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── _conventions_findings_to_memory ──────────────────────────────────


def _seed_conventions_finding(store: Any, *, title: str, retired: bool = False) -> int:
    """A ``meta.source='conventions-import'`` finding with a
    ``finding_body`` chunk — the shape ``precis conventions import``
    minted before the re-kind."""
    ref = store.insert_ref(
        kind="finding",
        slug=None,
        title=title,
        meta={"source": "conventions-import", "convention_rule": title},
    )
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=title, meta={"chunk_kind": "finding_body"})]
    )
    if retired:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (ref.id,)
            )
            conn.commit()
    return ref.id


def _ref_row(store: Any, ref_id: int) -> tuple[str, dict[str, Any]]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind, meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return str(row[0]), dict(row[1] or {})


class TestConventionsFindingsToMemory:
    """The ~160 ``meta.source='conventions-import'`` findings re-kind to
    ``kind='memory'`` (rules are repo-dev operational memory, not
    evidence); a hand-made finding and a retired import row are outside
    the selection."""

    def test_dry_run_selects_and_writes_nothing(self, store) -> None:
        mod = _load("_conventions_findings_to_memory")
        a = _seed_conventions_finding(store, title="rule one")
        _seed_conventions_finding(store, title="rule two")
        _seed_conventions_finding(store, title="retired rule", retired=True)

        out = mod.run(store, apply=False)

        assert any("2 conventions-import finding(s)" in line for line in out)
        assert any(f"would move fi{a}" in line for line in out)
        assert any("dry run" in line for line in out)
        assert _ref_row(store, a)[0] == "finding"

    def test_apply_rekinds_chunks_meta_and_leaves_outsiders(self, store) -> None:
        mod = _load("_conventions_findings_to_memory")
        a = _seed_conventions_finding(store, title="rule one")
        _seed_conventions_finding(store, title="retired rule", retired=True)
        outsider = store.insert_ref(
            kind="finding", slug=None, title="hand-made", meta={}
        )

        out = mod.run(store, apply=True)

        assert any("1 ref(s) moved" in line for line in out)
        assert any(f"moved fi{a}" in line and f"me{a}" in line for line in out)

        kind, meta = _ref_row(store, a)
        assert kind == "memory"
        assert meta["moved_from_kind"] == "finding"
        assert meta["moved_by"] == "conventions_findings_to_memory"
        assert meta["source"] == "conventions-import"  # key survives
        with store.pool.connection() as conn:
            ck = conn.execute(
                "SELECT chunk_kind FROM chunks WHERE ref_id = %s AND ord = 0",
                (a,),
            ).fetchone()
        assert ck[0] == "memory_body"

        # Outside the selection: no source key, and the retired row.
        assert _ref_row(store, outsider.id)[0] == "finding"


# ── _taproot_review_orphans_rehome ────────────────────────────────────


def _seed_orphan(store: Any, *, ref_id: int, review_tag: bool = True) -> int:
    """A live ``finding`` at an explicit ref_id — the fi34814–35098 June
    ``TAPROOT:review`` batch shape (no inbound link unless the caller
    adds one)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO refs (ref_id, kind, title, meta) "
            "VALUES (%s, 'finding', %s, '{}')",
            (ref_id, f"review note {ref_id}"),
        )
        conn.commit()
    if review_tag:
        store.add_tag(ref_id, Tag.closed("TAPROOT", "review"), set_by="agent")
    return ref_id


class TestTaprootReviewOrphansRehome:
    def test_selection_is_range_tag_orphan(self, store) -> None:
        mod = _load("_taproot_review_orphans_rehome")
        in_a = _seed_orphan(store, ref_id=34814)
        in_b = _seed_orphan(store, ref_id=35098)
        _seed_orphan(store, ref_id=35100)  # outside the range
        no_tag = _seed_orphan(store, ref_id=34860, review_tag=False)
        linked = _seed_orphan(store, ref_id=34850)
        # An inbound link de-orphans it.
        store.add_link(
            src_ref_id=no_tag,
            dst_ref_id=linked,
            dst_pos=None,
            relation="related-to",
        )
        parent = seed_ref(store, title="the tex draft", kind="draft")

        dry = mod.run(store, parent_ref_id=None, apply=False)
        assert any("2 TAPROOT:review orphan" in line for line in dry)
        assert any(f"fi{in_a}" in line for line in dry)
        assert any(f"fi{in_b}" in line for line in dry)

        out = mod.run(store, parent_ref_id=parent, apply=True)
        assert any("2 finding(s) re-homed" in line for line in out)
        for orphan in (in_a, in_b):
            edge = [
                l
                for l in store.links_for(orphan, direction="out", relation="part-of")
                if l.dst_ref_id == parent
            ]
            assert edge, f"fi{orphan} missing part-of edge to parent"

        # Idempotent: a second apply adds no duplicate edge.
        mod.run(store, parent_ref_id=parent, apply=True)
        assert len(store.links_for(in_a, direction="out", relation="part-of")) == 1

    def test_apply_refuses_a_missing_parent(self, store) -> None:
        mod = _load("_taproot_review_orphans_rehome")
        _seed_orphan(store, ref_id=34814)
        with pytest.raises(SystemExit, match="does not resolve"):
            mod.run(store, parent_ref_id=999999, apply=True)


# ── _merge_fi501221_fi501223 ─────────────────────────────────────────

_LOSER_CLAIM = CanonicalClaim(
    sentence=(
        "The dispersion correction changes by under 2 meV across S22 "
        "between B86, PBE and revPBE densities."
    ),
    scope={"material": "dispersion correction", "quantity": "under 2 meV"},
)
_WINNER_CLAIM = CanonicalClaim(
    sentence=(
        "The dispersion correction is insensitive to the density it is "
        "evaluated on, changing by under 2 meV across S22 between B86, "
        "PBE and revPBE densities."
    ),
    scope={"material": "dispersion correction", "quantity": "under 2 meV"},
)


class TestMergeFi501221Fi501223:
    """fi501221 collapses into fi501223 — the winner keeps the fuller
    claim sentence; the loser's evidence edge repoints (default, Reto
    may override via ``--winner``/``--loser``)."""

    def test_defaults_keep_501223(self) -> None:
        mod = _load("_merge_fi501221_fi501223")
        assert (mod._LOSER, mod._WINNER) == (501221, 501223)

    def test_run_dry_then_apply_repoints_edges(self, store) -> None:
        mod = _load("_merge_fi501221_fi501223")
        loser = mint_hub(store, _LOSER_CLAIM)
        winner = mint_hub(store, _WINNER_CLAIM)
        paper = seed_ref(store, title="vdW-DF", kind="paper")
        attach_evidence(
            store, hub_ref_id=loser, paper_ref_id=paper, role="corroborates"
        )

        dry = mod.run(store, winner_ref_id=winner, loser_ref_id=loser, apply=False)
        assert "dry run" in dry
        assert store.get_ref(kind="finding", id=loser) is not None

        mod.run(store, winner_ref_id=winner, loser_ref_id=loser, apply=True)
        # Loser soft-deleted; its corroborates edge now lands on the winner.
        assert store.get_ref(kind="finding", id=loser) is None
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT dst_ref_id, relation FROM links WHERE src_ref_id = %s",
                (paper,),
            ).fetchall()
            retired = conn.execute(
                "SELECT retired_at IS NOT NULL FROM refs WHERE ref_id = %s",
                (loser,),
            ).fetchone()[0]
        assert (winner, "corroborates") in [(int(d), str(r)) for d, r in rows]
        assert retired is True
