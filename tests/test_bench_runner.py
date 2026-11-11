"""Scripted-tier ablation runner (td604857, first cut).

Pure tests need nothing. The DB tests ingest two tiny synthetic QASPER-shaped
papers with the ``store`` fixture (they skip when no Postgres is reachable),
run the in-process scripted caller against them with the mock embedder, and
check determinism, censoring, refusals, the two baselines' token counts, the
planted-fact adapter, the layer-coverage guard and the results header.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from precis.bench import (
    cli,
    corpus,
    gold,
    layers,
    planted,
    planted_ingest,
    qasper_tasks,
    raw,
    runner,
)
from precis.bench.switches import ARMS
from tests.test_bench_gold import _ans

# ---------------------------------------------------------------------------
# fixtures: two distinct synthetic papers (distinct vocabulary so lexical
# retrieval is unambiguous), 8 paragraphs of 200-700 chars each
# ---------------------------------------------------------------------------


def _para(tag: int, k: int) -> str:
    return (
        f"Zork{tag} study number {k}: the quasar{tag}x{k} instrument measured "
        f"flux{tag}x{k} values with high precision in the laboratory, and the "
        f"resulting calibration{tag}x{k} curve was stable across every night "
        f"of the long observing campaign that the team{tag}x{k} carried out."
    )


def _paper(tag: int) -> dict[str, Any]:
    paras = [_para(tag, k) for k in range(12)]
    qs = [
        (
            f"What did the quasar{tag}x2 instrument measure?",
            [_ans(spans=("flux",), evidence=(paras[2],))],
        ),
        (
            f"How stable was the calibration{tag}x5 curve?",
            [_ans(free="Stable.", evidence=(paras[5],))],
        ),
        (
            f"Did the team{tag}x7 carry out the observing campaign?",
            [_ans(unanswerable=True)],
        ),
        (f"Why was the campaign long{tag}?", [_ans(free="Because.")]),
        (
            f"What about the quasar{tag}x0 flux{tag}x0?",
            [_ans(yes_no=True, evidence=(paras[0],))],
        ),
    ]
    return {
        "title": f"Zork {tag} Study",
        "abstract": f"An abstract about zork{tag}.",
        "full_text": [
            {"section_name": "Introduction", "paragraphs": paras[:6]},
            {"section_name": "Methods", "paragraphs": paras[6:]},
        ],
        "qas": [
            {"question": q, "question_id": f"{tag}-{j}", "answers": a}
            for j, (q, a) in enumerate(qs)
        ],
        "figures_and_tables": [],
    }


@pytest.fixture
def papers() -> dict[str, tuple[str, dict[str, Any]]]:
    return {f"9904.0000{t}": ("train", _paper(t)) for t in (1, 2)}


@pytest.fixture
def manifest(papers) -> dict[str, Any]:
    return corpus.build_manifest(papers, quick_n=2, full_n=2)


@pytest.fixture
def task_rows(papers, manifest) -> list[dict[str, Any]]:
    rows, _ = qasper_tasks.build_tasks(manifest["quick"], papers)
    return rows


@pytest.fixture
def world(store, papers, manifest, task_rows):
    """Ingested papers, a runtime on the mock embedder, a catalog."""
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import MockEmbedder
    from precis.runtime import PrecisRuntime

    with store.pool.connection() as conn:
        corpus.ingest_papers(conn, manifest["quick"], papers, set_name="quick")
    embedder = MockEmbedder(dim=store.embedding_dim())
    runtime = PrecisRuntime(
        config=PrecisConfig(),
        hub=boot(store=store, embedder=embedder, boot_writes=False),
    )
    return {
        "store": store,
        "runtime": runtime,
        "embedder": embedder,
        "catalog": runner.Catalog(store),
        "papers": papers,
        "rows": task_rows,
    }


def _by_question(rows, text: str) -> runner.BenchTask:
    tasks, _ = runner.select_tasks(rows)
    return next(t for t in tasks if t.question.startswith(text))


def _comparable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "wall_s"}


# ---------------------------------------------------------------------------
# pure
# ---------------------------------------------------------------------------


def test_normalise_volatile_makes_reruns_comparable():
    a = "pa1 [paper] T  [created -5m · read 1× · last -0s]\n  pa2 — X · -7m  (0.58)"
    b = "pa1 [paper] T  [created -2h · read 12× · last -3m]\n  pa2 — X · -15d  (0.58)"
    assert runner.normalise_volatile(a) == runner.normalise_volatile(b)
    assert "abc-def" in runner.normalise_volatile("abc-def")  # words untouched


def test_token_estimator_is_chars_over_four():
    assert runner.tokens_of("x" * 400) == 100.0
    assert "chars/4" in runner.TOKEN_ESTIMATOR


def test_arm_flags_cover_every_arm_and_refuse_unknown():
    assert runner.ALL_ARMS[0] == "all-on" and runner.ALL_ARMS[-2:] == runner.BASELINES
    assert set(ARMS) <= set(runner.ALL_ARMS) and len(ARMS) == 7
    assert runner.arm_flags("all-on") == {} and runner.arm_flags("whole-paper") == {}
    assert runner.arm_flags("no-kwd") == {"kwd": False}
    assert runner.arm_flags("semantic-only") == {"lexical": False}
    with pytest.raises(runner.BenchRunError, match="unknown arm"):
        runner.arm_flags("no-such-arm")


def _gold_row(**kw: Any) -> dict[str, Any]:
    row = {
        "id": "t1",
        "type": "lookup",
        "question": "q?",
        "answer": ["a"],
        "answer_kind": "extractive",
        "docs": ["qasper:1"],
        "evidence": [
            {"doc": "qasper:1", "unit": "s0p0", "span": [0, 5], "doc_span": [0, 5]}
        ],
        "labelled_by": "qasper",
        "source": {},
    }
    row.update(kw)
    return row


def _bare_result(task: runner.BenchTask, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "found": False,
        "tokens_first": None,
        "tokens_total": 10.0,
        "calls": 3,
        "errors": 0,
        "refusals": 0,
        "stop": "exhausted",
        "budget": 100,
        "recall": {k: None for k in runner.RECALL_KS},
        "transcript": None,
        "wall_s": 0.0,
    }
    args.update(kw)
    return runner._result(task, "all-on", **args)


def test_result_censors_at_the_budget():
    t = runner.BenchTask(_gold_row())
    missed = _bare_result(t)
    assert (missed["found"], missed["censored"], missed["tokens_to_first_gold"]) == (
        False,
        True,
        100.0,
    )
    hit = _bare_result(t, found=True, tokens_first=42.0)
    assert (hit["found"], hit["censored"], hit["tokens_to_first_gold"]) == (
        True,
        False,
        42.0,
    )
    # found only by the call that overran the budget: not reached within it
    late = _bare_result(t, found=True, tokens_first=130.0)
    assert (late["found"], late["censored"], late["tokens_to_first_gold"]) == (
        False,
        True,
        100.0,
    )


def test_unanswerable_rows_record_cost_only():
    t = runner.BenchTask(_gold_row(answer=[], answer_kind="unanswerable", evidence=[]))
    r = _bare_result(t, tokens_total=77.0)
    assert r["abstention"] and r["found"] is None and not r["censored"]
    assert r["tokens_to_first_gold"] is None and r["tokens_total"] == 77.0


def test_select_tasks_drops_only_unevidenced_answerable_rows():
    rows = [
        _gold_row(id="a"),
        _gold_row(id="b", evidence=[]),
        _gold_row(id="c", answer=[], answer_kind="unanswerable", evidence=[]),
        _gold_row(id="m", type="memory", docs=[], evidence=[], labelled_by="planted"),
    ]
    tasks, skipped = runner.select_tasks(rows, {"m": ("The fact.",)})
    assert [t.id for t in tasks] == ["a", "c", "m"]
    assert skipped == {"answerable_without_evidence": 1}
    assert tasks[2].is_memory and tasks[2].has_gold and tasks[2].source == "planted"


ZERO = {
    "refs": 3,
    "chunks": 100,
    "chunks_with_keywords_frac": 0.0,
    "chunks_with_llm_summary_frac": 0.0,
    "chunks_with_embedding_frac": 0.0,
    "refs_with_categorizer_tag_frac": 0.0,
    "refs_with_edge_frac": 0.0,
}


def test_vacuity_follows_layer_coverage():
    for arm in (
        "no-kwd",
        "no-summary",
        "no-categorizer",
        "no-fisheye",
        "no-recall",
        "lexical-only",
    ):
        v = layers.arm_vacuity(arm, ZERO, embedder_id="bge-m3")
        assert v["vacuous"] is True and "holds nothing of it" in v["reason"], arm
    v = layers.arm_vacuity("semantic-only", ZERO, embedder_id="bge-m3")
    assert v["vacuous"] is False and v["degenerate"] is True
    full = {k: (0.9 if k.endswith("_frac") else v) for k, v in ZERO.items()}
    for arm in ARMS:
        assert layers.arm_vacuity(arm, full, embedder_id="bge-m3")["vacuous"] is False
    # a layer present on 0.5% of its denominator is still "~0"
    low = {**full, "chunks_with_keywords_frac": 0.005}
    assert layers.arm_vacuity("no-kwd", low, embedder_id="bge-m3")["vacuous"] is True
    edge = {**full, "chunks_with_keywords_frac": layers.VACUOUS_BELOW}
    assert layers.arm_vacuity("no-kwd", edge, embedder_id="bge-m3")["vacuous"] is False


def test_hash_embedder_is_flagged_on_every_arm_that_ranks_semantically():
    full = {k: (0.9 if k.endswith("_frac") else v) for k, v in ZERO.items()}
    for arm in runner.ALL_ARMS:
        note = layers.arm_vacuity(arm, full, embedder_id="mock").get("embedder_note")
        if arm == "lexical-only":
            assert note is None
        else:
            assert note == "embedder: hash (not meaningful)", arm
    assert "embedder_note" not in layers.arm_vacuity(
        "all-on", full, embedder_id="bge-m3"
    )


def _row(task: str, arm: str, sha: str | None, **kw: Any) -> dict[str, Any]:
    base = {
        "task": task,
        "type": "lookup",
        "source": "qasper",
        "arm": arm,
        "caller": "triage",
        "abstention": False,
        "found": True,
        "censored": False,
        "applicable": True,
        "stop": "found",
        "tokens_to_first_gold": 100.0,
        "tokens_total": 150.0,
        "calls": 4,
        "verbatim_opens": 1,
        "errors": 0,
        "refusals": 0,
        "recall_at_1": 1.0,
        "recall_at_3": 1.0,
        "recall_at_5": 1.0,
        "recall_at_10": 1.0,
        "transcript_sha256": sha,
        "wall_s": 0.0,
    }
    base.update(kw)
    return base


def test_identical_transcripts_mark_an_arm_vacuous_even_when_coverage_says_no():
    full = {k: (0.9 if k.endswith("_frac") else v) for k, v in ZERO.items()}
    rows = [
        _row("t1", "all-on", "aa"),
        _row("t2", "all-on", "bb"),
        _row("t1", "no-kwd", "aa"),
        _row("t2", "no-kwd", "bb"),
        _row("t1", "no-summary", "aa"),
        _row("t2", "no-summary", "XX"),
    ]
    assert runner.identical_to_all_on(rows) == {"no-kwd": 1.0, "no-summary": 0.5}
    block = runner.arm_table(
        ["all-on", "no-kwd", "no-summary"], full, rows, embedder_id="bge-m3"
    )
    assert (
        block["no-kwd"]["vacuous"] is True and "observed" in block["no-kwd"]["reason"]
    )
    assert block["no-summary"]["vacuous"] is False
    assert block["all-on"]["switches_off"] == [] and block["no-kwd"][
        "switches_off"
    ] == ["kwd"]


def test_report_markdown_has_one_row_per_arm_and_deltas_vs_all_on():
    rows = [
        _row("t1", "all-on", "a", tokens_to_first_gold=100.0),
        _row("t2", "all-on", "b", tokens_to_first_gold=300.0, recall_at_5=0.0),
        _row("t1", "no-kwd", "a", tokens_to_first_gold=60.0, refusals=2, calls=5),
        _row(
            "t2",
            "no-kwd",
            "b",
            tokens_to_first_gold=8000.0,
            censored=True,
            found=False,
            recall_at_5=0.0,
        ),
        _row("t1", "whole-paper", None, tokens_to_first_gold=5000.0, recall_at_5=None),
        _row(
            "t3",
            "all-on",
            "c",
            abstention=True,
            found=None,
            tokens_to_first_gold=None,
            tokens_total=900.0,
            recall_at_5=None,
        ),
    ]
    results = {
        "header": {
            "set": "quick",
            "precis_sha": "abcdef012345",
            "arms": {"no-kwd": {"vacuous": True, "reason": "x"}},
            "layer_coverage": ZERO,
            "embedder": {"name": "bge-m3", "meaningful": True},
        },
        "rows": rows,
    }
    md = runner.report_markdown(results)
    lines: dict[str, str] = {}
    for line in md.splitlines():  # first table wins: all answerable tasks
        if line.startswith("| ") and "arm" not in line.split("|")[1]:
            lines.setdefault(line.split("|")[1].strip(), line)
    # all-on: n=2, recall@5 mean 0.5, median of (100, 300) = 200; no delta shown
    assert "| 2 | 0.500 | 200 | 0 | 4.0 | 1.0 | 0 | — | — |" in lines["all-on"]
    # no-kwd: n=2, median over the one task that reached gold = 60 (the censored
    # task, carrying the 8000 budget, is counted in "censored" and not averaged
    # in), 1 censored, refusals 2; delta vs all-on's 200
    assert "| 2 | 0.500 | 60 | 1 | 4.5 | 1.0 | 2 | +0.000 | -140 |" in lines["no-kwd"]
    assert "VACUOUS" in lines["no-kwd"]
    assert "| 1 | — | 5000 |" in lines["whole-paper"]
    assert "Unanswerable tasks" in md and "| all-on | 1 | 900 |" in md
    assert "Vacuous arms" in md


def test_results_filename_pins_date_and_short_sha():
    assert (
        runner.results_filename("2026-11-09T21:03:11Z", "0123456789abcdef", "quick")
        == "20261109Z-0123456-quick.json"
    )
    assert (
        runner.results_filename(
            "2026-11-09T21:03:11Z", "0123456789abcdef", "quick", "triage"
        )
        == "20261109Z-0123456-quick-triage.json"
    )


def test_load_results_rejects_other_json(tmp_path: Path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"eval": "retrieval"}), encoding="utf-8")
    with pytest.raises(runner.BenchRunError, match="not a benchmark results file"):
        runner.load_results(p)


# ---------------------------------------------------------------------------
# pure: the raw-log reader (gr624301)
# ---------------------------------------------------------------------------


def _raw_results() -> dict[str, Any]:
    return {
        "header": {"set": "quick", "caller": {"name": "triage"}},
        "rows": [
            {
                **_row("t1", "all-on", "a"),
                "call_log": [
                    {
                        "n": 1,
                        "verb": "search",
                        "what": "paper q?",
                        "tokens": 100.0,
                        "refusal": False,
                    },
                    {
                        "n": 2,
                        "verb": "get",
                        "what": "pc1 verbatim",
                        "tokens": 40.0,
                        "refusal": False,
                    },
                ],
            },
            {**_row("t2", "all-on", "b", found=False, censored=True), "call_log": []},
            {
                **_row("t1", "no-kwd", "c", refusals=1),
                "call_log": [
                    {
                        "n": 1,
                        "verb": "search",
                        "what": "paper q?",
                        "tokens": 100.0,
                        "refusal": False,
                    },
                    {
                        "n": 2,
                        "verb": "get",
                        "what": "pc1 kwd",
                        "tokens": 10.0,
                        "refusal": True,
                    },
                ],
            },
        ],
    }


def test_raw_reader_selects_by_arm_and_task_and_pages():
    res = _raw_results()
    md = raw.raw_markdown(res)
    assert md.splitlines()[0] == ("# raw log quick (caller triage) — rows 0-3 of 3")
    assert "1. search paper q? — 100 tok" in md
    assert "2. get pc1 verbatim — 40 tok" in md
    only = raw.raw_markdown(res, arms=("no-kwd",))
    assert only.splitlines()[0].endswith("— rows 0-1 of 1")
    assert "2. get pc1 kwd — 10 tok (refused)" in only
    assert "pc1 verbatim" not in only
    by_task = raw.raw_markdown(res, task="t1")
    assert by_task.splitlines()[0].endswith("— rows 0-2 of 2")
    page = raw.raw_markdown(res, offset=1, limit=1)
    assert page.splitlines()[0].endswith("— rows 1-2 of 3")
    assert "Next:" in page and "--offset 2" in page and "read rows 2-3 of 3" in page
    last = raw.raw_markdown(res, offset=2, limit=1)
    assert "Next:" not in last
    with pytest.raises(ValueError, match="offset"):
        raw.raw_markdown(res, offset=-1)


def test_raw_reader_tolerates_schema1_rows_without_call_log():
    res = {"header": {"set": "quick"}, "rows": [_row("t1", "all-on", "a")]}
    md = raw.raw_markdown(res)
    assert "task t1" in md and "tok→gold 100.0" in md
    assert "calls 4" in md  # the summary still prints
    assert "\n  1." not in md  # but no call lines


def test_raw_reader_next_hint_replays_path_and_selectors(tmp_path: Path):
    """``offset`` pages the *filtered* selection, so the ``Next:`` call must
    replay the file and every active selector — without them it would page
    the unfiltered table at the filtered ``end`` (review of td626657)."""
    res = _raw_results()
    res["rows"] += [
        {
            **_row(
                "t3",
                "all-on",
                "d",
                abstention=True,
                found=None,
                tokens_to_first_gold=None,
            ),
            "call_log": [],
        },
        {
            **_row(
                "t4",
                "all-on",
                "e",
                abstention=True,
                found=None,
                tokens_to_first_gold=None,
            ),
            "call_log": [],
        },
    ]
    page = raw.raw_markdown(
        res,
        arms=("all-on", "no-kwd"),
        source="qasper",
        limit=1,
        results_path=tmp_path / "run 1.json",
    )
    assert (
        f"scripts/bench raw '{tmp_path}/run 1.json' --arm all-on "
        "--arm no-kwd --source qasper --offset 1 --limit 1"
    ) in page
    by_task = raw.raw_markdown(res, task="t1", limit=1, results_path="r.json")
    assert "scripts/bench raw r.json --task t1 --offset 1 --limit 1" in by_task
    only = raw.raw_markdown(res, abstention=True, limit=1, results_path="r.json")
    assert "scripts/bench raw r.json --abstention --offset 1 --limit 1" in only
    # no path known: the parse-only placeholder stands in
    page = raw.raw_markdown(res, limit=1)
    assert "scripts/bench raw <results.json> --offset 1 --limit 1" in page


def test_raw_cli_next_hint_carries_the_real_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_raw_results()), encoding="utf-8")
    assert cli.main(["raw", str(p), "--arm", "all-on", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert f"scripts/bench raw {p} --arm all-on --offset 1 --limit 1" in out


def test_raw_cli_bad_paging_is_an_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_raw_results()), encoding="utf-8")
    for bad in (["--offset", "-1"], ["--limit", "0"]):
        assert cli.main(["raw", str(p), *bad]) == 1
        assert capsys.readouterr().err.startswith(
            "error: offset must be >= 0 and limit >= 1"
        )


# ---------------------------------------------------------------------------
# pure: the bench: read address' file resolution (gr618517)
# ---------------------------------------------------------------------------


def test_resolve_results_names_only_stems_inside_the_dir(tmp_path: Path):
    (tmp_path / "run1.json").write_text("{}", encoding="utf-8")
    assert raw.resolve_results(tmp_path, "run1") == tmp_path / "run1.json"
    assert raw.resolve_results(tmp_path, "run1.json") == tmp_path / "run1.json"
    assert raw.resolve_results(tmp_path, "missing") is None
    assert raw.resolve_results(tmp_path, "") is None
    # traversal resolves to None, never to a path outside the dir
    assert raw.resolve_results(tmp_path, "..") is None
    assert raw.resolve_results(tmp_path, "../run1") is None
    assert raw.resolve_results(tmp_path, "sub/run1") is None
    assert raw.resolve_results(tmp_path, "C:\\run1") is None


def test_list_results_stems_newest_first(tmp_path: Path):
    import os

    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("{}", encoding="utf-8")
    b.write_text("{}", encoding="utf-8")
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    assert raw.list_results_stems(tmp_path) == ["b", "a"]
    assert raw.list_results_stems(tmp_path / "absent") == []


def test_arm_and_task_vocabularies_come_from_the_rows():
    res = _raw_results()
    assert raw.arm_names(res) == ["all-on", "no-kwd"]
    assert raw.task_ids(res) == ["t1", "t2"]


# ---------------------------------------------------------------------------
# pure: the ledger line (the run's raw MCP-log records, gr618517)
# ---------------------------------------------------------------------------


def test_call_line_is_the_ledger_shape_of_a_call_log_entry():
    line = raw.call_line(
        {
            "call_id": 1042,
            "ts": None,
            "verb": "get",
            "kind": "paper",
            "outcome": "ok",
            "error_type": None,
            "latency_ms": 96,
            "result_bytes": 2048,
            "result_tokens_est": 512,
            "arg_sample": {"id": "pc88", "extent": "verbatim"},
        }
    )
    assert line.startswith("  1042 get paper ok — ")
    # gr618518: fields render by name — the ledger's own estimate, not
    # the results file's caller-side ``tokens``.
    assert "result_tokens_est=512" in line
    assert "result_bytes=2048" in line
    assert "96ms" in line
    assert '"extent": "verbatim"' in line and '"id": "pc88"' in line


def test_call_line_marks_errors_and_unmeasured_calls():
    err = raw.call_line(
        {
            "call_id": 7,
            "ts": None,
            "verb": "get",
            "kind": None,
            "outcome": "error",
            "error_type": "BadInput",
            "latency_ms": None,
            "result_bytes": None,
            "result_tokens_est": None,
            "arg_sample": None,
        }
    )
    assert "7 get error (BadInput)" in err
    assert "result_tokens_est=-" in err and "result_bytes=-" in err


# ---------------------------------------------------------------------------
# DB: the tool_calls ledger read (gr618517)
# ---------------------------------------------------------------------------


def _insert_tool_calls(store, rows: list[dict[str, Any]]) -> None:
    """tool_calls rows as ``record_call`` would write them — only the
    columns the ledger read renders need to be set."""
    with store.pool.connection() as conn:
        for r in rows:
            conn.execute(
                "INSERT INTO tool_calls (verb, kind, outcome, error_type, "
                "source, ts, latency_ms, result_bytes, result_tokens_est, "
                "arg_sample) VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()), "
                "%s, %s, %s, %s::jsonb)",
                (
                    r.get("verb", "get"),
                    r.get("kind"),
                    r.get("outcome", "ok"),
                    r.get("error_type"),
                    r.get("source"),
                    r.get("ts"),
                    r.get("latency_ms"),
                    r.get("result_bytes"),
                    r.get("result_tokens_est"),
                    json.dumps(r["arg_sample"]) if r.get("arg_sample") else None,
                ),
            )


def test_fetch_ledger_correlates_by_source_tag(store):
    tag = "bench:20261110Z-abc1234-quick-triage"
    _insert_tool_calls(
        store,
        [
            {
                "verb": "search",
                "kind": "paper",
                "source": tag,
                "result_bytes": 400,
                "result_tokens_est": 100,
                "arg_sample": {"kind": "paper", "q": "zork1"},
            },
            {"verb": "get", "source": tag, "result_tokens_est": 20},
            {"verb": "get", "result_tokens_est": 999},  # untagged — another caller
            {"verb": "get", "source": "bench:other-run", "result_tokens_est": 3},
        ],
    )
    recs, total = raw.fetch_ledger(store, source=tag)
    assert total == 2
    assert [r["verb"] for r in recs] == ["search", "get"]
    assert recs[0]["result_tokens_est"] == 100
    assert raw.call_line(recs[0]).count("result_tokens_est=100") == 1
    page, total = raw.fetch_ledger(store, source=tag, offset=1, limit=1)
    assert total == 2 and len(page) == 1 and page[0]["verb"] == "get"
    assert raw.fetch_ledger(store, source="bench:no-such-run") == ([], 0)


def test_fetch_ledger_window_keeps_untagged_rows_in_bounds(store):
    _insert_tool_calls(
        store,
        [
            {"verb": "get", "ts": "2001-01-01T00:30:00Z", "result_tokens_est": 11},
            {
                "verb": "get",
                "ts": "2001-01-01T00:40:00Z",
                "source": "bench:tagged-run",
                "result_tokens_est": 13,
            },
            {"verb": "get", "ts": "2001-01-02T00:00:00Z", "result_tokens_est": 17},
        ],
    )
    recs, total = raw.fetch_ledger(
        store,
        started_at="2001-01-01T00:00:00Z",
        ended_at="2001-01-01T02:00:00Z",
    )
    assert total == 1  # in-window AND untagged; the tagged row never leaks in
    assert recs[0]["result_tokens_est"] == 11
    assert raw.fetch_ledger(store) == ([], 0)


# ---------------------------------------------------------------------------
# DB: the caller, the baselines, the coverage guard
# ---------------------------------------------------------------------------


def test_canonical_text_roundtrips_from_chunks(world):
    cat = world["catalog"]
    for qid, (_, paper) in world["papers"].items():
        (ref_id,) = cat.doc_ref_ids(f"qasper:{qid}")
        assert cat.canonical_text(ref_id) == corpus.canonical_text(paper)


def test_scripted_caller_finds_gold_and_prices_the_calls(world):
    task = _by_question(world["rows"], "What did the quasar1x2 instrument")
    r = runner.run_scripted(world["runtime"], world["catalog"], task, "all-on")
    assert r["found"] is True and r["censored"] is False and r["stop"] == "found"
    assert 0 < r["tokens_to_first_gold"] <= r["tokens_total"]
    assert r["calls"] >= 2 and r["errors"] == 0 and r["refusals"] == 0
    # the lexical match is in the pool of 10 hits, at rank >= 1
    assert r["recall_at_10"] == 1.0 and r["recall_at_1"] in (0.0, 1.0)
    assert len(r["transcript_sha256"]) == 64


def test_scripted_caller_is_deterministic(world):
    tasks, _ = runner.select_tasks(world["rows"])
    a = [
        runner.run_scripted(world["runtime"], world["catalog"], t, "all-on")
        for t in tasks
    ]
    b = [
        runner.run_scripted(world["runtime"], world["catalog"], t, "all-on")
        for t in tasks
    ]
    assert [_comparable(x) for x in a] == [_comparable(x) for x in b]
    assert len({x["task"] for x in a}) == len(tasks)


def test_tiny_budget_censors_the_task(world):
    task = _by_question(world["rows"], "What did the quasar1x2 instrument")
    r = runner.run_scripted(
        world["runtime"], world["catalog"], task, "all-on", budget=1
    )
    assert r["censored"] is True and r["found"] is False
    assert r["tokens_to_first_gold"] == 1.0 and r["stop"] == "budget"
    assert r["calls"] <= 2  # the search, at most one get: the budget stops the rest


def test_the_ladder_caller_asks_for_a_switched_off_rung_and_is_refused(world):
    task = _by_question(world["rows"], "What did the quasar1x2 instrument")
    base = runner.run_scripted(
        world["runtime"], world["catalog"], task, "all-on", caller="ladder"
    )
    nokwd = runner.run_scripted(
        world["runtime"], world["catalog"], task, "no-kwd", caller="ladder"
    )
    nosum = runner.run_scripted(
        world["runtime"], world["catalog"], task, "no-summary", caller="ladder"
    )
    assert base["refusals"] == 0
    assert nokwd["refusals"] >= 1 and nosum["refusals"] >= 1
    assert nokwd["errors"] == 0 and nosum["errors"] == 0  # refusals are not errors
    # the gold is still reached (verbatim is untouched), the answer is the same
    assert nokwd["found"] and nosum["found"]


def test_unanswerable_task_runs_the_sequence_and_reports_tokens_only(world):
    task = _by_question(world["rows"], "Did the team1x7 carry out")
    assert task.abstain
    r = runner.run_scripted(world["runtime"], world["catalog"], task, "all-on")
    assert r["abstention"] and r["found"] is None and r["censored"] is False
    assert r["recall_at_5"] is None and r["tokens_to_first_gold"] is None
    assert r["tokens_total"] > 0 and r["calls"] >= 2


def test_chunk_rag_tokens_are_the_ranked_chunk_texts(world):
    from precis.mesh_eval.retrieval import search_chunk_triples

    task = _by_question(world["rows"], "What did the quasar1x2 instrument")
    r = runner.run_chunk_rag(world["store"], world["embedder"], world["catalog"], task)
    triples = search_chunk_triples(
        world["store"], task.question, runner.SEARCH_K, world["embedder"]
    )
    infos = world["catalog"].chunks(int(b.id) for b, _r, _s in triples)
    expect = 0.0
    for block, _ref, _s in triples:
        expect += len(block.text) / 4
        if runner.item_overlaps(task, infos[int(block.id)]):
            break
    else:
        pytest.fail("the gold paragraph was not retrieved")
    assert r["calls"] == 1 and r["refusals"] == 0 and r["found"] is True
    assert r["tokens_to_first_gold"] == pytest.approx(expect, abs=0.01)
    assert r["tokens_total"] == pytest.approx(expect, abs=0.01)
    assert r["recall_at_10"] == 1.0 and r["transcript_sha256"] is None


def test_whole_paper_tokens_are_the_canonical_text_of_the_docs(world):
    task = _by_question(world["rows"], "What did the quasar1x2 instrument")
    r = runner.run_whole_paper(world["catalog"], task)
    paper = world["papers"]["9904.00001"][1]
    assert r["calls"] == 1 and r["found"] is True
    assert r["tokens_total"] == pytest.approx(
        len(corpus.canonical_text(paper)) / 4, abs=0.01
    )
    assert r["tokens_to_first_gold"] == pytest.approx(r["tokens_total"], abs=0.01)
    assert r["recall_at_5"] is None
    two = runner.BenchTask(
        {**task.row, "docs": ["qasper:9904.00001", "qasper:9904.00002"]}
    )
    r2 = runner.run_whole_paper(world["catalog"], two)
    other = world["papers"]["9904.00002"][1]
    want = (len(corpus.canonical_text(paper)) + len(corpus.canonical_text(other))) / 4
    assert r2["calls"] == 2 and r2["tokens_total"] == pytest.approx(want, abs=0.01)


def test_baseline_rows_carry_a_per_call_token_log(world):
    task = _by_question(world["rows"], "What did the quasar1x2 instrument")
    rag = runner.run_chunk_rag(
        world["store"], world["embedder"], world["catalog"], task
    )
    assert rag["call_log"] and all(c["verb"] == "chunk" for c in rag["call_log"])
    assert [c["n"] for c in rag["call_log"]] == list(range(1, len(rag["call_log"]) + 1))
    assert sum(c["tokens"] for c in rag["call_log"]) == pytest.approx(
        rag["tokens_total"], abs=0.1
    )
    wp = runner.run_whole_paper(world["catalog"], task)
    assert wp["call_log"] and all(c["verb"] == "doc" for c in wp["call_log"])
    assert sum(c["tokens"] for c in wp["call_log"]) == pytest.approx(
        wp["tokens_total"], abs=0.1
    )


def test_layer_coverage_counts_what_the_db_holds(world):
    store = world["store"]
    with store.pool.connection() as conn:
        ids = [
            int(r[0])
            for r in conn.execute("SELECT ref_id FROM refs WHERE meta ? 'qasper'")
        ]
        cov = layers.coverage(conn, ids)
        assert cov["refs"] == 2 and cov["chunks"] > 0
        assert cov["chunks_with_keywords"] == 0 and cov["chunks_with_embedding"] == 0
        assert cov["chunks_with_llm_summary"] == 0 and cov["mesh_edges"] == 0
        assert cov["refs_with_categorizer_tag"] == 0
        assert (
            layers.arm_vacuity("no-kwd", cov, embedder_id="bge-m3")["vacuous"] is True
        )
        conn.execute(
            "UPDATE chunks SET keywords = ARRAY['quasar'] WHERE ref_id = ANY(%s) AND ord >= 0",
            (ids,),
        )
        cov2 = layers.coverage(conn, ids)
        assert cov2["chunks_with_keywords"] == cov2["chunks"]
        assert cov2["chunks_with_keywords_frac"] == 1.0
        assert (
            layers.arm_vacuity("no-kwd", cov2, embedder_id="bge-m3")["vacuous"] is False
        )
        assert (
            layers.arm_vacuity("no-summary", cov2, embedder_id="bge-m3")["vacuous"]
            is True
        )


# ---------------------------------------------------------------------------
# DB: planted facts through the same arms
# ---------------------------------------------------------------------------


def _planted_world(world, n: int = 7, seed: int = 3):
    store, runtime = world["store"], world["runtime"]
    with store.pool.connection() as conn:
        ids = [
            int(r[0])
            for r in conn.execute("SELECT ref_id FROM refs WHERE meta ? 'qasper'")
        ]
        protected = planted_ingest.evidence_units(world["rows"])
        hosts = planted_ingest.pick_hosts(
            conn, ids, protected=protected, seed=seed, n_docs=3, per_doc=4
        )
        built, side = planted.build(hosts, seed, n, at=runner.PLANTED_AT)
        rows = [gold.from_planted(r, side["docs"]) for r in built]
        refs = planted_ingest.ingest_docs(
            conn, side["docs"], seed=seed, at=runner.PLANTED_AT
        )
        mem = planted_ingest.ingest_memory(runtime, conn, side["memory_nodes"])
    return hosts, rows, side, refs, mem


def test_pick_hosts_is_seeded_and_never_takes_a_gold_paragraph(world):
    store = world["store"]
    protected = planted_ingest.evidence_units(world["rows"])
    assert protected  # the fixture does cite paragraphs
    with store.pool.connection() as conn:
        ids = [
            int(r[0])
            for r in conn.execute("SELECT ref_id FROM refs WHERE meta ? 'qasper'")
        ]
        a = planted_ingest.pick_hosts(
            conn, ids, protected=protected, seed=3, n_docs=3, per_doc=4
        )
        b = planted_ingest.pick_hosts(
            conn, ids, protected=protected, seed=3, n_docs=3, per_doc=4
        )
        c = planted_ingest.pick_hosts(
            conn, ids, protected=protected, seed=4, n_docs=3, per_doc=4
        )
        with pytest.raises(ValueError, match="eligible host paragraphs"):
            planted_ingest.pick_hosts(
                conn, ids, protected=protected, seed=3, n_docs=8, per_doc=12
            )
    assert a == b and a != c
    gold_texts = {
        u.text
        for qid, (_, p) in world["papers"].items()
        for u in corpus.canonical_units(p)
        if (f"qasper:{qid}", u.para_id) in protected
    }
    assert gold_texts
    used = {p for d in a for p in d["paragraphs"]}
    assert len(used) == 12 and not (used & gold_texts)


def test_planted_docs_and_memory_nodes_ingest_idempotently(world):
    store = world["store"]
    _, rows, side, refs, mem = _planted_world(world)
    # an agreeing stance task plants one fact twice: one memory node per fact id
    facts = {n["fact_id"] for n in side["memory_nodes"]}
    assert len(refs) == 3 and len(mem) == len(facts) > 0
    with store.pool.connection() as conn:
        ref_id = refs[side["docs"][0]["id"]]
        text_now = world["catalog"].canonical_text(ref_id)
        assert text_now == gold.joined_text(side["docs"][0]["paragraphs"])
        n_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        again = planted_ingest.ingest_docs(
            conn, side["docs"], seed=3, at=runner.PLANTED_AT
        )
        mem2 = planted_ingest.ingest_memory(
            world["runtime"], conn, side["memory_nodes"]
        )
        assert again == refs and mem2 == mem
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == n_chunks


def test_planted_tasks_run_through_the_same_arms(world):
    _, rows, side, refs, mem = _planted_world(world)
    gold_text = planted_ingest.gold_sentences(rows, side["memory_nodes"])
    tasks, skipped = runner.select_tasks(rows, gold_text)
    assert skipped == {"answerable_without_evidence": 0}
    types = {t.row["type"] for t in tasks}
    assert {"lookup", "memory"} <= types
    for t in tasks:
        assert t.source == "planted"
    # This DB has no embeddings and the lexical leg wants every query term in
    # the chunk, so ask with the planted sentence itself (a real run asks the
    # generated question and relies on the embeddings).
    by_doc = {d["id"]: d["paragraphs"] for d in side["docs"]}
    base = next(t for t in tasks if t.row["type"] == "lookup")
    ev = base.row["evidence"][0]
    sentence = by_doc[ev["doc"]][ev["unit"]][ev["span"][0] : ev["span"][1]]
    lookup = runner.BenchTask({**base.row, "question": sentence})
    m = next(t for t in tasks if t.is_memory)
    memory = runner.BenchTask({**m.row, "question": m.gold_text[0]}, m.gold_text)
    assert memory.gold_text and not memory.docs
    cat = world["catalog"]
    r_l = runner.run_scripted(world["runtime"], cat, lookup, "all-on")
    r_m = runner.run_scripted(world["runtime"], cat, memory, "all-on")
    assert r_l["found"] is True, r_l
    assert r_m["found"] is True and r_m["recall_at_10"] == 1.0, r_m
    rag = runner.run_chunk_rag(world["store"], world["embedder"], cat, memory)
    assert rag["found"] is True
    wp = runner.run_whole_paper(cat, memory)
    assert wp["applicable"] is False and wp["found"] is None and wp["censored"] is False
    wp_l = runner.run_whole_paper(cat, lookup)
    assert wp_l["found"] is True and wp_l["tokens_total"] > 0


# ---------------------------------------------------------------------------
# DB: the whole thing, results header
# ---------------------------------------------------------------------------


def test_run_cli_writes_a_results_file_whose_header_pins_everything(
    world, manifest, task_rows, tmp_path: Path, monkeypatch
):
    man_path = tmp_path / "manifest.json"
    man_path.write_text(corpus.manifest_text(manifest), encoding="utf-8")
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text(gold.dump_tasks(task_rows), encoding="utf-8")
    monkeypatch.setenv(
        "PRECIS_BENCH_GIT_SHA", "0123456789abcdef0123456789abcdef01234567"
    )
    monkeypatch.setenv("PRECIS_BENCH_GIT_DIRTY", "0")
    out = tmp_path / "res" / "r.json"
    path = runner.run_cli(
        set_name="quick",
        arms=["no-kwd", "chunk-rag", "whole-paper"],
        seed=3,
        out=out,
        embedder_name="mock",
        manifest_path=man_path,
        tasks_path=tasks_path,
        planted_n=7,
        budget=2000,
        limit=None,
        dsn=world["store"].dsn,
        fill=False,
        host_docs=3,
        host_paras=4,
    )
    res = runner.load_results(path)
    h = res["header"]
    assert (
        h["schema"] == runner.RESULTS_SCHEMA
        and h["tier"] == "scripted"
        and h["set"] == "quick"
    )
    assert h["precis_sha"].startswith("0123456") and h["git_dirty"] is False
    assert h["corpus_sha256"] == manifest["corpus_sha256"]["quick"]
    assert h["tasks_sha256"] == runner.file_sha256(tasks_path)
    assert h["planted"]["seed"] == 3 and h["planted"]["at"] == runner.PLANTED_AT
    assert len(h["planted"]["sha256"]) == 64 and h["planted"]["n"] == 7
    assert h["embedder"]["name"] == "mock" and h["embedder"]["meaningful"] is False
    assert h["token_estimator"] == runner.TOKEN_ESTIMATOR and h["token_budget"] == 2000
    assert (
        h["caller"]["ladder"] == list(runner.LADDER)
        and h["caller"]["read_k"] == runner.READ_K
    )
    assert h["models"] is None and h["judge"] is None
    for key in (
        "chunks_with_keywords_frac",
        "chunks_with_llm_summary_frac",
        "chunks_with_embedding_frac",
        "refs_with_categorizer_tag_frac",
        "refs_with_edge_frac",
        "embedder_counts",
    ):
        assert key in h["layer_coverage"], key
    # all-on is always run (every delta and the vacuity test need it)
    assert list(h["arms"]) == ["all-on", "no-kwd", "chunk-rag", "whole-paper"]
    assert h["arms"]["no-kwd"]["switches_off"] == ["kwd"]
    assert h["arms"]["no-kwd"]["vacuous"] is True  # no keywords in this DB
    assert h["arms"]["all-on"]["embedder_note"] == "embedder: hash (not meaningful)"
    assert h["started_at"].endswith("Z") and h["ended_at"].endswith("Z")
    assert (
        h["tasks"]["qasper"] > 0
        and h["tasks"]["planted"] == 7
        and h["tasks"]["abstention"] == 2
    )
    assert h["tasks"]["skipped"] == {"answerable_without_evidence": 2}
    n_tasks = sum(h["tasks"][k] for k in ("qasper", "planted"))
    assert len(res["rows"]) == n_tasks * 4
    assert {r["arm"] for r in res["rows"]} == {
        "all-on",
        "no-kwd",
        "chunk-rag",
        "whole-paper",
    }
    assert all("call_log" in r for r in res["rows"])
    md = runner.report_markdown(res)
    assert "| no-kwd |" in md and "VACUOUS" in md and "Unanswerable" in md


def test_run_cli_is_deterministic_apart_from_wall_time(
    world, manifest, task_rows, tmp_path: Path
):
    man_path = tmp_path / "manifest.json"
    man_path.write_text(corpus.manifest_text(manifest), encoding="utf-8")
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text(gold.dump_tasks(task_rows), encoding="utf-8")
    kw: dict[str, Any] = dict(
        set_name="quick",
        arms=["no-summary"],
        seed=3,
        embedder_name="mock",
        manifest_path=man_path,
        tasks_path=tasks_path,
        planted_n=7,
        budget=2000,
        limit=None,
        dsn=world["store"].dsn,
        fill=False,
        host_docs=3,
        host_paras=4,
    )
    a = runner.load_results(runner.run_cli(out=tmp_path / "a.json", **kw))
    b = runner.load_results(runner.run_cli(out=tmp_path / "b.json", **kw))
    assert [_comparable(r) for r in a["rows"]] == [_comparable(r) for r in b["rows"]]
    assert a["header"]["planted"] == b["header"]["planted"]
    assert copy.deepcopy(a["header"]["arms"]) == b["header"]["arms"]


# ---------------------------------------------------------------------------
# the triage caller (stub runtime: scripted rung texts, no DB)
# ---------------------------------------------------------------------------


class _StubCatalog(runner.Catalog):
    def __init__(self) -> None:
        super().__init__(store=None)

    def chunks(self, ids: Iterable[int]) -> dict[int, runner.ChunkInfo]:
        return {}


class _StubRuntime:
    """``search`` returns fixed hits; ``get`` returns scripted text per
    (handle, extent). Refuses a switched-off ``kwd`` / ``summary`` rung the way
    the server does (an error naming the switches variable)."""

    def __init__(
        self,
        hits: list[str],
        texts: dict[tuple[str, str], str],
        rows: dict[str, str] | None = None,
    ) -> None:
        self.hits, self.texts, self.rows = hits, texts, rows or {}
        self.log: list[tuple[str, str]] = []

    def dispatch_with_status(self, verb, args, fold=True):
        from precis.bench import switches

        if verb == "search":
            rows = "\n".join(f"{h}\t{self.rows.get(h, 'kw')}\t" for h in self.hits)
            return f"# hits\n{rows}\nNext:\n x", False
        handle, ext = args["id"], args["extent"]
        sw = switches.current()
        if (ext == "kwd" and not sw.kwd) or (ext == "summary" and not sw.summary):
            return f"refused: {switches.ENV_VAR}", True
        self.log.append((handle, ext))
        return self.texts.get((handle, ext), f"{handle} {ext}"), False


def _stub_task() -> runner.BenchTask:
    return runner.BenchTask(
        {
            "id": "t1",
            "labelled_by": "qasper",
            "question": "How does alpha beta gamma work?",
            "docs": [],
            "answer_kind": "extractive",
            "type": "lookup",
            "evidence": [{"doc": "d", "unit": "p0", "span": [0, 1]}],
        }
    )


#: question content words: alpha beta gamma work. kwd coverage per hit:
#: pc1 .25, pc2 0, pc3 1.0, pc4 .5, pc5 0; the gold is in pc4's verbatim.
_HITS = ["pc1", "pc2", "pc3", "pc4", "pc5"]
_KWD = {
    ("pc1", "kwd"): "pa1 [paper] gamma",
    ("pc2", "kwd"): "pa1 [paper] unrelated",
    ("pc3", "kwd"): "pa1 [paper] alpha beta gamma work",
    ("pc4", "kwd"): "pa1 [paper] alpha beta",
    ("pc5", "kwd"): "pa1 [paper] nothing",
    ("pc4", "verbatim"): "the GOLD chunk",
}


@pytest.fixture
def stub_gold(monkeypatch):
    monkeypatch.setattr(runner, "rendered_gold", lambda task, text, cat: "GOLD" in text)


def _stub_run(arm: str, caller: str, **kw: Any):
    rt = _StubRuntime(list(_HITS), dict(_KWD))
    row = runner.run_scripted(
        rt, _StubCatalog(), _stub_task(), arm, caller=caller, **kw
    )
    return row, rt


def _verbatims(rt: _StubRuntime) -> list[str]:
    return [h for h, ext in rt.log if ext == "verbatim"]


def test_overlap_score_is_question_content_word_coverage():
    q = "How does alpha beta gamma work?"
    assert runner.overlap_score(q, "alpha beta gamma work") == 1.0
    assert runner.overlap_score(q, "pa1 [paper] alpha beta") == 0.5
    assert runner.overlap_score(q, "nothing here") == 0.0
    assert runner.overlap_score("the of a", "the of a") == 0.0  # no content words
    # coverage does not shrink as the rung text grows (Jaccard would)
    assert runner.overlap_score(q, "alpha beta " + "filler " * 200) == 0.5


def test_triage_is_deterministic(stub_gold):
    a, rt_a = _stub_run("all-on", "triage", threshold=0.5)
    b, rt_b = _stub_run("all-on", "triage", threshold=0.5)
    assert _comparable(a) == _comparable(b) and rt_a.log == rt_b.log
    assert a["caller"] == "triage"


def test_call_log_records_every_call_with_tokens_and_marks_refusals(stub_gold):
    row, _rt = _stub_run("all-on", "triage", threshold=0.5)
    log = row["call_log"]
    assert [c["n"] for c in log] == list(range(1, len(log) + 1))
    assert log[0]["verb"] == "search" and log[0]["what"].startswith("paper")
    assert all(c["tokens"] > 0 for c in log)
    assert sum(c["tokens"] for c in log) == pytest.approx(row["tokens_total"], abs=0.2)
    lad = _stub_run("no-kwd", "ladder")[0]
    refused = [c for c in lad["call_log"] if c["refusal"]]
    assert refused and all(c["what"].endswith("kwd") for c in refused)
    unrefused = [c for c in lad["call_log"] if not c["refusal"]]
    assert len(unrefused) == lad["calls"] - lad["refusals"]


def test_triage_opens_by_score_above_the_threshold_and_fewer_than_ladder(stub_gold):
    tri, rt_t = _stub_run("all-on", "triage", threshold=0.5)
    lad, rt_l = _stub_run("all-on", "ladder")
    # kwd read on every hit, then verbatim for the scores >= 0.5, best first:
    # pc3 (1.0) then pc4 (0.5, gold). pc1 (rank 1, score .25) is never opened.
    assert [h for h, e in rt_t.log if e == "kwd"] == _HITS
    assert _verbatims(rt_t) == ["pc3", "pc4"]
    assert tri["found"] is True and tri["stop"] == "found"
    # the ladder climbs every hit down to the gold in rank order
    assert _verbatims(rt_l) == ["pc1", "pc2", "pc3", "pc4"]
    assert lad["found"] is True and lad["caller"] == "ladder"
    assert tri["verbatim_opens"] == 2 < lad["verbatim_opens"] == 4
    assert tri["tokens_to_first_gold"] < lad["tokens_to_first_gold"]


def test_triage_threshold_cuts_what_it_opens(stub_gold):
    row, rt = _stub_run("all-on", "triage", threshold=0.9)
    assert _verbatims(rt) == ["pc3"]  # only pc3 clears 0.9; the gold (pc4) is missed
    assert row["found"] is False and row["censored"] is True
    # nothing cleared: nothing opened, and no fisheye / +recall on an unopened hit
    row0, rt0 = _stub_run("all-on", "triage", threshold=1.1)
    assert _verbatims(rt0) == [] and row0["verbatim_opens"] == 0
    assert [e for _h, e in rt0.log if e in ("fisheye", "+recall")] == []


def test_triage_follows_a_miss_with_fisheye_then_recall_on_the_best_opened_hit(
    stub_gold,
):
    texts = {k: v for k, v in _KWD.items() if k != ("pc4", "verbatim")}
    rt = _StubRuntime(list(_HITS), texts)  # no gold anywhere
    row = runner.run_scripted(
        rt, _StubCatalog(), _stub_task(), "all-on", caller="triage", threshold=0.5
    )
    assert row["found"] is False
    assert rt.log[-2:] == [("pc3", "fisheye"), ("pc3", "+recall")]
    # each once, on the best-scoring opened hit
    assert [e for _h, e in rt.log].count("fisheye") == 1


def test_triage_without_cheap_rungs_opens_verbatim_in_rank_order(stub_gold):
    rt = _StubRuntime(list(_HITS), dict(_KWD))
    flags = {"kwd": False, "summary": False}
    s = runner.Session(rt, flags, runner.TOKEN_BUDGET)
    found = runner._triage(
        s, _stub_task(), _StubCatalog(), list(_HITS), runner.READ_K, threshold=0.99
    )
    # no kwd / summary read, no score, no cut: rank order, up to the gold (pc4)
    assert [e for _h, e in rt.log if e in ("kwd", "summary")] == []
    assert _verbatims(rt) == ["pc1", "pc2", "pc3", "pc4"]
    assert found is not None and s.refusals == 0
    # a miss in rank order caps at READ_K opens, then fisheye / +recall on the first
    texts = {k: v for k, v in _KWD.items() if k != ("pc4", "verbatim")}
    rt2 = _StubRuntime(list(_HITS) + ["pc6", "pc7"], texts)
    s2 = runner.Session(rt2, flags, runner.TOKEN_BUDGET)
    assert (
        runner._triage(s2, _stub_task(), _StubCatalog(), rt2.hits, runner.READ_K, 0.0)
        is None
    )
    assert _verbatims(rt2) == _HITS and rt2.log[-2:] == [
        ("pc1", "fisheye"),
        ("pc1", "+recall"),
    ]


def test_triage_never_requests_a_rung_the_arm_switched_off(stub_gold):
    for arm in ("all-on", *ARMS):
        row, _rt = _stub_run(arm, "triage", threshold=0.0)
        assert row["refusals"] == 0 and row["errors"] == 0, arm
    # contrast: the ladder asks and is refused
    assert _stub_run("no-kwd", "ladder")[0]["refusals"] >= 1
    # no-kwd: the cheap read moves to summary; no-fisheye / no-recall skip those reads
    _row, rt = _stub_run("no-kwd", "triage", threshold=0.0)
    assert {e for _h, e in rt.log} <= {"summary", "verbatim", "fisheye", "+recall"}
    rt_nf = _StubRuntime(list(_HITS), {})  # no gold: the follow-ups run
    runner.run_scripted(
        rt_nf,
        _StubCatalog(),
        _stub_task(),
        "no-fisheye",
        caller="triage",
        threshold=0.0,
    )
    assert "fisheye" not in {e for _h, e in rt_nf.log}
    assert ("pc1", "+recall") in rt_nf.log
    rt_nr = _StubRuntime(list(_HITS), {})
    runner.run_scripted(
        rt_nr, _StubCatalog(), _stub_task(), "no-recall", caller="triage", threshold=0.0
    )
    assert "+recall" not in {e for _h, e in rt_nr.log}
    assert ("pc1", "fisheye") in rt_nr.log


#: search-row keywords per hit; coverage of the stub question: pc1 0, pc2 .25,
#: pc3 1.0, pc4 .5, pc5 0. The gold is in pc4's verbatim.
_ROWS = {
    "pc1": "unrelated, other",
    "pc2": "gamma",
    "pc3": "alpha beta, gamma work",
    "pc4": "alpha beta",
    "pc5": "nothing",
}


def _rowkw_run(arm: str, threshold: float, texts=None):
    rt = _StubRuntime(list(_HITS), dict(_KWD) if texts is None else texts, dict(_ROWS))
    row = runner.run_scripted(
        rt, _StubCatalog(), _stub_task(), arm, caller="rowkw", threshold=threshold
    )
    return row, rt


def test_row_texts_reads_each_hits_own_search_row():
    body = "# 2 hits\n\n{handle\tchunk_keywords\tsize}\npc9\talpha, beta\t\npc4\tgamma\t1kB\nNext:\npc1\tx"
    got = runner.row_texts(_stub_task(), body)
    assert got == {"pc9": "alpha, beta\t", "pc4": "gamma\t1kB"}  # Next: is not a hit
    mem = runner.BenchTask({**_stub_task().row, "type": "memory"})
    mbody = "## memory 7: t\nalpha fact\n\n## memory 9: u\nbeta fact\nNext:\n"
    assert set(runner.row_texts(mem, mbody)) == {"me7", "me9"}
    assert "beta fact" in runner.row_texts(mem, mbody)["me9"]


def test_rowkw_scores_the_search_rows_and_reads_no_cheap_rung(stub_gold):
    row, rt = _rowkw_run("all-on", 0.5)
    # no kwd / summary read at all; verbatim by row score: pc3 (1.0) then pc4 (.5, gold)
    assert [e for _h, e in rt.log if e in ("kwd", "summary")] == []
    assert _verbatims(rt) == ["pc3", "pc4"]
    assert row["found"] is True and row["caller"] == "rowkw"
    assert row["calls"] == 3  # the search and two opens
    # a cut above every score opens nothing, and no follow-up on an unopened hit
    row0, rt0 = _rowkw_run("all-on", 1.1)
    assert rt0.log == [] and row0["verbatim_opens"] == 0
    # threshold 0: everything, best score first, ties in rank (pc1 and pc5 last)
    nogold = {k: v for k, v in _KWD.items() if k != ("pc4", "verbatim")}
    _row, rt1 = _rowkw_run("all-on", 0.0, nogold)
    assert _verbatims(rt1) == ["pc3", "pc4", "pc2", "pc1", "pc5"]
    assert rt1.log[-2:] == [("pc3", "fisheye"), ("pc3", "+recall")]


def test_rowkw_without_the_keyword_layer_has_no_signal_and_opens_in_rank_order(
    stub_gold,
):
    nogold = {k: v for k, v in _KWD.items() if k != ("pc4", "verbatim")}
    for arm in ("no-kwd", "no-cheap-rungs"):
        _row, rt = _rowkw_run(arm, 0.99, nogold)  # the cut is ignored: unscored
        assert _verbatims(rt) == _HITS, arm
    # no-summary leaves the rows alone: scored
    _row, rt = _rowkw_run("no-summary", 0.99, nogold)
    assert _verbatims(rt) == ["pc3"]


def test_rowkw_and_triage_never_ask_for_a_removed_rung_in_any_arm(stub_gold):
    for arm in ("all-on", *ARMS, *runner.COMBINED_ARMS):
        for caller in ("triage", "rowkw"):
            rt = _StubRuntime(list(_HITS), {}, dict(_ROWS))
            row = runner.run_scripted(
                rt, _StubCatalog(), _stub_task(), arm, caller=caller, threshold=0.0
            )
            assert row["refusals"] == 0 and row["errors"] == 0, (arm, caller)


def test_no_cheap_rungs_switches_both_rungs_off_and_triage_opens_in_rank_order(
    stub_gold,
):
    assert runner.arm_flags("no-cheap-rungs") == {"kwd": False, "summary": False}
    assert "no-cheap-rungs" in runner.ALL_ARMS
    nogold = {k: v for k, v in _KWD.items() if k != ("pc4", "verbatim")}
    rt = _StubRuntime(list(_HITS), nogold)
    row = runner.run_scripted(
        rt,
        _StubCatalog(),
        _stub_task(),
        "no-cheap-rungs",
        caller="triage",
        threshold=0.9,
    )
    assert [e for _h, e in rt.log if e in ("kwd", "summary")] == []
    assert _verbatims(rt) == _HITS and row["refusals"] == 0
    # the ladder asks for both and is refused for both on every hit
    lad = runner.run_scripted(
        _StubRuntime(list(_HITS), nogold),
        _StubCatalog(),
        _stub_task(),
        "no-cheap-rungs",
        caller="ladder",
    )
    assert lad["refusals"] >= 2


def test_a_combined_arm_is_vacuous_only_when_every_layer_it_removes_is_empty():
    rows = [_row("t1", "all-on", "a"), _row("t1", "no-cheap-rungs", "b")]
    block = runner.arm_table(["all-on", "no-cheap-rungs"], ZERO, rows, embedder_id="x")
    assert block["no-cheap-rungs"]["vacuous"] is True  # kwd and summary both empty
    assert block["no-cheap-rungs"]["switches_off"] == ["kwd", "summary"]
    kwd_only = {**ZERO, "chunks_with_keywords_frac": 0.8}
    block2 = runner.arm_table(
        ["all-on", "no-cheap-rungs"], kwd_only, rows, embedder_id="x"
    )
    assert block2["no-cheap-rungs"]["vacuous"] is False
    assert runner.identical_to_all_on(rows) == {"no-cheap-rungs": 0.0}


# ---------------------------------------------------------------------------
# DB: the real runtime under the triage caller, and the lexical-only arm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["triage", "rowkw"])
def test_triage_callers_are_deterministic_and_refusal_free_on_the_real_runtime(
    world, caller
):
    tasks, _ = runner.select_tasks(world["rows"])
    for arm in ("all-on", *ARMS, *runner.COMBINED_ARMS):
        run = [
            runner.run_scripted(
                world["runtime"], world["catalog"], t, arm, caller=caller
            )
            for t in tasks
        ]
        again = [
            runner.run_scripted(
                world["runtime"], world["catalog"], t, arm, caller=caller
            )
            for t in tasks
        ]
        assert [_comparable(r) for r in run] == [_comparable(r) for r in again], arm
        assert all(r["refusals"] == 0 and r["errors"] == 0 for r in run), arm
        assert all(r["caller"] == caller for r in run)
    assert runner.DEFAULT_CALLER == "triage"


def test_lexical_only_is_the_lexical_leg_of_hybrid_and_nothing_else(world):
    """The arm removes the semantic leg. What is left must be the candidates
    and order hybrid's own lexical leg produces: the same websearch_to_tsquery
    AND query, ts_rank_cd order, chunk_id tie-break. Hybrid's leg is read from
    the search trace (each fused row's lexical_rank)."""
    from precis.embedder import MockEmbedder
    from precis.mesh_eval.retrieval import extract_chunk_handles
    from precis.utils import search_trace
    from precis.utils.embed_query import query_vec_for

    store = world["store"]
    with store.pool.connection() as conn:
        default = conn.execute(
            "SELECT name FROM embedders WHERE is_default = TRUE"
        ).fetchone()[0]
    # vectors stored under the DB's default embedder name: a real semantic leg
    embedder = MockEmbedder(dim=store.embedding_dim(), model=default)
    layers.fill(store, embedder, keywords=False)
    q = "study number laboratory"  # AND-matches every body chunk of both papers

    def traced(**call: Any) -> list[dict[str, Any]]:
        trace = search_trace.SearchTrace()
        token = search_trace.CURRENT.set(trace)
        try:
            store.chunks.search_chunks(
                q=q, limit=50, kind="paper", card_kinds=("card_combined",), **call
            )
        finally:
            search_trace.CURRENT.reset(token)
        return list(trace.candidates.values())

    def lex_leg(cands: list[dict[str, Any]]) -> list[str]:
        ranked = [c for c in cands if c["lexical_rank"]]
        return [c["handle"] for c in sorted(ranked, key=lambda c: c["lexical_rank"])]

    vec = query_vec_for(embedder, q, "hybrid")
    hybrid = traced(query_vec=vec, mode="hybrid")
    assert any(c["semantic_distance"] is not None for c in hybrid)  # sem leg ran
    hybrid_leg = lex_leg(hybrid)
    assert len(hybrid_leg) == 24 and hybrid_leg == lex_leg(traced(mode="lexical"))
    # and through the dispatched search the arm runs
    s = runner.Session(world["runtime"], runner.arm_flags("lexical-only"), 10**9)
    body, _ = s.call(
        "search", {"kind": "paper", "q": q, "unique_per": "chunk", "page_size": 10}
    )
    assert extract_chunk_handles(body.split("\nNext:", 1)[0]) == hybrid_leg[:10]
