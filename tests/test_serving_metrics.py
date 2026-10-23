"""Serving-metrics scrape (llm-lane-scheduler S0): Prometheus text
parsing, the SGLang/vLLM name map, and the host swap-in counter.

No DB needed — ``_get_text`` is monkeypatched so no real HTTP happens.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from precis.workers import serving_metrics

# Names and TYPEs from the real capture on castor's running image
# (SGLang v0.5.21, ``--enable-metrics``): the only retraction series is
# the num_retracted_reqs GAUGE — no counter exists. Plus a raft of
# metrics we don't consume.
_SGLANG_FIXTURE = """\
# HELP sglang:num_running_reqs The number of running requests.
# TYPE sglang:num_running_reqs gauge
sglang:num_running_reqs{model_name="openai/gpt-oss-120b",engine_type="unified"} 7
# HELP sglang:num_queue_reqs The number of requests in the waiting queue.
# TYPE sglang:num_queue_reqs gauge
sglang:num_queue_reqs{model_name="openai/gpt-oss-120b"} 3
# HELP sglang:token_usage The token usage.
# TYPE sglang:token_usage gauge
sglang:token_usage{model_name="openai/gpt-oss-120b"} 0.87
# HELP sglang:num_retracted_reqs The number of retracted requests.
# TYPE sglang:num_retracted_reqs gauge
sglang:num_retracted_reqs{model_name="openai/gpt-oss-120b"} 4
# HELP sglang:evicted_tokens_total The total number of KV evicted tokens.
# TYPE sglang:evicted_tokens_total counter
sglang:evicted_tokens_total{model_name="openai/gpt-oss-120b"} 9021
sglang:gen_throughput{model_name="openai/gpt-oss-120b"} 123.4
sglang:cache_hit_rate{model_name="openai/gpt-oss-120b"} 0.5
"""

_VLLM_FIXTURE = """\
vllm:num_requests_running{model_name="m"} 2.0
vllm:num_requests_waiting{model_name="m"} 0.0
vllm:gpu_cache_usage_perc{model_name="m"} 0.42
vllm:num_preemptions_total{model_name="m"} 9.0
"""


def _patch_get(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    monkeypatch.setattr(serving_metrics, "_get_text", lambda url, timeout: body)


def test_parse_prometheus_skips_comments_and_keeps_series() -> None:
    two_series = _SGLANG_FIXTURE + 'sglang:num_queue_reqs{model_name="m2"} 1\n'
    names = serving_metrics.parse_prometheus(two_series)
    assert names["sglang:num_running_reqs"] == [7.0]
    assert names["sglang:num_queue_reqs"] == [3.0, 1.0]
    assert "sglang:num_queue_reqs{model_name=" not in names


def test_scrape_sums_count_series_across_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_get(
        monkeypatch,
        _SGLANG_FIXTURE + 'sglang:num_queue_reqs{model_name="m2"} 1\n',
    )
    assert (
        serving_metrics.scrape_serving_metrics("http://x/metrics")["serving_queued"]
        == 4.0
    )


def test_scrape_takes_max_across_labels_for_kv_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """token_usage is a 0..1 ratio — two label series must NOT sum."""
    _patch_get(
        monkeypatch,
        'sglang:token_usage{model_name="a"} 0.40\n'
        'sglang:token_usage{model_name="b"} 0.87\n',
    )
    assert serving_metrics.scrape_serving_metrics("http://x/metrics") == {
        "serving_kv_usage": 0.87,
    }


def test_scrape_maps_the_retraction_gauge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``num_retracted_reqs`` is a gauge — its level lands in
    ``serving_retractions_total`` (the 0232 column name kept)."""
    _patch_get(
        monkeypatch,
        'sglang:num_retracted_reqs{model_name="a"} 3\n',
    )
    assert serving_metrics.scrape_serving_metrics("http://x/metrics") == {
        "serving_retractions_total": 3.0,
    }


def test_scrape_ignores_evicted_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``evicted_tokens_total`` counts KV evictions, not retractions —
    it must never feed the retraction column."""
    _patch_get(
        monkeypatch,
        'sglang:evicted_tokens_total{model_name="a"} 9021\n',
    )
    assert serving_metrics.scrape_serving_metrics("http://x/metrics") == {}


def test_scrape_maps_sglang_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, _SGLANG_FIXTURE)
    assert serving_metrics.scrape_serving_metrics("http://x/metrics") == {
        "serving_running": 7.0,
        "serving_queued": 3.0,
        "serving_kv_usage": 0.87,
        "serving_retractions_total": 4.0,
    }


def test_scrape_maps_vllm_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """One scrape serves both stacks (vllm-per-node-serving slice 3) —
    except retractions: vLLM's ``num_preemptions_total`` is monotonic
    and can't feed a level column, so it is not mapped."""
    _patch_get(monkeypatch, _VLLM_FIXTURE)
    assert serving_metrics.scrape_serving_metrics("http://x/metrics") == {
        "serving_running": 2.0,
        "serving_queued": 0.0,
        "serving_kv_usage": 0.42,
    }


def test_scrape_empty_on_fetch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, timeout: float) -> str:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(serving_metrics, "_get_text", _boom)
    assert serving_metrics.scrape_serving_metrics("http://x/metrics") == {}


def test_scrape_empty_when_metrics_not_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server without ``--enable-metrics`` answers (a 404 body or HTML)
    with no recognised names — nothing written."""
    _patch_get(monkeypatch, "<html>404 Not Found</html>\nsome_other_metric 1\n")
    assert serving_metrics.scrape_serving_metrics("http://x/metrics") == {}


def test_swap_in_pages_parses_vmstat(tmp_path: Any) -> None:
    f = tmp_path / "vmstat"
    f.write_text("nr_free_pages 123\npswpin 4567\npswpout 89\n", encoding="utf-8")
    assert serving_metrics.collect_swap_in_pages(str(f)) == 4567.0


def test_swap_in_pages_missing_file_returns_none(tmp_path: Any) -> None:
    assert serving_metrics.collect_swap_in_pages(str(tmp_path / "nope")) is None


def test_collect_snapshot_adds_swap_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, _SGLANG_FIXTURE)
    monkeypatch.setattr(serving_metrics, "collect_swap_in_pages", lambda: 42.0)
    snap = serving_metrics.collect_serving_snapshot()
    assert snap["swap_in_pages_total"] == 42.0
    assert snap["serving_running"] == 7.0


def test_collect_snapshot_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        serving_metrics,
        "scrape_serving_metrics",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        serving_metrics,
        "collect_swap_in_pages",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert serving_metrics.collect_serving_snapshot() == {}
