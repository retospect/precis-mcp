"""Recall number for ``precis memory index --q … --k 5``
(docs/backlog/memory-recall-walk-keep.md AC 5).

A synthetic ``SPACE:repo-dev`` memory set (24 topics shaped like harness
topic files, each with a mirror filename and a hook) plus research-space
distractors that share vocabulary. Ten task queries, one expected topic
each; the recall render must put the expected topic in its top 5 at least
8 times out of 10. Lexical leg + the hash-based test embedder (no stored vectors), so the
number is a floor for the real hybrid search, not a semantic benchmark.
"""

from __future__ import annotations

import pytest

from precis.cli.memory import SPACE_TAG, render_memory_index
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from tests.conftest import id_of

K = 5
PASS_AT = 8

# slug, title, hook, body
TOPICS = [
    (
        "worker_busy",
        "Worker busy versus starved",
        "no runner lines is not dead",
        "A worker that logs no runner lines may be busy on a long job, not dead. "
        "Check the heartbeat table before restarting the worker.",
    ),
    (
        "gate_slots",
        "Gate slot congestion",
        "queue wait burns agent quota",
        "When the local gate semaphore is congested, ship through the remote "
        "check instead of waiting for a slot.",
    ),
    (
        "pgbouncer_session",
        "Pgbouncer transaction pooling",
        "never SET at session level",
        "Pgbouncer pools by transaction, so a session level SET sticks to a "
        "shared connection and breaks other clients.",
    ),
    (
        "timestamps_utc",
        "Timestamps are UTC",
        "label every timestamp Z",
        "Use datetime.now(UTC); never utcnow or date.today. Label timestamps "
        "with a trailing Z.",
    ),
    (
        "migration_sealed",
        "Sealed migrations",
        "ship a new migration, never edit",
        "A sealed migration is forward-only. Never edit it; add a new numbered "
        "migration file instead.",
    ),
    (
        "chunks_append_only",
        "Chunk rows are append-only",
        "delete and insert, never update",
        "Body chunks rows are never updated in place; replace with delete plus "
        "insert so embeddings re-derive.",
    ),
    (
        "ssrf_guard",
        "SSRF guard for fetches",
        "agent URLs go through safe_get",
        "Any URL supplied by an agent must be fetched through safe_get or "
        "safe_stream to block private address ranges.",
    ),
    (
        "docker_wedge",
        "Docker inspect wedge",
        "docker inspect can hang forever",
        "A wedged docker daemon makes docker inspect hang forever; use a "
        "timeout and avoid killing gate containers.",
    ),
    (
        "psqlrc_pollution",
        "psqlrc pollutes scripted psql",
        "scripted psql needs -X",
        "A personal psqlrc injects output into scripted psql runs. Always pass "
        "the -X flag to skip it.",
    ),
    (
        "torch_reembed",
        "Torch bump triggers re-embed",
        "dependency bump means full re-embed",
        "Upgrading torch changes embedding numerics, which forces a complete "
        "re-embed of every chunk.",
    ),
    (
        "tailnet_addresses",
        "No cluster addresses in the repo",
        "the repository is public",
        "The repository is public, so tailnet and LAN addresses must never "
        "appear in tracked files; use the gitignored overlay.",
    ),
    (
        "uid_gid_parity",
        "uid and gid parity",
        "gid equals uid on every node",
        "Cluster accounts keep gid equal to uid with no exceptions, otherwise "
        "shared volume permissions break.",
    ),
    (
        "oauth_false_expiry",
        "OAuth false expiry",
        "slow not-logged-in is not expiry",
        "A slow not logged in message is not a token expiry; do not re-mint "
        "the OAuth token.",
    ),
    (
        "mutate_false_survivor",
        "Mutation false survivors",
        "a survivor is a lead only",
        "A surviving mutant is a lead, not proof of a weak test; check the "
        "mutate diff runbook before acting.",
    ),
    (
        "pcb_route_moves",
        "PCB route moves parts",
        "route moves unfrozen parts",
        "The pcb route operation relocates unfrozen parts; freeze the "
        "placement first and never test on a board a user owns.",
    ),
    (
        "skill_toc_fetch",
        "Skill table of contents fetch",
        "toc then tilde N",
        "A paginated skill is read through its toc first, then a numbered "
        "section selector.",
    ),
    (
        "stale_dev_image",
        "Stale dev image",
        "check source drift in status",
        "A stale dev image shows up as source drift in the status skill; "
        "rebuild the image before debugging.",
    ),
    (
        "ship_index_race",
        "Concurrent ship index race",
        "parallel squash merges share the index",
        "Concurrent squash merges co-mingle the shared git index; serialise "
        "ships through the lock.",
    ),
    (
        "embedder_dim",
        "Embedder dimension mismatch",
        "vector dim must match the column",
        "The embedder output dimension must equal the vector column width, "
        "else inserts fail with a dimension error.",
    ),
    (
        "discord_bridge",
        "Discord bridge restarts",
        "bot reconnect needs a fresh token",
        "The Discord bridge bot reconnects with a fresh gateway token after "
        "every restart.",
    ),
    (
        "spend_limit_parks",
        "Spend limit parks todos",
        "delete the ref_tag to unpark",
        "A hit spend limit parks todos behind a ref tag; deleting that tag "
        "un-parks them.",
    ),
    (
        "node_deploy_window",
        "No deploy window during backups",
        "03:00 to 04:20 UTC is blocked",
        "The nightly pg_dump blocks ADD COLUMN, so never deploy between three "
        "and four twenty UTC.",
    ),
    (
        "hexfold_check",
        "Hexfold check is not geometry",
        "passing check is not clean geometry",
        "A passing hexfold check does not prove the folded geometry is clean; "
        "inspect the mesh.",
    ),
    (
        "worktree_path_trap",
        "Worktree path trap",
        "agents return main paths, re-prefix",
        "Explore agents return paths in the main checkout; re-prefix them with "
        "the worktree before editing.",
    ),
]

# (task query, expected slug). The lexical leg is a term-AND and the test
# embedder is hash-based (no semantics), so each query is a short task phrase
# whose every word occurs in the expected body, not the title verbatim.
QUERIES = [
    ("worker logs no runner lines, busy or dead", "worker_busy"),
    ("gate semaphore congested, wait for a slot", "gate_slots"),
    ("pgbouncer session SET sticks to shared connection", "pgbouncer_session"),
    ("label timestamps with Z, use datetime", "timestamps_utc"),
    ("edit a sealed migration", "migration_sealed"),
    ("update body chunks in place, embeddings re-derive", "chunks_append_only"),
    ("fetch a URL supplied by an agent", "ssrf_guard"),
    ("docker inspect hang, avoid killing gate containers", "docker_wedge"),
    ("torch upgrade changes embedding numerics", "torch_reembed"),
    ("deploy while the nightly pg_dump blocks ADD COLUMN", "node_deploy_window"),
]


def _seed(hub: Hub) -> dict[str, int]:
    store: Store = hub.live_store
    handler = MemoryHandler(hub=hub)
    ids: dict[str, int] = {}
    for slug, title, hook, body in TOPICS:
        out = handler.put(text=body, title=title, tags=[SPACE_TAG], meta={"hook": hook})
        ref_id = id_of(out.body)
        store.stamp_ref_meta(ref_id, {"file_mirror": {"filename": f"{slug}.md"}})
        ids[slug] = ref_id
    # Research-space distractors reuse the vocabulary but must never surface.
    for slug in ("worker_busy", "ssrf_guard", "torch_reembed"):
        body = next(b for s, _t, _h, b in TOPICS if s == slug)
        handler.put(text=body + " research view", title="Research echo " + slug)
    return ids


def test_recall_fixture_hits_at_least_eight_of_ten(hub: Hub, store: Store) -> None:
    ids = _seed(hub)
    hits = 0
    misses: list[str] = []
    for query, want in QUERIES:
        out = render_memory_index(store, q=query, k=K, embedder=hub.embedder)
        lines = out.splitlines()
        assert len(lines) <= K, (query, out)
        assert out.startswith("no memory") or all(ln.startswith("- ") for ln in lines)
        assert not any("Research echo" in ln for ln in lines), out
        if any(f"(me{ids[want]}, {want}.md)" in ln for ln in lines):
            hits += 1
        else:
            misses.append(f"{query!r} -> {want}")
    print(f"RECALL SCORE: {hits}/{len(QUERIES)} at k={K}; misses: {misses}")
    assert hits >= PASS_AT, f"recall {hits}/{len(QUERIES)} < {PASS_AT}: {misses}"


def test_recall_lines_equal_the_handler_index_view(hub: Hub, store: Store) -> None:
    _seed(hub)
    query = QUERIES[0][0]
    want = (
        MemoryHandler(hub=hub)
        .search(q=query, tags=[SPACE_TAG], page_size=K, view="index")
        .body
    )
    got = render_memory_index(store, q=query, k=K, embedder=hub.embedder)
    assert got == want + "\n"


@pytest.mark.parametrize("k", [1, 3])
def test_recall_k_bounds_the_line_count(hub: Hub, store: Store, k: int) -> None:
    _seed(hub)
    out = render_memory_index(store, q="worker", k=k, embedder=hub.embedder)
    assert len(out.splitlines()) <= k
