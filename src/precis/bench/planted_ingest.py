"""Put planted facts into the dev DB: a thin adapter over the corpus write path.

:func:`precis.bench.planted.build` returns, besides the task rows, a sidecar
with the *modified* host documents (``docs``) and one ``memory_nodes`` entry
per planted fact. This module

1. picks the host paragraphs from the ingested QASPER papers
   (:func:`pick_hosts`): whole-paragraph chunks of a fixed length band, drawn
   with a seeded RNG, never a paragraph any QASPER task cites as evidence (a
   near-duplicate of a gold paragraph would compete with the real gold chunk
   and bend the QASPER numbers);
2. ingests each modified document as an extra ``kind='paper'`` ref
   (:func:`ingest_docs`) with the same chunker and offset contract as
   :mod:`precis.bench.corpus`, so :func:`precis.bench.gold.spans_overlap_chunk`
   scores a planted task exactly like a QASPER one; and
3. writes each memory node through the ordinary ``put(kind='memory')``
   dispatch (:func:`ingest_memory`), tagged ``bench-fact:<fact_id>`` so a
   second run finds it instead of writing a duplicate.

Ids: a planted document is ``planted:<seed>-<NN>`` (refs.paper_id); its refs
carry ``meta.bench_planted.doc``. Embeddings and keywords are the worker's job
(:func:`precis.bench.layers.fill`), as for the QASPER set.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from precis.bench import corpus, gold
from precis.bench.corpus import Unit
from precis.identity import make_content_hash
from precis.ingest.db_writer import PaperToWrite, write_paper
from precis.ingest.pipeline import _build_cards, _title_cite_key

#: Paragraph length band for a host (chars): long enough to read as prose,
#: short enough that one planted sentence keeps it within a couple of chunks.
HOST_MIN_CHARS = 200
HOST_MAX_CHARS = 700
#: Hosts per planted run and paragraphs per host document.
HOST_DOCS = 8
HOST_PARAS_PER_DOC = 12

FACT_TAG_PREFIX = "bench-fact:"


def evidence_units(rows: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """``{(doc id, unit id)}`` that any task cites as evidence."""
    return {
        (str(e["doc"]), str(e["unit"])) for row in rows for e in row.get("evidence", ())
    }


def pick_hosts(
    conn: Any,
    ref_ids: Sequence[int],
    *,
    protected: set[tuple[str, str]],
    seed: int,
    n_docs: int = HOST_DOCS,
    per_doc: int = HOST_PARAS_PER_DOC,
) -> list[dict[str, Any]]:
    """Host documents ``[{id, paragraphs}]`` drawn from ingested QASPER refs.

    A candidate is a body ``paragraph`` unit that is exactly one chunk
    (``para_char_start == 0`` and no sibling chunk of the same ``para_id``),
    so the chunk text *is* the paragraph. Candidates are ordered by
    ``(qasper id, para_id)`` and shuffled with ``random.Random(f"hosts:{seed}")``
    — a pure function of the DB content and the seed.
    """
    rows = conn.execute(
        "SELECT r.meta->'qasper'->>'id', c.meta->'qasper'->>'para_id', c.text, "
        "c.meta->'qasper'->>'para_char_start' "
        "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
        "WHERE c.ref_id = ANY(%s) AND c.ord >= 0 "
        "AND c.meta->'qasper'->>'src' = 'paragraph' "
        "ORDER BY 1, 2, c.ord",
        (list(ref_ids),),
    ).fetchall()
    per_unit: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
    for qid, para_id, text, start in rows:
        per_unit.setdefault((f"qasper:{qid}", str(para_id)), []).append((text, start))
    pool: list[str] = []
    for key in sorted(per_unit):
        chunks = per_unit[key]
        if key in protected or len(chunks) != 1:
            continue
        text, start = chunks[0]
        if start == "0" and HOST_MIN_CHARS <= len(text) <= HOST_MAX_CHARS:
            pool.append(text)
    need = n_docs * per_doc
    if len(pool) < need:
        raise ValueError(
            f"only {len(pool)} eligible host paragraphs, need {need}; "
            "ingest the set first (scripts/bench ingest)"
        )
    picked = random.Random(f"hosts:{seed}").sample(pool, need)
    return [
        {
            "id": f"planted:{seed}-{d:02d}",
            "paragraphs": picked[d * per_doc : (d + 1) * per_doc],
        }
        for d in range(n_docs)
    ]


def planted_paper_to_write(
    doc: Mapping[str, Any], *, seed: int, at: str
) -> PaperToWrite:
    """The :class:`PaperToWrite` for one modified host document.

    Units are the paragraphs at :func:`precis.bench.gold.paragraph_offsets`
    (whitespace-only ones skipped, indexes kept), so chunk ``doc_char_*``
    offsets agree with the ``doc_span`` that :func:`gold.from_planted` wrote.
    No title or abstract unit exists in the canonical text; the ref's title is
    a neutral label and the cards carry only it.
    """
    doc_id = str(doc["id"])
    paragraphs: Sequence[str] = doc["paragraphs"]
    offsets = gold.paragraph_offsets(paragraphs)
    units = [
        Unit(f"p{i}", "paragraph", p, (), None, i, offsets[i])
        for i, p in enumerate(paragraphs)
        if i in offsets
    ]
    title = f"Planted document {doc_id.rsplit(':', 1)[-1]}"
    cards = _build_cards(title=title, authors=[], abstract="", keywords=[])
    body = corpus.chunk_units(units, seen_texts=(c.text for c in cards))
    joined = gold.joined_text(paragraphs)
    return PaperToWrite(
        title=title,
        authors=[],
        year=None,
        kind="paper",
        provider="local",
        set_by="system",
        paper_id=doc_id,
        cite_key_prefix=_title_cite_key(title, None),
        content_hash=make_content_hash(joined),
        meta={
            "source_format": "planted",
            "abstract": "",
            "bench_planted": {
                "doc": doc_id,
                "seed": seed,
                "at": at,
                "n_paragraphs": len(units),
            },
        },
        chunks=cards + body,
    )


def existing_planted_ref(conn: Any, doc_id: str) -> int | None:
    row = conn.execute(
        "SELECT ref_id FROM refs WHERE kind = 'paper' AND retired_at IS NULL "
        "AND meta->'bench_planted'->>'doc' = %s ORDER BY ref_id LIMIT 1",
        (doc_id,),
    ).fetchone()
    return None if row is None else int(row[0])


def ingest_docs(
    conn: Any, docs: Iterable[Mapping[str, Any]], *, seed: int, at: str
) -> dict[str, int]:
    """Ingest the modified host documents; ``{doc id: ref_id}``.

    Idempotent per document: one already present is left alone (body chunks
    are append-only) — the sidecar is a pure function of the seed and hosts,
    so a present document has the same text."""
    out: dict[str, int] = {}
    for doc in docs:
        doc_id = str(doc["id"])
        have = existing_planted_ref(conn, doc_id)
        if have is None:
            with conn.transaction():
                write_paper(planted_paper_to_write(doc, seed=seed, at=at), conn=conn)
            have = existing_planted_ref(conn, doc_id)
        assert have is not None
        out[doc_id] = have
    return out


def _fact_tag(fact_id: str, role: str) -> list[str]:
    return [f"{FACT_TAG_PREFIX}{fact_id}", f"bench-role:{role}"]


def existing_memory_refs(conn: Any) -> dict[str, int]:
    """``{fact_id: ref_id}`` of planted memory nodes already in the DB."""
    rows = conn.execute(
        "SELECT t.value, r.ref_id FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        "JOIN refs r ON r.ref_id = rt.ref_id "
        "WHERE r.kind = 'memory' AND r.retired_at IS NULL "
        "AND t.namespace = 'OPEN' AND t.value LIKE %s",
        (FACT_TAG_PREFIX + "%",),
    ).fetchall()
    return {str(v)[len(FACT_TAG_PREFIX) :]: int(rid) for v, rid in rows}


def ingest_memory(
    runtime: Any, conn: Any, nodes: Iterable[Mapping[str, Any]]
) -> dict[str, int]:
    """Write each memory node through ``put(kind='memory')``; ``{fact_id:
    ref_id}``. A node whose ``bench-fact:`` tag already exists is skipped."""
    have = existing_memory_refs(conn)
    for node in nodes:
        fid = str(node["fact_id"])
        if fid in have:
            continue
        text, is_error = runtime.dispatch_with_status(
            "put",
            {
                "kind": "memory",
                "title": node["title"],
                "text": node["body"],
                "tags": _fact_tag(fid, str(node["role"])),
            },
            fold=True,
        )
        if is_error:
            raise RuntimeError(f"planted memory node {fid} was refused: {text[:300]}")
        have = existing_memory_refs(conn)
        if fid not in have:
            raise RuntimeError(
                f"planted memory node {fid} was not stored: {text[:300]}"
            )
    return have


def gold_sentences(
    rows: Iterable[Mapping[str, Any]], nodes: Iterable[Mapping[str, Any]]
) -> dict[str, tuple[str, ...]]:
    """``{task id: gold fact sentences}`` for the ``memory``-type tasks: the
    bodies of the task's ``gold`` memory nodes. Other task types carry their
    gold as spans and map to ``()``."""
    by_task: dict[str, list[str]] = {}
    for n in nodes:
        if n.get("role") == "gold":
            by_task.setdefault(str(n["task_id"]), []).append(str(n["body"]))
    return {
        str(r["id"]): tuple(by_task.get(str(r["id"]), ()))
        for r in rows
        if r["type"] == "memory"
    }
