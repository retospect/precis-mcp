"""Build the retrieval-eval gold set for one quest (prod, read-only).

Slice 2 of ``docs/backlog/graph-gardener-value.md``: for each of the
quest's rubric questions, the chunks that answer it are gold. The builder
pulls the rubric questions (``meta.rubric_objectives`` axes when set,
else the title's ``Rubric:`` criteria), retrieves a candidate pool per
question — the union of both eval arms' outputs, top ``--pool`` — and asks
a labelling model to mark which candidates answer the question, writing
``labelled_by: model:<id>`` until Reto's review flips it to ``reto``.

    uv run python scripts/llm_eval/build_retrieval_gold.py --prod \
        --quest qu500066 --endpoint-url <url> --model <id>

Output lands in the gitignored ``scripts/llm_eval/gold_set/local/`` —
prod-derived text never enters the public repo. Read-only throughout:
``--prod`` goes through the ``agent_ro`` role, and the labelling model is
an endpoint call, not a write.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _prod_ro import add_db_args, store_ro

DEFAULT_POOL = 30


def _out_default(quest: str) -> str:
    return f"scripts/llm_eval/gold_set/local/retrieval_{quest}.jsonl"


def _quest_row(store: Any, quest: str) -> dict[str, Any]:
    """Resolve ``--quest`` (``qu<id>`` or bare ref_id) to its refs row."""
    ident = quest.strip()
    ref_id = int(ident[2:]) if ident.startswith("qu") else int(ident)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id, kind, title, meta FROM refs "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
    if row is None:
        raise SystemExit(f"no live ref {ref_id}")
    if str(row[1]) != "quest":
        raise SystemExit(f"ref {ref_id} is a {row[1]}, not a quest")
    return {"ref_id": int(row[0]), "title": str(row[2] or ""), "meta": row[3] or {}}


def rubric_questions(title: str, meta: dict[str, Any]) -> list[str]:
    """The quest's rubric questions: ``meta.rubric_objectives`` axes when
    set, else the title's ``Rubric:`` line split on ``·``.

    Quests carry their rubric two ways: the measured-axis override
    (``rubric_objectives`` — one ``key`` + ``sense`` per axis) and the
    prose ``Rubric:`` tail of ``refs.title`` (``·``-separated criteria, the
    shape qu500066 uses). Either source yields the eval's ``question``
    strings verbatim — a criterion reads as the retrieval query it asks.
    """
    raw = meta.get("rubric_objectives")
    if isinstance(raw, list) and raw:
        out = []
        for item in raw:
            if isinstance(item, dict) and str(item.get("key") or "").strip():
                key = str(item["key"]).strip()
                sense = str(item.get("sense") or "").strip()
                out.append(f"{key} {sense}".strip())
        if out:
            return out
    for line in (title or "").splitlines():
        if line.strip().lower().startswith("rubric:"):
            crit = line.split(":", 1)[1]
            return [c.strip() for c in crit.split("·") if c.strip()]
    raise SystemExit(
        "quest has no rubric: set meta.rubric_objectives or a 'Rubric:' "
        "line in its title"
    )


def _candidate_pool(
    store: Any, question: str, eye_hits: list[str], pool: int, embedder: Any
) -> list[tuple[str, str]]:
    """Both arms' union, top ``pool``: the search arm's hits first, then any
    chunk handles the quest's fisheye render surfaced that search missed.

    Returns ``(handle, text)`` pairs — the label prompt cites handles.
    """
    from precis.utils import handle_registry
    from precis.utils.embed_query import query_vec_for

    vec = query_vec_for(embedder, question, "hybrid")
    triples = store.chunks.search_chunks(
        q=question,
        query_vec=vec,
        limit=pool,
        card_kinds=("card_combined",),
    )
    by_handle: dict[str, str] = {}
    for block, ref, _s in triples:
        h = handle_registry.try_format(ref.kind, int(block.id), chunk=True)
        if h is not None:
            by_handle[h] = block.text or ""
    for h in eye_hits:
        if h in by_handle:
            continue
        parsed = handle_registry.parse(h)
        if parsed is None:
            continue
        text = _chunk_text(store, int(parsed[2]))
        if text is not None:
            by_handle[h] = text
    return [(h, t) for h, t in list(by_handle.items())[:pool]]


def _chunk_text(store: Any, chunk_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s AND retired_at IS NULL",
            (chunk_id,),
        ).fetchone()
    return None if row is None else str(row[0] or "")


def _label_prompt(question: str, candidates: list[tuple[str, str]]) -> str:
    parts = [
        "Rubric question for a research quest:",
        question,
        "",
        "Candidate passages, each tagged with its chunk handle:",
    ]
    for handle, text in candidates:
        excerpt = " ".join(text.split())[:1200]
        parts.append(f"\n[{handle}] {excerpt}")
    parts.append(
        "\nWhich of these passages answer the rubric question — could a "
        "reader cite the passage as evidence for or against the criterion? "
        "Reply with a JSON array of the chosen chunk handles and nothing "
        'else, e.g. ["pc12","pc34"]. If none answer it, reply [].'
    )
    return "\n".join(parts)


def _label_with_model(
    call: Any, question: str, candidates: list[tuple[str, str]]
) -> list[str]:
    """Ask the labelling model which candidates answer ``question``.

    Only handles that were actually in the pool count — a model-invented
    handle is dropped, never written to gold.
    """
    from precis.mesh_eval.scorers import _last_json

    res = call([{"role": "user", "content": _label_prompt(question, candidates)}], 512)
    if res.error:
        raise RuntimeError(f"labelling call failed: {res.error}")
    data = _last_json(res.text, (list,))
    if not isinstance(data, list):
        raise RuntimeError(f"labelling reply unparseable: {res.text[:200]!r}")
    pool = {h for h, _t in candidates}
    picked: list[str] = []
    for h in data:
        h = str(h)
        if h in pool and h not in picked:
            picked.append(h)
    return picked


def build_records(
    store: Any,
    quest_handle: str,
    questions: list[str],
    label: Any,
    *,
    labelled_by: str,
    pool: int = DEFAULT_POOL,
    embedder: Any = None,
    at: str | None = None,
) -> list[Any]:
    """The gold records for one quest: candidates → model labels → rows.

    ``label(question, candidates) -> [handles]`` is the model seam; tests
    inject a stub, ``main`` wires ``endpoint_caller``.
    """
    from precis.mesh_eval.retrieval import GoldRecord, extract_chunk_handles
    from precis.utils.eye_render import render_eye

    stamp = at or datetime.now(UTC).isoformat()
    eye_hits = extract_chunk_handles(render_eye(store, quest_handle, "fisheye+1hop"))
    records: list[GoldRecord] = []
    for q in questions:
        candidates = _candidate_pool(store, q, eye_hits, pool, embedder)
        gold = label(q, candidates)
        records.append(
            GoldRecord(
                question=q,
                quest=quest_handle,
                gold_chunks=tuple(gold),
                labelled_by=labelled_by,
                at=stamp,
            )
        )
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_db_args(ap)
    ap.add_argument("--quest", required=True, help="Quest handle (qu<id> or ref_id).")
    ap.add_argument(
        "--endpoint-url", required=True, help="Labelling endpoint base url."
    )
    ap.add_argument("--model", required=True, help="Labelling model id.")
    ap.add_argument(
        "--api-key-env",
        default=None,
        help="Env var holding the endpoint key (default: none/dummy).",
    )
    ap.add_argument("--pool", type=int, default=DEFAULT_POOL)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    import os

    from precis.mesh_eval.retrieval import dump_gold
    from precis.mesh_eval.runner import EndpointConfig, endpoint_caller

    api_key = os.environ.get(args.api_key_env, "dummy") if args.api_key_env else "dummy"
    call = endpoint_caller(
        EndpointConfig(base_url=args.endpoint_url, model=args.model, api_key=api_key)
    )

    store = store_ro(args)
    try:
        row = _quest_row(store, args.quest)
        quest_handle = f"qu{row['ref_id']}"
        questions = rubric_questions(row["title"], row["meta"])
        records = build_records(
            store,
            quest_handle,
            questions,
            lambda q, cands: _label_with_model(call, q, cands),
            labelled_by=f"model:{args.model}",
            pool=args.pool,
        )
    finally:
        store.close()

    out = args.out or _out_default(quest_handle)
    path = dump_gold(
        records,
        {
            "quest": quest_handle,
            "quest_title": row["title"].splitlines()[0] if row["title"] else "",
            "built_at": records[0].at if records else "",
            "source": "prod: rubric questions, both-arm candidate pool, "
            "model-drafted labels (awaiting reto review)",
            "pool": args.pool,
        },
        out,
    )
    print(f"wrote {len(records)} records -> {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
