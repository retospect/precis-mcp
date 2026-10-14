"""``search(kind='job', under=<ref>)`` — the hierarchical job scope
(mcp-introspection gap closures 2026-10-14; ruling: jobs for a
quest/board/structure include sub-jobs AND jobs under its todo tree).

Attachment = ``refs.parent_id`` subtree (bounded ``depth=``) plus
``links`` either direction to a member — which is what "jobs under a
quest" actually means: coordinator children, todo-tree jobs, and
subject-linked fix/build jobs. ``view='tree'`` renders the skeleton;
``view='count'`` composes with the shared count path.
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.job import JobHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis.utils import handle_registry

pytestmark = pytest.mark.db


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


def _ref(
    store: Store, kind: str, title: str = "t", parent_id: int | None = None
) -> int:
    return store.insert_ref(
        kind=kind, slug=None, title=title, meta={}, parent_id=parent_id
    ).id


def _job(
    store: Store,
    *,
    job_type: str = "sweep",
    status: str | None = "queued",
    parent_id: int | None = None,
    title: str = "jt",
) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=title,
        meta={"job_type": job_type},
        parent_id=parent_id,
    )
    if status is not None:
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return ref.id


@pytest.fixture
def tree(store: Store) -> dict[str, int]:
    """quest Q → todo T1 → todo T2 → jobs J1/J2; T1 → job J3 → child J4;
    Q → coordinator job J5; J6 linked to T2; J7 linked to Q; J8 outside."""
    ids: dict[str, int] = {}
    ids["Q"] = _ref(store, "quest", "the quest")
    ids["T1"] = _ref(store, "todo", "t1", parent_id=ids["Q"])
    ids["T2"] = _ref(store, "todo", "t2", parent_id=ids["T1"])
    ids["J1"] = _job(
        store,
        job_type="fix_gripe",
        status="failed",
        parent_id=ids["T2"],
        title="job-one",
    )
    ids["J2"] = _job(
        store, job_type="sweep", status="done", parent_id=ids["T2"], title="job-two"
    )
    ids["J3"] = _job(
        store,
        job_type="plan_tick",
        status="running",
        parent_id=ids["T1"],
        title="job-three",
    )
    ids["J4"] = _job(
        store,
        job_type="fix_gripe",
        status="queued",
        parent_id=ids["J3"],
        title="job-four",
    )
    ids["J5"] = _job(
        store,
        job_type="quest_tick",
        status="queued",
        parent_id=ids["Q"],
        title="job-five",
    )
    ids["J6"] = _job(store, job_type="sweep", status="queued", title="job-six-linked")
    ids["J7"] = _job(
        store, job_type="fix_gripe", status="failed", title="job-seven-linked"
    )
    ids["J8"] = _job(
        store, job_type="sweep", status="queued", title="job-eight-OUTSIDE"
    )
    store.add_link(src_ref_id=ids["J6"], dst_ref_id=ids["T2"], set_by="agent")
    store.add_link(src_ref_id=ids["Q"], dst_ref_id=ids["J7"], set_by="agent")
    return ids


class TestUnderListing:
    def test_includes_subtree_and_linked_jobs(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        resp = jobs.search(under=tree["Q"])
        for key in ("J1", "J2", "J3", "J4", "J5", "J6", "J7"):
            assert f"job:{tree[key]} " in resp.body, (key, resp.body)
        assert f"job:{tree['J8']} " not in resp.body
        assert "7 job(s) under" in resp.body

    def test_under_by_handle_and_kind_id(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        handle = handle_registry.format_handle("quest", tree["Q"])
        for target in (handle, f"quest:{tree['Q']}", str(tree["Q"]), tree["Q"]):
            resp = jobs.search(under=target)
            assert "7 job(s) under" in resp.body, (target, resp.body)

    def test_under_todo_subtree(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        # A todo root: its own descendant jobs + jobs linked to members.
        resp = jobs.search(under=tree["T1"])
        for key in ("J1", "J2", "J3", "J4", "J6"):
            assert f"job:{tree[key]} " in resp.body, (key, resp.body)
        for key in ("J5", "J7"):
            assert f"job:{tree[key]} " not in resp.body, (key, resp.body)

    def test_status_filter(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        resp = jobs.search(under=tree["Q"], status="failed")
        assert "2 job(s) under" in resp.body
        assert f"job:{tree['J1']} " in resp.body
        assert f"job:{tree['J7']} " in resp.body
        assert f"job:{tree['J5']} " not in resp.body

    def test_depth_bounds_members(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        # depth=1: members are {Q, T1, J5} (the walk counts every kind,
        # jobs included). Jobs = parent in members ∪ linked to members:
        # J3 (parent T1), J5 (parent Q), J7 (linked to Q). J4's parent
        # J3 sits at depth 2, J1/J2 under T2 at depth 2 — all excluded.
        resp = jobs.search(under=tree["Q"], depth=1)
        body_ids = {int(m) for m in re.findall(r"job:(\d+)", resp.body)}
        assert tree["J3"] in body_ids
        assert tree["J5"] in body_ids
        assert tree["J7"] in body_ids
        for key in ("J1", "J2", "J4", "J6", "J8"):
            assert tree[key] not in body_ids, (key, resp.body)

    def test_pagination(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        resp = jobs.search(under=tree["Q"], page_size=3)
        assert resp.body.count("job:") == 3
        resp2 = jobs.search(under=tree["Q"], page_size=3, page=2)
        assert resp2.body.count("job:") == 3
        assert "page 2" in resp2.body


class TestUnderTreeView:
    def test_renders_indented_hierarchy(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        resp = jobs.search(under=tree["Q"], view="tree")
        body = resp.body
        assert "# tree under" in body
        # quest root line, then todo at depth 1, job lines carry status
        qh = handle_registry.format_handle("quest", tree["Q"])
        assert qh in body
        assert "fix_gripe [failed]" in body or "fix_gripe" in body
        # The linked-only job shows under its link target with '~'
        assert f"~ job:{tree['J7']}" in body
        # Outside job never appears (handle or raw-id spellings both)
        assert f"jo{tree['J8']}" not in body
        assert f"job:{tree['J8']}" not in body

    def test_tree_on_todo_root(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        resp = jobs.search(under=f"todo:{tree['T1']}", view="tree")
        assert f"jo{tree['J4']}" in resp.body
        assert f"jo{tree['J8']}" not in resp.body


class TestUnderCountView:
    def test_count_grouped_by_state(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        resp = jobs.search(under=tree["Q"], view="count", group_by="state")
        assert "# 7 job entr" in resp.body, resp.body
        assert "jobs under" in resp.body
        assert "STATUS:queued: 3" in resp.body
        assert "STATUS:failed: 2" in resp.body
        assert "STATUS:running: 1" in resp.body
        assert "STATUS:done: 1" in resp.body

    def test_count_under_via_dispatch(
        self, runtime_with_store: PrecisRuntime, store: Store, tree: dict[str, int]
    ) -> None:
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "job",
                "under": tree["Q"],
                "view": "count",
                "__extras__": {"group_by": "meta.job_type"},
            },
        )
        assert "# 7 job entr" in body, body
        assert "fix_gripe: 3" in body
        assert "sweep: 2" in body


class TestUnderErrors:
    def test_unknown_ref(self, jobs: JobHandler) -> None:
        with pytest.raises((BadInput, NotFound), match="exist|resolves"):
            jobs.search(under="quest:99999999")

    def test_bad_depth(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        with pytest.raises(BadInput, match="depth="):
            jobs.search(under=tree["Q"], depth=0)
        with pytest.raises(BadInput, match="depth="):
            jobs.search(under=tree["Q"], depth="deep")

    def test_axis_rejected(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        with pytest.raises(BadInput, match="taxon"):
            jobs.search(under=tree["Q"], axis="x")

    def test_depth_without_under(self, jobs: JobHandler) -> None:
        with pytest.raises(BadInput, match="under="):
            jobs.search(depth=2)

    def test_q_rejected(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        with pytest.raises(BadInput, match="q="):
            jobs.search(under=tree["Q"], q="gripe")

    def test_unknown_view(
        self, jobs: JobHandler, store: Store, tree: dict[str, int]
    ) -> None:
        with pytest.raises(BadInput, match="unknown view"):
            jobs.search(under=tree["Q"], view="histogram")

    def test_under_since_via_dispatch_not_source_search(
        self, runtime_with_store: PrecisRuntime, store: Store, tree: dict[str, int]
    ) -> None:
        # under= + since= must reach the job handler (not fall through to
        # the cross-kind source-search primitive and drop the scope).
        body = runtime_with_store.dispatch(
            "search",
            {
                "kind": "job",
                "under": tree["Q"],
                "since": "2000-01-01",
            },
        )
        assert "job(s) under" in body, body
        assert "source search" not in body
