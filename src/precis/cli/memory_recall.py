"""Recall quality versus agent-context cost, shared by CLI and weekly cadence.

Use runtime dispatch rather than stored bodies: neighbourhoods, recall rings,
hints and agent result budgets all contribute to the text an agent receives.
Ranks count only index bullets, not hint lines, and are censored at rank_k.
The curve's median includes every readable wanted node (even a search miss);
missing/retired answers have unavailable get sizes, never a zero-cost success.
"""

from __future__ import annotations

import re
from statistics import median
from typing import Any

from precis.cli.memory import SPACE_TAG, _mirrored_handles
from precis.config import PrecisConfig
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store

CURVE_KS = (3, 5, 8, 12)
TOKEN_ESTIMATOR = "chars/4"


def _size(text: str) -> dict[str, int | float]:
    return {"chars": len(text), "tokens": len(text) / 4}


def measure_recall(
    store: Store,
    entries: list[dict[str, Any]],
    *,
    k: int = 5,
    embedder: Any = None,
) -> dict[str, Any]:
    """Per-query renders and k -> recall/median index-plus-fisheye tokens."""
    hub = Hub(store=store, embedder=embedder)
    MemoryHandler(hub=hub)._register_with(hub)
    runtime = PrecisRuntime(config=PrecisConfig(), hub=hub)
    ks = sorted({*CURVE_KS, k})
    mirrored = _mirrored_handles(store, [e["want"] for e in entries])
    queries: list[dict[str, Any]] = []
    for entry in entries:
        indexes: dict[str, Any] = {}
        ranks: dict[int, int | None] = {}
        for cutoff in ks:
            text, error = runtime.dispatch_with_status(
                "search",
                {
                    "kind": "memory",
                    "q": entry["query"],
                    "tags": [SPACE_TAG],
                    "page_size": cutoff,
                    "view": "index",
                },
                fold=True,
            )
            if error:
                raise RuntimeError(f"memory recall search failed: {text}")
            indexes[str(cutoff)] = _size(text)
            handles = re.findall(r"^- .+? \((me\d+)[,)]", text, re.M)
            ranks[cutoff] = next(
                (i for i, handle in enumerate(handles, 1) if handle == entry["want"]),
                None,
            )
        sizes: dict[str, Any] = {}
        for key, view in (
            ("fisheye_1hop", "fisheye+1hop"),
            ("fisheye_1hop_recall", "fisheye+1hop+recall"),
        ):
            text, error = runtime.dispatch_with_status(
                "get", {"kind": "memory", "id": entry["want"], "view": view}, fold=True
            )
            if error and not text.startswith(("[error:NotFound]", "[error:Gone]")):
                raise RuntimeError(f"memory recall get failed: {text}")
            sizes[key] = None if error else _size(text)
        queries.append(
            {
                "query": entry["query"],
                "want": entry["want"],
                "origin": "mirror" if entry["want"] in mirrored else "native",
                "rank": ranks[max(ks)],
                "recall_at_k": {
                    str(cutoff): ranks[cutoff] is not None for cutoff in ks
                },
                "index": indexes,
                **sizes,
            }
        )
    curve: list[dict[str, Any]] = []
    for cutoff in ks:
        costs = [
            row["index"][str(cutoff)]["tokens"] + row["fisheye_1hop"]["tokens"]
            for row in queries
            if row["fisheye_1hop"] is not None
        ]
        hits = sum(row["recall_at_k"][str(cutoff)] for row in queries)
        curve.append(
            {
                "k": cutoff,
                "hits": hits,
                "total": len(queries),
                "recall": hits / len(queries) if queries else 0.0,
                "median_tokens": median(costs) if costs else None,
                "measured": len(costs),
            }
        )
    return {
        "token_estimator": TOKEN_ESTIMATOR,
        "rank_k": max(ks),
        "queries": queries,
        "curve": curve,
        "median_tokens_k5": next(
            row["median_tokens"] for row in curve if row["k"] == 5
        ),
    }


def render_recall_curve(measurements: dict[str, Any]) -> str:
    """The fixed four-cutoff knee table; costs include the wanted-node get."""
    out = [
        f"tokens estimated by {measurements['token_estimator']}; "
        "median = index + fisheye+1hop (readable answers, including misses)",
        "k  recall  median_tokens  measured",
    ]
    for row in measurements["curve"]:
        if row["k"] not in CURVE_KS:
            continue
        cost = row["median_tokens"]
        shown = f"{cost:.1f}" if cost is not None else "n/a"
        out.append(
            f"{row['k']:2} {row['hits']}/{row['total']} ({row['recall']:.1%}) "
            f"{shown} {row['measured']}"
        )
    return "\n".join(out) + "\n"
