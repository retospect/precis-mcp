"""Tests for ``precis.workers.job_describe`` (td573331): the per-job-type
``job_sentence`` describers, the meta-only ``quest_id_from_meta``
resolution, and the platform-upkeep collapse. The DB-free halves run
against plain dicts / a stub store — the real-PG coverage lives in
``tests/precis_web/test_status_now.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from precis.store import Tag
from precis.workers.job_describe import (
    DESCRIBERS,
    PLATFORM_UPKEEP,
    UPKEEP_QUEST_HANDLE,
    collapse_upkeep,
    job_row_sentence,
    job_sentence,
    quest_id_from_meta,
)
from precis.workers.job_types import known_job_types


def test_every_registered_type_has_sentence() -> None:
    for name in known_job_types():
        s = job_sentence(name, status="running")
        assert s.strip(), f"{name}: empty sentence"
        assert "(unlinked)" not in s, name
        assert "(materializer" not in s, name


def test_platform_upkeep_membership() -> None:
    for name in (
        "derived_drain",
        "embed_batch",
        "gripe_validate",
        "doctor_tick",
        "conflict_sweep",
        "taproot_backfill",
        "news_poll",
        "money_refresh",
        "secret_status",
        "draft_refresh",
        "graph_garden",
        "plan_dispatch",
        "pr_poll",
        "ci_verdict_poll",
    ):
        assert name in PLATFORM_UPKEEP, name
    for name in ("quest_tick", "struct_relax", "meditation", "card_forge"):
        assert name not in PLATFORM_UPKEEP, name


def test_quest_tick_running_sentence_from_tick_event() -> None:
    s = job_sentence(
        "quest_tick",
        params={"quest_id": 42},
        meta={"coordinator_state": {"slice_count": 7}},
        status="running",
        last_event=(
            "tick #7: ok — 2 candidate(s), 1 sim(s), 3 harvested, "
            "0 graduated, 1 search(es) (+4 papers); frontier moved"
        ),
        quest_headline="Hunt catalysts",
    )
    assert "Hunt catalysts" in s
    assert "qu42" in s
    assert "tick 7" in s
    assert "(unlinked)" not in s


def test_quest_tick_failed_names_error_and_handle() -> None:
    s = job_sentence(
        "quest_tick",
        params={"quest_id": 42},
        meta={"coordinator_state": {"tick_failures": 3}},
        status="failed",
        summary=(
            "quest 42 loop resting after 3 consecutive failed/timed-out "
            "tick(s) (last: failed — claude exploded). Re-armed by a "
            "fresh quest_tick coordinator job once the cause is fixed."
        ),
    )
    assert "qu42" in s
    assert "resting" in s
    assert "claude exploded" in s


def test_quest_tick_failed_without_summary_uses_error() -> None:
    s = job_sentence(
        "quest_tick",
        params={"quest_id": 42},
        meta={"coordinator_state": {"tick_failures": 2}},
        status="failed",
        error="LLM endpoint 400",
    )
    assert "qu42" in s
    assert "2" in s
    assert "LLM endpoint 400" in s


def test_struct_relax_sentence_names_quest() -> None:
    s = job_sentence(
        "struct_relax",
        params={
            "structure_ref_id": 99,
            "fidelity": "ml",
            "model": "mace",
            "poscar": "Si2 anode\n 1.0\n 5.0 0.0 0.0\n",
        },
        meta={"node": "gpu-1", "wall_s": 87.4},
        status="succeeded",
        summary=(
            "relax[ml] converged: E_tot=-10.5000 eV in 12 steps → "
            "run-cube #7 (cache_key abcdef123456…)."
        ),
        quest_headline="Silicon anodes (qu42)",
    )
    assert "Si2 anode" in s
    assert "qu42" in s
    assert "mace" in s
    assert "gpu-1" in s
    assert "converged" in s


def test_struct_relax_falls_back_to_summary() -> None:
    s = job_sentence(
        "struct_relax",
        params={},
        meta={},
        status="succeeded",
        summary="relax[gpaw] did NOT converge: E_tot=-1.0 eV after 5 steps",
    )
    assert "structure relax" in s
    assert "did NOT converge" in s


def test_derived_drain_counts_passed_in() -> None:
    s = job_sentence(
        "derived_drain",
        params={"pass": "llm_summarize", "queued": 12, "running": 3},
        meta={},
        status="running",
    )
    assert "summarising" in s
    assert "12 queued behind 3 running" in s


def test_gripe_validate_parses_summary() -> None:
    s = job_sentence(
        "gripe_validate",
        params={},
        meta={},
        status="succeeded",
        summary=(
            "gripe_validate (prod head abc123def): verify=5 live=2 "
            "pending=1 no_sha=0 failed=0 held=1 user=0 spec=0 dry_run=0"
        ),
    )
    assert s == "checked 5 shipped gripe fixes against prod, 1 held"


def test_quest_id_from_quest_tick_meta() -> None:
    assert (
        quest_id_from_meta({"job_type": "quest_tick", "params": {"quest_id": 7}}) == 7
    )
    assert (
        quest_id_from_meta(
            {"job_type": "quest_tick", "coordinator_state": {"quest_id": 9}}
        )
        == 9
    )
    assert quest_id_from_meta({"job_type": "embed_batch", "params": {}}) is None


def test_quest_id_from_structure_tag() -> None:
    """The structure step reads the precomputed ``{sid: qid}`` map — the
    resolver itself never queries."""
    meta = {"job_type": "struct_relax", "params": {"structure_ref_id": 99}}
    assert quest_id_from_meta(meta, {99: 42}) == 42
    assert quest_id_from_meta(meta, {}) is None
    assert quest_id_from_meta(meta) is None


def test_job_row_sentence_resolves_structure_tag_quest() -> None:
    """The single-job card path reads the one structure's tags itself."""

    class _Quest:
        title = "Hunt catalysts"

    class _Store:
        def tags_for(self, ref_id: int) -> list[Tag]:
            assert ref_id == 99
            return [Tag.open("quest:42")]

        def get_ref(self, *, kind: str, id: int) -> Any:
            assert kind == "quest" and id == 42
            return _Quest()

    s = job_row_sentence(
        _Store(),
        meta={
            "job_type": "struct_relax",
            "params": {"structure_ref_id": 99, "poscar": "Si2 anode\n"},
        },
        status="running",
    )
    assert "Hunt catalysts" in s
    assert "Si2 anode" in s


def test_describer_failure_falls_back_to_generic() -> None:
    """A describer that raises degrades to the generic sentence — a page
    of jobs never dies on one bad meta (td573331 fix round)."""

    def _boom(**kw: Any) -> str:
        raise RuntimeError("malformed meta")

    with patch.dict(DESCRIBERS, {"quest_tick": _boom}):
        s = job_sentence("quest_tick", status="failed", summary="quest died")
    assert s == "quest tick: quest died"


def test_job_sentence_dict_error_is_coerced() -> None:
    """``meta.error`` is not always a str (handlers/job.py guards the same
    shape); a dict must render, not crash ``_first_line``."""
    dict_error: Any = {"code": 8, "detail": "claude exploded"}
    s = job_sentence(
        "fix_gripe",
        params={"gripe_id": 5},
        status="failed",
        error=dict_error,
    )
    assert "fixing gr5" in s
    assert "claude exploded" in s


def test_job_sentence_missing_and_wrong_type_params() -> None:
    # a missing param keeps the generic shape, not a crash
    s = job_sentence("struct_relax", params={}, status="running")
    assert "a structure" in s
    # params not even a dict degrade to the empty-params sentence
    bad_params: list[Any] = ["not-a-dict", ["x"]]
    for bad in bad_params:
        s = job_sentence(
            "struct_relax",
            params=bad,
            status="running",
            last_event="relax started",
        )
        assert s == "structure relax: relax started"
    # a wrong-typed poscar is coerced and still reads as its first line
    s = job_sentence(
        "struct_relax",
        params={"poscar": {"bad": 1}, "model": "mace"},
        status="running",
    )
    assert "relaxing" in s
    assert "mace" in s


def test_job_sentence_masks_secrets_in_rendered_text() -> None:
    """Free text from a job goes through ``mask_secrets`` before render —
    a pasted credential never echoes onto a row (td573331 fix round)."""
    # Assembled at runtime — the tree-wide secret gate
    # (tests/test_deploy_tree_no_secrets.py) scans test files too and
    # refuses a literal key-shaped string.
    secret = "sk-ant-" + "abc123" + "def456"
    s = job_sentence("sandbox_run", params={"prompt": f"deploy with {secret}"})
    assert secret not in s
    assert "<redacted:" in s
    s = job_sentence(
        "fix_gripe",
        params={"gripe_id": 5},
        status="failed",
        error=f"boom {secret}",
    )
    assert secret not in s
    s = job_sentence("oddball_type", status="running", last_event=f"leaked {secret}")
    assert secret not in s


def test_collapse_upkeep_counts() -> None:
    rows = [
        {
            "ref_id": 1,
            "job_type": "embed_batch",
            "status": "running",
            "age_s": 100.0,
            "params": {},
            "meta": {},
        },
        {
            "ref_id": 2,
            "job_type": "embed_batch",
            "status": "queued",
            "age_s": 2400.0,
            "params": {},
            "meta": {},
        },
        {
            "ref_id": 3,
            "job_type": "embed_batch",
            "status": "queued",
            "age_s": 300.0,
            "params": {},
            "meta": {},
        },
        {
            "ref_id": 4,
            "job_type": "news_poll",
            "status": "queued",
            "age_s": 50.0,
            "params": {},
            "meta": {},
        },
        {
            "ref_id": 5,
            "job_type": "quest_tick",
            "status": "running",
            "age_s": 10.0,
            "params": {},
            "meta": {},
        },
    ]
    out = collapse_upkeep(rows)
    assert {r["job_type"] for r in out} == {"embed_batch", "news_poll"}
    emb = next(r for r in out if r["job_type"] == "embed_batch")
    assert emb["upkeep"] is True
    assert emb["running"] == 1
    assert emb["queued"] == 2
    assert emb["quest"] == UPKEEP_QUEST_HANDLE
    assert emb["ref_id"] == 1  # the running row represents the group
    assert "platform upkeep" in emb["sentence"]
    assert "embedding new text chunks" in emb["sentence"]
    assert "1 running, 2 queued" in emb["sentence"]
    assert "oldest 40m" in emb["sentence"]


def test_job_row_sentence_scans_blocks_and_resolves_quest() -> None:
    class _Block:
        def __init__(self, chunk_kind: str, text: str) -> None:
            self.chunk_kind = chunk_kind
            self.text = text

    class _Quest:
        title = "Hunt catalysts"

    class _Store:
        def get_ref(self, *, kind: str, id: int) -> Any:
            assert kind == "quest" and id == 42
            return _Quest()

    blocks = [
        _Block(
            "job_event",
            "tick #3: ok — 1 candidate(s), 0 sim(s), 0 harvested, 0 graduated, 0 search(es) (+0 papers); quiet",
        ),
        _Block("job_summary", "earlier summary"),
    ]
    s = job_row_sentence(
        _Store(),
        meta={"job_type": "quest_tick", "params": {"quest_id": 42}},
        status="running",
        blocks=blocks,
    )
    assert "Hunt catalysts" in s
    assert "tick 3" in s
