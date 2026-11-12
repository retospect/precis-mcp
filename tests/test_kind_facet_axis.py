"""gr639965: overflowing queryless lists name their per-kind facet groups.

Gripe sets ``cluster:``; memory/todo/quest/job left the axis unset, so
an overflowing list there gave no one-paste narrowing call. Each fixed
kind's axis is the grouping its tags/meta actually carry: memory's
``section:`` (threads/gotchas/runbooks/workflow/reference), todo's
``project:`` workspace tag, job's ``meta.job_type`` — job's type never
appears as a tag, so its facet line rides the meta axis and names
``args={'job_type': …}`` as the narrowing door.

The lists below carry a realistic live filter (a ``topic:``/``STATUS:``
tag), the shape an overflowing list actually arrives with — an
unfiltered enumeration is refused on non-browse kinds. The coverage
(0.9) / cap (20) defaults are exercised against realistic
distributions — one dominant group plus a tail that folds into the
``… +N groups`` cell — mirroring the gripe facet tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.job import JobHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store, Tag

_FILTER = {
    "memory": ("topic:recall-test", Tag.open("topic:recall-test")),
    "todo": ("STATUS:open", Tag.closed("STATUS", "open")),
    "job": ("STATUS:queued", Tag.closed("STATUS", "queued")),
}


def _insert(store: Store, kind: str, title: str, *, meta: dict[str, Any] | None = None):
    ref = store.insert_ref(kind=kind, slug=None, title=title, meta=meta or {})
    filter_value, filter_tag = _FILTER[kind]
    store.add_tag(ref.id, filter_tag, set_by="agent")
    return ref


def _tag(store: Store, ref_id: int, open_value: str) -> None:
    store.add_tag(ref_id, Tag.open(open_value), set_by="agent")


# ── memory: section: axis ────────────────────────────────────────────


def test_memory_overflow_names_section_groups(store: Store) -> None:
    h = MemoryHandler(hub=Hub(store=store))
    for i in range(3):
        r = _insert(store, "memory", f"thread {i}")
        _tag(store, r.id, "section:threads")
    r = _insert(store, "memory", "gotcha 0")
    _tag(store, r.id, "section:gotchas")
    _insert(store, "memory", "loose 0")
    body = h.search(tags=["topic:recall-test"], page_size=2).body
    assert "sections: section:threads 3 · section:gotchas 1 · (no section) 1" in body
    assert "narrow: tags=['topic:recall-test', 'section:threads']" in body


def test_memory_coverage_folds_the_tail(store: Store) -> None:
    """10/12 < 90%, threads+gotchas = 11/12 ≥ 90% → runbooks folds.

    The 0.9 coverage default is checked against the shape real recall
    lists have: one dominant section plus scattered others.
    """
    h = MemoryHandler(hub=Hub(store=store))
    for i in range(10):
        r = _insert(store, "memory", f"thread {i}")
        _tag(store, r.id, "section:threads")
    r = _insert(store, "memory", "gotcha 0")
    _tag(store, r.id, "section:gotchas")
    r = _insert(store, "memory", "runbook 0")
    _tag(store, r.id, "section:runbooks")
    body = h.search(tags=["topic:recall-test"], page_size=2).body
    assert "sections: section:threads 10 · section:gotchas 1 · … +1 groups (1)" in body
    assert "section:runbooks" not in body


# ── todo: project: axis ──────────────────────────────────────────────


def test_todo_overflow_names_project_groups(store: Store) -> None:
    h = TodoHandler(hub=Hub(store=store))
    for i in range(3):
        r = _insert(store, "todo", f"alpha {i}")
        _tag(store, r.id, "project:alpha")
    r = _insert(store, "todo", "beta 0")
    _tag(store, r.id, "project:beta")
    _insert(store, "todo", "loose 0")
    body = h.search(tags=["STATUS:open"], page_size=2).body
    assert "projects: project:alpha 3 · project:beta 1 · (no project) 1" in body
    assert "narrow: tags=['STATUS:open', 'project:alpha']" in body


# ── job: meta.job_type axis ──────────────────────────────────────────


def test_job_overflow_names_job_type_groups(store: Store) -> None:
    h = JobHandler(hub=Hub(store=store))
    for i in range(3):
        _insert(store, "job", f"garden {i}", meta={"job_type": "graph_garden"})
    _insert(store, "job", "fix 0", meta={"job_type": "fix_gripe"})
    _insert(store, "job", "legacy 0")  # no meta.job_type
    body = h.search(tags=["STATUS:queued"], page_size=2).body
    assert "job_types: graph_garden 3 · fix_gripe 1 · (no job_type) 1" in body
    # The narrowing door is args={'job_type': …} — the type never
    # appears as a tag, so a tags= hint would paste a dead call. The
    # live tags= filter rides along so the pasted call reproduces
    # this list's scope.
    assert (
        "narrow: search(kind='job', tags=['STATUS:queued'], "
        "args={'job_type': 'graph_garden'})"
    ) in body


def test_job_line_caps_group_count(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 20-group default caps the meta-axis line the same way as the
    tag-axis one (folded ``… +N groups`` tail)."""
    from precis.handlers import _numeric_ref

    monkeypatch.setattr(_numeric_ref, "FACET_LINE_MAX_GROUPS", 2)
    h = JobHandler(hub=Hub(store=store))
    for jt, n in (("graph_garden", 3), ("fix_gripe", 2), ("doctor_tick", 1)):
        for i in range(n):
            _insert(store, "job", f"{jt} {i}", meta={"job_type": jt})
    body = h.search(tags=["STATUS:queued"], page_size=2).body
    assert "job_types: graph_garden 3 · fix_gripe 2 · … +1 groups (1)" in body
    assert "doctor_tick 1" not in body


@pytest.mark.parametrize(
    ("kind", "handler", "label"),
    [
        ("memory", MemoryHandler, "sections:"),
        ("todo", TodoHandler, "projects:"),
        ("job", JobHandler, "job_types:"),
    ],
)
def test_page_that_fits_has_no_facet_line(store: Store, kind, handler, label) -> None:
    h = handler(hub=Hub(store=store))
    meta = {"job_type": "graph_garden"} if kind == "job" else {}
    _insert(store, kind, "only", meta=meta)
    body = h.search(tags=[_FILTER[kind][0]], page_size=10).body
    assert label not in body
