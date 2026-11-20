"""Quest lit-search consumes the general considered-verdict ledger
(gr662069, spec me626852).

``run_search_step`` holds out every source the quest has a valid
``not-useful`` ``considered`` verdict on: the exclusion is handed to the
search leg, the link loop never re-serves it, and the step's notes say how
many were held. Only ``not-useful`` excludes -- an ``uncertain`` verdict
still links normally. This replaces a private "seen, not linked" ledger
with the general mechanism.
"""

from __future__ import annotations

from precis.quest import search as qsearch
from precis.store import ChunkInsert, Store


def _paper(store: Store, slug: str) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Paper {slug}")
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=f"{slug} body text")])
    return ref.id


def _serving(store: Store, quest_id: int) -> set[int]:
    return {
        ln.src_ref_id
        for ln in store.links_for(quest_id, direction="in", relation="serves")
    }


def _setup(store: Store, verdict: str):
    quest = store.insert_ref(kind="quest", slug=None, title="the quest question").id
    p, q, r = (_paper(store, f"jo-{verdict}-{x}") for x in "pqr")
    store.add_link(
        src_ref_id=quest,
        dst_ref_id=p,
        relation="considered",
        meta={
            "verdict": verdict,
            "depth": "abstract",
            "reason": "lithium only, question is sodium",
        },
    )
    seen: list[list[int]] = []

    def search_fn(_store, _query, exclude):
        seen.append(list(exclude))
        return [(p, 1.0), (q, 0.9)]

    return quest, p, q, r, search_fn, seen


def test_not_useful_candidate_is_held_out(store: Store) -> None:
    quest, p, q, _r, search_fn, seen = _setup(store, "not-useful")
    assert qsearch.judged_out(store, quest) == {p}  # valid before the step

    step = qsearch.run_search_step(store, quest, ["sodium cells"], search_fn=search_fn)

    assert seen and p in seen[0]
    served = _serving(store, quest)
    assert q in served
    assert p not in served
    assert any(n.startswith("held out 1 considered-not-useful") for n in step.notes)


def test_verdict_stays_valid_after_the_step_logs(store: Store) -> None:
    """The step's own logbook entry is a quest body chunk; the scope pin
    (``precis_scope_sha``, migration 0324) leaves ``quest_log`` chunks out,
    so the verdict survives the tick that consumed it instead of going
    stale and re-serving the judged-out paper on the next run."""
    quest, p, _q, _r, search_fn, _seen = _setup(store, "not-useful")

    qsearch.run_search_step(store, quest, ["sodium cells"], search_fn=search_fn)

    assert qsearch.judged_out(store, quest) == {p}


def test_uncertain_verdict_does_not_exclude(store: Store) -> None:
    quest, p, q, _r, search_fn, seen = _setup(store, "uncertain")

    step = qsearch.run_search_step(store, quest, ["sodium cells"], search_fn=search_fn)

    assert not qsearch.judged_out(store, quest)
    assert seen and p not in seen[0]
    assert {p, q} <= _serving(store, quest)
    assert not any(n.startswith("held out") for n in step.notes)
