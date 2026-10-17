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

from pathlib import Path

from precis.cli.memory import SPACE_TAG, render_memory_index
from precis.cli.memory_mirror import import_mirror
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from tests.conftest import id_of
from tests.test_memory_recall_fixture import QUERIES, TOPICS, K


def _distractors(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    for slug in ("worker_busy", "ssrf_guard", "torch_reembed"):
        body = next(b for s, _t, _h, b in TOPICS if s == slug)
        handler.put(text=body + " research view", title="Research echo " + slug)


def _seed_native(hub: Hub, _tmp_path: Path) -> dict[str, int]:
    handler = MemoryHandler(hub=hub)
    ids = {
        slug: id_of(
            handler.put(
                text=body, title=title, tags=[SPACE_TAG], meta={"hook": hook}
            ).body
        )
        for slug, title, hook, body in TOPICS
    }
    _distractors(hub)
    return ids


def _seed_mirror(hub: Hub, tmp_path: Path) -> dict[str, int]:
    root = tmp_path / "memory"
    root.mkdir()
    bullets = []
    for slug, title, hook, body in TOPICS:
        (root / f"{slug}.md").write_text(
            f"---\nname: {title}\ndescription: {hook}\nmetadata:\n  type: project\n---\n"
            f"{body}\n",
            encoding="utf-8",
        )
        bullets.append(f"- [{title}]({slug}.md) — {hook}\n")
    (root / "MEMORY.md").write_text(
        "# Memory index\n\n" + "".join(bullets), encoding="utf-8"
    )
    report = import_mirror(hub.live_store, root, namespace="recall")
    assert report.created == len(TOPICS) + 1
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
