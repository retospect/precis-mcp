"""File-level shard partition shared by check.yml and tests/conftest.py.

The key is the node id's path part: every test of one file lands in one
shard, so module/class fixtures build once per gate instead of once per
shard, and check.yml's `plan` job can hand each shard only its own files to
collect. conftest keeps an identical copy — pytest runs it without
``scripts/`` on sys.path — and tests/test_ci_shards.py pins the two equal.
"""

from __future__ import annotations

import json
import zlib
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINS_JSON = ROOT / "tests" / "shard_weights.json"


@cache
def load_pins(path: Path = PINS_JSON) -> dict[int, dict[str, int]]:
    """``{n: {test file: 0-based shard}}`` from tests/shard_weights.json, the
    time-balanced file pins, one table per shard count the gate can run
    (scripts/shard-weights regenerates it). Missing file → no pins. Cached:
    a process partitions thousands of ids against one read."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        int(n): {k: int(v) for k, v in t.items()} for n, t in data["tables"].items()
    }


def shard_index(
    nodeid: str, n: int, pins: dict[int, dict[str, int]] | None = None
) -> int:
    """0-based shard for ``nodeid`` under an ``n``-way split, by FILE: the
    node id's path part is pinned through the table for ``n``, else crc32 —
    not ``hash()``: Python salts str hashes per process, and every process
    (xdist workers, ci-shard-plan, conftest) must agree on the partition.
    Each id maps to exactly one shard, so the shards partition the suite
    whatever the pins say."""
    path = nodeid.split("::", 1)[0]
    table = (load_pins() if pins is None else pins).get(n, {})
    pinned = table.get(path)
    if pinned is not None:
        return pinned
    return zlib.crc32(path.encode("utf-8")) % n
