"""The draft reader's hand-driven working set (ADR 0051 §6) — pen/eye marks,
sticky-with-TTL storage on the draft ref meta, the *around here* ring promotion,
and the planner render that consumes ``meta.working_set``."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.utils import handle_registry
from precis_web import draft_eyes


def _dc(chunk_id: int) -> str:
    return handle_registry.format_handle("draft", chunk_id, chunk=True)


# ── pure marker logic ─────────────────────────────────────────────────


def test_penning_auto_opens_an_eye_unpen_keeps_it() -> None:
    marks: dict[str, Any] = {"pens": [], "eyes": {}}
    draft_eyes.toggle_pen(marks, "dc41")
    assert marks["pens"] == ["dc41"]
    assert marks["eyes"]["dc41"] == "fisheye+1hop"  # pen implies eye
    draft_eyes.toggle_pen(marks, "dc41")  # un-pen
    assert marks["pens"] == []
    assert "dc41" in marks["eyes"]  # eye stays — it's harmless context


def test_un_eyeing_a_penned_chunk_drops_the_pen() -> None:
    marks: dict[str, Any] = {"pens": [], "eyes": {}}
    draft_eyes.toggle_pen(marks, "dc41")
    draft_eyes.toggle_eye(marks, "dc41", on=False)
    assert marks["eyes"] == {}
    assert marks["pens"] == []  # can't edit-hint what you're not looking at


def test_eye_default_extent_is_kind_aware() -> None:
    marks: dict[str, Any] = {"pens": [], "eyes": {}}
    draft_eyes.toggle_eye(marks, "pa721")  # a paper → cluster map (summary)
    draft_eyes.toggle_eye(marks, "me55")  # a note → full fisheye+1hop
    assert marks["eyes"]["pa721"] == "summary"
    assert marks["eyes"]["me55"] == "fisheye+1hop"


def test_to_working_set_meta_shape() -> None:
    marks = {"pens": ["dc41"], "eyes": {"dc41": "fisheye+1hop", "pa721": "summary"}}
    ws = draft_eyes.to_working_set_meta(marks)
    assert ws["edit_hint"] == ["dc41"]
    assert {"handle": "pa721", "extent": "summary"} in ws["eyes"]
    assert {"handle": "dc41", "extent": "fisheye+1hop"} in ws["eyes"]


# ── sticky storage + TTL (real store) ─────────────────────────────────


def test_marks_round_trip_on_ref_meta(hub: Hub) -> None:
    store = hub.store
    ref = store.insert_ref(kind="draft", slug="d1", title="D")
    marks = {"pens": ["dc1"], "eyes": {"dc1": "fisheye+1hop"}}
    draft_eyes.save_marks(store, ref.id, marks)
    loaded = draft_eyes.load_marks(store, ref.id)
    assert loaded["pens"] == ["dc1"]
    assert loaded["eyes"] == {"dc1": "fisheye+1hop"}
    assert loaded["updated_at"] is not None


def test_marks_expire_past_ttl(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    store = hub.store
    ref = store.insert_ref(kind="draft", slug="d2", title="D")
    draft_eyes.save_marks(store, ref.id, {"pens": ["dc1"], "eyes": {"dc1": "verbatim"}})
    # TTL of 0 hours → any stored set reads back empty (never-sticky).
    monkeypatch.setenv("PRECIS_DRAFT_EYES_TTL_HOURS", "0")
    loaded = draft_eyes.load_marks(store, ref.id)
    assert loaded["pens"] == [] and loaded["eyes"] == {}


# ── around-here ring promotion + planner render (real draft) ──────────


def _draft_citing_a_paper(hub: Hub) -> tuple[int, str, int]:
    """A draft whose one section cites a paper. Returns (ref_id, section dc
    handle, paper ref_id)."""
    store = hub.store
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    paper = store.insert_ref(kind="paper", slug="coolpaper", title="A Cool Paper")
    ref, _title = store.create_draft(name="d", title="My Draft", project_ref_id=proj)
    created = store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"This builds on paper:{paper.id}.",
        at={"last": True},
    )
    return ref.id, _dc(created[0].chunk_id), paper.id


def test_around_here_promotes_the_cited_paper_to_an_eye(hub: Hub) -> None:
    store = hub.store
    ref_id, section_dc, paper_id = _draft_citing_a_paper(hub)
    marks: dict[str, Any] = {"pens": [], "eyes": {}}
    draft_eyes.expand_around(store, ref_id, [section_dc], marks)
    assert section_dc in marks["eyes"]  # the section itself
    assert handle_registry.format_handle("paper", paper_id) in marks["eyes"]  # the cite


def test_planner_renders_the_curated_working_set(hub: Hub) -> None:
    from precis.workers.planner_prompt import _render_reader_working_set

    store = hub.store
    _ref_id, section_dc, _paper_id = _draft_citing_a_paper(hub)
    # A change-request todo carrying the hand-curated working set.
    todo = store.insert_ref(kind="todo", slug=None, title="fix the section")
    store.stamp_ref_meta(
        todo.id,
        {
            "working_set": {
                "eyes": [{"handle": section_dc, "extent": "fisheye"}],
                "edit_hint": [section_dc],
            }
        },
    )
    out = _render_reader_working_set(store, todo.id)
    assert "Edit these, at a minimum" in out
    assert section_dc in out  # the pen hint + the rendered eye
    assert "Working set" in out


def test_planner_working_set_empty_when_no_meta(hub: Hub) -> None:
    from precis.workers.planner_prompt import _render_reader_working_set

    store = hub.store
    todo = store.insert_ref(kind="todo", slug=None, title="plain todo")
    assert _render_reader_working_set(store, todo.id) == ""
