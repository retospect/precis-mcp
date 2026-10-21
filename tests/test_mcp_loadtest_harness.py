"""Pin the pure parts of scripts/mcp_loadtest/harness.py.

The harness sits outside mypy's ``src tests`` scope and no gate runs it, so
these tests are what stops the verdict thresholds drifting into being wrong
(docs/backlog/mcp-concurrency-load-test.md, "Still owed").
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_NEEDS_SYSCONF = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: os.sysconf is absent on Windows"
)

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mcp_loadtest" / "harness.py"


def _load():
    spec = importlib.util.spec_from_file_location("mcp_loadtest_harness", _PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


h = _load()


# --- pct -------------------------------------------------------------------


def test_pct_empty_is_nan():
    assert math.isnan(h.pct([], 0.5))


def test_pct_single_value():
    assert h.pct([7.0], 0.99) == 7.0


def test_pct_interpolates_unsorted_input():
    xs = [40.0, 10.0, 30.0, 20.0]
    assert h.pct(xs, 0.0) == 10.0
    assert h.pct(xs, 1.0) == 40.0
    assert h.pct(xs, 0.5) == pytest.approx(25.0)


# --- tool_error ------------------------------------------------------------


def test_tool_error_success_is_none():
    assert h.tool_error(SimpleNamespace(isError=False, content=[])) is None


def test_tool_error_reads_runtime_envelope():
    res = SimpleNamespace(
        isError=True, content=[SimpleNamespace(text="[error:BadInput] nope")]
    )
    assert h.tool_error(res) == "BadInput"


def test_tool_error_without_envelope():
    res = SimpleNamespace(isError=True, content=[SimpleNamespace(text="boom")])
    assert h.tool_error(res) == "isError"


# --- CPU meters ------------------------------------------------------------


@_NEEDS_SYSCONF
def test_cpu_meter_first_read_and_missing_pid_are_unobservable():
    m = h.CpuMeter(2**22 + 12345)  # above pid_max on Linux; no /proc on macOS
    assert m.read() == -1.0
    assert m.read() == -1.0


@_NEEDS_SYSCONF
def test_multi_cpu_meter_sums_and_any_unreadable_is_unobservable():
    m = h.MultiCpuMeter([1, 2])
    vals = iter([0.5, 0.7])
    for sub in m.meters:
        sub.read = lambda: next(vals)
    assert m.read() == pytest.approx(1.2)

    bad = h.MultiCpuMeter([1, 2])
    reads = iter([0.9, -1.0])
    for sub in bad.meters:
        sub.read = lambda: next(reads)
    assert bad.read() == -1.0


def test_assign_url_round_robin():
    urls = ["a", "b", "c"]
    assert [h.assign_url(urls, i) for i in range(5)] == ["a", "b", "c", "a", "b"]


def test_per_process_cpu():
    assert h.per_process_cpu(2.4, 2) == pytest.approx(1.2)
    assert h.per_process_cpu(-1.0, 3) == -1.0


# --- verdict ---------------------------------------------------------------


def _samples(step, canary_ms, srv=-1.0, own=0.05, conns=4, n=10):
    return [
        h.Sample(step, float(i), conns, 1, canary_ms, None, srv, own) for i in range(n)
    ]


def _calls(step, n=20, error=None):
    return [h.Call(step, "canary:skill", 10.0, error) for _ in range(n)]


def _run(ramp, per_step, ncpu=12, n_servers=1, pg_max=100, call_err=None):
    samples, calls = [], []
    for step, (canary, srv, own, conns) in zip(ramp, per_step, strict=True):
        samples += _samples(step, canary, srv, own, conns)
        calls += _calls(step, error=call_err)
    return h.verdict(ramp, calls, samples, pg_max, n_servers, ncpu)


def _walls(lines):
    return [v for v in lines if v.startswith("WALL")]


def test_verdict_gil_single_process():
    out = _run([1, 32], [(16, 0.6, 0.04, 2), (1289, 1.17, 0.05, 4)])
    (wall,) = _walls(out)
    assert "GIL" in wall


def test_verdict_gil_judged_per_process():
    # 2 processes at 1.1 cores each: still the GIL, not whole-box saturation.
    out = _run([1, 32], [(16, 1.0, 0.04, 2), (900, 2.2, 0.05, 4)], n_servers=2)
    (wall,) = _walls(out)
    assert "GIL" in wall


def test_verdict_pool_when_server_idle():
    out = _run([1, 32], [(16, 0.1, 0.04, 2), (2000, 0.05, 0.05, 4)])
    (wall,) = _walls(out)
    assert "anyio worker pool" in wall


def test_verdict_box_saturated():
    out = _run([1, 32], [(16, 1.0, 0.04, 2), (2000, 11.0, 0.05, 4)])
    (wall,) = _walls(out)
    assert "server CPU" in wall


def test_verdict_harness_bottleneck_is_inconclusive():
    out = _run([1, 32], [(16, 0.3, 0.5, 2), (2000, 0.5, 0.95, 4)])
    assert not _walls(out)
    assert any(v.startswith("INCONCLUSIVE") for v in out)


def test_verdict_external_target_does_not_name_pool():
    # srv CPU unobservable (-1): must not fall through to the pool verdict.
    out = _run([1, 32], [(16, -1.0, 0.04, -1), (2000, -1.0, 0.05, -1)], pg_max=None)
    assert any(v.startswith("WALL NOT NAMED") for v in out)
    assert not any("anyio worker pool" in v for v in out)


def test_verdict_flat_canary_says_no_wall():
    out = _run([1, 32], [(16, 0.5, 0.04, 2), (20, 0.6, 0.05, 4)])
    assert any("NOT the binding constraint" in v for v in out)
    assert any(v.startswith("NO WALL REACHED") for v in out)


def test_verdict_postgres_connections():
    out = _run([1, 32], [(16, 0.5, 0.04, 2), (20, 0.6, 0.05, 95)])
    assert any(v.startswith("WALL = Postgres") for v in out)


# --- parse_args ------------------------------------------------------------


def test_url_requires_live_target(monkeypatch):
    monkeypatch.setenv("PRECIS_LOADTEST_TOKEN", "t")
    with pytest.raises(SystemExit):
        h.parse_args(["--url", "http://x/mcp"])


def test_url_requires_token(monkeypatch):
    monkeypatch.delenv("PRECIS_LOADTEST_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        h.parse_args(["--url", "http://x/mcp", "--live-target"])


def test_url_rejects_servers(monkeypatch):
    monkeypatch.setenv("PRECIS_LOADTEST_TOKEN", "t")
    with pytest.raises(SystemExit):
        h.parse_args(["--url", "http://x/mcp", "--live-target", "--servers", "2"])


def test_url_accepted_with_confirmation(monkeypatch):
    monkeypatch.setenv("PRECIS_LOADTEST_TOKEN", "t")
    args = h.parse_args(["--url", "http://x/mcp", "--live-target"])
    assert args.url == "http://x/mcp"


def test_servers_must_be_positive():
    with pytest.raises(SystemExit):
        h.parse_args(["--servers", "0"])


def test_verdict_flags_verb_failing_alone():
    calls = [h.Call(1, "search:paper", 5.0, "BadInput") for _ in range(3)]
    calls += [h.Call(1, "canary:skill", 5.0, None) for _ in range(3)]
    out = h.verdict([1], calls, _samples(1, 10.0), 100, 1, 12)
    (broken,) = [v for v in out if v.startswith("VERB MIX BROKEN")]
    assert "search:paper" in broken and "BadInput" in broken


def test_verb_mix_has_no_known_stale_call():
    # get(kind='paper', q=..., view=...) is BadInput on the current runtime.
    for _label, tool, args, _parks in h.VERB_MIX:
        assert not (tool == "get" and "q" in args and "view" in args)


def test_live_target_without_url_is_rejected():
    with pytest.raises(SystemExit):
        h.parse_args(["--live-target"])


# --- seeded guard (--require-seeded, set by the wrapper's --db) -------------


def test_seeded_error_none_when_corpus_present():
    counts = {t: 5 for t in h.SEEDED_TABLES}
    assert h.seeded_error(counts) is None


def test_seeded_error_names_only_the_empty_tables():
    counts = {"refs": 0, "chunks": 12, "chunk_embeddings": 0}
    err = h.seeded_error(counts)
    assert err is not None
    assert "refs=0" in err
    assert "chunk_embeddings=0" in err
    assert "chunks=0" not in err
    # The refusal must say how to fix it, not just that it refused.
    assert "seed-prod-shaped" in err


def test_seeded_error_missing_table_counts_as_empty():
    assert h.seeded_error({}) is not None


def test_require_seeded_accepted_for_local_run():
    args = h.parse_args(["--require-seeded"])
    assert args.require_seeded


def test_require_seeded_rejected_with_url(monkeypatch):
    # An external target's DB is not reachable for the corpus check; a silent
    # no-op would read as "verified".
    monkeypatch.setenv("PRECIS_LOADTEST_TOKEN", "t")
    with pytest.raises(SystemExit):
        h.parse_args(["--url", "http://x/mcp", "--live-target", "--require-seeded"])


def test_render_records_corpus_line():
    md = h.render(
        [1],
        [],
        [],
        None,
        5.0,
        corpus={"refs": 10, "chunks": 20, "chunk_embeddings": 20},
    )
    assert "corpus: refs=10, chunks=20, chunk_embeddings=20" in md


def test_render_omits_corpus_line_when_unmeasured():
    md = h.render([1], [], [], None, 5.0)
    assert "corpus:" not in md
