"""AC 5 of ``docs/backlog/memory-native-authoring.md``: native vs mirror recall.

The ``test_memory_recall_fixture.py`` topics and queries, seeded two ways
into a fresh test DB: as native ``put(kind='memory')`` nodes (no
``file_mirror`` stamp), and as topic files imported by
``cli/memory_mirror.py::import_mirror`` (frontmatter ``name:`` = title,
``description:`` = hook, plus the ``MEMORY.md`` index root it always
mints). Both are scored with the same ``render_memory_index`` call; native
must score the same or better. The printed ``RECALL native …/mirror …``
line is the baseline the spec's decisions log records.
"""

from __future__ import annotations

import json
from pathlib import Path

from precis.cli.memory import SPACE_TAG, render_memory_index, score_recall
from precis.cli.memory_mirror import import_mirror
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from tests.conftest import id_of
from tests.test_memory_recall_fixture import QUERIES, TOPICS, K

Topic = tuple[str, str, str, str]

#: The P4 question set (``src/precis/data/memory_recall_set.json``): real
#: recall queries, each with its prod answer node's handle and that node's
#: title + hook snapshot. Packaged in ``precis.data`` so the weekly
#: ``memory_recall`` cadence and ``precis memory index --set`` score the
#: same file.
RECALL_SET = json.loads(
    (
        Path(__file__).parents[1] / "src" / "precis" / "data" / "memory_recall_set.json"
    ).read_text(encoding="utf-8")
)
#: One-way floor for the question set at k=K per seed mode, measured
#: 2026-10-17 on the title + hook snapshots (3/36 each: the hash test
#: embedder has no semantics, so only queries whose every term is in the
#: snapshot hit). Raise it when a fix lands; never lower it.
#: 2026-10-26: 6/36 — the gr515867 prod title edits (query-verbatim
#: titles on me494842/me478000) synced into the snapshots lifted two
#: more entries over the hash scorer's term bar.
SET_FLOOR = {"mirror": 6, "native": 6}


def _set_topics() -> list[Topic]:
    """One topic per distinct answer node; the body is the hook (the
    snapshot carries no prod body text: the repo is public)."""
    seen: dict[str, Topic] = {}
    for e in RECALL_SET:
        seen.setdefault(e["want"], (e["want"], e["title"], e["hook"], e["hook"]))
    return list(seen.values())


def _distractors(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    for slug in ("worker_busy", "ssrf_guard", "torch_reembed"):
        body = next(b for s, _t, _h, b in TOPICS if s == slug)
        handler.put(text=body + " research view", title="Research echo " + slug)


def _seed_native(
    hub: Hub, _tmp_path: Path, topics: list[Topic] = TOPICS
) -> dict[str, int]:
    handler = MemoryHandler(hub=hub)
    ids = {
        slug: id_of(
            handler.put(
                text=body, title=title, tags=[SPACE_TAG], meta={"hook": hook}
            ).body
        )
        for slug, title, hook, body in topics
    }
    _distractors(hub)
    return ids


def _seed_mirror(
    hub: Hub, tmp_path: Path, topics: list[Topic] = TOPICS
) -> dict[str, int]:
    root = tmp_path / "memory"
    root.mkdir(parents=True)
    bullets = []
    for slug, title, hook, body in topics:
        # JSON strings are YAML double-quoted scalars: a hook with ': ' parses.
        name, desc = (json.dumps(s, ensure_ascii=False) for s in (title, hook))
        (root / f"{slug}.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: project\n"
            f"---\n{body}\n",
            encoding="utf-8",
        )
        bullets.append(f"- [{title}]({slug}.md) — {hook}\n")
    (root / "MEMORY.md").write_text(
        "# Memory index\n\n" + "".join(bullets), encoding="utf-8"
    )
    report = import_mirror(hub.live_store, root, namespace="recall")
    assert report.created == len(topics) + 1
    _distractors(hub)
    store: Store = hub.live_store
    with store.tx() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->'file_mirror'->>'filename' FROM refs "
            "WHERE kind='memory' AND retired_at IS NULL AND meta ? 'file_mirror'"
        ).fetchall()
    return {fname[: -len(".md")]: rid for rid, fname in rows if fname != "MEMORY.md"}


def _score(hub: Hub, store: Store, ids: dict[str, int]) -> tuple[int, list[str]]:
    hits, misses = 0, []
    for query, want in QUERIES:
        out = render_memory_index(store, q=query, k=K, embedder=hub.embedder)
        lines = out.splitlines()
        assert len(lines) <= K, (query, out)
        assert not any("Research echo" in ln for ln in lines), out
        handle = f"(me{ids[want]}"
        if any(handle + ")" in ln or handle + "," in ln for ln in lines):
            hits += 1
        else:
            misses.append(f"{query!r} -> {want}")
    return hits, misses


def _retire_all_memory(store: Store) -> None:
    with store.tx() as conn:
        rids = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='memory' AND retired_at IS NULL"
        ).fetchall()
        for (rid,) in rids:
            store.retire_ref(rid, conn=conn)


def test_recall_native_scores_at_least_mirror(
    hub: Hub, store: Store, tmp_path: Path
) -> None:
    # One DB, one seed at a time: the mirror set is retired before the
    # native set is written, so the two never compete for the top K.
    scores = {}
    for mode, seed in (("mirror", _seed_mirror), ("native", _seed_native)):
        hits, misses = _score(hub, store, seed(hub, tmp_path))
        scores[mode] = hits
        print(f"RECALL {mode}: {hits}/{len(QUERIES)} at k={K}; misses: {misses}")
        _retire_all_memory(store)
    assert scores["native"] >= scores["mirror"], scores


def test_recall_set_holds_its_floor(hub: Hub, store: Store, tmp_path: Path) -> None:
    # The question set's answer nodes plus the AC5 topics as competition,
    # seeded each way in turn; per-entry ranks print for the spec's table.
    topics = _set_topics() + list(TOPICS)
    scores = {}
    for mode, seed in (("mirror", _seed_mirror), ("native", _seed_native)):
        ids = seed(hub, tmp_path / mode, topics)
        entries = [
            {"query": e["query"], "want": f"me{ids[e['want']]}"} for e in RECALL_SET
        ]
        ranks = score_recall(store, entries, k=K, embedder=hub.embedder)
        for e, rank in zip(RECALL_SET, ranks, strict=True):
            print(f"SET {mode} {e['want']} rank={rank} {e['query']!r}")
        scores[mode] = sum(r is not None for r in ranks)
        print(f"RECALL SET {mode}: {scores[mode]}/{len(RECALL_SET)} at k={K}")
        _retire_all_memory(store)
    assert all(scores[m] >= SET_FLOOR[m] for m in SET_FLOOR), (scores, SET_FLOOR)


def test_meta_only_title_hook_edit_moves_recall(hub: Hub, store: Store) -> None:
    # The P4 fix path: edit(mode='replace', title=, meta={'hook'}) with no
    # text= rewrites the header card search reads and leaves the body alone.
    handler = MemoryHandler(hub=hub)
    rid = id_of(
        handler.put(
            text="Body about refs/stash in worktrees.",
            title="stash_slug",
            tags=[SPACE_TAG],
            meta={"hook": "old hook"},
        ).body
    )
    query = "set work aside to compare before and after"
    entry = [{"query": query, "want": f"me{rid}"}]
    assert score_recall(store, entry, k=K, embedder=hub.embedder) == [None]
    handler.edit(
        id=rid,
        mode="replace",
        title="Set work aside with a WIP commit, not git stash",
        meta={"hook": "to compare before and after, commit WIP or write a patch"},
    )
    assert score_recall(store, entry, k=K, embedder=hub.embedder) == [1]
    assert "refs/stash in worktrees" in handler.get(id=rid).body
