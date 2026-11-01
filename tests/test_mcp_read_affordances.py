"""gr548280: all three operational reads must survive the dispatch boundary."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis.utils.llm.operations import LLM_OPERATIONS


def test_memory_count_facets_through_dispatch(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    now = datetime.now(UTC)
    for i, space in enumerate(("repo-dev", "research", None)):
        ref = store.insert_ref(kind="memory", slug=None, title=f"memory {i}", meta={})
        if space:
            store.add_tag(ref.id, Tag.closed("SPACE", space), set_by="agent")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET updated_at = %s WHERE ref_id = %s",
                (now - timedelta(days=(0, 15, 90)[i]), ref.id),
            )
    retired = store.insert_ref(kind="memory", slug=None, title="retired", meta={})
    store.retire_ref(retired.id)
    store.insert_ref(kind="todo", slug=None, title="not memory", meta={})

    for facet, expected in (
        ("tag:SPACE", {"SPACE:repo-dev": 1, "SPACE:research": 1, "(untagged)": 1}),
        ("touched", {"<=7d": 1, "<=30d": 1, ">30d": 1}),
    ):
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "memory",
                "view": "count",
                "__extras__": {"group_by": facet},
            },
        )
        assert body.startswith("# 3 memory entr"), body
        got = {
            label: int(count)
            for label, count in re.findall(r"^  (.+): (\d+)$", body, re.M)
        }
        assert got == expected, body
        assert sum(got.values()) == 3


def test_memory_group_by_requires_count(runtime_with_store: PrecisRuntime) -> None:
    body = runtime_with_store.dispatch(
        "search",
        {
            "kind": "memory",
            "view": "index",
            "__extras__": {"group_by": "tag:SPACE"},
        },
    )
    assert "[error:BadInput]" in body and "view='count'" in body


def test_llm_stored_chain_and_ops_through_dispatch(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    stamp = datetime(2026, 10, 31, 12, 34, 56, tzinfo=UTC)
    route = {
        "chain": [
            {
                "placement": "local",
                "transport": "local",
                "model": "test-local",
                "max_tokens": 4096,
                "reasoning_effort": "low",
                "api_key": "must-not-render",
            },
            {
                "transport": "openai_tools",
                "model": "test-fallback",
                "placement": "cloud",
            },
        ],
        "quota_fallback": True,
    }
    with store.pool.connection() as conn:
        for source, value in (
            ("graph_garden:propose", json.dumps(route)),
            ("test-unregistered", '{"model": "test-inert"}'),
            ("test-invalid", "{invalid"),
        ):
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, %s)",
                (f"llm.op.{source}", value, stamp),
            )
    body = runtime_with_store.dispatch(
        "get", {"kind": "llm", "id": "graph_garden:propose"}
    )
    for text in (
        "1. primary:",
        "model=test-local",
        "max_tokens=4096",
        "reasoning_effort=low",
        "2. fallback:",
        "model=test-fallback",
        "quota_fallback=True",
        "last set: 2026-10-31T12:34:56Z",
        "Stored configuration only",
    ):
        assert text in body, body
    assert "must-not-render" not in body
    listing = runtime_with_store.dispatch("get", {"kind": "llm", "id": "/ops"})
    for source in (*LLM_OPERATIONS, "test-unregistered", "test-invalid"):
        assert source in listing
    inert = runtime_with_store.dispatch(
        "get", {"kind": "llm", "id": "test-unregistered"}
    )
    assert "inert" in inert and "model=test-inert" in inert
    invalid = runtime_with_store.dispatch("get", {"kind": "llm", "id": "test-invalid"})
    assert "invalid stored override" in invalid
    default = runtime_with_store.dispatch("get", {"kind": "llm", "id": "llm_summarize"})
    assert "no stored override; last set: never" in default
    refused = runtime_with_store.dispatch(
        "get",
        {"kind": "llm", "id": "graph_garden:propose", "view": "raw"},
    )
    assert "[error:BadInput]" in refused and "next:" in refused.lower()
    with store.pool.connection() as conn:
        calls = conn.execute("SELECT count(*) FROM llm_call_log").fetchone()
        settings = conn.execute("SELECT count(*) FROM app_settings").fetchone()
    assert calls is not None and calls[0] == 0
    assert settings is not None and settings[0] == 3


def test_garden_review_counts_through_dispatch(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    now = datetime.now(UTC)
    ref = store.insert_ref(kind="memory", slug=None, title="review target", meta={})
    with store.pool.connection() as conn:
        for actor, verdict, note, age in (
            ("graph-garden", "proposed", '{"garden": {}}', 1),
            ("garden-consumer", "approved", '{"garden": {}}', 1),
            ("graph-garden", "rejected", '{"garden": {}}', 1),
            ("garden-consumer", "rejected", '{"garden": {}}', 1),
            (
                "garden-consumer",
                "proposed",
                '{"garden_review": {"held": "ambiguous"}}',
                1,
            ),
            ("garden-reviewer", "approved", "sample only", 1),
            ("someone-else", "approved", "not garden", 1),
            ("graph-garden", "proposed", '{"garden": {}}', 10),
        ):
            conn.execute(
                "INSERT INTO reviews (target_kind, target_id, actor, content_sha, "
                "verdict, note, at) VALUES ('ref', %s, %s, 'test', %s, %s, %s)",
                (ref.id, actor, verdict, note, now - timedelta(days=age)),
            )
    last = store.insert_ref(
        kind="job",
        slug=None,
        title="ran",
        meta={"job_type": "graph_garden", "garden": {"proposals": 1}},
    )
    store.insert_ref(
        kind="job",
        slug=None,
        title="skipped",
        meta={"job_type": "graph_garden", "garden": {"skipped": "dark"}},
    )
    store.insert_ref(
        kind="job", slug=None, title="queued", meta={"job_type": "graph_garden"}
    )
    args = {"kind": "job", "id": "/garden", "__extras__": {"since": "7d"}}
    body = runtime_with_store.dispatch("get", args)
    for text in (
        "proposed: 1",
        "approved: 1",
        "rejected: 2",
        "held-for-human: 1",
        f"last run id: jo{last.id}",
        "ledger events",
    ):
        assert text in body, body
    path = runtime_with_store.dispatch("get", {"kind": "job", "id": "/garden?since=7d"})
    assert path.splitlines()[1:] == body.splitlines()[1:]
    wider = runtime_with_store.dispatch(
        "get",
        {**args, "__extras__": {"since": "14d"}},
    )
    assert "proposed: 2" in wider
    with store.pool.connection() as conn:
        reviews = conn.execute("SELECT count(*) FROM reviews").fetchone()
    assert reviews is not None and reviews[0] == 8


@pytest.mark.parametrize(
    "args",
    [
        {"id": "/garden", "__extras__": {"since": "bad"}},
        {"id": "/garden", "__extras__": {"since": "-1d"}},
        {"id": "/garden", "__extras__": {"since": "0h"}},
        {"id": "/garden", "__extras__": {"handler": "graph_garden"}},
        {"id": "/garden?other=1"},
    ],
)
def test_garden_bad_filters(runtime_with_store: PrecisRuntime, args: dict) -> None:
    body = runtime_with_store.dispatch("get", {"kind": "job", **args})
    assert "[error:BadInput]" in body and "next:" in body.lower(), body


def test_garden_empty(runtime_with_store: PrecisRuntime) -> None:
    body = runtime_with_store.dispatch("get", {"kind": "job", "id": "/garden"})
    for label in ("proposed", "approved", "rejected", "held-for-human"):
        assert f"{label}: 0" in body
    assert "last run id: (none in window)" in body
