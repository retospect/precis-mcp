"""HNSW recall benchmark (gr690288): recall math, SQL builders, report shape.

Unit tests cover the pure pieces (no database). The DB-backed tests run the
whole benchmark against the shared test DB — a table far too small for the
planner to pick HNSW on its own, so they force the index with an
``ann_set_local`` nudge (the exact leg is never nudged) and assert the
fallback semantics the report documents.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.bench.ann_recall import (
    _PLAN_MARKER,
    _RESULT_MARKER,
    AnnRecallConfig,
    AnnRecallError,
    LegResult,
    PgUrlRunner,
    ProdPsqlRunner,
    ann_set_statements,
    context_sql,
    exact_set_statements,
    leg_transaction_text,
    plan_probe_sql,
    plan_used_hnsw,
    ranking_query_sql,
    recall_at_k,
    run_report,
    statement_timeout_sql,
)
from tests.conftest import _active_dsn

_DIM = 1024
_EMBEDDER = "bge-m3"  # migration-seeded default (embedders.dim=1024)


# ---------------------------------------------------------------- recall math


def test_recall_at_k_counts_set_overlap() -> None:
    exact = [10, 11, 12, 13, 14]
    ann = [10, 11, 12, 13, 99]
    assert recall_at_k(exact, ann, 1) == 1.0
    assert recall_at_k(exact, ann, 5) == 0.8
    assert recall_at_k(exact, ann, 3) == 1.0


def test_recall_at_k_degenerate_and_validation() -> None:
    assert recall_at_k([], [], 5) == 1.0  # fewer rows than k: nothing to miss
    assert recall_at_k([1], [1, 2], 5) == 1.0
    with pytest.raises(ValueError):
        recall_at_k([1], [1], 0)


# ----------------------------------------------------------------- config


def test_config_normalizes_and_validates() -> None:
    cfg = AnnRecallConfig(ks=(10, 1, 5, 5), ef_search=(40, 40, 100))
    assert cfg.ks == (1, 5, 10)
    assert cfg.ef_search == (40, 100)
    with pytest.raises(ValueError):
        AnnRecallConfig(k=5, ks=(1, 10))  # k deeper than the ranking depth
    with pytest.raises(ValueError):
        AnnRecallConfig(ef_search=(1001,))  # pgvector's hard ceiling
    with pytest.raises(ValueError):
        AnnRecallConfig(n_queries=0)


# ----------------------------------------------------------------- SQL shape


def test_exact_and_ann_legs_share_the_ranking_query() -> None:
    """Recall is only meaningful when both legs rank identically."""
    cfg = AnnRecallConfig()
    assert ranking_query_sql(cfg) == ranking_query_sql(cfg)
    assert "<=>" in ranking_query_sql(cfg)
    assert f"LIMIT {cfg.k}" in ranking_query_sql(cfg)


def test_legs_differ_only_in_set_local_statements() -> None:
    cfg = AnnRecallConfig(ef_search=(40, 80))
    exact = exact_set_statements(cfg)
    assert any("enable_indexscan = off" in s for s in exact)
    assert any("enable_bitmapscan = off" in s for s in exact)
    assert not any("hnsw" in s for s in exact)
    assert ann_set_statements(cfg, 40)[1] == "SET LOCAL hnsw.ef_search = 40"
    assert ann_set_statements(cfg, 80)[1] == "SET LOCAL hnsw.ef_search = 80"
    # The filtered-scan regime the product's search sets (no backfill reads
    # as recall loss the product never experiences).
    for s in ann_set_statements(cfg, 40)[1:]:
        assert s.startswith("SET LOCAL hnsw.")
    assert "SET LOCAL hnsw.iterative_scan = 'strict_order'" in ann_set_statements(
        cfg, 40
    )


def test_every_leg_carries_a_bounded_server_budget() -> None:
    """gr690288 review: the prod agent_rw role's 120s statement_timeout is
    shorter than the exact leg at >1M rows; every leg must SET LOCAL a
    bounded override (never 0/unbounded) that rolls back with it."""
    cfg = AnnRecallConfig()
    stmt = statement_timeout_sql(cfg)
    assert stmt == f"SET LOCAL statement_timeout = {cfg.statement_timeout_ms}"
    assert exact_set_statements(cfg)[0] == stmt
    assert ann_set_statements(cfg, 40)[0] == stmt
    with pytest.raises(ValueError):
        AnnRecallConfig(statement_timeout_ms=0)  # 0 = unbounded: refused
    # A host-set override lands in the leg text verbatim.
    cfg2 = AnnRecallConfig(statement_timeout_ms=300_000)
    assert "SET LOCAL statement_timeout = 300000" in exact_set_statements(cfg2)


def test_ranking_excludes_the_query_row_itself() -> None:
    """The sample ranks stored vectors as queries; the distance-0 self-hit
    must not dominate the exact top-k (see the module docstring)."""
    sql_text = ranking_query_sql(AnnRecallConfig())
    assert "ce.chunk_id <> q.qid" in sql_text


def test_ann_nudges_apply_to_ann_leg_only() -> None:
    cfg = AnnRecallConfig(ann_set_local=("enable_seqscan = off",))
    sets = ann_set_statements(cfg, 40)
    assert sets == (
        statement_timeout_sql(cfg),
        "SET LOCAL hnsw.ef_search = 40",
        "SET LOCAL hnsw.iterative_scan = 'strict_order'",
        "SET LOCAL hnsw.max_scan_tuples = 200000",
        "SET LOCAL enable_seqscan = off",
    )
    assert not any("enable_seqscan" in s for s in exact_set_statements(cfg))


def test_sample_is_deterministic_per_seed() -> None:
    a = ranking_query_sql(AnnRecallConfig(seed=1))
    b = ranking_query_sql(AnnRecallConfig(seed=1))
    c = ranking_query_sql(AnnRecallConfig(seed=2))
    assert a == b
    assert a != c
    assert "hashtextextended(chunk_id::text, 1)" in a


def test_embedder_filter_is_quoted() -> None:
    cfg = AnnRecallConfig(embedder="O'Neil")
    sql_text = ranking_query_sql(cfg)
    assert "embedder = 'O''Neil'" in sql_text


def test_prod_transaction_text_never_commits() -> None:
    cfg = AnnRecallConfig()
    text = leg_transaction_text(ann_set_statements(cfg, 40), ranking_query_sql(cfg))
    assert text.startswith("BEGIN;\n")
    assert "SET LOCAL hnsw.ef_search = 40;" in text
    assert "COMMIT" not in text
    # The SELECT is the last statement so psql prints it before the exit
    # rolls the transaction back.
    assert text.rstrip().endswith(";")


def test_prod_transaction_text_marks_the_payload() -> None:
    """psql -c echoes a command tag (BEGIN, SET) for every non-row
    statement, so marker rows must delimit the plan and payload sections."""
    text = leg_transaction_text(
        ann_set_statements(AnnRecallConfig(), 40), "SELECT 1", analyze=True
    )
    lines = text.rstrip().splitlines()
    assert lines[-2] == f"SELECT '{_RESULT_MARKER}';"
    assert lines[-1] == "SELECT 1;"
    # The probe runs BEFORE the payload in the same transaction, behind its
    # own marker, as EXPLAIN ANALYZE of the verbatim payload statement.
    probe_i = lines.index(f"SELECT '{_PLAN_MARKER}';")
    result_i = lines.index(f"SELECT '{_RESULT_MARKER}';")
    assert probe_i < result_i
    assert "EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF)" in lines[probe_i + 1]


def test_plan_probe_explains_the_ranking_statement_it_labels() -> None:
    """The pre-land review (td761407) caught the check probing a different
    statement (one query under LIMIT 1) than the one the recall came from.
    The probe must EXPLAIN the exact ranking text — with ANALYZE for the
    ANN legs, so the plan is the plan of a real execution of that
    statement, and planner-only where a second execution would be waste."""
    cfg = AnnRecallConfig()
    ranking = ranking_query_sql(cfg)
    analyzed = plan_probe_sql(ranking, analyze=True)
    assert analyzed.startswith("EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF)\n")
    assert analyzed.endswith(ranking)
    plain = plan_probe_sql(ranking)
    assert plain.startswith("EXPLAIN (COSTS OFF)\n")
    assert "ANALYZE" not in plain
    assert plain.removeprefix("EXPLAIN (COSTS OFF)\n") == ranking


def test_prod_runner_bounds_the_leg_with_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outbound wait-bound ratchet: one leg call must not block forever."""
    seen: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=f"{_PLAN_MARKER}\nQUERY PLAN\n{_RESULT_MARKER}\n{{}}\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = ProdPsqlRunner(script=tmp_path / "x", timeout_s=123).run((), "SELECT 1")
    assert res.payload == "{}"
    assert res.plan == "QUERY PLAN"
    assert seen["timeout"] == 123


def test_prod_runner_timeout_raises_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="prod-psql", timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AnnRecallError, match="timeout_s"):
        ProdPsqlRunner(script=tmp_path / "x", timeout_s=5).run((), "SELECT 1")


def test_url_runner_bounds_the_connect_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute(self, query: str, params: Any = None) -> _FakeConn:
            return self

        def fetchall(self) -> list[list[Any]]:
            return [[{"a": 1}]]

    fake_psycopg = types.SimpleNamespace(
        connect=lambda *a, **k: (seen.update(k), _FakeConn())[1]
    )
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    res = PgUrlRunner(url="postgresql://x", connect_timeout=9).run(
        (), "SELECT NULL::json"
    )
    assert res.payload == {"a": 1}
    assert seen["connect_timeout"] == 9


def test_plan_used_hnsw_parses_explain() -> None:
    on = " Index Scan using chunk_embeddings_vec_hnsw_idx on chunk_embeddings ce"
    off = " Seq Scan on chunk_embeddings ce"
    assert plan_used_hnsw(f"QUERY PLAN\n{on}\n")
    assert not plan_used_hnsw(f"QUERY PLAN\n{off}\n")


def test_prod_runner_defaults_to_the_sanctioned_hop() -> None:
    assert ProdPsqlRunner().script.name == "prod-psql"


def _fake_prod_psql(tmp_path: Path, emit: str) -> Path:
    """A stand-in for scripts/prod-psql that swallows stdin and prints
    ``emit`` — simulating the remote psql's stdout, command tags included."""
    script = tmp_path / "prod-psql"
    script.write_text(
        "#!/usr/bin/env bash\ncat >/dev/null\n" + emit + "\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_prod_runner_splits_off_psql_command_tags(tmp_path: Path) -> None:
    """psql -At -c prints a BEGIN/SET command tag per non-row statement
    before each section; the runner must return only plan and payload."""
    script = _fake_prod_psql(
        tmp_path,
        f"printf '%s\\n' BEGIN SET SET SET '{_PLAN_MARKER}' "
        f"' Seq Scan on chunk_embeddings ce' '{_RESULT_MARKER}' '{{\"a\":1}}'",
    )
    res = ProdPsqlRunner(script=script).run(
        exact_set_statements(AnnRecallConfig()), "SELECT 1"
    )
    assert res.payload == '{"a":1}'
    assert res.plan == "Seq Scan on chunk_embeddings ce"


def test_prod_runner_null_payload_raises_not_tag(tmp_path: Path) -> None:
    """A NULL json_agg prints an empty line under -At: without the marker
    split the tags read as the result ('BEGIN') and crash json.loads."""
    script = _fake_prod_psql(
        tmp_path,
        f"printf '%s\\n' BEGIN SET '{_PLAN_MARKER}' 'plan' '{_RESULT_MARKER}' ''",
    )
    with pytest.raises(AnnRecallError, match="NULL|no result"):
        ProdPsqlRunner(script=script).run((), "SELECT NULL::json")


def test_prod_runner_output_without_marker_raises(tmp_path: Path) -> None:
    script = _fake_prod_psql(tmp_path, "printf '%s\\n' BEGIN SET '{\"a\":1}'")
    with pytest.raises(AnnRecallError, match="marker"):
        ProdPsqlRunner(script=script).run((), "SELECT 1")


def test_leg_sql_is_parameter_free() -> None:
    """Guards the trap in the module docstring: ``%s`` placeholders let
    psycopg cache a generic prepared plan, which ignores the per-leg
    ``SET LOCAL`` planner GUCs and silently corrupts the exact leg."""
    cfg = AnnRecallConfig(embedder="x")
    texts = (
        context_sql(cfg),
        ranking_query_sql(cfg),
        plan_probe_sql(ranking_query_sql(cfg), analyze=True),
    )
    for sql_text in texts:
        assert "%s" not in sql_text
        assert "?" not in sql_text


# ------------------------------------------------------------- report (fake)


class _FakeRunner:
    """Serves canned context / exact / ann responses by leg shape."""

    def __init__(
        self,
        exact: list[dict[str, Any]],
        ann: dict[int, list[dict[str, Any]]],
        plans: dict[int, str],
        exact_plan: str = "",
    ) -> None:
        self.exact = exact
        self.ann = ann
        self.plans = plans
        self.exact_plan = exact_plan or _OFF_PLAN

    @staticmethod
    def _ef(statements: Any, default: int = 40) -> int:
        for s in statements:
            if s.startswith("SET LOCAL hnsw.ef_search"):
                return int(s.rsplit(" ", 1)[1])
        return default

    def run(self, statements: Any, query: str, *, analyze: bool = False) -> LegResult:
        if "ranked AS" not in query:
            # The context snapshot.
            return LegResult(
                plan="",
                payload=json.dumps(
                    {
                        "rows_total": 2_000_000,
                        "rows_ok": 1_900_000,
                        "embedder": _EMBEDDER,
                        "embedder_counts": {_EMBEDDER: 1_900_000},
                        "hnsw_indexes": [
                            {
                                "name": "chunk_embeddings_vec_hnsw_idx",
                                "reloptions": ["m=16", "ef_construction=64"],
                            }
                        ],
                        "pgvector_version": "0.8.1",
                        "hnsw_defaults": {
                            "ef_search": "40",
                            "m": "16",
                            "ef_construction": "64",
                        },
                    }
                ),
            )
        if any("enable_indexscan" in s for s in statements):
            return LegResult(plan=self.exact_plan, payload=json.dumps(self.exact))
        ef = self._ef(statements)
        return LegResult(plan=self.plans[ef], payload=json.dumps(self.ann[ef]))


_ON_PLAN = (
    "QUERY PLAN\n"
    " Index Scan using chunk_embeddings_vec_hnsw_idx on chunk_embeddings ce\n"
    " Execution Time: 12.3 ms\n"
)
_OFF_PLAN = "QUERY PLAN\n Seq Scan on chunk_embeddings ce\n"


def test_run_report_computes_recall_and_flags_fallback() -> None:
    exact = [
        {"qid": 1, "ids": [10, 11, 12, 13, 14]},
        {"qid": 2, "ids": [7, 8, 9, 20, 21]},
    ]
    ann = {
        40: [
            {"qid": 1, "ids": [10, 11, 12, 13, 99]},  # recall@5 0.8, @1 1.0
            {"qid": 2, "ids": [7, 8, 9, 20, 21]},  # 1.0
        ],
        100: [
            {"qid": 1, "ids": [10, 11, 12, 13, 14]},
            {"qid": 2, "ids": [7, 8, 9, 20, 21]},
        ],
    }
    runner = _FakeRunner(exact, ann, plans={40: _ON_PLAN, 100: _OFF_PLAN})
    cfg = AnnRecallConfig(n_queries=2, k=5, ks=(1, 5), ef_search=(40, 100))
    report = run_report(cfg, runner)

    assert report["schema"] == 1
    assert report["gripe"] == "gr690288"
    assert report["context"]["embedder"] == _EMBEDDER
    assert report["context"]["rows_ok"] == 1_900_000

    by_ef = {leg["ef_search"]: leg for leg in report["ann_legs"]}
    assert by_ef[40]["plan_used_index"] is True
    assert by_ef[40]["plan"] is None
    assert by_ef[40]["exec_ms"] == 12.3  # parsed from the ANALYZE probe
    assert by_ef[40]["recall"]["1"] == {"mean": 1.0, "min": 1.0}
    assert by_ef[40]["recall"]["5"] == {"mean": 0.9, "min": 0.8}
    # The 100 leg fell back to a seqscan: recall is trivially 1.0 and the
    # report must say so, keeping the full plan for the judgment read.
    assert by_ef[100]["plan_used_index"] is False
    assert by_ef[100]["plan"] == _OFF_PLAN
    assert by_ef[100]["recall"]["5"] == {"mean": 1.0, "min": 1.0}
    assert any("SEQSCAN" not in n and "seqscan" in n for n in report["notes"])


def test_run_report_rejects_a_moving_database() -> None:
    exact = [{"qid": 1, "ids": [1, 2]}]
    ann = {40: [{"qid": 2, "ids": [1, 2]}]}  # different sample mid-run
    runner = _FakeRunner(exact, ann, plans={40: _ON_PLAN})
    cfg = AnnRecallConfig(n_queries=1, k=2, ks=(1,), ef_search=(40,))
    with pytest.raises(AnnRecallError, match="different query set"):
        run_report(cfg, runner)


def test_plan_probe_runs_inside_the_leg_call() -> None:
    """The probe and the ranking travel in ONE runner call (one transaction,
    the leg's full GUC set) with analyze=True — the plan is the plan of an
    execution of the very statement the recall is computed from."""
    seen: list[tuple[tuple[str, ...], str, bool]] = []

    class Recorder(_FakeRunner):
        def run(
            self, statements: Any, query: str, *, analyze: bool = False
        ) -> LegResult:
            seen.append((tuple(statements), query, analyze))
            return super().run(statements, query, analyze=analyze)

    cfg = AnnRecallConfig(
        n_queries=1, k=2, ks=(1,), ef_search=(40,), ann_set_local=("x = 1",)
    )
    exact = [{"qid": 1, "ids": [1, 2]}]
    runner = Recorder(exact, {40: exact}, plans={40: _ON_PLAN})
    run_report(cfg, runner)
    ann_calls = [c for c in seen if any(s.startswith("SET LOCAL hnsw.") for s in c[0])]
    assert len(ann_calls) == 1  # one transaction per leg: probe + payload
    statements, _q, analyze = ann_calls[0]
    assert analyze is True
    assert "SET LOCAL x = 1" in statements
    assert any(s.startswith("SET LOCAL hnsw.ef_search") for s in statements)
    assert any("statement_timeout" in s for s in statements)


def test_exact_leg_using_the_index_is_an_error() -> None:
    """If the forced-off GUCs did not take, the 'exact' leg is not exact —
    refuse rather than report a vacuous comparison."""
    exact = [{"qid": 1, "ids": [1, 2]}]
    runner = _FakeRunner(exact, {}, plans={}, exact_plan=_ON_PLAN)
    cfg = AnnRecallConfig(n_queries=1, k=2, ks=(1,), ef_search=(40,))
    with pytest.raises(AnnRecallError, match="not exact"):
        run_report(cfg, runner)


# ------------------------------------------------------- report (real pgvector)


# At a few hundred rows the planner prefers a pkey bitmap scan + sort over the
# HNSW index once stats exist; with all three off only HNSW avoids a sort.
_FORCE_HNSW = ("enable_seqscan = off", "enable_bitmapscan = off", "enable_sort = off")


def _seed(store: Any, n: int) -> None:
    rng = np.random.default_rng(690288)
    for i in range(n):
        ref = store.insert_ref(kind="paper", slug=f"annr-{i}", title=f"P {i}", meta={})
        vec = rng.standard_normal(_DIM)
        vec /= np.linalg.norm(vec)
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
                "VALUES (%s, 0, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
                (ref.id, f"body {i}"),
            ).fetchone()
            conn.execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
                "VALUES (%s, %s, %s, 'ok', 1)",
                (int(row[0]), _EMBEDDER, vec.tolist()),
            )
    # Fresh stats: a just-seeded table has none until autovacuum wakes, and
    # the planner's choice under enable_seqscan=off then varies by timing.
    with store.pool.connection() as conn:
        conn.execute("ANALYZE chunk_embeddings")


def test_report_on_the_test_db(store: Any) -> None:
    """The full benchmark against real pgvector.

    240 rows is far below HNSW's usefulness, so the forced-index leg proves
    the index path end to end while recall stays 1.0 — at this size the
    index is exact. That is the expected shape of a small-replica run; the
    number the gripe wants comes from the same script on prod.
    """
    n = 240
    _seed(store, n)
    runner = PgUrlRunner(_active_dsn())

    cfg = AnnRecallConfig(
        n_queries=8,
        k=20,
        ks=(1, 5, 10, 20),
        ef_search=(40,),
        ann_set_local=_FORCE_HNSW,
    )
    report = run_report(cfg, runner)

    assert report["context"]["rows_ok"] == n
    assert report["context"]["embedder"] == _EMBEDDER
    assert report["context"]["hnsw_indexes"], "the test DB must carry the HNSW index"
    # The exact leg's plan is captured too — and must show the forced scan.
    assert "Seq Scan" in report["exact_leg"]["plan"]
    leg = report["ann_legs"][0]
    assert leg["plan_used_index"] is True, leg["plan"]
    # ANALYZE ran the statement for real: a server-side exec time is reported.
    assert leg["exec_ms"] is not None
    for k in ("1", "5", "10", "20"):
        assert leg["recall"][k]["mean"] == 1.0, (
            f"recall@{k} below 1.0 at {n} rows — the index is not exact here?"
        )

    # Without the nudge the planner may seqscan; recall must still be 1.0
    # (exact fallback) and the leg must be labelled, not misread as ANN.
    plain = run_report(AnnRecallConfig(n_queries=4, k=10, ks=(5, 10)), runner)
    plain_leg = plain["ann_legs"][0]
    assert plain_leg["recall"]["10"]["mean"] == 1.0
    if plain_leg["plan_used_index"] is False:
        assert plain_leg["plan"] is not None


def test_empty_table_raises_a_clear_error(store: Any) -> None:
    with pytest.raises(AnnRecallError, match="no embeddings match|NULL"):
        run_report(AnnRecallConfig(), PgUrlRunner(_active_dsn()))


@pytest.mark.skipif(shutil.which("psql") is None, reason="needs a psql binary")
def test_report_through_a_tagged_psql(store: Any, tmp_path: Path) -> None:
    """End-to-end through a real ``psql -At -c``, which echoes BEGIN/SET
    command tags exactly like the remote hop does — the failure the
    pre-land review caught on gr690288."""
    n = 120
    _seed(store, n)
    fake = tmp_path / "prod-psql"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "exec psql -X -P pager=off -v ON_ERROR_STOP=1 "
        "${PRECIS_PROD_PSQL_OPTS} " + shlex.quote(_active_dsn()) + ' -c "$(cat)"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    cfg = AnnRecallConfig(
        n_queries=6,
        k=10,
        ks=(1, 10),
        ef_search=(40,),
        ann_set_local=_FORCE_HNSW,
    )
    report = run_report(cfg, ProdPsqlRunner(script=fake))

    assert report["context"]["rows_ok"] == n
    leg = report["ann_legs"][0]
    assert leg["plan_used_index"] is True, leg["plan"]
    assert leg["exec_ms"] is not None  # the ANALYZE probe ran for real
    assert leg["recall"]["1"]["mean"] == 1.0
    assert leg["recall"]["10"]["mean"] == 1.0
