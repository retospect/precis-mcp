"""Rung 6d-2 of the paper-writing pipeline — ``weave_section``: recompose a
dossier section from a *batch* of placed papers.

Design: ``docs/backlog/paper-writing-pipeline.md`` §"Integrate — the tick
body" step 2 (Weave, section-batch). "For each section with placed papers,
hand the model the section at fisheye+1hop + its papers' claims →
recompose (merge duplicates, one argument, transitions), mint citations,
link each paper --<disposition>--> dossier, log." The unit is
``(section, batch-of-placed-papers)``, **not** per-paper — per-paper
production degenerates into list-prose ("X found A. Y found B.").

This module is a pure callable — it does no logging (the tick, rung 6e,
logs the returned result to the quest logbook) and has no MCP surface.
Consumes rung 6a's placement (:mod:`precis.quest.placement`), rung 6c's
extractor (:mod:`precis.quest.claims`), and rung 6d-1's minter
(:mod:`precis.quest.citation_mint`).

**The woven body chunk.** The weave owns exactly one body chunk per
section — the direct child of the section heading marked
``meta.weave_body: True``. A re-weave finds and edits that same chunk
(``edit_text`` — chunk identity survives, so the R3 review ledger and
diff-since continuity carry over); a first weave appends a fresh one.
Any *other* (human-authored, unmarked) body chunk under the heading is
never touched. ``chunk_kind="paragraph"`` — the standard prose default
draft chunks already use (``handlers/draft.py``'s ``put`` default) — and
``split=False`` since the recomposed prose is one coherent unit, not a
sequence of independent paragraphs (mirrors ``add_figure``'s /
``_put_table``'s "derived projection, must not fragment" rationale).

**Dispositions** ride the rung-2 ``links`` edges (migration 0085):
``cited-in`` / ``corroborates`` mint a citation per claim used (rung
6d-1) and add the disposition edge; ``superseded-in`` adds the edge with
no citation (recorded, not separately woven); ``off-topic-for`` adds the
edge *and* drops the paper's ``topic:`` tag(s) that match the dossier's
own topics — the paper was considered and rejected, so it stops showing
up in the next tick's pending set (``unintegrated_papers``).
"""

from __future__ import annotations

import json
from typing import Any, cast

from precis.errors import BadInput, NotFound
from precis.quest.citation_mint import mint_citation
from precis.quest.claims import extract_claims, own_chunks
from precis.store.types import Relation
from precis.utils.eye_render import render_eye

_SYS = (
    "You compose a section of a living research review. Integrate the "
    "papers' claims into the EXISTING section prose as ONE coherent "
    "argument: merge duplicate points onto a single sentence citing "
    "multiple sources, group corroborating findings, surface "
    "contradictions, keep flow and transitions. Never a list ('X found "
    "A. Y found B.'). Cite with the given source handles inline."
)

_VALID_DISPOSITIONS: frozenset[str] = frozenset(
    {"cited-in", "corroborates", "superseded-in", "off-topic-for"}
)

#: Dispositions that weave a citation per claim used, in addition to the
#: disposition edge. The other two dispositions (``superseded-in`` /
#: ``off-topic-for``) add only the edge — see the module docstring.
_CITING_DISPOSITIONS: frozenset[str] = frozenset({"cited-in", "corroborates"})


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse ``text`` as a JSON *object*, tolerating surrounding prose.

    Mirrors ``precis.quest.claims._extract_json`` / ``workers.classify_topics``'s
    ``_extract_json``, but the weave's payload shape is an object
    (``{"section_text": ..., "papers": [...]}``), not an array — this looks
    for ``{`` / ``}`` instead of ``[`` / ``]``.
    """
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


def _fallback_text(store: Any, paper_ref_id: int, ref: Any | None) -> str:
    """A paper's ``card_abstract`` chunk text, else its ``refs.title`` —
    the composition input for a paper that yielded no claims."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'card_abstract' ORDER BY ord LIMIT 1",
            (paper_ref_id,),
        ).fetchone()
    if row is not None and row[0]:
        return str(row[0])
    title = getattr(ref, "title", None) if ref is not None else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return f"(no abstract or title for paper {paper_ref_id})"


def _ord_for_chunk_id(store: Any, chunk_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ord FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    assert row is not None, f"weave: chunk {chunk_id} vanished mid-call"
    return int(row[0])


def _find_woven_body(store: Any, heading_chunk_id: int) -> tuple[str, str] | None:
    """The ``(legacy handle, content_sha)`` of the heading's marked
    ``weave_body`` child, or ``None`` if this section has never been
    woven. The **legacy** ``¶`` handle (``chunks.handle``) — ``edit_text``
    keys on that, not the universal ``dc<id>`` display handle (mirrors
    ``quest.dossier``'s same note). The ``content_sha`` lets the caller
    pass ``base_sha=`` on re-weave so a concurrent edit isn't silently
    clobbered (rung 6d-2 review fix)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """SELECT handle, content_sha FROM chunks
                WHERE parent_chunk_id = %s AND retired_at IS NULL
                  AND meta->>'weave_body' = 'true'
                ORDER BY pos COLLATE "C" ASC LIMIT 1""",
            (heading_chunk_id,),
        ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def _drop_matching_topic_tags(
    store: Any, dossier_ref_id: int, paper_ref_id: int, *, conn: Any
) -> int:
    """Drop the paper's ``topic:<t>`` tag(s) that intersect the dossier's
    own ``topic:`` tags — the ``off-topic-for`` side effect (design:
    "off-topic-for also drops the topic tag"). Returns the count dropped."""
    dossier_topics = {
        str(t) for t in store.tags_for(dossier_ref_id) if str(t).startswith("topic:")
    }
    if not dossier_topics:
        return 0
    dropped = 0
    for t in store.tags_for(paper_ref_id):
        if str(t) in dossier_topics:
            store.remove_tag(paper_ref_id, t, conn=conn)
            dropped += 1
    return dropped


def _build_prompt(
    heading_title: str, section_context: str, papers: list[dict[str, Any]]
) -> str:
    lines = [
        f"Section: {heading_title}",
        "",
        "Current section context (fisheye+1hop):",
        section_context or "(empty)",
        "",
        "Papers placed in this section — integrate their claims:",
        "",
    ]
    for p in papers:
        lines.append(f"[{p['index']}] {p['title']} (ref_id={p['ref_id']})")
        if p["claims"]:
            for c in p["claims"]:
                lines.append(
                    f"  - {c['text']}  [source_handle={c['source_handle']}, "
                    f"source_ord={c['source_ord']}]"
                )
        else:
            lines.append("  (no extracted claims — abstract/title fallback)")
            lines.append(f"  {p['fallback_text']}")
        lines.append("")
    lines.append(
        'Return STRICT JSON: {"section_text": "<recomposed prose, inline '
        '[source_handle] markers>", "papers": [{"index": <int>, '
        '"disposition": "cited-in"|"corroborates"|"superseded-in"|'
        '"off-topic-for", "claims_used": [{"text": <str>, "source_handle": '
        '<str>, "source_ord": <int>}]}]}. No prose outside the JSON.'
    )
    return "\n".join(lines)


def _normalize_papers_out(
    raw: Any, papers_ctx: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Map the model's ``papers`` array (keyed by echoed ``index``) back
    onto the input papers, dropping anything malformed. A paper the model
    didn't address (missing index, or an unrecognized ``disposition``)
    comes back with ``disposition=None`` — no write happens for it; the
    weave never guesses a disposition the model didn't actually assert."""
    by_index: dict[int, dict[str, Any]] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int) and not isinstance(idx, bool):
                by_index[idx] = item

    out: list[dict[str, Any]] = []
    for p in papers_ctx:
        item = by_index.get(p["index"])
        disposition: str | None = None
        claims_used: list[dict[str, Any]] = []
        if item is not None:
            d = item.get("disposition")
            if isinstance(d, str) and d in _VALID_DISPOSITIONS:
                disposition = d
            raw_claims = item.get("claims_used")
            if isinstance(raw_claims, list):
                for c in raw_claims:
                    if not isinstance(c, dict):
                        continue
                    text = c.get("text")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    source_handle = c.get("source_handle")
                    source_ord = c.get("source_ord")
                    claims_used.append(
                        {
                            "text": text.strip(),
                            "source_handle": (
                                source_handle
                                if isinstance(source_handle, str)
                                else None
                            ),
                            "source_ord": (
                                source_ord
                                if isinstance(source_ord, int)
                                and not isinstance(source_ord, bool)
                                else None
                            ),
                        }
                    )
        out.append(
            {
                "ref_id": p["ref_id"],
                "disposition": disposition,
                "claims_used": claims_used,
            }
        )
    return out


def weave_section(
    store: Any,
    client: Any,
    dossier_ref_id: int,
    section_handle: str,
    paper_ref_ids: list[int],
    *,
    claims_client: Any | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Recompose ``section_handle`` (a dossier heading) from the papers
    placed there (rung 6a's output for this section).

    1. Renders the section at ``fisheye+1hop`` for prompt context.
    2. Extracts each paper's claims (rung 6c); a paper with none falls
       back to its abstract/title.
    3. Asks ``client`` to recompose the section as one argument + assign
       each paper a disposition (mints no citations itself — that's a
       parse, not a write).
    4. Unless ``dry_run``, applies: edits/creates the section's one
       ``weave_body`` chunk, mints a citation per claim used for
       ``cited-in``/``corroborates`` papers, and links every classified
       paper ``--<disposition>--> dossier`` at the section heading.

    Returns ``{"ok": False, "error": "unparseable", "applied": False}`` on
    unparseable model output (no writes). Otherwise ``{"ok": True,
    "applied": bool, "section_handle", "section_text"/"body_handle",
    "papers": [...], "citation_ids": [...], "section_text_len"}`` —
    ``dry_run=True`` omits ``body_handle``/``citation_ids`` (nothing was
    written) and each ``papers`` entry carries the proposed
    ``claims_used`` instead of ``citation_ids``.

    Raises ``NotFound`` if ``section_handle`` doesn't resolve to a live
    draft heading — a caller error (the placement rung already validated
    the section exists), not a model-output problem.

    Assumes the caller passes papers NOT yet dispositioned for this
    section (the tick batches only ``unintegrated_papers``); re-weaving an
    already-dispositioned paper is not idempotent for citations (a second
    ``cited-in`` pass mints a second citation) — resolved by the future
    claim-clustering dedup, so callers should not re-feed dispositioned
    papers today.
    """
    heading = store.get_draft_chunk(section_handle, kind="draft")
    if heading is None:
        raise NotFound(
            f"weave_section: unknown section handle {section_handle!r}",
            next=f"get(kind='draft', id={dossier_ref_id!r}, view='toc')",
        )

    refs_map = store.fetch_refs_by_ids(list(paper_ref_ids)) if paper_ref_ids else {}

    # ``ord`` -> {"handle", "text"} per paper, from that paper's OWN
    # own_chunks() — the ground truth a claims_used entry's source_ord is
    # checked against before minting (never trust the model's echoed
    # source_handle/source_ord alone; a multi-paper batch can cross-wire
    # them — rung 6d-2 review fix).
    excerpts: dict[int, dict[int, dict[str, str]]] = {}
    papers_ctx: list[dict[str, Any]] = []
    for i, pid in enumerate(paper_ref_ids):
        ref = refs_map.get(pid)
        title = getattr(ref, "title", None) if ref is not None else None
        title = (
            title.splitlines()[0][:200]
            if isinstance(title, str) and title.strip()
            else (f"(untitled paper {pid})")
        )
        claims = extract_claims(store, claims_client or client, pid)
        excerpts[pid] = {
            c["ord"]: {"handle": c["handle"], "text": c["text"]}
            for c in own_chunks(store, pid)
        }
        fallback_text = None if claims else _fallback_text(store, pid, ref)
        papers_ctx.append(
            {
                "index": i,
                "ref_id": pid,
                "title": title,
                "claims": claims,
                "fallback_text": fallback_text,
            }
        )

    section_context = render_eye(store, section_handle, "fisheye+1hop")
    prompt = _build_prompt(heading.text, section_context, papers_ctx)

    try:
        out = client.complete(
            [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": prompt},
            ]
        )
        parsed = _extract_json_object(out.text)
    except Exception:
        parsed = None

    if parsed is None or not isinstance(parsed.get("section_text"), str):
        return {"ok": False, "error": "unparseable", "applied": False}

    section_text = parsed["section_text"]
    if not section_text.strip():
        # An empty/whitespace-only section_text is a bad model turn, not a
        # deliberate "blank the section" instruction — applying it would
        # silently wipe a previously-woven body (rung 6d-2 review fix).
        return {"ok": False, "error": "empty_section_text", "applied": False}

    papers_out = _normalize_papers_out(parsed.get("papers"), papers_ctx)

    if dry_run:
        return {
            "ok": True,
            "applied": False,
            "section_handle": section_handle,
            "section_text": section_text,
            "papers": papers_out,
        }

    # ── apply ──────────────────────────────────────────────────────────
    found = _find_woven_body(store, heading.chunk_id)
    if found is not None:
        existing_handle, existing_sha = found
        try:
            body_chunk = store.edit_text(
                existing_handle,
                section_text,
                base_sha=existing_sha,
                source={"reason": "weave"},
                kind="draft",
            )
        except BadInput:
            # Optimistic-concurrency mismatch — a human (or another
            # weave) edited the body chunk since we read its sha. Bail
            # out before any per-paper write, rather than clobbering the
            # concurrent edit (rung 6d-2 review fix).
            return {
                "ok": False,
                "error": "conflict",
                "applied": False,
                "section_handle": section_handle,
            }
        assert body_chunk is not None
    else:
        created = store.add_chunks(
            ref_id=dossier_ref_id,
            chunk_kind="paragraph",
            text=section_text,
            at={"into": section_handle},
            meta={"weave_body": True},
            split=False,
            kind="draft",
        )
        body_chunk = created[0]
    body_handle = body_chunk.dc

    heading_ord = _ord_for_chunk_id(store, heading.chunk_id)

    result_papers: list[dict[str, Any]] = []
    all_citation_ids: list[int] = []
    for p in papers_out:
        pid = p["ref_id"]
        disposition = p["disposition"]
        if disposition is None:
            result_papers.append(
                {"ref_id": pid, "disposition": None, "citation_ids": []}
            )
            continue

        citation_ids: list[int] = []
        try:
            if disposition in _CITING_DISPOSITIONS:
                paper_excerpts = excerpts.get(pid, {})
                for cu in p["claims_used"]:
                    source_ord = cu["source_ord"]
                    info = (
                        paper_excerpts.get(source_ord)
                        if source_ord is not None
                        else None
                    )
                    if info is None:
                        # source_ord isn't among THIS paper's own excerpts —
                        # cross-paper or hallucinated attribution (a
                        # multi-paper batch can cross-wire the model's
                        # echoed source_ord/source_handle). Never mint a
                        # citation for it (rung 6d-2 review fix); use the
                        # verified own-chunk handle/text, not the model's
                        # echoed ones, for the ones we do mint.
                        continue
                    cid = mint_citation(
                        store,
                        claim=cu["text"],
                        paper_ref_id=pid,
                        source_handle=info["handle"],
                        source_quote=info["text"],
                        set_by="weave",
                    )
                    citation_ids.append(cid)
                with store.tx() as conn:
                    store.add_link(
                        src_ref_id=pid,
                        dst_ref_id=dossier_ref_id,
                        dst_pos=heading_ord,
                        relation=cast(Relation, disposition),
                        set_by="system",
                        conn=conn,
                    )
            elif disposition == "superseded-in":
                with store.tx() as conn:
                    store.add_link(
                        src_ref_id=pid,
                        dst_ref_id=dossier_ref_id,
                        dst_pos=heading_ord,
                        relation=cast(Relation, "superseded-in"),
                        set_by="system",
                        conn=conn,
                    )
            else:  # "off-topic-for"
                with store.tx() as conn:
                    store.add_link(
                        src_ref_id=pid,
                        dst_ref_id=dossier_ref_id,
                        dst_pos=heading_ord,
                        relation=cast(Relation, "off-topic-for"),
                        set_by="system",
                        conn=conn,
                    )
                    _drop_matching_topic_tags(store, dossier_ref_id, pid, conn=conn)
        except Exception as exc:
            # One paper's mint/link failure shouldn't sink the whole
            # batch — report it and keep processing the rest (rung 6d-2
            # review fix). Whatever citations minted before the failure
            # stay reported (and stay minted — not rolled back).
            all_citation_ids.extend(citation_ids)
            result_papers.append(
                {
                    "ref_id": pid,
                    "disposition": disposition,
                    "error": str(exc),
                    "citation_ids": citation_ids,
                }
            )
            continue

        all_citation_ids.extend(citation_ids)
        result_papers.append(
            {"ref_id": pid, "disposition": disposition, "citation_ids": citation_ids}
        )

    return {
        "ok": True,
        "applied": True,
        "section_handle": section_handle,
        "body_handle": body_handle,
        "papers": result_papers,
        "citation_ids": all_citation_ids,
        "section_text_len": len(section_text),
    }


__all__ = ["weave_section"]
