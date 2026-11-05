"""Memory access inventory (gr563563): stored lifetime counters, no read effects."""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg import Connection

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis.store.types import ChunkInsert
from precis.tools import core as tools_core
from precis.tools.mcp_slim import make_slim_verb

pytestmark = pytest.mark.db

FIRST = datetime(2026, 1, 1, tzinfo=UTC)
LAST_AGENT = FIRST + timedelta(days=1)
LAST_HUMAN = FIRST + timedelta(days=2)


@pytest.fixture(autouse=True)
def json_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_AGENT_TABLE_FORMAT", "json")


def _note(
    store: Store,
    title: str,
    body: str | None = None,
    *,
    space: str | None = None,
    reads: tuple[int, int] | None = None,
) -> int:
    ref_id = store.insert_ref(kind="memory", slug=None, title=title, meta={}).id
    if body is not None:
        store.chunks.insert_chunks(
            ref_id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})],
        )
    if space is not None:
        store.add_tag(ref_id, Tag.closed("SPACE", space), set_by="system")
    if reads is not None:
        n_agent, n_human = reads
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_reads "
                "(ref_id, n_agent, n_human, first_read_at, last_agent_at, "
                "last_human_at, last_agent_reader, last_human_reader) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    ref_id,
                    n_agent,
                    n_human,
                    FIRST,
                    LAST_AGENT if n_agent else None,
                    LAST_HUMAN if n_human else None,
                    "test-agent" if n_agent else None,
                    "test-human" if n_human else None,
                ),
            )
    return ref_id


@pytest.fixture
def ranked(store: Store) -> dict[str, int]:
    # Reads: 12, 1, 5; chars: 5, 100, 30; products: 60, 100, 150.
    return {
        "reads": _note(store, "Read leader", "a" * 5, space="repo-dev", reads=(8, 4)),
        "size": _note(store, "Size leader", "b" * 100, space="research", reads=(0, 1)),
        "score": _note(store, "Score leader", "c" * 30, reads=(2, 3)),
    }


def _search(runtime: PrecisRuntime, **options: Any) -> str:
    body, is_error = runtime.dispatch_with_status(
        "search", {"kind": "memory", "view": "reads", **options}
    )
    assert not is_error, body
    return body


def _rows(body: str) -> list[dict[str, Any]]:
    for line in body.splitlines(keepends=True):
        if line.strip() == "[" or line.strip() == "[]":
            rows, _end = json.JSONDecoder().raw_decode(
                body[body.index(line) :].lstrip()
            )
            assert isinstance(rows, list)
            return rows
    assert "no memory entries" in body, body
    return []


def _handles(body: str) -> list[str]:
    return [str(row["handle"]) for row in _rows(body)]


@pytest.mark.parametrize(
    ("sort", "order"),
    [
        (None, ("reads", "score", "size")),
        ("reads", ("reads", "score", "size")),
        ("size", ("size", "score", "reads")),
        ("score", ("score", "size", "reads")),
    ],
)
def test_reads_orders_by_total_size_or_product(
    runtime_with_store: PrecisRuntime,
    ranked: dict[str, int],
    sort: str | None,
    order: tuple[str, ...],
) -> None:
    out = _search(runtime_with_store, **({"sort": sort} if sort else {}))
    assert _handles(out) == [f"me{ranked[key]}" for key in order]
    assert out.lower().count("lifetime totals") == 1
    assert "not a window" in out.lower()


def test_reads_includes_untagged_and_shows_stored_values(
    runtime_with_store: PrecisRuntime, ranked: dict[str, int]
) -> None:
    rows = {row["handle"]: row for row in _rows(_search(runtime_with_store))}
    assert set(rows) == {f"me{ref_id}" for ref_id in ranked.values()}
    row = rows[f"me{ranked['score']}"]
    assert row["title"] == "Score leader"
    assert row["space"] == "untagged"
    assert int(row["n_agent"]) == 2
    assert int(row["n_human"]) == 3
    assert int(row["body_chars"]) == 30
    assert row["first_read_at"] == "2026-01-01T00:00:00Z"
    assert row["last_read_at"] == "2026-01-03T00:00:00Z"
    assert rows[f"me{ranked['reads']}"]["space"] == "SPACE:repo-dev"


@pytest.mark.parametrize(
    ("space", "key"),
    [
        ("repo-dev", "reads"),
        ("SPACE:repo-dev", "reads"),
        ("research", "size"),
        ("untagged", "score"),
    ],
)
def test_reads_space_filter(
    runtime_with_store: PrecisRuntime, ranked: dict[str, int], space: str, key: str
) -> None:
    assert _handles(_search(runtime_with_store, space=space)) == [f"me{ranked[key]}"]


def test_reads_ignores_expired_space_tag(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref_id = _note(store, "Expired space", "body")
    store.add_tag(
        ref_id,
        Tag.closed("SPACE", "repo-dev"),
        set_by="system",
        expires_at=FIRST,
    )
    assert _handles(_search(runtime_with_store, space="untagged")) == [f"me{ref_id}"]
    assert _handles(_search(runtime_with_store, space="repo-dev")) == []


def test_reads_combines_space_and_open_tag_filters(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    match = _note(store, "Both tags", "body", space="repo-dev", reads=(3, 2))
    store.add_tag(match, Tag.open("topic:read-audit"), set_by="system")
    store.add_tag(match, Tag.open("topic:another"), set_by="system")
    _note(store, "Space only", "body", space="repo-dev", reads=(20, 20))
    tag_only = _note(store, "Open tag only", "body", reads=(20, 20))
    store.add_tag(tag_only, Tag.open("topic:read-audit"), set_by="system")

    rows = _rows(
        _search(runtime_with_store, tags=["SPACE:repo-dev", "topic:read-audit"])
    )

    assert [row["handle"] for row in rows] == [f"me{match}"]
    assert int(rows[0]["n_agent"]) == 3
    assert int(rows[0]["n_human"]) == 2
    assert int(rows[0]["body_chars"]) == 4


def test_reads_includes_live_archived_rulings(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref_id = store.insert_ref(
        kind="memory", slug=None, title="Archived ruling", meta={"status": "archived"}
    ).id
    store.add_tag(ref_id, Tag.open("ruling"), set_by="system")

    assert _handles(_search(runtime_with_store)) == [f"me{ref_id}"]


@pytest.mark.parametrize(
    ("options", "keys"),
    [
        ({"min_reads": 5}, ("reads", "score")),
        ({"min_chars": 30}, ("score", "size")),
        ({"min_reads": 5, "min_chars": 30}, ("score",)),
        ({"min_reads": 13}, ()),
    ],
)
def test_reads_minimum_filters_are_inclusive(
    runtime_with_store: PrecisRuntime,
    ranked: dict[str, int],
    options: dict[str, int],
    keys: tuple[str, ...],
) -> None:
    assert _handles(_search(runtime_with_store, **options)) == [
        f"me{ranked[key]}" for key in keys
    ]


def test_reads_counts_characters_and_only_live_memory_body_chunks(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    live = _note(store, "Unicode body", "é🙂\n", space="repo-dev", reads=(1, 0))
    store.add_tag(live, Tag.open("topic:one"), set_by="system")
    store.add_tag(live, Tag.open("topic:two"), set_by="system")
    store.chunks.insert_chunks(
        live,
        [
            ChunkInsert(ord=1, text=" xy ", meta={"chunk_kind": "memory_body"}),
            ChunkInsert(
                ord=2, text="ignored extra text", meta={"chunk_kind": "paragraph"}
            ),
            ChunkInsert(
                ord=-1, text="ignored card text", meta={"chunk_kind": "card_combined"}
            ),
        ],
    )
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, retired_at) "
            "VALUES (%s, 3, 'memory_body', 'retired text', %s)",
            (live, FIRST),
        )
    retired = _note(store, "Retired memory", "x" * 100, reads=(100, 100))
    store.retire_ref(retired)
    store.insert_ref(kind="paper", slug="reads-non-memory", title="Not a memory")
    rows = _rows(_search(runtime_with_store))
    assert [row["handle"] for row in rows] == [f"me{live}"]
    assert int(rows[0]["body_chars"]) == 7
    assert rows[0]["last_read_at"] == "2026-01-02T00:00:00Z"


def test_reads_includes_unread_and_bodyless_memories(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref_id = _note(store, "Unread bodyless")
    rows = _rows(_search(runtime_with_store, min_chars=0, min_reads=0))
    assert len(rows) == 1
    assert rows[0]["handle"] == f"me{ref_id}"
    assert int(rows[0]["n_agent"]) == int(rows[0]["n_human"]) == 0
    assert int(rows[0]["body_chars"]) == 0
    assert rows[0]["first_read_at"] == rows[0]["last_read_at"] == "-"


def test_listing_and_rendering_never_change_read_counters(
    runtime_with_store: PrecisRuntime, store: Store, ranked: dict[str, int]
) -> None:
    unread = _note(store, "Never read", "body")
    with store.pool.connection() as conn:
        before = conn.execute("SELECT * FROM ref_reads ORDER BY ref_id").fetchall()
    for sort in ("reads", "size", "score"):
        assert len(_rows(_search(runtime_with_store, sort=sort))) == len(ranked) + 1
    with store.pool.connection() as conn:
        after = conn.execute("SELECT * FROM ref_reads ORDER BY ref_id").fetchall()
    assert after == before
    assert all(row[0] != unread for row in after)


def test_reads_pagination_is_complete_and_stable_on_ties(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    refs = [_note(store, f"Equal {i}", "x", reads=(1, 1)) for i in range(5)]
    pages = [_search(runtime_with_store, page=page, page_size=2) for page in (1, 2, 3)]
    assert [_handles(page) for page in pages] == [
        [f"me{ref_id}" for ref_id in refs[:2]],
        [f"me{ref_id}" for ref_id in refs[2:4]],
        [f"me{refs[4]}"],
    ]
    assert "next:" in pages[0].lower() and "next:" in pages[1].lower()
    assert "next:" not in pages[2].lower()
    assert _handles(_search(runtime_with_store, page=4, page_size=2)) == []


def test_core_wrapper_forwards_reads_filters_and_pagination(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools_core, "_runtime", runtime_with_store)
    refs = [
        _note(store, f"Scoped {i}", "x" * (10 + i), space="repo-dev", reads=(2, 1))
        for i in range(3)
    ]
    _note(store, "Outside space", "x" * 100, reads=(100, 100))
    out = tools_core.search(
        kind="memory",
        view="reads",
        args={
            "sort": "size",
            "space": "repo-dev",
            "min_chars": 10,
            "min_reads": 3,
            "page": 2,
            "page_size": 1,
        },
    )
    assert isinstance(out, str), out
    assert _handles(out) == [f"me{refs[1]}"]
    slim = make_slim_verb("search", tools_core.search)
    slim_out = slim(
        kind="memory",
        view="reads",
        args={
            "sort": "size",
            "space": "repo-dev",
            "min_chars": 10,
            "min_reads": 3,
            "page": 2,
            "page_size": 1,
        },
    )
    assert isinstance(slim_out, str), slim_out
    assert _handles(slim_out) == [f"me{refs[1]}"]
    direct = tools_core.search(
        kind="memory",
        view="reads",
        sort="size",
        space="repo-dev",
        min_chars=10,
        min_reads=3,
        args={"page": 2},
        page_size=1,
    )
    assert isinstance(direct, str), direct
    assert _handles(direct) == [f"me{refs[1]}"]


@pytest.mark.parametrize(
    ("options", "keys"),
    [
        ({"sort": "size"}, ("size", "score", "reads")),
        ({"space": "repo-dev"}, ("reads",)),
        ({"min_chars": 30}, ("score", "size")),
        ({"min_reads": 5}, ("reads", "score")),
    ],
)
def test_slim_wrapper_applies_reads_filters_inside_args(
    runtime_with_store: PrecisRuntime,
    ranked: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any],
    keys: tuple[str, ...],
) -> None:
    monkeypatch.setattr(tools_core, "_runtime", runtime_with_store)
    slim = make_slim_verb("search", tools_core.search)
    out = slim(kind="memory", view="reads", args=options)
    assert isinstance(out, str), out
    assert _handles(out) == [f"me{ranked[key]}" for key in keys]


def test_slim_wrapper_runs_printed_reads_next_call(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools_core, "_runtime", runtime_with_store)
    refs = [
        _note(store, f"Next {i}", "x" * (10 + i), space="repo-dev", reads=(2, 1))
        for i in range(3)
    ]
    slim = make_slim_verb("search", tools_core.search)
    options = {
        "sort": "size",
        "space": "repo-dev",
        "min_chars": 10,
        "min_reads": 3,
        "page": 1,
        "page_size": 1,
    }
    out = slim(kind="memory", view="reads", args=options)
    assert isinstance(out, str), out
    assert _handles(out) == [f"me{refs[2]}"]
    # The agent table renders as tabs or JSON; the call text is the same in both.
    printed = re.search(r"search\(kind='memory', view='reads'.*\)", out)
    assert printed is not None, out
    next_call = printed[0]
    call = ast.parse(next_call, mode="eval").body
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name) and call.func.id == "search"
    assert not call.args
    assert all(keyword.arg is not None for keyword in call.keywords)
    kwargs = {
        keyword.arg: ast.literal_eval(keyword.value)
        for keyword in call.keywords
        if keyword.arg is not None
    }
    assert kwargs == {
        "kind": "memory",
        "view": "reads",
        "args": {**options, "page": 2},
    }
    second = slim(**kwargs)
    assert isinstance(second, str), second
    assert _handles(second) == [f"me{refs[1]}"]


@pytest.mark.parametrize("kind, view", [("memory", "rulings"), ("paper", "reads")])
def test_slim_wrapper_still_refuses_paging_other_searches(
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    view: str,
) -> None:
    monkeypatch.setattr(tools_core, "_runtime", runtime_with_store)

    def unexpected(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("page continuation reached dispatch")

    monkeypatch.setattr(tools_core, "_dispatch", unexpected)
    out = make_slim_verb("search", tools_core.search)(
        kind=kind, view=view, args={"page": 2}
    )
    assert "search no longer paginates" in str(out), out


@pytest.mark.parametrize("q", [None, "", "*", "  "])
def test_reads_accepts_blank_or_match_all_query(
    runtime_with_store: PrecisRuntime, store: Store, q: str | None
) -> None:
    ref_id = _note(store, "All memories", "body")
    assert _handles(_search(runtime_with_store, q=q)) == [f"me{ref_id}"]


@pytest.mark.parametrize(
    "options",
    [
        {"q": "specific words"},
        {"sort": "wrong"},
        {"sort": "recency"},
        {"since": "2026-01-01"},
        {"min_reads": -1},
        {"min_chars": -1},
        {"min_reads": True},
        {"min_chars": "large"},
        {"page": 0},
        {"page_size": 0},
    ],
)
def test_reads_refuses_unapplied_or_invalid_arguments(
    runtime_with_store: PrecisRuntime, options: dict[str, Any]
) -> None:
    out, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "view": "reads", **options}
    )
    assert is_error, out
    assert "BadInput" in out
    assert "next" in out.lower()


def test_reads_uses_one_query_including_row_rendering(
    hub: Hub, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = [_note(store, f"Inventory {i}", "body", reads=(i, 0)) for i in range(20)]
    queries: list[object] = []
    original = Connection.execute

    def execute(conn: Connection, query: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(query, (str, bytes)) or query.strip():
            queries.append(query)
        return original(conn, query, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", execute)
    out = MemoryHandler(hub=hub).search(view="reads", page_size=20)
    assert _handles(out.body) == [f"me{ref_id}" for ref_id in reversed(refs)]
    assert len(queries) == 1, queries


def test_oversized_page_refuses_and_smaller_pages_preserve_every_row(
    hub: Hub, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "2400")
    refs = [
        _note(store, f"{i} " + "é" * 180, "body", space="research", reads=(i + 2, 1))
        for i in range(8)
    ]
    tags = ["SPACE:research", "topic:read-audit"]
    for ref_id in refs:
        store.add_tag(ref_id, Tag.open("topic:read-audit"), set_by="system")
    handler = MemoryHandler(hub=hub)
    filters: dict[str, Any] = {
        "sort": "score",
        "space": "research",
        "min_chars": 4,
        "min_reads": 3,
    }

    with pytest.raises(BadInput, match="no partial page returned") as exc:
        handler.search(view="reads", tags=tags, page=2, page_size=4, **filters)

    assert isinstance(exc.value.next, str)
    assert exc.value.next.startswith("restart with a smaller page size: search(")
    assert f"tags={tags!r}" in exc.value.next
    retry_args = ast.literal_eval(exc.value.next.rsplit("args=", 1)[1][:-1])
    assert retry_args == {**filters, "page": 1, "page_size": 2}
    seen = []
    for page in range(1, 5):
        out = handler.search(view="reads", tags=tags, **{**retry_args, "page": page})
        assert len(out.body.encode("utf-8")) <= 2000
        seen.extend(_handles(out.body))
    assert seen == [f"me{ref_id}" for ref_id in reversed(refs)]


def test_oversized_single_row_names_same_page_and_larger_budget(
    hub: Hub, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "2400")
    _note(store, "Small first", "body")
    _note(store, "Small second", "body")
    oversized = _note(store, "é" * 1200, "body")
    handler = MemoryHandler(hub=hub)

    with pytest.raises(BadInput, match="no partial page returned") as exc:
        handler.search(view="reads", space="untagged", page=3, page_size=1)

    assert isinstance(exc.value.next, str)
    assert exc.value.next.startswith(
        "raise the result byte budget, then retry: search("
    )
    retry_args = ast.literal_eval(exc.value.next.rsplit("args=", 1)[1][:-1])
    assert retry_args == {
        "sort": "reads",
        "space": "untagged",
        "page": 3,
        "page_size": 1,
        "min_chars": 0,
        "min_reads": 0,
    }
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "5000")
    assert _handles(handler.search(view="reads", **retry_args).body) == [
        f"me{oversized}"
    ]
