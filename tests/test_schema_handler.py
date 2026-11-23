"""Exercise schema introspection through the registered runtime get verb."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store


@pytest.fixture
def schema_relations(store: Store) -> Iterator[None]:
    with store.tx() as conn:
        conn.execute("""
            CREATE TABLE public.schema_probe_parent (
                id integer PRIMARY KEY,
                category text NOT NULL DEFAULT 'fixed',
                "payload-key" jsonb,
                CONSTRAINT category_present CHECK (length(category) > 0)
            )
        """)
        conn.execute("""
            CREATE TABLE public.schema_probe_child (
                parent_id integer REFERENCES public.schema_probe_parent(id)
            )
        """)
        conn.execute(
            "CREATE INDEX schema_probe_category ON public.schema_probe_parent(category)"
        )
        conn.execute("""
            COMMENT ON COLUMN public.schema_probe_parent."payload-key" IS 'JSON fixture'
        """)
        conn.execute("""
            INSERT INTO public.schema_probe_parent (id, "payload-key") VALUES
                (1, '{"common":"fixture_hidden_value","other":{"nested":1}}'),
                (2, '{"common":null}'), (3, '["array_member"]'), (4, NULL), (5, 'null')
        """)
        conn.execute("ANALYZE public.schema_probe_parent")
    try:
        yield
    finally:
        with store.tx() as conn:
            conn.execute(
                "DROP TABLE public.schema_probe_child, public.schema_probe_parent"
            )


def test_schema_get(
    runtime_with_store: PrecisRuntime,
    schema_relations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_AGENT_TABLE_FORMAT", "json")

    def get(**kwargs: Any) -> str:
        return runtime_with_store.dispatch("get", {"kind": "schema", **kwargs})

    index = get(__extras__={"page_size": 1})
    assert "'page': 2" in index

    parent = get(id="schema_probe_parent")
    assert "PRIMARY KEY" in parent and "category_present" in parent
    assert "JSON fixture" in parent and "'fixed'" in parent
    assert "schema_probe_child" in parent.split("## Referenced by", 1)[1]
    assert "id='schema_probe_child'" in parent

    child = get(id="schema_probe_child")
    assert "schema_probe_parent" in child.split("## Referenced by", 1)[0]
    assert "id='schema_probe_parent'" in child

    profile = get(id="schema_probe_parent", view="profile")
    assert '"estimated_distinct_nonnull": 1' in profile
    assert '"low_cardinality_key": true' in profile
    assert "fixture_hidden_value" not in profile

    keys = get(
        id="schema_probe_parent",
        view="jsonb",
        __extras__={"column": "payload-key", "page_size": 1},
    )
    assert '"key": "common"' in keys and '"rows_with_key": 2' in keys
    assert "'page': 2" in keys
    assert all(
        value not in keys
        for value in ("fixture_hidden_value", "nested", "array_member")
    )
    more = get(
        id="schema_probe_parent",
        view="jsonb",
        __extras__={"column": "payload-key", "page_size": 1, "page": 2},
    )
    assert '"key": "other"' in more and '"rows_with_key": 1' in more
    assert "'page': 3" not in more

    assert "No visible public relation" in get(id="pg_catalog.pg_roles")
    assert "Choose a JSONB column" in get(
        id="schema_probe_parent", view="jsonb", __extras__={"column": "category"}
    )
