"""Mutation previews + acks carry the dependants surface (gr626866).

``delete(dry_run=True)`` and ``edit(dry_run=True)`` append a
``## Dependants`` block (first page + the view call to continue); real
delete/edit acks end with a ``dependants: X shown of Y`` line, marked
``INCOMPLETE`` when the walk was cut. No refusal and no write change —
the block is review surface.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.handlers.job import JobHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.paper import PaperHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store, Tag
from tests.conftest import id_of


def _mk_ref(store: Store, kind: str, title: str) -> int:
    return store.insert_ref(kind=kind, slug=None, title=title).id


def _mk_paper(store: Store, slug: str, title: str = "test paper") -> int:
    return store.insert_ref(kind="paper", slug=slug, title=title).id


def _citing_finding(store: Store, target: int) -> int:
    dep = _mk_ref(store, "finding", "claim relying on the target")
    store.add_link(src_ref_id=dep, dst_ref_id=target, relation="cites")
    return dep


class TestDeletePreview:
    def test_memory_delete_dry_run_shows_dependants(
        self, hub: Hub, store: Store
    ) -> None:
        h = MemoryHandler(hub=hub)
        mid = id_of(h.put(text="a note others cite").body)
        dep = _citing_finding(store, mid)
        resp = h.delete(id=mid, dry_run=True)
        assert "Dependants" in resp.body
        assert f"fi{dep}" in resp.body
        assert "view='dependants'" in resp.body
        # nothing written: the ref is still live
        assert store.get_ref(kind="memory", id=mid) is not None

    def test_memory_real_delete_ack_line(self, hub: Hub, store: Store) -> None:
        h = MemoryHandler(hub=hub)
        mid = id_of(h.put(text="doomed but cited").body)
        dep = _citing_finding(store, mid)
        resp = h.delete(id=mid, reason="stale")
        assert "deleted memory" in resp.body
        assert "dependants: 1 shown of 1" in resp.body
        assert store.get_ref(kind="memory", id=mid) is None
        # the dependant lives on — no behaviour change, just the report
        assert store.get_ref(kind="finding", id=dep) is not None

    def test_todo_delete_dry_run_and_ack(self, hub: Hub, store: Store) -> None:
        h = TodoHandler(hub=hub)
        tid = _mk_ref(store, "todo", "a cited todo")
        _citing_finding(store, tid)
        dry = h.delete(id=tid, dry_run=True)
        assert "Dependants" in dry.body
        assert "view='dependants'" in dry.body
        assert store.get_ref(kind="todo", id=tid) is not None
        real = h.delete(id=tid)
        assert "dependants: 1 shown of 1" in real.body

    def test_delete_ack_marks_incomplete(self, hub: Hub, store: Store) -> None:
        h = FindingHandler(hub=hub)
        fid = _mk_ref(store, "finding", "claim with many citers")
        for i in range(12):
            dep = _mk_ref(store, "memory", f"citing note {i}")
            store.add_link(src_ref_id=dep, dst_ref_id=fid, relation="cites")
        resp = h.delete(id=fid)
        assert "dependants:" in resp.body
        assert "INCOMPLETE" in resp.body


class TestEditPreview:
    def test_memory_edit_dry_run_shows_dependants(self, hub: Hub, store: Store) -> None:
        h = MemoryHandler(hub=hub)
        mid = id_of(h.put(text="alpha line\nbeta line").body)
        dep = _citing_finding(store, mid)
        resp = h.edit(
            id=mid, mode="find-replace", find="beta", text="BETA", dry_run=True
        )
        assert "Dependants" in resp.body
        assert f"fi{dep}" in resp.body
        # real body untouched by the preview
        ref = store.get_ref(kind="memory", id=mid)
        assert ref is not None and "beta" in (h._body_text(ref))

    def test_memory_real_edit_ack_line(self, hub: Hub, store: Store) -> None:
        h = MemoryHandler(hub=hub)
        mid = id_of(h.put(text="alpha line\nbeta line").body)
        _citing_finding(store, mid)
        resp = h.edit(id=mid, mode="find-replace", find="beta", text="BETA")
        assert "edited body of memory" in resp.body
        assert "dependants: 1 shown of 1" in resp.body

    def test_finding_edit_dry_run_shows_dependants(
        self, hub: Hub, store: Store
    ) -> None:
        fh = FindingHandler(hub=hub)
        _mk_paper(store, "cited2026src")
        fid = int(
            id_of(
                fh.put(
                    title="a claim under test",
                    body="beta clause",
                    scope={},
                    cited_in="cited2026src",
                ).body
            )
        )
        dep = store.insert_ref(
            kind="draft", slug="restdraftclaim", title="draft resting on the claim"
        ).id
        store.add_link(src_ref_id=dep, dst_ref_id=fid, relation="cites")
        resp = fh.edit(
            id=fid,
            mode="find-replace",
            find="beta clause",
            text="gamma clause",
            dry_run=True,
        )
        assert "Dependants" in resp.body
        assert f"dr{dep}" in resp.body
        assert "view='dependants'" in resp.body

    def test_finding_real_edit_ack_line(self, hub: Hub, store: Store) -> None:
        fh = FindingHandler(hub=hub)
        _mk_paper(store, "cited2026src2")
        fid = int(
            id_of(
                fh.put(
                    title="another claim",
                    body="the clause",
                    scope={},
                    cited_in="cited2026src2",
                ).body
            )
        )
        _citing_finding(store, fid)
        resp = fh.edit(id=fid, unacquirable_note="print-only source")
        assert "dependants: 1 shown of 1" in resp.body


class TestMemoryEditAckGaps:
    """Review td681243: two memory edit ack paths dropped the
    ``dependants:`` summary — metadata-only patches and whole-body
    ``mode='replace'`` (both real-edit doors; dry_run refuses them)."""

    def test_meta_only_edit_ack_line(self, hub: Hub, store: Store) -> None:
        h = MemoryHandler(hub=hub)
        mid = id_of(h.put(text="a cited note").body)
        _citing_finding(store, mid)
        resp = h.edit(id=mid, hook="fresh index line")
        assert "meta:" in resp.body
        assert "dependants: 1 shown of 1" in resp.body

    def test_replace_edit_ack_line(self, hub: Hub, store: Store) -> None:
        h = MemoryHandler(hub=hub)
        mid = id_of(h.put(text="old body").body)
        _citing_finding(store, mid)
        resp = h.edit(id=mid, mode="replace", text="the brand new body")
        assert "replaced body of memory" in resp.body
        assert "dependants: 1 shown of 1" in resp.body


class TestJobDeleteOrdering:
    """Review td681243: the job delete preview said "would delete"
    before the nonterminal-status and run-record refusals ran — a false
    ack. The refusals now precede the preview."""

    def test_dry_run_refuses_non_terminal(self, hub: Hub) -> None:
        store = hub.live_store
        job = store.insert_ref(kind="job", slug=None, title="queued job")
        store.add_tag(job.id, Tag.parse_strict("STATUS:queued", kind="job"))
        h = JobHandler(hub=hub)
        with pytest.raises(BadInput) as ei:
            h.delete(id=job.id, dry_run=True)
        assert "STATUS:cancelled" in str(ei.value.next)

    def test_dry_run_refuses_run_record(self, hub: Hub) -> None:
        store = hub.live_store
        job = store.insert_ref(kind="job", slug=None, title="archived run job")
        store.update_ref(
            job.id, meta_patch={"run_record": {"schema": "precis.pcb-run/1"}}
        )
        h = JobHandler(hub=hub)
        with pytest.raises(BadInput, match="run record"):
            h.delete(id=job.id, dry_run=True)

    def test_dry_run_terminal_job_still_previews(self, hub: Hub) -> None:
        store = hub.live_store
        job = store.insert_ref(kind="job", slug=None, title="done job")
        store.add_tag(job.id, Tag.parse_strict("STATUS:succeeded", kind="job"))
        h = JobHandler(hub=hub)
        resp = h.delete(id=job.id, dry_run=True)
        assert "would delete" in resp.body
        assert "Dependants" in resp.body
        assert store.get_ref(kind="job", id=job.id) is not None


class TestPaperMutation:
    def test_paper_retire_dry_run_shows_dependants(
        self, hub: Hub, store: Store
    ) -> None:
        ph = PaperHandler(hub=hub)
        pid = _mk_paper(store, "doomed2026paper")
        dep = _mk_ref(store, "finding", "claim corroborated by the paper")
        store.add_link(src_ref_id=pid, dst_ref_id=dep, relation="corroborates")
        resp = ph.edit(
            id="doomed2026paper",
            args={"retire": True, "reason": "dup", "force": True},
            dry_run=True,
        )
        assert "Dependants" in resp.body
        assert f"fi{dep}" in resp.body
        assert "view='dependants'" in resp.body
        assert store.get_ref(kind="paper", id=pid) is not None

    def test_paper_retire_ack_and_field_edit(self, hub: Hub, store: Store) -> None:
        ph = PaperHandler(hub=hub)
        pid = _mk_paper(store, "ackpaper2026")
        dep = _mk_ref(store, "finding", "claim corroborated by the paper")
        store.add_link(src_ref_id=pid, dst_ref_id=dep, relation="corroborates")
        resp = ph.edit(
            id="ackpaper2026",
            args={"retire": True, "reason": "superseded", "force": True},
        )
        assert "retired pa" in resp.body
        assert "dependants: 1 shown of 1" in resp.body
        # metadata edit ack carries the same line
        live = _mk_paper(store, "livepaper2026")
        dep2 = _mk_ref(store, "finding", "another claim")
        store.add_link(src_ref_id=live, dst_ref_id=dep2, relation="corroborates")
        resp2 = ph.edit(id="livepaper2026", year=2020)
        assert "updated paper" in resp2.body
        assert "dependants: 1 shown of 1" in resp2.body
