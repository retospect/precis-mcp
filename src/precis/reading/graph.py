"""Infer concept-graph edges over a cohort (reading-prep loop, slice 3).

Given a reading cohort's concepts (name + definition), ONE LLM call identifies
the typed edges the graph needs:

  - ``has-prerequisite`` — the learning DAG (``X requires Y`` ⇒ learn Y first),
  - ``analogy-of`` — structurally analogous pairs (teach one via the other),
  - ``contrasts-with`` — confusably-similar pairs (build a contrast card).

Names are resolved back to concept ids *within the cohort* (normalized); edges to
unknown names or self-loops are skipped. Re-runnable — ``add_link`` is idempotent
(ON CONFLICT), so a growing cohort can re-infer safely. Cards + mastery + routing
consume these edges in later slices. See docs/design/reading-prep-loop.md.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from precis.reading.concepts import normalize_name

log = logging.getLogger(__name__)

_MAX_CONCEPTS = 40  # cap per call so the prompt stays bounded

_SYS = (
    "You are a curriculum designer mapping how concepts relate. Given a list of "
    "concepts you identify: which concepts REQUIRE understanding another first "
    "(prerequisites), which pairs are structurally ANALOGOUS (learn one via the "
    "other), and which pairs are easily CONFUSED (contrasts). Use ONLY the exact "
    "concept names given — never invent one. Reply with ONLY the requested JSON."
)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            obj = json.loads(text[a : b + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _load_cohort_concepts(
    store: Any, cohort: str, limit: int
) -> list[tuple[int, str, str]]:
    """``(ref_id, name, definition)`` for every concept in the cohort. Uses
    ``jsonb_exists`` (the function form of the jsonb ``?`` array-membership
    operator — ``?`` collides with the client-side param placeholder)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->>'name', meta->>'definition' FROM refs "
            "WHERE kind = 'concept' AND deleted_at IS NULL "
            "AND jsonb_exists(meta->'cohorts', %s) "
            "ORDER BY ref_id LIMIT %s",
            (cohort, limit),
        ).fetchall()
    return [(int(r[0]), r[1] or "", r[2] or "") for r in rows]


def _build_prompt(concepts: list[tuple[int, str, str]]) -> str:
    lines = ["CONCEPTS:"]
    for _cid, name, definition in concepts:
        lines.append(f"- {name}" + (f": {definition}" if definition else ""))
    lines.append(
        "\nTASK: Identify edges AMONG these concepts only (use the exact names "
        "above). A prerequisite is a hard dependency, not mere relatedness; omit "
        "an edge rather than force one. Return JSON exactly:\n"
        '{"prerequisites":[{"concept":"<X>","requires":"<Y>"}],'
        '"analogies":[{"a":"<X>","b":"<Y>"}],'
        '"contrasts":[{"a":"<X>","b":"<Y>"}]}'
    )
    return "\n".join(lines)


def infer_edges(
    store: Any, *, cohort: str, client: Any, max_concepts: int = _MAX_CONCEPTS
) -> dict[str, int]:
    """Infer + write the concept-graph edges for one cohort. Returns
    ``{prerequisite, analogy, contrast, skipped}``."""
    zero = {"prerequisite": 0, "analogy": 0, "contrast": 0, "skipped": 0}
    concepts = _load_cohort_concepts(store, cohort, max_concepts)
    if len(concepts) < 2:
        return dict(zero)  # nothing to relate

    by_name = {normalize_name(name): cid for cid, name, _ in concepts}
    out = client.complete(
        [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": _build_prompt(concepts)},
        ]
    )
    data = _extract_json(getattr(out, "text", "") or "")
    if not data:
        return dict(zero)

    counts = dict(zero)

    def _resolve(name: Any) -> int | None:
        return by_name.get(normalize_name(str(name or "")))

    def _add(a: Any, b: Any, relation: str, key: str) -> None:
        src, dst = _resolve(a), _resolve(b)
        if src is None or dst is None or src == dst:
            counts["skipped"] += 1
            return
        store.add_link(src_ref_id=src, dst_ref_id=dst, relation=relation)
        counts[key] += 1

    for e in data.get("prerequisites") or []:
        if isinstance(e, dict):
            # X requires Y  ⇒  X has-prerequisite Y.
            _add(
                e.get("concept"), e.get("requires"), "has-prerequisite", "prerequisite"
            )
    for e in data.get("analogies") or []:
        if isinstance(e, dict):
            _add(e.get("a"), e.get("b"), "analogy-of", "analogy")
    for e in data.get("contrasts") or []:
        if isinstance(e, dict):
            _add(e.get("a"), e.get("b"), "contrasts-with", "contrast")
    return counts


__all__ = ["infer_edges"]
