"""Gold-task model + loader for the LLM eval harness (slice 11).

A gold set is a JSON list of tasks, each drawn (ideally) from precis's *own*
workload — "own workload is the benchmark, not academia". The curated,
version-controlled sets live under ``scripts/llm_eval/gold_set/`` (mirroring the
ADR-0047 classifier precedent); a small seed ships as package data
(:func:`default_gold_path`) so ``precis llm eval`` runs out of the box.

Each task is one JSON object::

    {
      "task_id": "needle-catalyst-01",
      "axis": "long-context-recall",
      "scorer": "needle",
      "prompt": "…big doc… What is the widget code? …",
      "expect": {"needle": "WX-4417"},
      "tools_needed": false
    }

``axis`` must be a catalog capability axis (:data:`llm_catalog.CAPABILITY_AXES`);
``scorer`` names a wired scorer (:data:`scorers.SCORERS`) — a task naming an
unwired scorer (the heavy code/summarize axes) is loaded but the harness skips
it with a log rather than scoring it 0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from precis.errors import BadInput
from precis.llm_catalog import CAPABILITY_AXES


@dataclass(frozen=True, slots=True)
class GoldTask:
    """One golden task: a prompt, its axis/scorer, and the expected answer."""

    task_id: str
    axis: str
    scorer: str
    prompt: str
    expect: dict[str, Any] = field(default_factory=dict)
    tools_needed: bool = False


def default_gold_path() -> Path:
    """Path to the seed gold set shipped as package data."""
    return Path(str(resources.files("precis.data").joinpath("llm_eval/gold_set.json")))


def load_gold_set(path: str | Path | None = None) -> list[GoldTask]:
    """Load + validate a gold set from ``path`` (default: the seed set).

    Raises :class:`BadInput` on a malformed file or an out-of-vocabulary
    ``axis`` so a curation typo is caught at load, not mid-run.
    """
    p = Path(path) if path is not None else default_gold_path()
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BadInput(f"llm eval: cannot read gold set {p}: {exc}") from exc
    if not isinstance(raw, list):
        raise BadInput(f"llm eval: gold set {p} must be a JSON list of tasks")
    tasks: list[GoldTask] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BadInput(f"llm eval: gold task #{i} is not an object")
        axis = str(item.get("axis") or "")
        if axis not in CAPABILITY_AXES:
            raise BadInput(
                f"llm eval: gold task #{i} has unknown axis {axis!r}",
                options=list(CAPABILITY_AXES),
            )
        tasks.append(
            GoldTask(
                task_id=str(item.get("task_id") or f"task-{i}"),
                axis=axis,
                scorer=str(item.get("scorer") or ""),
                prompt=str(item.get("prompt") or ""),
                expect=dict(item.get("expect") or {}),
                tools_needed=bool(item.get("tools_needed", False)),
            )
        )
    return tasks


__all__ = ["GoldTask", "default_gold_path", "load_gold_set"]
