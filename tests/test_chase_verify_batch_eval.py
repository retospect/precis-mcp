"""Unit tests for ``scripts/llm_eval/chase_verify_batch_eval.py``.

Both prod seams are faked: ``connect_ro`` (the ``_prod_ro`` read-only
connection) returns a canned cursor over ``llm_call_log``/``llm_blob``
rows, and ``route`` is replaced by a deterministic dispatcher — no DB, no
LLM calls. The logged prompts are built through the *real*
``_chase_llm`` templates so the parse/regroup/rebuild round-trip is
exercised byte-for-byte.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "llm_eval"
sys.path.insert(0, str(_SCRIPTS))


def _load() -> Any:
    path = _SCRIPTS / "chase_verify_batch_eval.py"
    spec = importlib.util.spec_from_file_location("chase_verify_batch_eval", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["chase_verify_batch_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load()


@pytest.fixture()
def cl() -> Any:
    return m._chase_llm()


def _claim_block(cl: Any, claim: str) -> str:
    return cl._verify_claim_block(
        claim=claim, scope={"k": "v"}, claim_source_text="the claim passage"
    )


def _item_block(cl: Any, cite: str, chunk: str, kind: str = "paper") -> str:
    return cl._verify_item_block(
        source_kind=kind,
        cite_key=cite,
        chunk_ord=3,
        chunk_text=chunk,
        section_path="2.1 Results",
        neighbours=["prev chunk", "next chunk"],
        source_identity=None,
    )


def _single_prompt(cl: Any, claim: str, cite: str, chunk: str) -> str:
    return cl._PROMPT_VERIFY.format(
        source_kind="paper",
        claim_block=_claim_block(cl, claim),
        item_block=_item_block(cl, cite, chunk),
    )


def _batch_prompt(cl: Any, claim: str, chunks: list[str]) -> str:
    return cl._verify_batch_prompt(
        _claim_block(cl, claim),
        [_item_block(cl, f"pa{i}", c) for i, c in enumerate(chunks)],
    )


def _verdict(supports: str, **extra: Any) -> dict[str, Any]:
    return {
        "supports": supports,
        "support_reason": "r",
        "caveats": [],
        "claim_setup": "c",
        "passage_setup": "p",
        "same_setup": True,
        "contradicts": False,
        "cited_others": [],
        "terminal": True,
        **extra,
    }


def _request_blob(prompt: str) -> str:
    return json.dumps({"prompt": prompt, "source": "chase:verify", "max_tokens": None})


class _Cur:
    """A psycopg-cursor stand-in: ``execute`` records the SQL and feeds
    ``fetchall`` from the row plan the test installed."""

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any]) -> _Cur:
        self._rows = self._conn.plan(sql, params)
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Res:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _Conn:
    """Fake read-only conn: ``cursor()`` for the sampling passes,
    ``execute`` for the op-chain / card reads."""

    def __init__(
        self, labels: list[dict[str, Any]], blobs: dict[str, str], extra: Any = None
    ) -> None:
        self._labels = labels
        self._blobs = blobs
        self._extra = extra or {}
        self.queries: list[str] = []

    def plan(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.queries.append(sql)
        if "llm_call_log" in sql:
            return self._labels
        if "llm_blob" in sql:
            return [
                {"hash": h, "text": self._blobs[h]}
                for h in params["hashes"]
                if h in self._blobs
            ]
        raise AssertionError(f"unexpected SQL: {sql[:60]}")

    def cursor(self, row_factory: Any = None) -> _Cur:
        return _Cur(self)

    def execute(self, sql: str, params: dict[str, Any]) -> _Res:
        self.queries.append(sql)
        if "app_settings" in sql:
            return _Res((self._extra.get("op"),))
        if "FROM refs" in sql:
            return _Res((self._extra.get("card"),))
        raise AssertionError(f"unexpected SQL: {sql[:60]}")

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _labels(rows: list[dict[str, Any]]) -> _Conn:
    return _Conn(
        labels=[
            {"id": i, "ts": "2026-10-20", "model": "claude-haiku", **r}
            for i, r in enumerate(rows)
        ],
        blobs={r["request_hash"]: r["blob"] for r in rows},
    )


def _res(text: str, model: str) -> SimpleNamespace:
    return SimpleNamespace(
        error=None, text=text, data=None, cost_usd=0.01, model=model, placement=None
    )


def test_parse_logged_prompt_single(cl: Any) -> None:
    claim = _claim_block(cl, "catalyst X gives 90% yield")
    block = _item_block(cl, "pa111", "chunk text")
    prompt = cl._PROMPT_VERIFY.format(
        source_kind="paper", claim_block=claim, item_block=block
    )
    assert m.parse_logged_prompt(prompt) == (claim, [block])


def test_parse_logged_prompt_batch(cl: Any) -> None:
    claim = _claim_block(cl, "claim")
    blocks = [_item_block(cl, f"pa{i}", f"chunk {i}") for i in range(3)]
    prompt = cl._verify_batch_prompt(claim, blocks)
    assert m.parse_logged_prompt(prompt) == (claim, blocks)


def test_parse_logged_prompt_rejects_drift() -> None:
    assert m.parse_logged_prompt("some other prompt entirely") is None


def test_rebuild_round_trips_single(cl: Any) -> None:
    claim = _claim_block(cl, "claim")
    block = _item_block(cl, "pa1", "text")
    prompt, n = m.rebuild_prompt(claim, [block], cl)
    assert n == 1
    assert prompt == cl._PROMPT_VERIFY.format(
        source_kind="paper", claim_block=claim, item_block=block
    )


def test_end_to_end_fake_arms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cl: Any
) -> None:
    """8 items over 2 claims: candidate agrees with frontier on 7/8."""
    rows = []
    for i in range(6):  # claim A: 6 singles → one batch of 6
        rows.append(
            {
                "request_hash": f"ha{i}",
                "response": json.dumps(_verdict("yes")),
                "blob": _request_blob(
                    _single_prompt(cl, "claim A", f"paA{i}", f"A chunk {i}")
                ),
            }
        )
    for i in range(2):  # claim B: 2 singles → one batch of 2
        rows.append(
            {
                "request_hash": f"hb{i}",
                "response": json.dumps(_verdict("no")),
                "blob": _request_blob(
                    _single_prompt(cl, "claim B", f"paB{i}", f"B chunk {i}")
                ),
            }
        )
    conn = _labels(rows)
    monkeypatch.setattr(m, "connect_ro", lambda args: conn)
    monkeypatch.delenv(m.CANDIDATE_CHAIN_ENV, raising=False)

    calls: list[Any] = []

    def fake_route(req: Any) -> SimpleNamespace:
        calls.append(req)
        n_items = 6 if "ITEM 6" in req.prompt else 2
        # claim B incumbent says "no" but both arms say "yes" → cand/incumbent
        # diverges, cand/frontier agrees — both stats are exercised.
        verdicts = [{"item": i + 1, **_verdict("yes")} for i in range(n_items)]
        if req.tier == m.Tier.FRONTIER and n_items == 6:
            verdicts[5]["supports"] = "partial"  # the one disagreement
        model = "claude-opus" if req.tier == m.Tier.FRONTIER else "gpt-oss"
        return _res(json.dumps({"verdicts": verdicts}), model)

    monkeypatch.setattr(m, "route", fake_route)
    out = tmp_path / "report.json"
    rc = m.main(
        [
            "--database-url",
            "postgresql://x",
            "--model",
            "openai/gpt-oss-120b",
            "--endpoint-url",
            "http://example.test/v1",
            "--n",
            "8",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))

    s = report["summary"]
    assert s["n_items"] == 8
    assert s["n_groups"] == 2
    # candidate said yes everywhere; frontier agreed on 7 (one partial).
    assert s["candidate_vs_frontier"]["k"] == 7
    assert s["candidate_vs_frontier"]["n"] == 8
    lo, hi = s["candidate_vs_frontier"]["wilson95"]
    assert 0.0 < lo < 7 / 8 < hi <= 1.0
    # incumbent: claim A all "yes" (agree 6/6), claim B all "no" (agree 0/2).
    assert s["candidate_vs_incumbent"]["k"] == 6
    assert s["candidate_vs_incumbent"]["n"] == 8
    assert s["unparseable"] == {"candidate": 0, "frontier": 0}
    # the candidate arm pinned the env chain + local_url; the reference arm
    # saw neither (env popped before it ran) and ran FRONTIER.
    cand = [c for c in calls if c.tier == m.Tier.MEDIUM]
    ref = [c for c in calls if c.tier == m.Tier.FRONTIER]
    assert len(cand) == len(ref) == 2
    assert all(c.local_url == "http://example.test/v1" for c in cand)
    assert all(c.source == m.SOURCE for c in cand)
    # the reference must NOT use the registered source — resolve_op would
    # force-remap it back to MEDIUM and the arm would stop being frontier.
    assert all(c.source != m.SOURCE for c in ref)
    assert all(c.log_call is False for c in calls)
    assert m.CANDIDATE_CHAIN_ENV not in __import__("os").environ
    # report carries verdicts, never the endpoint URL.
    assert "example.test" not in out.read_text(encoding="utf-8")
    partial = [
        i for i in report["items"] if (i["frontier"] or {})["supports"] == "partial"
    ]
    assert len(partial) == 1
    assert partial[0]["candidate"]["supports"] == "yes"


def test_op_chain_reads_settings_and_card(
    monkeypatch: pytest.MonkeyPatch, cl: Any
) -> None:
    """``--op-chain`` takes rung 0 verbatim + resolves the card endpoint."""
    conn = _Conn(
        labels=[],
        blobs={},
        extra={
            "op": json.dumps(
                {
                    "chain": [
                        {
                            "transport": "local",
                            "model": "openai/gpt-oss-120b",
                            "placement": "local",
                            "max_tokens": 1600,
                        },
                        {"placement": "cloud", "model": "x", "transport": "claude_p"},
                    ]
                }
            ),
            "card": {
                "served_by": [
                    {
                        "endpoint": "http://10.0.0.1:8000/v1",
                        "model": "openai/gpt-oss-120b",
                    },
                    {"endpoint": "http://localhost:9/v1"},
                ]
            },
        },
    )
    args = m.argparse.Namespace(op_chain=True)
    rung, endpoint = m.candidate_rung(args, conn)
    assert rung == {
        "transport": "local",
        "model": "openai/gpt-oss-120b",
        "placement": "local",
        "max_tokens": 1600,
    }
    # the localhost entry is skipped (host-private), the routable one wins.
    assert endpoint == "http://10.0.0.1:8000/v1"


def test_sample_groups_logged_batch_rows(cl: Any) -> None:
    """A logged BATCH row yields one item per verdict, sharing the claim key."""
    prompt = _batch_prompt(cl, "claim Z", ["zc 0", "zc 1", "zc 2"])
    response = json.dumps(
        {
            "verdicts": [
                {"item": 1, **_verdict("yes")},
                {"item": 2, **_verdict("partial")},
                {"item": 3, **_verdict("no")},
            ]
        }
    )
    conn = _labels(
        [
            {
                "request_hash": "hbatch",
                "response": response,
                "blob": _request_blob(prompt),
            }
        ]
    )
    items = m.sample_items(conn, cl, n=10, seed="1", days=30, cap=100, model_like="%")
    assert len(items) == 3
    assert {i["item_idx"] for i in items} == {0, 1, 2}
    assert len({i["claim_key"] for i in items}) == 1
    groups = m.regroup(items, per_call=6, seed="1")
    assert len(groups) == 1
    rebuilt, n = m.rebuild_prompt(
        groups[0]["claim_block"], [i["item_block"] for i in groups[0]["items"]], cl
    )
    assert n == 3 and "3 candidate sources follow" in rebuilt
