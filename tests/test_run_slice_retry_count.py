"""Pre-land restore retry accounting against the real job history query."""

from precis.store import Store
from precis.store.types import Tag
from precis.workers._mint import mint_background_job
from precis.workers.job_types import run_slice


def test_unrestored_count_excludes_current_unmarked_job(store: Store) -> None:
    """Caught gr698949: the running job used to reset six prior free runs."""
    with store.tx() as conn:
        todo = store.insert_ref(kind="todo", slug=None, title="restore", conn=conn)
        for _ in range(6):
            mint_background_job(
                store,
                conn=conn,
                job_type="run_slice",
                executor="ssh_node",
                title="prior restore",
                parent_id=todo.id,
                status="succeeded",
                meta_extra={"attempt_free": run_slice.PRELAND_HOLD_UNRESTORED},
            )
        current = mint_background_job(
            store,
            conn=conn,
            job_type="run_slice",
            executor="ssh_node",
            title="current restore",
            parent_id=todo.id,
            status="running",
        )
    assert run_slice._unrestored_free_run(store, todo.id, current.id) == 6

    # A completed run that burned an attempt breaks the consecutive streak.
    store.stamp_ref_meta(current.id, {"attempt_free": None})
    store.add_tag(current.id, Tag.closed("STATUS", "succeeded"), replace_prefix=True)
    assert run_slice._unrestored_free_run(store, todo.id, current.id + 1) == 0
