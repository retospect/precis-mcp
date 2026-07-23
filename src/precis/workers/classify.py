"""classify — chunk-axis classifier pass (ADR 0047, cascade).

Self-contained ref-pass (shaped like ``llm_summarize``, not a
``WorkerHandler`` subclass: it needs DB JOINs + an outbound LLM call).
For each claimed paper body chunk it runs the **cascade**:

  1. ``junk`` gate — cheap binary; furniture short-circuits to
     ``ROLE3:furniture`` without a second call,
  2. ``role3`` — own / background / furniture, the distinction
     citation-grounding needs,
  3. (optional) escalate ``own`` chunks to a stronger model.

It writes one chunk tag ``ROLE3:<value>`` via ``store.add_tag(...,
pos=ord)`` and leases each chunk in the shared ``chunk_claims`` table
under artifact ``classify:cascade-v<version>`` (bump ``CLASSIFY_VERSION``
to re-tag the corpus). Idempotent: the claim excludes chunks already
carrying a ``ROLE3`` tag.

Eval + rationale live in ``scripts/classify/EVAL_RESULTS.md``; the free
local model scores role3 88% accept-aware / 91% own-precision and junk
94% discard-precision, so this runs on the cheap ``summarizer`` alias.
Default-OFF (``PRECIS_CLASSIFY_ENABLED=1`` or ``--only classify``) — a
1.3M-chunk backfill is a deliberate, node-targeted batch.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from precis.store.types import Tag

CLASSIFY_VERSION = "1"
OUTPUT_NAMESPACE = "ROLE3"
ARTIFACT = f"classify:cascade-v{CLASSIFY_VERSION}"
_AXES_DIR = Path(__file__).resolve().parent.parent / "data" / "axes"
_ROLE3_VALS = {"own", "background", "furniture"}


def _load_axis(axis_id: str) -> dict:
    return yaml.safe_load((_AXES_DIR / f"{axis_id}.yaml").read_text())


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            return json.loads(text[a : b + 1])
        except Exception:
            return None
    return None


def _render_examples(axis: dict) -> str:
    ex = axis.get("examples") or []
    if not ex:
        return ""
    out = ["Worked examples (learn the boundaries):"]
    for e in ex:
        why = f"   # {e['why']}" if e.get("why") else ""
        out.append(f'- "{e["text"]}" -> {{"value": "{e["value"]}"}}{why}')
    return "\n".join(out) + "\n"


def _build_prompt(axis: dict, row: dict) -> str:
    """Chunk context packet declared by the axis `context:` field."""
    want = set(axis.get("context", []))
    lines: list[str] = []
    if "title" in want and row.get("title"):
        lines.append(f"Paper title: {row['title']}")
    if "section_path" in want and row.get("section_path"):
        lines.append(f"Section: {row['section_path']}")
    if "position" in want and row.get("position"):
        lines.append(f"Position in document: {row['position']}")
    if "neighbor_gists_1" in want:
        if row.get("prev_gist"):
            lines.append(f"Previous chunk (gist): {row['prev_gist']}")
        if row.get("next_gist"):
            lines.append(f"Next chunk (gist): {row['next_gist']}")
    lines.append("")
    lines.append(f"CHUNK TEXT:\n{row.get('text', '')}")
    ex = _render_examples(axis)
    ex_block = f"\n{ex}\n" if ex else "\n"
    return f"{axis['prompt'].rstrip()}\n{ex_block}---\n" + "\n".join(lines) + "\n"


_SYS = (
    "You are a precise single-label classifier. Reply with ONLY the "
    "requested JSON object, no prose."
)


def _classify_one(client: Any, axis: dict, row: dict) -> str | None:
    try:
        out = client.complete(
            [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": _build_prompt(axis, row)},
            ]
        )
    except Exception:
        return None
    return (_extract_json(out.text) or {}).get("value")


# ---- DB: claim + enrich (gold-parity context) -------------------------


def _claim(conn, *, limit: int) -> list[dict]:
    sql = """
    WITH cand AS (
      SELECT c.chunk_id, c.ref_id, c.ord, c.text, c.section_path
      FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
      WHERE r.kind = 'paper' AND r.deleted_at IS NULL
        AND c.ord >= 0 AND c.chunk_kind = 'paragraph' AND length(c.text) > 120
        AND NOT EXISTS (SELECT 1 FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id
                        WHERE ct.chunk_id = c.chunk_id AND t.namespace = %(ns)s)
        AND NOT EXISTS (SELECT 1 FROM chunk_claims cl
                        WHERE cl.chunk_id = c.chunk_id AND cl.artifact = %(art)s)
      ORDER BY c.chunk_id LIMIT %(limit)s
      FOR UPDATE OF c SKIP LOCKED
    ), leased AS (
      INSERT INTO chunk_claims (chunk_id, artifact)
      SELECT chunk_id, %(art)s FROM cand ON CONFLICT DO NOTHING
    )
    SELECT chunk_id, ref_id, ord, text, section_path FROM cand
    """
    rows = conn.execute(
        sql, {"ns": OUTPUT_NAMESPACE, "art": ARTIFACT, "limit": limit}
    ).fetchall()
    return [
        {
            "chunk_id": r[0],
            "ref_id": r[1],
            "ord": r[2],
            "text": r[3],
            "section_path": list(r[4] or []),
        }
        for r in rows
    ]


def _enrich(conn, rows: list[dict]) -> None:
    for row in rows:
        ref_id, ord_ = row["ref_id"], row["ord"]
        meta = conn.execute(
            "SELECT r.title, (SELECT count(*) FROM chunks c2 "
            "WHERE c2.ref_id=r.ref_id AND c2.ord>=0) FROM refs r WHERE r.ref_id=%s",
            (ref_id,),
        ).fetchone()
        row["title"] = meta[0] if meta else ""
        row["position"] = f"{ord_}/{meta[1] if meta else 0}"
        row["section_path"] = " ▸ ".join(row["section_path"]) or "(none)"
        neigh = {}
        for nord in (ord_ - 1, ord_ + 1):
            if nord < 0:
                continue
            nr = conn.execute(
                "SELECT c.text, (SELECT s.text FROM chunk_summaries s "
                "WHERE s.chunk_id=c.chunk_id AND s.summarizer='llm-v1' "
                "AND s.status='ok' LIMIT 1) FROM chunks c "
                "WHERE c.ref_id=%s AND c.ord=%s LIMIT 1",
                (ref_id, nord),
            ).fetchone()
            if nr:
                neigh[nord] = (nr[1] or nr[0] or "").strip().replace("\n", " ")[:160]
        row["prev_gist"] = neigh.get(ord_ - 1, "")
        row["next_gist"] = neigh.get(ord_ + 1, "")


# ---- the pass ---------------------------------------------------------


def run_classify_pass(
    store: Any,
    *,
    client: Any,
    batch_size: int = 16,
    escalate_client: Any | None = None,
) -> dict:
    """One claim→classify→write cycle. Returns {claimed, ok, failed}.

    ``escalate_client`` (Tier 2, optional — ``PRECIS_CLASSIFY_ESCALATE_MODEL``)
    re-judges chunks the cheap ``client`` calls ``own`` — the
    citation-critical, error-prone class — with a stronger model. It must be a
    **distinct** client bound to the escalate model (see ``cli/worker.py``'s
    wiring); passing ``client`` itself here would silently re-run the
    identical judgment on the identical model twice, which is a no-op
    disguised as a re-judge — the env knob would gate *whether* to
    "escalate" without ever changing *which* model runs.
    """
    junk_axis = _load_axis("junk")
    role3_axis = _load_axis("role3")

    with store.pool.connection() as conn:
        rows = _claim(conn, limit=batch_size)
        _enrich(conn, rows)
        conn.commit()
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = failed = 0
    dist: Counter = Counter()
    for row in rows:
        # cascade: junk gate -> role3 -> optional escalate
        if _classify_one(client, junk_axis, row) == "junk":
            val = "furniture"
        else:
            val = _classify_one(client, role3_axis, row)
            if val == "own" and escalate_client is not None:
                ev = _classify_one(escalate_client, role3_axis, row)  # Tier 2 re-judge
                if ev in _ROLE3_VALS:
                    val = ev
        if val not in _ROLE3_VALS:
            failed += 1
            continue
        dist[val] += 1
        with store.pool.connection() as conn:
            store.add_tag(
                row["ref_id"],
                Tag.closed(OUTPUT_NAMESPACE, val),
                pos=row["ord"],
                set_by="agent",
                replace_prefix=True,
                conn=conn,
            )
            conn.commit()
        ok += 1
    return {"claimed": len(rows), "ok": ok, "failed": failed, "dist": dict(dist)}
