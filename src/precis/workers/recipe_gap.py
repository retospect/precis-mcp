"""Report unknown demand sources outside the router's hot path."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store


def run_recipe_gap(store: Store) -> int:
    """File at most one gripe per source per UTC day, suppressing open ones.

    The scheduler leases this pass fleet-wide; each source's duplicate check and
    write share a transaction. Closed gripes suppress repeats only on filing day.
    A source a live recipe already covers is skipped: logged rows keep their
    ``unknown.`` task kind for the rolling 24 h window, so once the gripe is
    resolved the stale rows would refile it the next UTC day (gr572095 — the
    ``axis:`` family row covers ``axis:role``; rows logged before the
    registration still match the scan's filter).
    """
    from precis.store.types import ChunkInsert, Tag
    from precis.utils.dispatch_score import SCORE_META_KEY
    from precis.utils.llm import recipes
    from precis.workers.auto_gripe import AUTO_SCORE

    with store.pool.connection() as conn:
        sources = conn.execute(
            "SELECT DISTINCT COALESCE(source, '') FROM llm_call_log "
            "WHERE ts > now() - interval '24 hours' "
            "AND starts_with(features->>'task_kind', 'unknown.')"
        ).fetchall()
    filed = 0
    for (source,) in sources:
        if recipes.lookup(source) is not None:
            continue
        fingerprint = f"recipe-gap:{source or '(empty)'}"
        with store.tx() as conn:
            # Also serializes direct/manual invocations beside the cadence.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (fingerprint,)
            )
            existing = conn.execute(
                "SELECT 1 FROM refs r WHERE r.kind = 'gripe' "
                "AND r.retired_at IS NULL AND r.meta->>'fingerprint' = %s "
                "AND (r.created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') "
                "AT TIME ZONE 'UTC' OR NOT EXISTS ("
                "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' "
                "AND t.value IN ('done', 'wontfix'))) LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if existing:
                continue
            ref = store.insert_ref(
                kind="gripe",
                slug=None,
                title=f"Missing task recipe: {source or '(empty)'}",
                meta={"fingerprint": fingerprint, SCORE_META_KEY: AUTO_SCORE},
                conn=conn,
            )
            store.chunks.insert_chunks(
                ref.id,
                [
                    ChunkInsert(
                        ord=0,
                        text=(
                            f"{fingerprint}\nLLM calls in the last 24 hours have an "
                            "unknown task kind. Register this source in task_recipes."
                        ),
                        meta={"chunk_kind": "gripe_body"},
                    )
                ],
                conn=conn,
            )
            store.add_tag(
                ref.id,
                Tag.closed("STATUS", "open"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
            filed += 1
    return filed
