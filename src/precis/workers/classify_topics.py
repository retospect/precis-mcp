"""classify_topics — paper→topic dossier cascade classifier (ADR 0060).

Self-contained ref-pass (shaped like ``classify`` / ``paper_glossary`` — DB
reads + an outbound LLM call, not a pure ``WorkerHandler``). For each claimed
``paper`` or ``patent`` it runs a two-tier cascade against the curated topic taxonomy in
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

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from precis.store.types import Tag

log = logging.getLogger(__name__)

# v3 (2026-07-25): topic set changed — llm-improvements → llm (rescope),
# + ml-general, bayesian-statistics, co2-conversion, catalyst-stability,
# nh3-synthesis; nanobuds narrowed, noxrr split from nh3-synthesis. Bumping
# lazily re-classifies the corpus against the new set (also the retroactive-
# backfill path for the added topics). Harmless while the pass is default-OFF.
CLASSIFY_TOPICS_VERSION = "3"
MARKER_NAMESPACE = "TOPICCASCADE"
_TOPICS_DIR = Path(__file__).resolve().parent.parent / "data" / "topics"
_CONTEXT_CHARS = 3000
# Below this many stripped chars, the abstract is considered too thin to
# classify off alone — fall back to (or supplement with) body text.
_THIN_ABSTRACT_CHARS = 400
_BODY_FALLBACK_CHUNKS = 5

_SYS = (
    "You are a precise multi-label classifier for standing research topics. "
    "Reply with ONLY the requested JSON object, no prose."
)


def _load_topics() -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text()) for path in sorted(_TOPICS_DIR.glob("*.yaml"))
    ]


def all_topic_slugs() -> list[str]:
    """Every topic slug in ``data/topics/*.yaml`` — for the worker-CLI gate +
    the ``classify_topics`` closure to enumerate without duplicating the
    glob."""
    return [
        str(t["slug"]) for t in _load_topics() if isinstance(t, dict) and t.get("slug")
    ]


def topic_marker_value(enabled_slugs: Iterable[str]) -> str:
    """Done-marker value encoding the enabled-topic SET (ADR 0068 backfill).

    Order-independent, set-sensitive: a change to the enabled set changes the
    value, so ``_claim`` re-claims the corpus lazily against the new set.
    """
    slugs = sorted({str(s) for s in enabled_slugs})
    # "\x1f" (unit separator) can't appear in a slug, so two distinct
    # slug-sets can't collide onto the same joined string (unlike ",").
    digest = hashlib.blake2b(
        "\x1f".join(slugs).encode("utf-8"), digest_size=4
    ).hexdigest()
    return f"{CLASSIFY_TOPICS_VERSION}-{digest}"


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
    topics: list[dict[str, Any]], candidates: list[str], title: str, context: str
) -> str:
    lines = [
        f"Paper title: {title}",
        "",
        f"Abstract / opening text:\n{context[:_CONTEXT_CHARS]}",
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
    context: str,
) -> list[str] | None:
    """Returns the confirmed topic-slug list, or ``None`` on a call/parse failure."""
    try:
        out = client.complete(
            [
                {"role": "system", "content": _SYS},
                {
                    "role": "user",
                    "content": _build_prompt(topics, candidates, title, context),
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
    conn: Any, *, limit: int, marker_value: str, ref_ids: list[int] | None = None
) -> list[tuple[int, str]]:
    """Papers or patents with body content lacking a current-marker tag.
    Existence of a fresh ``TOPICCASCADE`` ref tag carrying ``marker_value`` is
    the 'done' marker (no separate lease table, mirroring
    ``paper_glossary``); idempotent + re-claimable by changing the marker
    (a version bump, or — ADR 0068 — a change to the enabled-topic set).
    ``ref_ids`` optionally restricts the sweep to specific refs (targeted
    backfill / tests)."""
    ref_filter = "AND r.ref_id = ANY(%(ref_ids)s)" if ref_ids else ""
    sql = f"""
        SELECT r.ref_id, r.title
        FROM refs r
        WHERE r.kind = ANY(%(kinds)s) AND r.deleted_at IS NULL
          {ref_filter}
          AND EXISTS (
            SELECT 1 FROM chunks c
            WHERE c.ref_id = r.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
          )
          AND NOT EXISTS (
            SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
            WHERE rt.ref_id = r.ref_id AND t.namespace = %(ns)s AND t.value = %(marker_value)s
          )
        ORDER BY r.ref_id
        LIMIT %(limit)s
    """
    params: dict[str, Any] = {
        "kinds": ["paper", "patent"],
        "ns": MARKER_NAMESPACE,
        "marker_value": marker_value,
        "limit": limit,
    }
    if ref_ids:
        params["ref_ids"] = list(ref_ids)
    rows = conn.execute(sql, params).fetchall()
    return [(int(r[0]), str(r[1] or "")) for r in rows]


def _context_text(conn: Any, ref_id: int) -> str:
    """Classification context: the abstract when it's substantial, else the
    abstract (if any) topped up with the first few body chunks — a thin or
    missing abstract otherwise starves tier-0/tier-1 of signal even though
    the paper has a rich body (the claim SQL already requires body chunks)."""
    row = conn.execute(
        "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_abstract' "
        "AND retired_at IS NULL LIMIT 1",
        (ref_id,),
    ).fetchone()
    abstract = ((row[0] if row else "") or "").strip()
    if len(abstract) >= _THIN_ABSTRACT_CHARS:
        return abstract

    rows = conn.execute(
        "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
        "AND retired_at IS NULL ORDER BY ord LIMIT %s",
        (ref_id, _BODY_FALLBACK_CHUNKS),
    ).fetchall()
    body = "\n\n".join((r[0] or "") for r in rows).strip()

    text = f"{abstract}\n\n{body}" if abstract else body
    return text[:_CONTEXT_CHARS]


# ── the pass ───────────────────────────────────────────────────────────


def run_classify_topics_pass(
    store: Any,
    *,
    client: Any,
    batch_size: int = 16,
    enabled_slugs: list[str] | None = None,
    ref_ids: list[int] | None = None,
) -> dict[str, Any]:
    """One claim → tier0 → tier1 → write cycle. Returns
    ``{claimed, ok, failed, dist}``. ``enabled_slugs`` restricts classification
    to that topic subset (ADR 0068 per-topic gating) — ``None`` classifies
    against the full taxonomy (back-compat for CLI/tests); an empty list
    short-circuits to a no-op. ``ref_ids`` optionally restricts the sweep to
    specific papers (targeted backfill / tests); ``None`` sweeps the whole
    corpus."""
    topics = _load_topics()
    if enabled_slugs is None:
        effective = topics
    else:
        wanted = {str(s) for s in enabled_slugs}
        effective = [
            t for t in topics if isinstance(t, dict) and str(t.get("slug")) in wanted
        ]
    if not effective:
        return {"claimed": 0, "ok": 0, "failed": 0}
    effective_slugs = [str(t["slug"]) for t in effective]
    marker_value = topic_marker_value(effective_slugs)

    with store.pool.connection() as conn:
        rows = _claim(
            conn, limit=batch_size, marker_value=marker_value, ref_ids=ref_ids
        )
        conn.commit()
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = failed = 0
    dist: Counter[str] = Counter()
    for ref_id, title in rows:
        with store.pool.connection() as conn:
            context = _context_text(conn, ref_id)

        candidates = _tier0_candidates(effective, f"{title} {context}")
        if not candidates:
            confirmed: list[str] = []
        else:
            classified = _classify_one(client, effective, candidates, title, context)
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
                Tag.closed(MARKER_NAMESPACE, marker_value),
                set_by="agent",
                replace_prefix=True,
                conn=conn,
            )
            conn.commit()
        ok += 1
    return {"claimed": len(rows), "ok": ok, "failed": failed, "dist": dict(dist)}
