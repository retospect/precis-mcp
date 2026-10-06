"""Re-arm an operator-confirmed Elsevier preview cohort, without deleting evidence.

Explicit IDs, expected count and dry-run default prevent a short-paper heuristic
from becoming a corpus-wide rewrite. Existing markup_refetch/oa_requeued pins
admit these already-ingested refs to the fetcher and preserve bodies/hashes until
validated replacement lands. A durable event makes repeated jobs idempotent.
No provider/model call runs in this job; fetch workers do later acquisition.
"""

from typing import Any

from psycopg.types.json import Jsonb

from precis.errors import BadInput
from precis.store import Store
from precis.store.si_links import utc_stamp
from precis.workers.job_types import JobTypeSpec


def requeue(
    store: Store, ref_ids: list[int], *, expected_count: int, dry_run: bool = True
) -> dict[str, Any]:
    if (
        type(dry_run) is not bool
        or type(expected_count) is not int
        or expected_count < 1
    ):
        raise BadInput("dry_run must be boolean and expected_count a positive integer")
    if not ref_ids or any(type(i) is not int or i <= 0 for i in ref_ids):
        raise BadInput("ref_ids must be a nonempty list of positive paper IDs")
    if len(set(ref_ids)) != len(ref_ids) or len(ref_ids) != expected_count:
        raise BadInput("ref_ids must be unique and match expected_count")
    queued = []
    for ref_id in ref_ids:
        with store.tx() as conn:
            row = conn.execute(
                "SELECT ref_id FROM refs WHERE ref_id=%s AND kind='paper' AND retired_at IS NULL FOR UPDATE",
                (ref_id,),
            ).fetchone()
            if row is None:
                continue
            eligible = conn.execute(
                """SELECT 1 WHERE EXISTS (
                    SELECT 1 FROM ref_identifiers WHERE ref_id=%s AND id_kind='doi' AND id_value LIKE '10.1016/%%')
                  AND EXISTS (SELECT 1 FROM ref_events WHERE ref_id=%s
                    AND source IN ('fetcher:elsevier','fetcher:elsevier_xml') AND event='fetch_ok')
                  AND NOT EXISTS (SELECT 1 FROM ref_events WHERE ref_id=%s
                    AND source='elsevier_backfill' AND event='preview_requeued')
                  AND (SELECT COALESCE(sum(length(text)),0) FROM chunks WHERE ref_id=%s AND ord>=0) BETWEEN 1 AND 4999""",
                (ref_id, ref_id, ref_id, ref_id),
            ).fetchone()
            if eligible is None:
                continue
            if not dry_run:
                stamp = utc_stamp()
                conn.execute(
                    "UPDATE refs SET meta=meta || %s WHERE ref_id=%s",
                    (
                        Jsonb(
                            {
                                "markup_refetch": {
                                    "at": stamp,
                                    "reason": "gr470193 confirmed preview",
                                },
                                "oa_requeued": {
                                    "at": stamp,
                                    "reason": "gr470193 confirmed preview",
                                },
                            }
                        ),
                        ref_id,
                    ),
                )
                store.append_event(
                    ref_id,
                    source="elsevier_backfill",
                    event="preview_requeued",
                    payload={"gripe": "gr470193"},
                    conn=conn,
                )
            queued.append(ref_id)
    return {
        "dry_run": dry_run,
        "requested": len(ref_ids),
        "eligible": len(queued),
        "ref_ids": queued,
    }


def _dispatch(ctx: Any, spec: Any) -> None:
    params = ctx.meta.get("params") or {}
    result = requeue(
        ctx.store,
        params.get("ref_ids") or [],
        expected_count=params.get("expected_count", 0),
        dry_run=params.get("dry_run", True),
    )
    ctx.set_meta(backfill=result)
    ctx.append_chunk(
        "job_summary",
        f"Elsevier preview backfill: {result['eligible']}/{result['requested']} eligible; dry_run={result['dry_run']}",
    )


SPEC = JobTypeSpec(
    name="elsevier_abstract_backfill",
    params_schema={
        "type": "object",
        "properties": {
            "ref_ids": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "uniqueItems": True,
                "minItems": 1,
            },
            "expected_count": {"type": "integer", "minimum": 1},
            "dry_run": {"type": "boolean", "default": True},
        },
        "required": ["ref_ids", "expected_count"],
        "additionalProperties": False,
    },
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),
    description="Dry-run by default: requeue an explicit confirmed Elsevier preview cohort; no acquisition/model calls.",
    dispatch=_dispatch,
)
