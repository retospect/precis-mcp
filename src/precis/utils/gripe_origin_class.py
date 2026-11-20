"""Deterministic gripe provenance; unknown origins stay unknown."""

import re
from collections.abc import Iterable, Mapping
from typing import Any

ORIGINS = ("human", "self-improvement", "specification")


def classify_origin(
    tags: Iterable[str], title: str, writer: Mapping[str, Any] | None = None
) -> str | None:
    """Prefer explicit classification, then filing tags, then the first line.

    Writer metadata is evidence only for a stated human; an agent identity
    alone does not distinguish specification work from self-improvement.
    """
    values = [t.removeprefix("OPEN:").removeprefix("FLAG:") for t in tags]
    for tag in values:
        if tag.startswith("origin-class:"):
            value = tag.partition(":")[2]
            return value if value in ORIGINS else None
    if writer and writer.get("human"):
        return "human"

    def infer(value: str) -> str | None:
        value = value.strip().lower()
        if value.startswith(("reto:ask", "fix:reto", "user request")):
            return "human"
        if value.startswith(
            (
                "quest-need:",
                "[callerr:",
                "callerr:",
                "watchdog:",
                "surface-review",
                "dup-suggest:",
                "memory-lint",
                "backlog-lint",
                "cluster-desk",
            )
        ):
            return "self-improvement"
        if value.startswith(("spec:", "needs-spec", "wave:")):
            return "specification"
        if re.match(r"slice \d+ of docs/backlog/", value):
            return "specification"
        return None

    inferred = {infer(tag) for tag in values}
    for origin in ORIGINS:
        if origin in inferred:
            return origin
    return infer(title.splitlines()[0] if title.splitlines() else "")
