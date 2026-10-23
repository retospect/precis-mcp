"""Local-serving metrics scrape — SGLang ``/metrics`` plus host swap-in.

llm-lane-scheduler slice S0 (shared with vllm-per-node-serving slice 3 —
ONE scrape, both consumers): the heartbeat pass on a serving host pulls
the server's Prometheus endpoint each beat and lands the counters in
``host_heartbeat_log`` (migration 0232). The
``local-serving-pressure`` condition reads the columns; S3's capacity
controller reads them next.

Self-gating like :mod:`precis.workers.llm_serving`: the scrape targets
loopback, so a host with nothing serving on the port fails fast and
writes nothing (NULL columns), and a server running without metrics
enabled answers no recognised names — also nothing written. Host swap-in
(``/proc/vmstat`` ``pswpin``) is collected on every Linux host — on a
GB10-class box GPU and CPU share unified memory, so host swap-in is
model pressure too; elsewhere it costs one file read.

The retraction counter survives in ``host_heartbeat_log`` even across a
server restart: deltas clamp at zero, so a reset reads as "counter
rebased", not negative retraction.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

#: Prometheus endpoint. ``PRECIS_SERVING_METRICS_URL`` wins; else the
#: SGLang role's default port on loopback (deploy/roles/sglang
#: ``sglang_port: 8000``). Loopback only — never an agent-supplied URL,
#: so safe_fetch's SSRF guard doesn't apply.
_DEFAULT_METRICS_URL = "http://127.0.0.1:8000/metrics"

#: ``/proc/vmstat`` counter: pages swapped IN since boot (monotonic).
_VMSTAT_PATH = "/proc/vmstat"

#: Column name -> (aggregation, accepted Prometheus metric names in
#: priority order — first non-None wins). Counts SUM across a name's
#: label sets; the KV utilisation RATIO takes MAX (summing label sets of
#: a 0..1 fraction is meaningless). SGLang exposes ``sglang:``-prefixed
#: names with ``--enable-metrics``; the vLLM equivalents are listed too —
#: one scrape serves both stacks (vllm-per-node-serving.md).
#:
#: Retractions take only MONOTONIC counter series:
#: ``sglang:num_retracted_reqs`` is a GAUGE (requests currently held
#: retracted) — summing its positive deltas over-counts, so it is
#: deliberately absent. If the running image lacks a retraction counter
#: the column stays NULL until the mapping is corrected — confirm the
#: names against castor's image after the role deploys.
_METRIC_NAMES: dict[str, tuple[str, tuple[str, ...]]] = {
    "serving_running": (
        "sum",
        (
            "sglang:num_running_reqs",
            "sglang_num_running_reqs",
            "vllm:num_requests_running",
            "num_requests_running",
        ),
    ),
    "serving_queued": (
        "sum",
        (
            "sglang:num_queue_reqs",
            "sglang_num_queue_reqs",
            "vllm:num_requests_waiting",
            "num_requests_waiting",
        ),
    ),
    "serving_kv_usage": (
        "max",
        (
            "sglang:token_usage",
            "sglang_token_usage",
            "vllm:gpu_cache_usage_perc",
            "gpu_cache_usage_perc",
        ),
    ),
    "serving_retractions_total": (
        "sum",
        (
            "sglang:num_retracted_requests_total",
            "sglang_num_retracted_requests_total",
            "sglang:num_retractions_count",
            "sglang_num_retractions_count",
            "vllm:num_preemptions_total",
            "num_preemptions_total",
        ),
    ),
}


def serving_metrics_url() -> str:
    return os.environ.get("PRECIS_SERVING_METRICS_URL", _DEFAULT_METRICS_URL)


def parse_prometheus(text: str) -> dict[str, list[float]]:
    """Parse the Prometheus text exposition format → ``{name: [values]}``.

    One value per series (label set) under each metric name — the caller
    aggregates (counts sum, ratios max); ``#`` comment/HELP/TYPE lines
    are skipped. Histogram ``_bucket``/``_sum``/``_count`` series land
    under their own names — callers pick the names they want from
    :data:`_METRIC_NAMES`.
    """
    out: dict[str, list[float]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        sample, _, value = line.rpartition(" ")
        name = sample.split("{", 1)[0].strip()
        if not name:
            continue
        try:
            v = float(value)
        except ValueError:
            continue
        out.setdefault(name, []).append(v)
    return out


def _get_text(url: str, timeout: float = 2.0) -> str:
    req = urllib.request.Request(url, headers={"Accept": "text/plain"})
    # Loopback only (127.0.0.1) — not an agent-supplied URL, so
    # safe_fetch's SSRF guard doesn't apply.
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def scrape_serving_metrics(
    url: str | None = None, timeout: float = 2.0
) -> dict[str, float]:
    """GET the serving host's ``/metrics`` → the ``serving_*`` columns.

    ``{}`` on ANY failure — refused connection (nothing serving), 404
    (metrics not enabled), timeout, parse error — and when the body has
    none of the recognised names. The caller writes the dict verbatim, so
    ``{}`` means "nothing to record this beat" (columns stay NULL).
    """
    target = url or serving_metrics_url()
    try:
        text = _get_text(target, timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return {}
    names = parse_prometheus(text)
    out: dict[str, float] = {}
    for column, (agg, candidates) in _METRIC_NAMES.items():
        for name in candidates:
            if name in names:
                values = names[name]
                out[column] = max(values) if agg == "max" else sum(values)
                break
    return out


def collect_swap_in_pages(path: str = _VMSTAT_PATH) -> float | None:
    """Cumulative pages swapped in since boot (``pswpin``), or ``None``.

    Linux-only; ``None`` on any other platform or an unreadable file. A
    host-level counter — it belongs to the whole box, matching the
    unified-memory reading that host swap IS serving pressure.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("pswpin "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def collect_serving_snapshot() -> dict[str, float]:
    """One beat's worth of serving columns for ``record_heartbeat_history``.

    Keys are the ``host_heartbeat_log`` column names
    (``serving_running``/``serving_queued``/``serving_kv_usage``/
    ``serving_retractions_total``/``swap_in_pages_total``); absent keys
    mean "not collected" and land as NULL. Never raises — a scrape hiccup
    must not fail the beat.
    """
    try:
        out = scrape_serving_metrics()
    except Exception:
        log.warning("serving metrics scrape failed", exc_info=True)
        out = {}
    try:
        swap = collect_swap_in_pages()
    except Exception:
        log.warning("swap-in read failed", exc_info=True)
        swap = None
    if swap is not None:
        out["swap_in_pages_total"] = swap
    return out
