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
from pathlib import Path
from typing import Any

import pytest

from precis.bench import (
    corpus,
    gold,
    layers,
    planted,
    planted_ingest,
    qasper_tasks,
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
        assert v["vacuous"] is True and "nothing to remove" in v["reason"], arm
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
        "abstention": False,
        "found": True,
        "censored": False,
        "applicable": True,
        "stop": "found",
        "tokens_to_first_gold": 100.0,
        "tokens_total": 150.0,
        "calls": 4,
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
    assert "| 2 | 0.500 | 200 | 0 | 4.0 | 0 | — | — |" in lines["all-on"]
    # no-kwd: n=2, median of (60, 8000) = 4030, 1 censored, refusals 2; delta vs all-on
    assert "| 2 | 0.500 | 4030 | 1 | 4.5 | 2 | +0.000 | +3830 |" in lines["no-kwd"]
    assert "VACUOUS" in lines["no-kwd"]
    assert "| 1 | — | 5000 |" in lines["whole-paper"]
    assert "Unanswerable tasks" in md and "| all-on | 1 | 900 |" in md
    assert "Vacuous arms" in md


def test_results_filename_pins_date_and_short_sha():
    assert (
        runner.results_filename("2026-11-09T21:03:11Z", "0123456789abcdef", "quick")
        == "20261109Z-0123456-quick.json"
    )


def test_load_results_rejects_other_json(tmp_path: Path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"eval": "retrieval"}), encoding="utf-8")
    with pytest.raises(runner.BenchRunError, match="not a benchmark results file"):
        runner.load_results(p)


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


def test_a_switched_off_rung_is_a_counted_refusal_not_a_silent_skip(world):
    task = _by_question(world["rows"], "What did the quasar1x2 instrument")
    base = runner.run_scripted(world["runtime"], world["catalog"], task, "all-on")
    nokwd = runner.run_scripted(world["runtime"], world["catalog"], task, "no-kwd")
    nosum = runner.run_scripted(world["runtime"], world["catalog"], task, "no-summary")
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
