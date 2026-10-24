"""``JobTypeSpec.validate`` plumbing (gr511724): the put-time semantic
hook. ``JobHandler.put`` runs ``spec.validate(store, params)`` right
after the static ``params_schema`` check, so a submit whose params
type-check but cannot run is refused — by the hook's own ``BadInput`` —
instead of queueing a job that fails the same check at dispatch.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.store import Store
from precis.workers import job_types as jt
from precis.workers.job_types import JobTypeSpec

_NAME = "test_validate_hook"


def _spec(validate: Callable[..., None]) -> JobTypeSpec:
    return JobTypeSpec(
        name=_NAME,
        params_schema={"type": "object", "properties": {}},
        compatible_executors=frozenset({"coordinator"}),
        requires=frozenset(),
        description="validate-hook plumbing test",
        dispatch=lambda ctx, spec: None,
        validate=validate,
    )


@pytest.fixture
def registry() -> Iterator[dict[str, JobTypeSpec]]:
    """Inject a fake job_type (no entry-point discovery at test time);
    remove it after."""
    jt._REGISTRY.pop(_NAME, None)
    yield jt._REGISTRY
    jt._REGISTRY.pop(_NAME, None)


def _parent(store: Store) -> int:
    return store.insert_ref(kind="todo", slug=None, title="owner", meta={}).id


def _job_count(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM refs WHERE kind = 'job'").fetchone()
    assert row is not None
    return int(row[0])


def test_refusing_validate_blocks_the_put(
    registry: dict[str, JobTypeSpec], hub: Hub, store: Store
) -> None:
    def _refuse(store: Store, params: dict[str, Any]) -> None:
        raise BadInput(
            "test_validate_hook: params.semantic is not a live taxon",
            next="pass a live taxon slug",
        )

    registry[_NAME] = _spec(_refuse)
    with pytest.raises(BadInput, match="not a live taxon"):
        JobHandler(hub=hub).put(
            job_type=_NAME, parent_id=_parent(store), params={"semantic": "nope"}
        )
    assert _job_count(store) == 0


def test_passing_validate_enqueues(
    registry: dict[str, JobTypeSpec], hub: Hub, store: Store
) -> None:
    seen: list[dict[str, Any]] = []

    def _ok(store: Store, params: dict[str, Any]) -> None:
        seen.append(dict(params))

    registry[_NAME] = _spec(_ok)
    r = JobHandler(hub=hub).put(
        job_type=_NAME, parent_id=_parent(store), params={"semantic": "ok"}
    )
    assert r is not None
    assert seen == [{"semantic": "ok"}]
    assert _job_count(store) == 1
