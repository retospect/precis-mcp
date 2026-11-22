"""``user_secret:<handle>`` is read-gated in the store; ``user:<handle>`` is not (gr556002)."""

from __future__ import annotations

import logging

import pytest

from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers.todo import TodoHandler
from precis.handlers.web import WebHandler
from precis.store import Store, Tag
from precis.store._secret_scope import ADMIN_ENV, viewer_handle
from precis.store.revision_context import revision_context
from precis.store.types import ChunkInsert
from precis.todo import work_gripes, work_quest


@pytest.fixture
def refs(store: Store) -> dict[str, int]:
    out = {
        "secret": store.insert_ref(kind="memory", slug=None, title="alice diary").id,
        "shown": store.insert_ref(kind="memory", slug=None, title="alice display").id,
        "plain": store.insert_ref(kind="memory", slug=None, title="plain note").id,
    }
    for rid in out.values():
        store.chunks.insert_chunks(
            rid, [ChunkInsert(ord=0, text="alice quokka diary entry")]
        )
    store.add_tag(out["secret"], Tag.open("user_secret:alice"))
    store.add_tag(out["shown"], Tag.open("user:alice"))
    store.add_link(
        src_ref_id=out["secret"], dst_ref_id=out["plain"], relation="related-to"
    )
    store.add_link(
        src_ref_id=out["shown"], dst_ref_id=out["plain"], relation="related-to"
    )
    return out


def _get(store: Store, ref_id: int) -> object:
    return store.get_ref(kind="memory", id=ref_id)


def test_owner_reads_secret(store: Store, refs: dict[str, int]) -> None:
    with revision_context(actor="user:alice"):
        assert viewer_handle() == "alice"
        assert _get(store, refs["secret"]) is not None


@pytest.mark.parametrize("actor", [None, "user:bob", "agent"])
def test_stranger_cannot_read_secret(
    store: Store, refs: dict[str, int], actor: str | None
) -> None:
    with revision_context(actor=actor):
        assert _get(store, refs["secret"]) is None
        assert store.fetch_refs_by_ids([refs["secret"], refs["plain"]]).keys() == {
            refs["plain"]
        }
        assert _get(store, refs["shown"]) is not None  # display tag gates nothing
        found = store.chunks.search_chunks_lexical(q="quokka")
        assert {r.id for _c, r, _s in found} == {refs["shown"], refs["plain"]}
        titled = store.search_refs_lexical(q="alice")
        assert refs["secret"] not in {t[0].id for t in titled}
        assert refs["secret"] not in {r.id for r in store.list_refs(kind="memory")}
        assert store.count_refs(kind="memory") == 2
        links = store.links_for(refs["plain"])
        assert [lk.src_ref_id for lk in links] == [refs["shown"]]
        assert store.count_links_for_refs([refs["plain"]]) == {refs["plain"]: 1}
        nbh = store.neighbourhood("memory", refs["plain"])
        assert {n["id"] for n in nbh["nodes"]} == {refs["shown"]}


def test_admin_reads_past_gate_and_is_logged(
    store: Store,
    refs: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(ADMIN_ENV, "reto")
    with caplog.at_level(logging.WARNING, logger="precis.secret_scope"):
        with revision_context(actor="user:reto"):
            assert _get(store, refs["secret"]) is not None
    assert any("admin read" in r.getMessage() for r in caplog.records)


def test_hostile_handle_is_not_a_viewer() -> None:
    with revision_context(actor="user:x' OR '1'='1"):
        assert viewer_handle() is None


def test_secondary_read_paths_are_fenced(store: Store, refs: dict[str, int]) -> None:
    """The folder picker and metadata probes honour the fence."""
    sid = refs["secret"]
    store.add_tag(sid, Tag.open("zz-probe"))
    folder = store.insert_ref(kind="folder", slug=None, title="alice folder").id
    store.add_tag(folder, Tag.open("user_secret:alice"))
    with revision_context(actor="user:bob"):
        assert folder not in {f[0] for f in store.list_folders()}
        assert store.find_first_meta_for_open_tag(kind="memory", tag="zz-probe") is None
    with revision_context(actor="user:alice"):
        assert folder in {f[0] for f in store.list_folders()}
        assert (
            store.find_first_meta_for_open_tag(kind="memory", tag="zz-probe")
            is not None
        )


def test_board_helpers_fence_secret_refs(store: Store, refs: dict[str, int]) -> None:
    """``open_blocked_by`` / ``_live_claims`` don't name or flag a secret ref."""
    from datetime import UTC, datetime, timedelta

    from precis.todo.blocked import open_blocked_by
    from precis.todo.work_view import _live_claims

    todo = store.insert_ref(kind="todo", slug=None, title="visible todo").id
    store.add_link(src_ref_id=todo, dst_ref_id=refs["secret"], relation="blocked-by")
    store.add_tag(
        refs["secret"],
        Tag.open("claimed-by:lane-test"),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with revision_context(actor="user:bob"):
        # The link row is fenced out entirely: the secret target reads as
        # absent, so the visible todo shows as unblocked rather than naming it.
        assert open_blocked_by(store, [todo]) == {}
        assert _live_claims(store, [refs["secret"], todo]) == set()
    with revision_context(actor="user:alice"):
        assert open_blocked_by(store, [todo]) == {todo: (("memory", refs["secret"]),)}
        assert _live_claims(store, [refs["secret"], todo]) == {refs["secret"]}


def test_private_cache_fill_preserves_entry(
    store: Store, refs: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = WebHandler(hub=Hub(store=store))
    query = "https://example.com/private-entry"
    key = handler._canonical_key(query)
    slug = handler._slug_for(key)
    request_hash = handler._hash(key)
    ref, _ = store.put_cache_entry(
        kind="web",
        slug=slug,
        provider="web",
        request_hash=request_hash,
        title="private entry",
        body_blocks=[ChunkInsert(ord=0, text="private original body")],
        ttl_seconds=None,
    )
    store.add_tag(ref.id, Tag.open("user_secret:alice"))
    store.add_link(src_ref_id=ref.id, dst_ref_id=refs["plain"], relation="related-to")

    def unexpected_fetch(key: str) -> None:
        pytest.fail(f"unexpected provider request for {key}")

    monkeypatch.setattr(handler, "_fetch_guarded", unexpected_fetch)
    with revision_context(actor="user:bob"):
        assert store.count_cache_entries(kind="web") == 0
        assert store.list_cache_entries(kind="web") == []
        with pytest.raises(NotFound, match="cache key is unavailable"):
            handler.get(id=query)
        with pytest.raises(NotFound, match="cache key is unavailable"):
            store.put_cache_entry(
                kind="web",
                slug=slug,
                provider="web",
                request_hash=request_hash,
                title="replacement",
                body_blocks=[ChunkInsert(ord=0, text="replacement body")],
                ttl_seconds=None,
            )

    with revision_context(actor="user:alice"):
        assert store.count_cache_entries(kind="web") == 1
        assert [row[0].id for row in store.list_cache_entries(kind="web")] == [ref.id]
        assert [chunk.text for chunk in store.chunks.list_chunks_for_ref(ref.id)] == [
            "private original body"
        ]
        assert Tag.open("user_secret:alice") in store.tags_for(ref.id)
        assert [link.dst_ref_id for link in store.links_for(ref.id)] == [refs["plain"]]


def test_tag_discovery_counts_only_visible_attachments(
    store: Store, refs: dict[str, int]
) -> None:
    common = Tag.open("private-probe:shared")
    hidden_only = Tag.open("private-probe:hidden")
    store.add_tag(refs["plain"], common)
    baseline = store.tag_metadata(namespace="OPEN", value=common.value)
    assert baseline is not None
    store.add_tag(refs["secret"], common)
    store.add_tag(refs["secret"], common, pos=0)
    store.add_tag(refs["secret"], hidden_only)
    with revision_context(actor="user:alice"):
        assert ("OPEN", common.value, 3) in store.list_all_tags()
        assert ("OPEN", common.value, 2) in store.list_all_tags(kind="memory")
    with revision_context(actor="user:bob"):
        assert store.tag_metadata(namespace="OPEN", value=common.value) == baseline
        assert store.tag_metadata(namespace="OPEN", value=hidden_only.value) is None
        for rows in (
            store.list_all_tags(),
            store.list_all_tags(kind="memory"),
            store.search_tags_lexical(q="private-probe"),
            store.suggest_tags("private-probe"),
        ):
            assert ("OPEN", common.value, 1) in rows
            assert hidden_only.value not in {value for _, value, _ in rows}


def test_paper_counts_passage_and_doi_prefix_follow_visibility(store: Store) -> None:
    prefix = "10.1234/secret-gate"
    hidden = store.insert_ref(kind="paper", slug="hidden-paper", title="Hidden paper")
    visible = store.insert_ref(
        kind="paper", slug="visible-paper", title="Visible paper"
    )
    for ref in (hidden, visible):
        store.insert_ref_identifiers(ref.id, [("doi", f"{prefix}-{ref.id}", "test")])
        store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="quokka passage")])
    chunk = store.chunks.list_chunks_for_ref(hidden.id)[0]
    store.add_tag(hidden.id, Tag.open("user_secret:alice"))
    for actor, expected_count in (("user:bob", 1), ("user:alice", 2)):
        with revision_context(actor=actor):
            assert (
                store.chunks.count_chunks_lexical(q="quokka", kind="paper")
                == expected_count
            )
            assert (
                store.chunks.count_paper_yearless_matches(q="quokka") == expected_count
            )
            assert store.chunks.count_chunks_for_kind("paper") == expected_count
            first = hidden.id if actor == "user:alice" else visible.id
            assert store.find_paper_ref_ids_by_doi_prefix(prefix, limit=1) == [first]
            expected = "quokka passage" if actor == "user:alice" else None
            assert store.chunks.chunk_text_by_id(chunk.id) == expected
            assert store.drafts.chunk_text_at(hidden.id, 0) == expected
            handle = f"pc{chunk.id}"
            assert (store.drafts.universal_chunk(handle) is not None) == bool(expected)
            assert (handle in store.drafts.universal_chunks([handle])) == bool(expected)


@pytest.mark.parametrize(
    ("view", "tags", "meta"),
    [
        ("flat", (), {}),
        ("index", (), {}),
        ("active", (), {}),
        ("doable", (), {}),
        ("waiting", ("waiting-for:review",), {}),
        ("blocked", ("STATUS:blocked",), {}),
        ("ask-user", ("ask-user",), {}),
        ("attention", ("ask-user",), {}),
        ("coordinator", ("waiting-for:coordinator",), {}),
        ("rescue", ("rescue:stalled",), {}),
        ("roots", (), {"rotation_root": True}),
        ("strategic", (), {"rotation_root": True}),
        ("projects", (), {"workspace": {"path": "example.tex"}}),
    ],
)
def test_todo_board_views_scope_titles(
    store: Store, view: str, tags: tuple[str, ...], meta: dict[str, object]
) -> None:
    handler = TodoHandler(hub=Hub(store=store))
    hidden = store.insert_ref(
        kind="todo", slug=None, title="Private board title", meta=meta
    )
    for tag in tags:
        store.add_tag(hidden.id, Tag.parse_strict(tag, kind="todo"))
    store.add_tag(hidden.id, Tag.open("user_secret:alice"))
    with revision_context(actor="user:bob"):
        assert "Private board title" not in handler.search(view=view).body
    with revision_context(actor="user:alice"):
        assert "Private board title" in handler.search(view=view).body


def test_board_walks_and_edges_require_visible_refs(store: Store) -> None:
    handler = TodoHandler(hub=Hub(store=store))
    root = store.insert_ref(kind="todo", slug=None, title="Public root")
    hidden = store.insert_ref(
        kind="todo", slug=None, title="Private branch", parent_id=root.id
    )
    leaf = store.insert_ref(
        kind="todo", slug=None, title="Public descendant", parent_id=hidden.id
    )
    gripe = store.insert_ref(kind="gripe", slug=None, title="Public gripe")
    old_job = store.insert_ref(
        kind="job",
        slug=None,
        title="Public job",
        parent_id=gripe.id,
        meta={"lane": "public-lane"},
    )
    new_job = store.insert_ref(
        kind="job",
        slug=None,
        title="Private job",
        parent_id=gripe.id,
        meta={"lane": "private-lane"},
    )
    quest = store.insert_ref(kind="quest", slug=None, title="Public quest")
    job = store.insert_ref(
        kind="job", slug=None, title="Descendant job", parent_id=leaf.id
    )
    scratch = store.insert_ref(kind="memory", slug=None, title="Private scratch")
    store.add_tag(scratch.id, Tag.parse_strict("SPACE:scratch", kind="memory"))
    store.add_tag(scratch.id, Tag.open(f"scratch-for:td{root.id}"))
    for rid in (hidden.id, new_job.id, scratch.id):
        store.add_tag(rid, Tag.open("user_secret:alice"))
    for rid in (old_job.id, new_job.id, job.id):
        store.add_tag(rid, Tag.parse_strict("STATUS:running", kind="job"))
    store.add_tag(gripe.id, Tag.open("thread:board-test"))
    store.add_link(src_ref_id=gripe.id, dst_ref_id=hidden.id, relation="blocked-by")
    store.add_link(src_ref_id=hidden.id, dst_ref_id=quest.id, relation="serves")

    for viewer, permitted in (("bob", False), ("alice", True)):
        with revision_context(actor=f"user:{viewer}"):
            tree = handler.get(id=root.id, view="tree").body
            assert ("Private branch" in tree) == permitted
            assert ("Public descendant" in tree) == permitted
            assert (
                "Private scratch" in handler.get(id=root.id, view="scratch").body
            ) == permitted
            assert ("Private branch" in handler.get(id=leaf.id).body) == permitted
            assert (
                f"td{hidden.id}" in handler.search(view="blocked").body
            ) == permitted
            flat = handler.search(view="flat").body
            parent = str(hidden.id) if permitted else "—"
            assert f"{leaf.id} | open | {parent} | Public descendant" in flat
            lanes = work_gripes.root_gripes(store, ["thread:board-test"])
            assert [row.lane for row in lanes.rows] == [
                "private-lane" if permitted else "public-lane"
            ]
            assert (
                new_job.id in {row["id"] for row in work_quest.live_jobs(store)}
            ) == permitted
            assert work_quest.resolve_job_quests(store, [job.id]) == {
                job.id: quest.id if permitted else None
            }
