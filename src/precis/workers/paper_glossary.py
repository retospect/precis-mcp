"""paper_glossary — per-paper inferred reading glossary (reading-prep loop, slice 1).

Self-contained ref-pass (shaped like ``classify`` / ``llm_summarize`` — it needs
DB reads + an outbound LLM call, not a pure ``WorkerHandler``). For each claimed
``paper`` it harvests the terms a reader would need to follow the paper:

  1. **defined abbreviations** — Schwartz-Hearst ``Long Form (ABBR)`` first-uses
     + explicit ``term`` chunks (``store.defined_abbrevs``),
  2. **undefined acronyms** — acronym-shaped tokens in title/abstract lacking a
     definition (``abbreviations.find_acronyms``),
  3. **key terms** — the per-chunk KeyBERT keywords already computed corpus-wide
     (``chunks.keywords``),

then makes ONE LLM call to **cluster + define** them, and writes the result as an
embeddable ``card_glossary`` chunk (``ord = -1000``) via DELETE+INSERT so the
embed / keyword cascade re-runs. The chunk ``text`` is a rendered glossary (for
search); ``meta`` carries the structured clusters so a later slice can promote
terms to learning objectives.

Derived, idempotent, reversible: a paper carrying a current-version glossary
(``meta.glossary_version``) is not re-claimed; bump ``GLOSSARY_VERSION`` to
re-derive the corpus lazily. **No AnkiWeb / account writes.** Default-OFF
(``PRECIS_PAPER_GLOSSARY_ENABLED=1`` or ``--only paper_glossary``) — a corpus-wide
backfill is a deliberate, node-targeted batch, like ``classify``. Full design:
docs/design/reading-prep-loop.md (slice 1).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg.types.json import Jsonb

from precis.utils.abbreviations import find_acronyms

log = logging.getLogger(__name__)

GLOSSARY_VERSION = "1"
CHUNK_KIND = "card_glossary"
GLOSSARY_ORD = -1000

_MAX_KEYWORDS = 30  # top KeyBERT terms handed to the model
_MAX_ABBREVS = 40  # cap defined/undefined lists so the prompt stays bounded
_ABSTRACT_CHARS = 1500  # abstract context is a header, not the whole body
_EMPTY_MARKER = "(no distinct glossary terms identified)"

_SYS = (
    "You are a domain expert building a concise reading glossary for a single "
    "paper. You select the terms a reader must understand to follow THIS paper, "
    "group them into a few meaningful clusters, define each in one tight "
    "sentence, and note why it matters here. Reply with ONLY the requested JSON "
    "object, no prose."
)


# ── prompt + parse + render (pure) ─────────────────────────────────────


def _build_prompt(
    title: str,
    abstract: str,
    defined: dict[str, str],
    undefined: list[str],
    keywords: list[str],
) -> str:
    lines = [f"PAPER TITLE: {title}"]
    if abstract:
        lines.append(f"\nABSTRACT:\n{abstract[:_ABSTRACT_CHARS]}")
    if defined:
        lines.append("\nDEFINED ABBREVIATIONS (short -> long):")
        for short, long in list(defined.items())[:_MAX_ABBREVS]:
            lines.append(f"- {short}: {long}")
    if undefined:
        lines.append(
            "\nUNDEFINED ACRONYMS (define from domain knowledge): "
            + ", ".join(undefined[:_MAX_ABBREVS])
        )
    if keywords:
        lines.append("\nKEY TERMS / PHRASES: " + ", ".join(keywords))
    lines.append(
        "\nTASK: Build a reading glossary. Select the terms a reader must know "
        "to follow THIS paper (skip generic words). Group them into a few "
        "meaningful clusters (conceptual / methodological / etymological, as "
        "fits). For each term give a one-sentence definition and a short 'why it "
        "matters for this paper' note. Return JSON exactly:\n"
        '{"clusters":[{"name":"<cluster>","terms":[{"term":"<term>",'
        '"definition":"<one sentence>","note":"<why it matters, brief>"}]}]}'
    )
    return "\n".join(lines)


def _extract_json(text: str) -> dict | None:
    """Tolerant JSON extraction — whole string first, then first ``{``..``}``."""
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


def _clean_clusters(data: dict | None) -> list[dict]:
    """Normalise the model output to ``[{name, terms:[{term,definition,note}]}]``,
    dropping empty clusters/terms. Defensive against a sloppy model."""
    clusters: list[dict] = []
    for cl in (data or {}).get("clusters") or []:
        if not isinstance(cl, dict):
            continue
        terms = []
        for t in cl.get("terms") or []:
            if not isinstance(t, dict):
                continue
            term = str(t.get("term") or "").strip()
            if not term:
                continue
            terms.append(
                {
                    "term": term,
                    "definition": str(t.get("definition") or "").strip(),
                    "note": str(t.get("note") or "").strip(),
                }
            )
        if terms:
            clusters.append({"name": str(cl.get("name") or "").strip(), "terms": terms})
    return clusters


def _render_glossary(clusters: list[dict]) -> str:
    """Render normalised clusters to embeddable markdown (the chunk ``text``)."""
    out: list[str] = []
    for cl in clusters:
        if cl.get("name"):
            out.append(f"## {cl['name']}")
        for t in cl["terms"]:
            line = f"**{t['term']}**"
            if t["definition"]:
                line += f" — {t['definition']}"
            if t["note"]:
                line += f" _{t['note']}_"
            out.append(line)
        out.append("")
    return "\n".join(out).strip()


# ── DB: claim + context + write ────────────────────────────────────────


def _claim(
    conn: Any, *, limit: int, ref_ids: list[int] | None = None
) -> list[tuple[int, str]]:
    """Papers with body content but no current-version glossary. Existence of a
    fresh ``card_glossary`` chunk is the 'done' marker (no separate lease table);
    idempotent + version-bumpable. ``ref_ids`` optionally restricts the sweep to
    specific papers (targeted backfill / tests)."""
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
            SELECT 1 FROM chunks g
            WHERE g.ref_id = r.ref_id AND g.chunk_kind = %(kind)s
              AND g.meta->>'glossary_version' = %(ver)s
          )
        ORDER BY r.ref_id
        LIMIT %(limit)s
    """
    params: dict[str, Any] = {
        "kind": CHUNK_KIND,
        "ver": GLOSSARY_VERSION,
        "limit": limit,
    }
    if ref_ids:
        params["ref_ids"] = list(ref_ids)
    rows = conn.execute(sql, params).fetchall()
    return [(int(r[0]), str(r[1] or "")) for r in rows]


def _context(
    store: Any, ref_id: int, title: str
) -> tuple[str, dict[str, str], list[str], list[str]]:
    """``(abstract, defined_abbrevs, undefined_acronyms, keywords)`` for one paper."""
    defined = store.defined_abbrevs(ref_id)
    with store.pool.connection() as conn:
        arow = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_abstract' "
            "AND retired_at IS NULL LIMIT 1",
            (ref_id,),
        ).fetchone()
        abstract = (arow[0] if arow else "") or ""
        if not abstract:
            brow = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
                "AND retired_at IS NULL ORDER BY ord LIMIT 1",
                (ref_id,),
            ).fetchone()
            abstract = (brow[0] if brow else "") or ""
        krows = conn.execute(
            "SELECT k, count(*) n FROM (SELECT unnest(keywords) k FROM chunks "
            "WHERE ref_id = %s AND keywords IS NOT NULL AND retired_at IS NULL) s "
            "GROUP BY k ORDER BY n DESC, k LIMIT %s",
            (ref_id, _MAX_KEYWORDS),
        ).fetchall()
        keywords = [str(r[0]) for r in krows if r[0]]
    acronyms = find_acronyms(f"{title}\n{abstract}")
    undefined = sorted(a for a in acronyms if a not in defined)
    return abstract, defined, undefined, keywords


def _write(
    store: Any, ref_id: int, text: str, clusters: list[dict], term_count: int
) -> None:
    """(Re-)emit the ref's ``card_glossary`` chunk. DELETE+INSERT so the embed /
    keyword cascade re-runs (mirrors ``upsert_card_combined``)."""
    meta = {
        "glossary_version": GLOSSARY_VERSION,
        "clusters": clusters,
        "term_count": term_count,
    }
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = %s",
            (ref_id, CHUNK_KIND),
        )
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta) "
            "VALUES (%s, 'agent', %s, %s, %s, %s)",
            (ref_id, GLOSSARY_ORD, CHUNK_KIND, text, Jsonb(meta)),
        )
        conn.commit()


# ── the pass ───────────────────────────────────────────────────────────


def run_paper_glossary_pass(
    store: Any, *, client: Any, batch_size: int = 8, ref_ids: list[int] | None = None
) -> dict[str, int]:
    """One claim → extract → cluster → write cycle. Returns ``{claimed, ok, failed}``.

    ``ref_ids`` optionally restricts the sweep to specific papers (targeted
    backfill / tests); ``None`` sweeps the whole corpus."""
    with store.pool.connection() as conn:
        rows = _claim(conn, limit=batch_size, ref_ids=ref_ids)
        conn.commit()
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = failed = 0
    for ref_id, title in rows:
        try:
            abstract, defined, undefined, keywords = _context(store, ref_id, title)
            if not (defined or undefined or keywords):
                # No candidate terms — record a version marker so the paper is
                # not re-claimed every pass. Rare (a paper with body but no
                # abbrevs/keywords).
                _write(store, ref_id, _EMPTY_MARKER, [], 0)
                ok += 1
                continue
            prompt = _build_prompt(title, abstract, defined, undefined, keywords)
            out = client.complete(
                [
                    {"role": "system", "content": _SYS},
                    {"role": "user", "content": prompt},
                ]
            )
            clusters = _clean_clusters(_extract_json(getattr(out, "text", "") or ""))
            if not clusters:
                # Model produced nothing usable — leave unclaimed for a retry.
                failed += 1
                continue
            term_count = sum(len(c["terms"]) for c in clusters)
            _write(store, ref_id, _render_glossary(clusters), clusters, term_count)
            ok += 1
        except Exception:
            log.exception("paper_glossary: failed ref_id=%s", ref_id)
            failed += 1
    return {"claimed": len(rows), "ok": ok, "failed": failed}


__all__ = ["CHUNK_KIND", "GLOSSARY_VERSION", "run_paper_glossary_pass"]
