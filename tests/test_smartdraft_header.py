"""Smartdraft's document header — the metadata panel and the rename.

Two things the reader had no surface for at all: the draft's own ``meta``
(``_ref_view`` flattened the ref to ``{id, title}`` and dropped the rest) and
a way to change its title (``refs.title`` had no write path anywhere, while
the title heading chunk was freely editable — so the two could drift).
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput, NotFound
from precis.handlers.draft import DraftHandler
from precis_web.routes.smartdraft import (
    _doc_meta,
    _flatten_meta,
    _meta_value,
    _ref_connection_groups,
    _ref_view,
)


class _FakeRef:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.kind = kw.get("kind", "draft")
        self.slug = kw.get("slug")
        self.title = kw.get("title", "T")
        self.meta = kw.get("meta", {})
        self.created_at = kw.get("created_at")
        self.updated_at = kw.get("updated_at")


# ── the meta panel's assembly ────────────────────────────────────────


def test_ref_view_carries_more_than_id_and_title() -> None:
    view = _ref_view(_FakeRef(id=7, slug="brief", title="Morning brief"))
    assert view["slug"] == "brief"
    assert view["kind"] == "draft"
    assert "created_at" in view and "updated_at" in view


def test_workspace_is_flattened_one_level() -> None:
    pairs = dict(_flatten_meta({"workspace": {"doc_type": "paper", "path": "/w"}}))
    assert pairs == {"workspace.doc_type": "paper", "workspace.path": "/w"}


def test_empty_containers_drop_out() -> None:
    # A `{}` abbrevs registry is noise; a populated one is information.
    assert _flatten_meta({"abbrevs": {}, "cohort": "c1", "voice": ""}) == [
        ("cohort", "c1")
    ]


def test_unknown_keys_render_rather_than_being_dropped() -> None:
    """The load-bearing property: this panel is a label table, not a
    whitelist. A key nobody has taught the UI about still shows up (raw), so
    the next worker to stamp one isn't invisible."""
    rows = {r["key"]: r for r in _doc_meta(_FakeRef(meta={"newfangled_key": "v"}))}
    assert rows["newfangled_key"]["value"] == "v"
    assert rows["newfangled_key"]["raw"] is True
    assert rows["newfangled_key"]["label"] == "newfangled_key"


def test_known_keys_get_a_label_and_sort_first() -> None:
    rows = _doc_meta(_FakeRef(meta={"zzz_unknown": "x", "voice": "bm_george"}))
    assert [r["key"] for r in rows] == ["voice", "zzz_unknown"]
    assert rows[0]["label"] == "TTS voice"
    assert rows[0]["raw"] is False


def test_a_failure_key_is_flagged_not_just_listed() -> None:
    # The case that motivated the panel: 8 prod drafts carried an
    # `audio_failed_at` that nothing in the UI ever showed.
    (row,) = _doc_meta(_FakeRef(meta={"audio_failed_at": "2026-08-06T14:44:41"}))
    assert row["warn"] is True


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("abbrevs", {"a": 1, "b": 2}, "2 abbreviations"),
        ("authoring_enabled", True, "on"),
        ("authoring_enabled", False, "off"),
        ("abbrev_ignore", ["ASA", "NOx"], "ASA, NOx"),
        ("cohort", 3, "3"),
        ("odd", {"n": 1}, '{"n": 1}'),
    ],
)
def test_meta_values_render_readably(key, value, expected) -> None:
    assert _meta_value(key, value) == expected


# ── document-level connections ───────────────────────────────────────


class _ConnStore:
    def __init__(self, conns):
        self._conns = conns

    def ref_connections(self, ref_id):
        return self._conns

    drafts = property(
        lambda self: self
    )  # drafts carve: fake serves as its own sub-store


def _conn(relation, direction="out", ident="x"):
    return {
        "relation": relation,
        "direction": direction,
        "kind": "paper",
        "ident": ident,
        "title": "t",
    }


def test_concerns_lead_the_connection_list() -> None:
    store = _ConnStore(
        [_conn("cites"), _conn("raises-concern-about", "in"), _conn("draft-of")]
    )
    groups = _ref_connection_groups(store, 1)
    assert [g["relation"] for g in groups] == [
        "raises-concern-about",
        "draft-of",
        "cites",
    ]


def test_no_edge_is_dropped_however_many_a_relation_has() -> None:
    # An earlier cap kept the first dozen chips and reduced the rest to a "+N
    # more" count, which put half a briefing's bibliography out of reach. The
    # panel bounds its height with a scrolling box now, so the assembly has to
    # hand over every edge — a cap here would silently truncate again.
    store = _ConnStore([_conn("cites", ident=f"p{i}") for i in range(30)])
    (group,) = _ref_connection_groups(store, 1)
    assert group["total"] == 30
    assert len(group["chips"]) == 30


# ── rename: refs.title and the title heading, together ───────────────


def _seed(store, *, title="Original title"):
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    ref, head = store.create_draft(name="rn", title=title, project_ref_id=proj)
    return ref, head


def _ref_title(store, ref_id):
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return row[0]


def test_rename_writes_both_the_ref_title_and_the_heading(store) -> None:
    ref, head = _seed(store)
    old, synced = store.set_draft_title(ref.id, "A better title")
    assert (old, synced) == ("Original title", True)
    assert _ref_title(store, ref.id) == "A better title"
    assert store.get_draft_chunk(head.handle).text == "A better title"


def test_rename_converges_an_already_drifted_heading(store) -> None:
    """The defect this closes: the heading was always editable while
    ``refs.title`` had no write path, so a draft could show one name in its
    reader and another in every search hit. Renaming must converge them,
    which means overwriting a diverged heading rather than preserving it."""
    ref, head = _seed(store)
    store.edit_text(head.handle, "Heading the author changed by hand")
    assert _ref_title(store, ref.id) == "Original title"  # drifted

    store.set_draft_title(ref.id, "Converged")
    assert _ref_title(store, ref.id) == "Converged"
    assert store.get_draft_chunk(head.handle).text == "Converged"


def test_rename_keeps_the_heading_handle_alive(store) -> None:
    # In-place edit, so inbound anchors to the title heading survive.
    ref, head = _seed(store)
    store.set_draft_title(ref.id, "Renamed")
    assert store.get_draft_chunk(head.handle) is not None


def test_rename_is_idempotent_on_the_heading(store) -> None:
    ref, head = _seed(store)
    store.set_draft_title(ref.id, "Original title")
    assert store.get_draft_chunk(head.handle).text == "Original title"


def test_a_no_op_rename_leaves_the_ref_untouched(store) -> None:
    """Saving the name it already has must not look like an edit: the header
    renders ``updated_at`` as "last touched", so bumping it on a no-op save
    would make an untouched document read as freshly worked on."""
    ref, _ = _seed(store)
    with store.pool.connection() as conn:
        before = conn.execute(
            "SELECT updated_at FROM refs WHERE ref_id = %s", (ref.id,)
        ).fetchone()[0]

    store.set_draft_title(ref.id, "  Original title  ")  # same, once stripped

    with store.pool.connection() as conn:
        after = conn.execute(
            "SELECT updated_at FROM refs WHERE ref_id = %s", (ref.id,)
        ).fetchone()[0]
        events = conn.execute(
            "SELECT count(*) FROM ref_events "
            "WHERE ref_id = %s AND event = 'title_changed'",
            (ref.id,),
        ).fetchone()[0]
    assert after == before
    assert events == 0


def test_a_no_op_ref_rename_still_converges_a_drifted_heading(store) -> None:
    # The convergence case where the ref title is BY DEFINITION unchanged —
    # so the no-op guard above must not short-circuit the heading sync.
    ref, head = _seed(store)
    store.edit_text(head.handle, "Drifted by hand")
    store.set_draft_title(ref.id, "Original title")
    assert store.get_draft_chunk(head.handle).text == "Original title"


def test_blank_title_is_rejected(store) -> None:
    ref, _ = _seed(store)
    with pytest.raises(BadInput):
        store.set_draft_title(ref.id, "   ")
    assert _ref_title(store, ref.id) == "Original title"


def test_rename_of_a_missing_ref_raises(store) -> None:
    with pytest.raises(NotFound):
        store.set_draft_title(999_999, "Nope")


def test_rename_logs_a_ref_event(store) -> None:
    ref, _ = _seed(store)
    store.set_draft_title(ref.id, "Renamed")
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT payload FROM ref_events "
            "WHERE ref_id = %s AND event = 'title_changed'",
            (ref.id,),
        ).fetchone()
    assert row is not None
    assert row[0]["old_title"] == "Original title"
    assert row[0]["new_title"] == "Renamed"


def test_a_draft_with_no_root_heading_renames_the_ref_alone(store) -> None:
    ref, head = _seed(store)
    # A draft must keep one live chunk, so give it a body before retiring
    # the heading — the shape an import (no root heading) lands in.
    store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="body", at={"last": True}
    )
    store.retire_chunk(head.handle)
    old, synced = store.set_draft_title(ref.id, "Headless")
    assert (old, synced) == ("Original title", False)
    assert _ref_title(store, ref.id) == "Headless"


def test_handler_edit_exposes_the_rename(hub) -> None:
    store = hub.store
    ref, head = _seed(store)
    resp = DraftHandler(hub=hub).edit(id="rn", title="Via the verb")
    assert "Via the verb" in resp.body
    assert _ref_title(store, ref.id) == "Via the verb"
    assert store.get_draft_chunk(head.handle).text == "Via the verb"
