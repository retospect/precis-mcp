"""Re-mint the frame keys of a ``trace.json`` golden in place.

``tests/workspaces/data/*/out/trace.json`` are fixture goldens; only
``bracket`` has a full generator (``bracket/make_fixture.py``). The other
five are hand-authored, so a key-recipe change means re-minting keys,
not regenerating runs: this walks the ``parent`` links and rewrites
``key``/``parent`` per the schema-2 recipe —
``sha8(parent.key, qual, occ)``, root ``sha8("precis-trace-root")``.
Collapsed frames follow the same rule (their ``occ`` is
``COLLAPSE_SIBLINGS``). A repeated new key gets the writer's tie-break
(``sha8(key, '#', args digest, n)``) and is counted in
``run.key_collisions``, so ``trace_schema.loads`` accepts the result.
Frames whose pre-existing ``key`` values collide are ambiguous parents
and are out of scope (``loads`` already rejects uncounted duplicates).

    uv run python tests/workspaces/data/rekey_trace.py \
        tests/workspaces/data/*/out/trace.json

Idempotent on a doc already on the current recipe;
``test_geom_trace.py::test_rekey_trace_is_idempotent_on_a_real_dump``
pins that.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from precis_geom import trace_schema as ts

_ROOT_KEY = ts.sha8("precis-trace-root")


def rekey_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``doc`` with every frame ``key``/``parent`` re-minted
    per the schema-2 recipe (tie-break + ``run.key_collisions`` as the
    writer emits them)."""
    frames = copy.deepcopy(doc["frames"])
    old_to_new: dict[str, str] = {}
    emitted: dict[str, int] = {}
    key_collisions = 0

    def mint(fr: dict[str, Any]) -> None:
        nonlocal key_collisions
        parent = fr.get("parent")
        key = (
            _ROOT_KEY
            if parent is None
            else ts.frame_key(old_to_new[parent], fr["qual"], fr["occ"])
        )
        n = emitted.get(key, 0)
        if n:
            digest = json.dumps(fr.get("args") or {}, sort_keys=True)
            new = ts.sha8(f"{key}\x00#\x00{digest}\x00{n}")
            key_collisions += 1
        else:
            new = key
        emitted[key] = n + 1
        emitted.setdefault(new, 1)
        old_to_new[fr["key"]] = new
        fr["key"] = new

    pending = list(frames)
    while pending:
        progressed = False
        for fr in pending[:]:
            parent = fr.get("parent")
            if parent is None or parent in old_to_new:
                mint(fr)
                pending.remove(fr)
                progressed = True
        if not progressed:
            raise ValueError("rekey_trace: parent keys form a cycle or are missing")
    for fr in frames:
        if fr["parent"] is not None:
            fr["parent"] = old_to_new[fr["parent"]]
    out = {**doc, "frames": frames}
    out["run"] = {**doc.get("run", {}), "key_collisions": key_collisions}
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    for name in argv:
        path = Path(name)
        doc = json.loads(path.read_text(encoding="utf-8"))
        fixed = rekey_doc(doc)
        ts.loads(ts.dumps(fixed))
        path.write_text(json.dumps(fixed, indent=1) + "\n", encoding="utf-8")
        print(f"{path}: {len(fixed['frames'])} frames re-minted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
