"""classify_topics — paper→topic dossier cascade classifier (ADR 0060).

Self-contained ref-pass (shaped like ``classify`` / ``paper_glossary`` — DB
reads + an outbound LLM call, not a pure ``WorkerHandler``). For each claimed
``paper`` it runs a two-tier cascade against the curated topic taxonomy in
``src/precis/data/topics/*.yaml`` (one file per top-level topic — a topic-dossier
`quest`'s identity):

  1. **tier-0** — free keyword/substring screen over title+abstract per topic.
     A paper matching no topic's keywords skips the LLM call entirely (the
     large majority of an arbitrary corpus won't touch any of these topics).
  2. **tier-1** — the keyword hits become *candidates*; a cheap local model
     confirms/expands them against the full topic list and returns the
     confirmed subset. **Multi-label**: a paper may be tagged into zero, one,
     or several topics (cross-cutting papers are expected — e.g. a catalysis
     paper that is also a health-biomarker paper).

Tier-2 escalation (a stronger model re-judging low-confidence tier-1 calls) is
deliberately not implemented yet — see ADR 0060's open questions.

Writes one open tag ``topic:<slug>`` per confirmed topic, plus a closed marker
tag ``TOPICCASCADE:<version>`` (written regardless of outcome, including zero
matches) so a processed paper is not re-claimed. Bump
``CLASSIFY_TOPICS_VERSION`` to force a lazy re-classify of the whole corpus —
this is also how a *newly added* topic backfills retroactively over papers
already in the corpus (ADR 0060's "and retroactively, for all the others").

No lease table: like ``paper_glossary``, existence of a current-version marker
tag is the 'done' check (no separate claims table — the paper corpus is small
enough, and the LLM call short enough, that a lease isn't needed here).

Default-OFF (``PRECIS_CLASSIFY_TOPICS_ENABLED=1`` or ``--only
classify_topics``) — a corpus-wide backfill is a deliberate, node-targeted
batch, like ``classify``/``paper_glossary``. See
docs/decisions/0060-topic-dossiers.md + docs/design/topic-dossiers.md.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from precis.store.types import Tag

log = logging.getLogger(__name__)

CLASSIFY_TOPICS_VERSION = "1"
MARKER_NAMESPACE = "TOPICCASCADE"
_TOPICS_DIR = Path(__file__).resolve().parent.parent / "data" / "topics"
_ABSTRACT_CHARS = 2000

_SYS = (
    "You are a precise multi-label classifier for standing research topics. "
    "Reply with ONLY the requested JSON object, no prose."
)


def _load_topics() -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text()) for path in sorted(_TOPICS_DIR.glob("*.yaml"))
    ]


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            parsed = json.loads(text[a : b + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _tier0_candidates(topics: list[dict[str, Any]], haystack: str) -> list[str]:
    """Cheap keyword screen. Returns candidate topic slugs (order = topic file order)."""
    lowered = haystack.lower()
    hits = []
    for topic in topics:
        for kw in topic.get("keywords") or []:
            if kw.lower() in lowered:
                hits.append(topic["slug"])
                break
    return hits


def _build_prompt(
    topics: list[dict[str, Any]], candidates: list[str], title: str, abstract: str
) -> str:
    lines = [
        f"Paper title: {title}",
        "",
        f"Abstract:\n{abstract[:_ABSTRACT_CHARS]}",
        "",
    ]
    lines.append(
        "Candidate standing research topics (a paper may genuinely belong to "
        "zero, one, or several — don't force a single pick):"
    )
    for topic in topics:
        lines.append(f"- {topic['slug']}: {topic['description'].strip()}")
    lines.append("")
    lines.append(
        "A cheap keyword screen flagged these as possible matches — verify "
        "each against the abstract, don't rubber-stamp: "
        f"{', '.join(candidates) if candidates else '(none)'}."
    )
    lines.append("")
    lines.append(
        'Return JSON: {"topics": ["<slug>", ...]} using only slugs from the '
        "list above. Empty list if none genuinely apply."
    )
    return "\n".join(lines)


def _classify_one(
    client: Any,
    topics: list[dict[str, Any]],
    candidates: list[str],
    title: str,
    abstract: str,
) -> list[str] | None:
    """Returns the confirmed topic-slug list, or ``None`` on a call/parse failure."""
    try:
        out = client.complete(
            [
                {"role": "system", "content": _SYS},
                {
                    "role": "user",
                    "content": _build_prompt(topics, candidates, title, abstract),
                },
            ]
        )
    except Exception:
        return None
    parsed = _extract_json(out.text)
    if parsed is None:
        return None
    raw = parsed.get("topics")
    if not isinstance(raw, list):
        return None
    valid = {topic["slug"] for topic in topics}
    return [slug for slug in raw if isinstance(slug, str) and slug in valid]


# ── DB: claim + context + write ────────────────────────────────────────


def _claim(
    conn: Any, *, limit: int, ref_ids: list[int] | None = None
) -> list[tuple[int, str]]:
    """Papers with body content lacking a current-version marker tag. Existence
    of a fresh ``TOPICCASCADE`` ref tag is the 'done' marker (no separate lease
    table, mirroring ``paper_glossary``); idempotent + version-bumpable.
    ``ref_ids`` optionally restricts the sweep to specific papers (targeted
    backfill / tests)."""
    ref_filter = "AND r.ref_id = ANY(%(ref_ids)s)" if ref_ids else ""
    sql = f"""
        SELECT r.ref_id, r.title
        FROM refs r
        WHERE r.kind = 'paper' AND r.deleted_at IS NULL
          {ref_filter}
          AND EXISTS (
            SELECT 1 FROM chunks c
            WHERE c.ref_id = r.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
          )
          AND NOT EXISTS (
            SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
            WHERE rt.ref_id = r.ref_id AND t.namespace = %(ns)s AND t.value = %(ver)s
          )
        ORDER BY r.ref_id
        LIMIT %(limit)s
    """
    params: dict[str, Any] = {
        "ns": MARKER_NAMESPACE,
        "ver": CLASSIFY_TOPICS_VERSION,
        "limit": limit,
    }
    if ref_ids:
        params["ref_ids"] = list(ref_ids)
    rows = conn.execute(sql, params).fetchall()
    return [(int(r[0]), str(r[1] or "")) for r in rows]


def _abstract(conn: Any, ref_id: int) -> str:
    row = conn.execute(
        "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_abstract' "
        "AND retired_at IS NULL LIMIT 1",
        (ref_id,),
    ).fetchone()
    text = (row[0] if row else "") or ""
    if not text:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord LIMIT 1",
            (ref_id,),
        ).fetchone()
        text = (row[0] if row else "") or ""
    return text


# ── the pass ───────────────────────────────────────────────────────────


def run_classify_topics_pass(
    store: Any, *, client: Any, batch_size: int = 16, ref_ids: list[int] | None = None
) -> dict[str, Any]:
    """One claim → tier0 → tier1 → write cycle. Returns
    ``{claimed, ok, failed, dist}``. ``ref_ids`` optionally restricts the sweep
    to specific papers (targeted backfill / tests); ``None`` sweeps the whole
    corpus."""
    topics = _load_topics()
    with store.pool.connection() as conn:
        rows = _claim(conn, limit=batch_size, ref_ids=ref_ids)
        conn.commit()
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = failed = 0
    dist: Counter[str] = Counter()
    for ref_id, title in rows:
        with store.pool.connection() as conn:
            abstract = _abstract(conn, ref_id)

        candidates = _tier0_candidates(topics, f"{title} {abstract}")
        if not candidates:
            confirmed: list[str] = []
        else:
            classified = _classify_one(client, topics, candidates, title, abstract)
            if classified is None:
                failed += 1
                continue
            confirmed = classified

        with store.pool.connection() as conn:
            for slug in confirmed:
                store.add_tag(
                    ref_id, Tag.open(f"topic:{slug}"), set_by="agent", conn=conn
                )
                dist[slug] += 1
            store.add_tag(
                ref_id,
                Tag.closed(MARKER_NAMESPACE, CLASSIFY_TOPICS_VERSION),
                set_by="agent",
                replace_prefix=True,
                conn=conn,
            )
            conn.commit()
        ok += 1
    return {"claimed": len(rows), "ok": ok, "failed": failed, "dist": dict(dist)}
