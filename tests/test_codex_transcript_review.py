"""Synthetic Codex rollout shapes observed locally; never private transcript text."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/mine-sessions"))
import codex_source
import extract
import stats as mine_stats

loader = importlib.machinery.SourceFileLoader(
    "fleet_review", str(ROOT / "scripts/fleet-review")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
review = importlib.util.module_from_spec(spec)
loader.exec_module(review)

START = datetime(2026, 10, 4, 12, tzinfo=UTC)
WORKER_ID = "00000000-0000-4000-8000-000000000001"


def row(kind, payload, *, ordinal=0, hour=12):
    return {
        "type": kind,
        "ordinal": ordinal,
        "timestamp": f"2026-10-04T{hour:02}:00:00Z",
        "payload": payload,
    }


def write_rollout(root, name, rows):
    path = root / f"2026/10/04/{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(item) for item in rows) + "\n", encoding="utf-8"
    )
    return path


def meta(name="parent", cwd="/project", **kwargs):
    return row("session_meta", {"id": name, "cwd": cwd, "source": "cli", **kwargs})


def call(call_id="a", name="mcp__precis__get", **kwargs):
    return row(
        "response_item",
        {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": '{"kind":"skill"}',
        },
        **kwargs,
    )


def output(call_id="a", text="ready", **kwargs):
    return row(
        "response_item",
        {"type": "function_call_output", "call_id": call_id, "output": text},
        **kwargs,
    )


def tokens(total, **kwargs):
    return row(
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total,
                    "cached_input_tokens": total // 2,
                },
                "last_token_usage": {"input_tokens": 9999},
            },
        },
        **kwargs,
    )


def test_joins_outputs_by_id_and_uses_structured_errors(tmp_path):
    write_rollout(
        tmp_path,
        "parent",
        [
            meta(),
            call(),
            call("b"),
            output("b", '{"isError":true,"message":"bad input"}'),
            output(text="é"),
        ],
    )
    events, coverage = codex_source.extract(tmp_path, cwds=["/project"])
    assert len(events) == 2
    assert events[0].result_bytes == 2
    assert events[0].verb == "get" and events[0].kind == "skill"
    assert events[1].is_error
    assert coverage["joined_results"] == 2


def test_per_response_usage_wins_over_repeated_cumulative_totals(tmp_path):
    token_row = row(
        "token_usage_record",
        {
            "thread_id": "parent",
            "response_id": "r1",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 50,
                "output_tokens": 10,
            },
            "thread_token_usage": {"input_tokens": 9999},
        },
    )
    write_rollout(
        tmp_path, "parent", [meta(), token_row, token_row, tokens(100), tokens(100)]
    )
    events, coverage = codex_source.extract(tmp_path, cwds=["/project"])
    result = mine_stats._tokens(events)
    assert result["input_total"] == 100 and result["output_total"] == 10
    assert result["cache_read_ratio"] == 0.5
    assert coverage["missing_or_duplicate_response_ids"] == 1


def test_fallback_deltas_include_baseline_before_window(tmp_path):
    write_rollout(
        tmp_path,
        "parent",
        [meta(), tokens(100, hour=11), tokens(130), tokens(130), tokens(150, hour=13)],
    )
    events, coverage = codex_source.extract(
        tmp_path, cwds=["/project"], since=START, until=START + timedelta(hours=1)
    )
    assert sum(e.usage.get("input_tokens", 0) for e in events) == 30
    assert coverage["events_in_window"] == 1


def test_thread_scope_includes_children_excludes_inherited_history_and_other_projects(
    tmp_path,
):
    write_rollout(tmp_path, "parent", [meta(), call(), output()])
    write_rollout(
        tmp_path,
        "child",
        [
            meta(
                "child",
                cwd="/different",
                parent_thread_id="parent",
                forked_from_id="parent",
                subagent_history_start_ordinal=4,
            ),
            meta(),
            call("inherited", ordinal=2),
            output("inherited", ordinal=3),
            call("own", ordinal=4),
            output("own", ordinal=5),
        ],
    )
    write_rollout(
        tmp_path, "other", [meta("other", cwd="/project-lookalike"), call(), output()]
    )
    events, coverage = codex_source.extract(tmp_path, threads=["parent"])
    assert {e.session for e in events} == {"codex:parent", "codex:child"}
    assert len(events) == 2
    assert next(e for e in events if e.sidechain).session == "codex:child"
    assert coverage["scope_files"] == 2
    assert coverage["inherited_rows_skipped"] == 2


def test_missing_boundary_and_results_are_explicit(tmp_path):
    write_rollout(tmp_path, "parent", [meta(), call(), output("unknown")])
    write_rollout(
        tmp_path,
        "child",
        [meta("child", parent_thread_id="parent", forked_from_id="parent"), call()],
    )
    events, coverage = codex_source.extract(
        tmp_path, cwds=["/project"], threads=["missing"]
    )
    assert len(events) == 1
    assert coverage["calls_without_result"] == 1
    assert coverage["orphan_outputs"] == 1
    assert coverage["forks_missing_history_boundary"] == 1
    assert coverage["requested_threads_missing"] == 1


def test_scope_required_and_cwd_uses_directory_boundary(tmp_path):
    write_rollout(tmp_path, "other", [meta(cwd="/project-other"), call(), output()])
    with pytest.raises(ValueError, match="requires"):
        codex_source.extract(tmp_path)
    assert codex_source.extract(tmp_path, cwds=["/project"])[0] == []


def test_child_cumulative_counter_uses_inherited_baseline(tmp_path):
    write_rollout(
        tmp_path,
        "child",
        [
            meta(
                "child",
                parent_thread_id="parent",
                forked_from_id="parent",
                subagent_history_start_ordinal=2,
            ),
            tokens(100, ordinal=1),
            tokens(130, ordinal=2),
            tokens(130, ordinal=3),
        ],
    )
    events, _ = codex_source.extract(tmp_path, threads=["child"])
    assert sum(e.usage.get("input_tokens", 0) for e in events) == 30


def test_mcp_namespace_command_profile_kind(tmp_path):
    write_rollout(
        tmp_path,
        "parent",
        [
            meta(),
            row(
                "response_item",
                {
                    "type": "function_call",
                    "name": "precis",
                    "namespace": "mcp__precis",
                    "call_id": "a",
                    "arguments": json.dumps(
                        {"command": "get(kind='skill', id='overview')"}
                    ),
                },
            ),
            output(),
        ],
    )
    events, _ = codex_source.extract(tmp_path, cwds=["/project"])
    assert (events[0].verb, events[0].kind) == ("get", "skill")


def test_codex_evidence_cannot_be_written_into_repository():
    with pytest.raises(ValueError, match="outside"):
        extract.main(
            [
                "--codex",
                "--cwd-root",
                "/project",
                "--out",
                str(ROOT / "private/events.jsonl"),
            ]
        )


def test_custom_orchestration_redacts_before_head_slice(tmp_path):
    secret = "postgresql://example:secret@db.invalid/database"
    write_rollout(
        tmp_path,
        "parent",
        [
            meta(),
            row(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "call_id": "a",
                    "name": "exec",
                    "input": secret,
                },
            ),
            output(text=secret),
        ],
    )
    events, coverage = codex_source.extract(tmp_path, cwds=["/project"])
    assert "secret" not in events[0].arg_digest + events[0].result_head
    assert coverage["opaque_orchestrator_calls"] == 1


def test_cli_codex_only_and_coverage_file(tmp_path):
    root = tmp_path / "sessions"
    write_rollout(root, "parent", [meta(), call(), output()])
    out = tmp_path / "evidence/events.jsonl"
    assert (
        extract.main(
            [
                "--codex",
                "--codex-root",
                str(root),
                "--cwd-root",
                "/project",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert (
        json.loads(out.with_name("coverage.json").read_text(encoding="utf-8"))[
            "coverage"
        ]["events_in_window"]
        == 1
    )
    assert all(
        json.loads(line)["corpus"] == "codex"
        for line in out.read_text(encoding="utf-8").splitlines()
    )


@pytest.fixture
def scheduler(tmp_path):
    path = tmp_path / "reviews/state.json"
    path.parent.mkdir()
    registration = tmp_path / "registrations/housekeeping.json"
    registration.parent.mkdir()
    registration.write_text(json.dumps({"thread_id": WORKER_ID}), encoding="utf-8")
    return tmp_path, path, review.initialize(path, START, "housekeeping")


def test_first_due_six_hours_and_successful_queue_is_not_completion(
    scheduler, monkeypatch
):
    root, path, state = scheduler
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(review.subprocess, "run", run)
    review.tick(root, path, state, START + timedelta(hours=5))
    assert not calls
    review.tick(root, path, state, START + timedelta(hours=6))
    review.tick(root, path, state, START + timedelta(hours=12))
    assert len(calls) == 1
    assert calls[0][:4] == ["codex", "queue", "--thread", WORKER_ID]
    assert state["high_water"] == review.timestamp(START)
    assert state["pending"]["status"] == "queued"


def test_queue_failure_and_restart_retain_same_interval(scheduler, monkeypatch):
    root, path, state = scheduler
    monkeypatch.setattr(
        review.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1)
    )
    review.tick(root, path, state, START + timedelta(hours=6))
    pending = state["pending"].copy()
    state = json.loads(path.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        review.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
    )
    review.tick(root, path, state, START + timedelta(hours=12))
    assert state["pending"]["id"] == pending["id"]
    assert state["pending"]["since"] == pending["since"]
    assert state["pending"]["attempts"] == 2


def test_uncertain_queue_is_not_retried_automatically(scheduler, monkeypatch):
    root, path, state = scheduler

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("codex", 60)

    monkeypatch.setattr(review.subprocess, "run", timeout)
    review.tick(root, path, state, START + timedelta(hours=6))
    review.tick(root, path, state, START + timedelta(hours=12))
    assert state["pending"]["status"] == "uncertain"
    assert state["pending"]["attempts"] == 1


def test_completion_requires_matching_report_then_advances_exact_window(
    scheduler, monkeypatch
):
    root, path, state = scheduler
    monkeypatch.setattr(
        review.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
    )
    review.tick(root, path, state, START + timedelta(hours=6))
    pending = state["pending"]
    report = root / "report.json"
    document = {
        "review_id": pending["id"],
        "since": pending["since"],
        "until": pending["until"],
        "coverage": {"missing": ["opaque nested MCP calls"]},
        "findings": ["stale instruction"],
        "actions": [],
    }
    report.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="owned followups"):
        review.complete(path, state, pending["id"], report, START)
    document["actions"] = [{"owner": "coordinator", "task": "correct instruction"}]
    report.write_text(json.dumps(document), encoding="utf-8")
    review.complete(path, state, pending["id"], report, START + timedelta(hours=7))
    assert state["pending"] is None
    assert state["high_water"] == pending["until"]
    assert state["history"][0]["report"] == str(report)


def test_state_initialization_is_idempotent_and_in_tree_paths_refused(scheduler):
    _, path, state = scheduler
    assert review.initialize(path, START + timedelta(hours=6), "other") == state
    with pytest.raises(ValueError, match="outside"):
        review.outside_tree(ROOT / "evidence")
