"""Demand metadata stays independent of routing and content hashes."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from precis import route_log
from precis.store import Store
from precis.utils.llm import recipes
from precis.utils.llm.operations import LLM_OPERATIONS
from precis.utils.llm.recipes import Recipe
from precis.utils.llm.requirement import _JUDGE_TIER
from precis.utils.llm.router import (
    LlmRequest,
    Tier,
    _route_features,
    _serialize_request,
)
from precis.workers.recipe_gap import run_recipe_gap


@pytest.fixture
def recipe_rows(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recipe]:
    rows: dict[str, Recipe] = {
        "llm_summarize": Recipe("llm_summarize", "chunk.summarize", consequence="low"),
        "review:": Recipe("review:", "judge.run", consequence="high"),
        "review:deep:": Recipe("review:deep:", "judge.claim", consequence="high"),
    }
    monkeypatch.setattr(recipes, "_rows", lambda: rows)
    return rows


@pytest.mark.parametrize(
    "source,expected",
    [
        ("llm_summarize", "llm_summarize"),
        ("llm_summarize:local", "llm_summarize"),
        ("review:fable", "review:"),
        ("review:deep:fable", "review:deep:"),
    ],
)
def test_lookup(recipe_rows: dict[str, Recipe], source: str, expected: str) -> None:
    assert recipes.lookup(source) == recipe_rows[expected]


def test_resolution(recipe_rows: dict[str, Recipe]) -> None:
    assert recipes.resolve("missing") == (
        "unknown.missing",
        "medium",
        "default",
        "unknown",
    )
    assert recipes.resolve("llm_summarize") == (
        "chunk.summarize",
        "low",
        "default",
        "recipe",
    )
    assert recipes.resolve("llm_summarize", "code.review", "high", "local-only") == (
        "code.review",
        "high",
        "local-only",
        "caller",
    )
    assert (
        recipes.resolve("llm_summarize", caller_consequence="medium").origin == "recipe"
    )


def test_cache_ttl_and_missing_table(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    conn = store.pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = [
        ("source", "chunk.rank", "none", "low", False, {}, None, []),
    ]
    clock: list[float] = [100.0]
    monkeypatch.setattr(route_log, "bound_store", lambda: store)
    monkeypatch.setattr(recipes, "monotonic", lambda: clock[0])
    monkeypatch.setattr(recipes, "_expires", 0.0)
    monkeypatch.setattr(recipes, "_store_identity", None)
    monkeypatch.setattr(recipes, "_cache", {})
    assert recipes.lookup("source") is not None
    clock[0] = 399.0
    assert recipes.lookup("source") is not None
    assert conn.execute.call_count == 1
    clock[0] = 401.0
    conn.execute.side_effect = RuntimeError("missing table")
    assert recipes.lookup("source") is not None
    assert conn.execute.call_count == 2
    monkeypatch.setattr(route_log, "bound_store", lambda: None)
    assert recipes.lookup("source") is None


def test_rows_failure_keeps_prior_rows(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed reload serves the previous rows and retries in ~30 s."""
    store = MagicMock()
    conn = store.pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = [
        ("source", "chunk.rank", "none", "low", False, {}, None, []),
    ]
    clock: list[float] = [100.0]
    monkeypatch.setattr(route_log, "bound_store", lambda: store)
    monkeypatch.setattr(recipes, "monotonic", lambda: clock[0])
    monkeypatch.setattr(recipes, "_expires", 0.0)
    monkeypatch.setattr(recipes, "_store_identity", None)
    monkeypatch.setattr(recipes, "_cache", {})
    assert recipes.lookup("source") is not None
    conn.execute.side_effect = RuntimeError("pool timeout")
    clock[0] = 401.0
    assert recipes.lookup("source") is not None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    clock[0] = 420.0
    assert recipes.lookup("source") is not None
    assert conn.execute.call_count == 2
    conn.execute.side_effect = None
    clock[0] = 432.0
    assert recipes.lookup("source") is not None
    assert conn.execute.call_count == 3


def test_rows_failure_no_prior_retries_soon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing cached a failed load retries in ~30 s, not the full TTL."""
    store = MagicMock()
    conn = store.pool.connection.return_value.__enter__.return_value
    conn.execute.side_effect = RuntimeError("missing table")
    clock: list[float] = [100.0]
    monkeypatch.setattr(route_log, "bound_store", lambda: store)
    monkeypatch.setattr(recipes, "monotonic", lambda: clock[0])
    monkeypatch.setattr(recipes, "_expires", 0.0)
    monkeypatch.setattr(recipes, "_store_identity", None)
    monkeypatch.setattr(recipes, "_cache", {})
    assert recipes.lookup("source") is None
    assert conn.execute.call_count == 1
    clock[0] = 129.0
    assert recipes.lookup("source") is None
    assert conn.execute.call_count == 1
    conn.execute.side_effect = None
    conn.execute.return_value.fetchall.return_value = [
        ("source", "chunk.rank", "none", "low", False, {}, None, []),
    ]
    clock[0] = 131.0
    assert recipes.lookup("source") is not None
    assert conn.execute.call_count == 2


@pytest.mark.parametrize(
    "caller,source,kind,origin,consequence",
    [
        (
            {
                "task_kind": "code.review",
                "consequence": "high",
                "privacy_class": "local-only",
            },
            "llm_summarize",
            "code.review",
            "caller",
            "high",
        ),
        ({}, "llm_summarize", "chunk.summarize", "recipe", "low"),
        ({}, "missing", "unknown.missing", "unknown", "medium"),
    ],
)
def test_record_stamps(
    recipe_rows: dict[str, Recipe],
    caller: dict[str, Any],
    source: str,
    kind: str,
    origin: str,
    consequence: str,
) -> None:
    store = MagicMock()
    record = route_log.LlmCallRecord(
        source=source,
        tier=None,
        transport=None,
        model=None,
        tools_needed=None,
        request_text="request",
        response_text="response",
        cost_usd=None,
        turns_used=None,
        duration_ms=None,
        errored=False,
        error=None,
        data_parsed=None,
        features=caller,
        store_blobs=False,
    )
    route_log.record_call(record, store=store)
    import json

    conn = store.pool.connection.return_value.__enter__.return_value
    features = json.loads(conn.execute.call_args.args[1][17])
    assert features == {
        "task_kind": kind,
        "task_kind_origin": origin,
        "consequence": consequence,
        "privacy_class": caller.get("privacy_class", "default"),
    }
    assert record.features == caller


def test_request_metadata_preserves_hash() -> None:
    req = LlmRequest(tier=Tier.MEDIUM, source="llm_summarize", model="pinned")
    changed = replace(
        req, task_kind="code.review", consequence="high", privacy_class="local-only"
    )
    assert _serialize_request(req) == _serialize_request(changed)
    assert _route_features(changed)["task_kind"] == "code.review"
    assert changed.model == "pinned"
    with pytest.raises(ValueError, match="consequence"):
        replace(req, consequence="invalid")
    with pytest.raises(ValueError, match="privacy_class"):
        replace(req, privacy_class="invalid")


def test_requirement_judge_listed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from precis.cli.llm import _cmd_op_list
    from precis.utils.llm import live_config

    assert LLM_OPERATIONS["llm:requirement-judge"].tier == _JUDGE_TIER == Tier.MEDIUM
    monkeypatch.setattr(live_config, "op_override", lambda source: None)
    store = MagicMock()
    store.pool.connection.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
    _cmd_op_list(store)
    assert (
        "llm:requirement-judge"
        in capsys.readouterr().out.split("excluded operations")[0]
    )


@pytest.mark.parametrize("existing,expected", [(None, 1), ((1,), 0)])
def test_recipe_gap(existing: tuple[int] | None, expected: int) -> None:
    store = MagicMock()
    store.pool.connection.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [
        ("missing",)
    ]
    conn = store.tx.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = existing
    store.insert_ref.return_value.id = 42
    assert run_recipe_gap(cast(Store, store)) == expected
    assert store.insert_ref.call_count == expected
    if expected:
        assert store.insert_ref.call_args.kwargs["meta"] == {
            "fingerprint": "recipe-gap:missing"
        }


def test_seed_sources() -> None:
    # Independent transcription of seed v0, including literal prefix families.
    sources: list[str] = [
        "classify",
        "classify:escalate",
        "classify_topics",
        "taproot:classify",
        "llm_summarize",
        "hub_tagline",
        "context_sentence",
        "card_describe",
        "card_forge",
        "se-note-rewrite",
        "survey_extract",
        "survey_extract:table_recipe",
        "taproot:extract",
        "taproot:extract-medium",
        "datasheet_facts",
        "bib_parse",
        "figdigit:propose",
        "inject_scan",
        "bom-consolidation",
        "stub_rank",
        "chase:verify",
        "taproot:reground-verify",
        "dtree:claim_check",
        "sim:verify",
        "measure_accept",
        "conflict_search:negate",
        "chase:locate",
        "chase:disambiguate",
        "cite_resolve:passage",
        "draft:cite-fit",
        "taproot:dedup",
        "taproot:containment",
        "taproot:merge-confirm",
        "taproot:reground-judge",
        "taproot:qualify",
        "llm:requirement-judge",
        "review:",
        "llm_eval",
        "graph_garden:propose",
        "taproot:reword",
        "graph_garden:review",
        "graph_garden:review_fable",
        "graph_garden:consume",
        "dream",
        "plan_tick",
        "quest_tick",
        "doctor_tick",
        "quest_review",
        "quest_weave",
        "quest_roadmap",
        "slicer",
        "spec-writer",
        "reading_brief",
        "briefing",
        "meditation",
        "idea_feed",
        "followup",
        "good_search:triage",
        "asa_bot",
        "asa-slack",
        "design-chat",
        "cad_propose",
        "cad_discuss",
        "se_propose_atomic",
        "structure_propose",
        "diagram_propose:",
        "mermaid",
        "figure",
        "markup:",
        "pcb-review",
        "pcb-argue",
        "code",
        "gripe_triage:tick",
        "gripe_parked:until",
        "diagnose_gripe",
        "triage",
        "brief",
        "review",
        "validate",
        "docs",
        "tex_llm_fix",
        "draft_refresh",
        "anki:fix",
    ]
    sql = (
        Path(__file__).parents[1] / "src/precis/migrations/0278_task_recipes.sql"
    ).read_text(encoding="utf-8")
    seeded = re.findall(r"^    \('([^']+)',", sql, flags=re.MULTILINE)
    assert sorted(seeded) == sorted(sources)


@pytest.mark.db
def test_gap_daily_and_open_dedupe(store: Store) -> None:
    """Exercise the real SQL, including closure and the UTC-day boundary."""
    from precis.store.types import Tag

    with store.pool.connection() as conn:
        conn.execute("DELETE FROM llm_call_log")
        conn.execute(
            "INSERT INTO llm_call_log (source, features, errored) "
            "VALUES ('recipe-gap-test', '{\"task_kind\":\"unknown.recipe-gap-test\"}', false)"
        )
        conn.commit()
    assert run_recipe_gap(store) == 1
    assert run_recipe_gap(store) == 0
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'gripe' "
            "AND meta->>'fingerprint' = 'recipe-gap:recipe-gap-test'"
        ).fetchone()
    assert row is not None
    ref_id: int = row[0]
    store.add_tag(
        ref_id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    assert run_recipe_gap(store) == 0
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '2 days' WHERE ref_id = %s",
            (ref_id,),
        )
        conn.commit()
    assert run_recipe_gap(store) == 1


def test_gap_cadence() -> None:
    from precis.workers.scheduler import CADENCES

    cadence = next(c for c in CADENCES if c.name == "recipe_gap")
    assert cadence.interval_s == 6 * 3600
    assert not cadence.spends
