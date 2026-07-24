"""Rung 6e-1 of the paper-writing pipeline — ``weave_tick``: the runnable
loop over a topic dossier (docs/design/paper-writing-pipeline.md
§"Integrate — the tick body" + §"Make/Maintain, one loop").

Composes the already-shipped substrate — placement (rung 6a, :mod:`precis.
quest.placement`), residual clustering (rung 6b, :mod:`precis.quest.
residual_cluster`), ``weave_section`` (rung 6d-2, :mod:`precis.quest.weave`)
— into the one callable a manual driver (``precis quest weave <qid>``) or,
later, the autonomous coordinator (rung 6e-2, not built here) can call. This
slice adds exactly one new model prompt of its own — the residual cluster's
section-title judgment — everything else is orchestration over calls that
already do their own writes/validation.

**Maintain vs. Make** (§"Make/Maintain, one loop"): a paper placed into an
EXISTING section is the Maintain leg — weave it in directly. A paper that
clears no section's floor is Make territory — residual clustering proposes
brand-new sections, each scaffolded then woven the same way. One tick does
both legs; nothing here decides which dominates, that's the coordinator's
call in 6e-2.

**Top-1 only.** :func:`~precis.quest.placement.place_papers` allows
multi-place (a paper can clear more than one section's floor), but a single
tick weaves only each paper's FIRST (highest-scoring) placement — giving a
paper two dispositions in one tick would trip ``weave_section``'s own
non-idempotent-citation warning for a re-fed paper. Full multi-place across
sections is a v1 refinement, not this slice's.

**``max_sections``** bounds how many section batches (Maintain + newly
scaffolded Make sections, combined) this one tick will actually weave — a
cost/latency valve for a future caller with a batch budget. A section left
unprocessed by the cap simply isn't dispositioned this tick, so its papers
stay in ``unintegrated_papers`` (Maintain) or ``residual_unplaced`` (Make)
for the next one — no state is lost, only deferred.
"""

from __future__ import annotations

import json
from typing import Any

from precis.quest.dossier import dossier_ref_id
from precis.quest.logbook import append_entry
from precis.quest.placement import place_papers, residual_paper_ids
from precis.quest.residual_cluster import cluster_residual
from precis.quest.weave import weave_section
from precis.utils import handle_registry

_TOPIC_PREFIX = "topic:"

_TITLE_SYS = (
    "You name sections for a living research review. Given a cluster of "
    "papers that didn't fit any existing section, propose ONE short, plain "
    "section title (a few words — no colons, no restating 'section')."
)


def _dossier_topics(store: Any, did: int) -> list[str]:
    """The dossier draft's own ``topic:<t>`` tags, as bare ``<t>`` slugs.

    Mirrors ``precis.handlers._integration_view._dossier_topics`` — that
    helper reads off a ``Ref`` object; this one takes the bare dossier ref
    id the tick already has in hand, so it isn't worth sharing the helper
    across a store/handlers layering boundary.
    """
    topics: list[str] = []
    for t in store.tags_for(did):
        s = str(t)
        if s.startswith(_TOPIC_PREFIX):
            topics.append(s[len(_TOPIC_PREFIX) :])
    return sorted(set(topics))


def _extract_title_json(text: str) -> dict[str, Any] | None:
    """Parse ``text`` as a JSON object, tolerating surrounding prose.

    Mirrors ``precis.quest.weave._extract_json_object`` / ``precis.quest.
    claims._extract_json``'s tolerant parse-or-``None`` shape (a JSON
    *object* payload here, ``{"title": ...}``) — kept as its own copy
    rather than reaching into another module's private helper, same
    call each of those two modules already made about each other.
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


def _judge_section_title(
    client: Any, label: list[str], exemplar_titles: list[str]
) -> str:
    """Ask ``client`` for a short section title for one residual cluster's
    digest (the digest — keyword ``label`` + capped ``exemplar_titles`` —
    never raw member titles beyond what :func:`~precis.quest.
    residual_cluster.cluster_residual` already capped).

    Falls back to a keyword-joined label (or a bare "New section") on any
    parse failure, empty reply, or client exception — a bad title-judgment
    turn must not sink the whole Make leg, only degrade its section name.
    """
    fallback = ", ".join(label) or "New section"
    prompt = (
        "Cluster keywords: "
        + (", ".join(label) or "(none)")
        + "\n\nExemplar paper titles:\n"
        + "\n".join(f"- {t}" for t in exemplar_titles)
        + "\n\n"
        'Return STRICT JSON: {"title": "<short section title>"}. No prose '
        "outside the JSON."
    )
    try:
        out = client.complete(
            [
                {"role": "system", "content": _TITLE_SYS},
                {"role": "user", "content": prompt},
            ]
        )
        parsed = _extract_title_json(out.text)
    except Exception:
        return fallback
    title = parsed.get("title") if parsed is not None else None
    return title.strip() if isinstance(title, str) and title.strip() else fallback


def weave_tick(
    store: Any,
    client: Any,
    quest_id: int,
    *,
    claims_client: Any | None = None,
    dry_run: bool = False,
    max_sections: int | None = None,
) -> dict[str, Any]:
    """Run one weave tick against ``quest_id``'s topic dossier.

    1. Resolves the dossier (``no_dossier`` if the quest has none) and its
       ``topic:`` tags (``no_topics`` if there are none — nothing to
       integrate).
    2. Batches every ``unintegrated_papers`` — tagged with one of the
       dossier's topics, no disposition edge yet.
    3. Places the batch (rung 6a); groups each paper by its **top-1**
       section (Maintain) and separately collects the residual (rung 6a's
       ``residual_paper_ids``).
    4. Weaves each Maintain-leg section batch via ``weave_section``.
    5. Clusters the residual (rung 6b); for each cluster, judges a section
       title, scaffolds it (unless ``dry_run``), and weaves it too (Make).
    6. Unless ``dry_run``, logs one ``result`` logbook entry summarizing
       the tick.

    A ``weave_section`` call returning ``ok=False`` (conflict, unparseable
    model output) is recorded in ``woven``, not raised — one bad section
    doesn't abort the tick. ``max_sections`` (see module docstring) caps
    how many section batches — Maintain + newly-scaffolded Make — this
    call actually weaves; anything past the cap is simply left for the
    next tick.
    """
    did = dossier_ref_id(store, quest_id)
    if did is None:
        return {"ok": False, "error": "no_dossier"}

    topics = _dossier_topics(store, did)
    if not topics:
        return {"ok": False, "error": "no_topics", "did": did}

    batch = [p["paper_ref_id"] for p in store.unintegrated_papers(did, topics)]
    if not batch:
        return {
            "ok": True,
            "applied": not dry_run,
            "woven": [],
            "note": "nothing unintegrated",
        }

    placements = place_papers(store, did, batch)

    # ── Maintain leg: group each paper by its top-1 placement only ──────
    # ``place_papers``' own ``handle`` field is the *legacy* ``¶`` anchor
    # (test_placement.py's docstring note) — ``weave_section`` in turn
    # calls ``render_eye``, which only resolves the universal ``dc<id>``
    # handle (ADR 0036), so this rebuilds it from ``section_chunk_id``
    # rather than passing the legacy one through.
    sections: dict[int, dict[str, Any]] = {}
    for pid, rows in placements.items():
        if not rows:
            continue
        top = rows[0]
        handle = handle_registry.format_handle(
            "draft", top["section_chunk_id"], chunk=True
        )
        bucket = sections.setdefault(
            top["section_chunk_id"], {"handle": handle, "pids": []}
        )
        bucket["pids"].append(pid)

    woven: list[dict[str, Any]] = []
    processed = 0

    def _budget_ok() -> bool:
        return max_sections is None or processed < max_sections

    for bucket in sections.values():
        if not _budget_ok():
            break
        woven.append(
            weave_section(
                store,
                client,
                did,
                bucket["handle"],
                bucket["pids"],
                claims_client=claims_client,
                dry_run=dry_run,
            )
        )
        processed += 1

    # ── Make/residual leg ────────────────────────────────────────────────
    residual = residual_paper_ids(placements)
    new_sections: list[dict[str, Any]] = []
    clustered_ids: set[int] = set()
    if residual:
        for digest in cluster_residual(store, residual):
            if not _budget_ok():
                break
            clustered_ids.update(digest["paper_ref_ids"])
            title = _judge_section_title(
                client, digest.get("label") or [], digest.get("exemplar_titles") or []
            )
            if dry_run:
                new_sections.append(
                    {
                        "title": title,
                        "handle": None,
                        "paper_ref_ids": digest["paper_ref_ids"],
                    }
                )
                continue
            handle = store.scaffold_sections(did, [(title, "sci-survey-section")])[0]
            woven.append(
                weave_section(
                    store,
                    client,
                    did,
                    handle,
                    digest["paper_ref_ids"],
                    claims_client=claims_client,
                    dry_run=dry_run,
                )
            )
            new_sections.append(
                {
                    "title": title,
                    "handle": handle,
                    "paper_ref_ids": digest["paper_ref_ids"],
                }
            )
            processed += 1

    residual_unplaced = [pid for pid in residual if pid not in clustered_ids]

    log_entry: int | None = None
    if not dry_run:
        papers_by_disposition: dict[str, int] = {}
        citations_minted = 0
        for r in woven:
            if not r.get("ok"):
                continue
            for p in r.get("papers", []):
                disp = p.get("disposition")
                if disp:
                    papers_by_disposition[disp] = papers_by_disposition.get(disp, 0) + 1
                citations_minted += len(p.get("citation_ids") or [])
        dispositioned = sum(papers_by_disposition.values())
        log_entry = append_entry(
            store,
            quest_id,
            text=(
                f"Weave tick: {len(sections)} section(s) touched, "
                f"{len(new_sections)} new section(s), {dispositioned} paper(s) "
                f"dispositioned ({papers_by_disposition}), {citations_minted} "
                f"citation(s) minted, {len(residual_unplaced)} residual unplaced."
            ),
            entry_type="result",
            by="system",
        )

    return {
        "ok": True,
        "applied": not dry_run,
        "did": did,
        "topics": topics,
        "batch_size": len(batch),
        "woven": woven,
        "new_sections": new_sections,
        "residual_unplaced": residual_unplaced,
        "log_entry": log_entry,
    }


__all__ = ["weave_tick"]
