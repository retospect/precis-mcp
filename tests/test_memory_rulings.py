"""Ruling nodes (gr548835 slice 1): meta keys, the rulings view, recall exclusion."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from precis.cli.memory import migrate_rulings
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils.memory_rulings import (
    parse_meta_line,
    status_of,
    validate_ruling_meta,
)
from tests.conftest import id_of

TODAY = date(2026, 11, 1)


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _ruling(
    handler: MemoryHandler, title: str, body: str | None = None, **meta: Any
) -> int:
    resp = handler.put(
        text=body or f"{title} body prose",
        title=title,
        tags=["ruling"],
        meta=meta or None,
    )
    return id_of(resp.body)


def _legacy(handler: MemoryHandler, title: str, body: str, *tags: str) -> int:
    """A backfilled ruling: tagged, no meta (a system write stamps no defaults)."""
    return handler.write_system(text=body, title=title, tags=["ruling", *tags])


def _meta(store: Store, mid: int) -> dict[str, Any]:
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None
    return dict(ref.meta)


def _open_tags(store: Store, mid: int) -> set[str]:
    return {v for _ns, v in store.ref_tags_bulk([mid]).get(mid, [])}


# ── validation ──────────────────────────────────────────────────────────


def test_create_defaults_status_ruled_on_and_review_by() -> None:
    out = validate_ruling_meta({}, existing=None, today=TODAY)
    assert out == {
        "status": "in-force",
        "ruled_on": "2026-11-01",
        "review_by": "2026-11-15",
    }


def test_review_by_defaults_to_ruled_on_plus_14_days() -> None:
    out = validate_ruling_meta({"ruled_on": "2026-10-01"}, existing=None, today=TODAY)
    assert out["review_by"] == "2026-10-15"


def test_explicit_review_by_wins() -> None:
    out = validate_ruling_meta(
        {"ruled_on": "2026-10-01", "review_by": "2026-12-31"},
        existing=None,
        today=TODAY,
    )
    assert out["review_by"] == "2026-12-31"


def test_edit_patch_touches_only_given_keys() -> None:
    existing = {"ruled_on": "2026-10-01", "review_by": "2026-10-15"}
    assert validate_ruling_meta(
        {"status": "absorbed"}, existing=existing, today=TODAY
    ) == {"status": "absorbed"}


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ({"status": "done"}, r"meta\['status'\] must be one of"),
        ({"ruled_on": "11/01/2026"}, r"ISO date"),
        ({"review_by": "2026-13-40"}, r"ISO date"),
        ({"ruled_on": 20261101}, r"ISO date"),
        ({"ruled_by": "  "}, r"ruled_by"),
        ({"ruled_on": "2026-11-10", "review_by": "2026-11-01"}, r"before ruled_on"),
    ],
)
def test_invalid_values_are_refused_with_a_next(
    raw: dict[str, Any], match: str
) -> None:
    with pytest.raises(BadInput, match=match) as exc:
        validate_ruling_meta(raw, existing=None, today=TODAY)
    assert exc.value.next


def test_status_refusal_names_the_closed_set() -> None:
    with pytest.raises(BadInput) as exc:
        validate_ruling_meta({"status": "stale"}, existing=None)
    assert exc.value.options == ["in-force", "absorbed", "archived", "retired"]


def test_status_of_falls_back_to_the_legacy_tag_then_in_force() -> None:
    assert status_of({"status": "archived"}, {"status:in-force"}) == "archived"
    assert status_of({}, {"status:absorbed"}) == "absorbed"
    assert status_of(None, set()) == "in-force"


def test_parse_meta_line_reads_the_last_meta_line() -> None:
    body = (
        "Land at once.\n\nMeta: old: x\n"
        "Meta: ruled_by: Reto; ruled_on=2026-11-01, status: in-force | review_by: 2026-11-15"
    )
    assert parse_meta_line(body) == {
        "ruled_by": "Reto",
        "ruled_on": "2026-11-01",
        "status": "in-force",
        "review_by": "2026-11-15",
    }
    assert parse_meta_line("no meta here") == {}


# ── put / edit ──────────────────────────────────────────────────────────


def test_put_ruling_stamps_defaults_and_the_ack_names_them(
    handler: MemoryHandler, store: Store
) -> None:
    resp = handler.put(text="land when feature-complete", tags=["ruling"])
    meta = _meta(store, id_of(resp.body))
    assert meta["status"] == "in-force"
    ruled_on = date.fromisoformat(meta["ruled_on"])
    assert meta["review_by"] == (ruled_on + timedelta(days=14)).isoformat()
    assert "ruling meta: " in resp.body and "status=in-force" in resp.body


def test_put_ruling_keeps_explicit_meta(handler: MemoryHandler, store: Store) -> None:
    mid = _ruling(
        handler,
        "smallest model first",
        ruled_by="Reto",
        ruled_on="2026-10-20",
        status="in-force",
    )
    meta = _meta(store, mid)
    meta.pop("filed_by", None)  # the server-side origin stamp is not under test
    assert meta == {
        "ruled_by": "Reto",
        "ruled_on": "2026-10-20",
        "status": "in-force",
        "review_by": "2026-11-03",
    }


def test_ruling_keys_on_a_non_ruling_put_are_refused(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput, match="only apply to a node tagged 'ruling'") as exc:
        handler.put(text="plain note", meta={"status": "absorbed"})
    assert "tags=['ruling']" in str(exc.value.next)


def test_ruling_keys_on_a_non_ruling_edit_are_refused(
    handler: MemoryHandler, store: Store
) -> None:
    mid = id_of(handler.put(text="plain note").body)
    with pytest.raises(BadInput, match="only apply to a node tagged 'ruling'"):
        handler.edit(id=mid, meta={"ruled_by": "Reto"})
    assert "ruled_by" not in _meta(store, mid)


def test_unknown_key_message_points_at_the_ruling_keys(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput, match="also takes"):
        handler.put(text="x", meta={"nope": 1})


def test_edit_status_validates_and_writes(handler: MemoryHandler, store: Store) -> None:
    mid = _ruling(handler, "a ruling", ruled_by="Reto")
    with pytest.raises(BadInput, match="must be one of"):
        handler.edit(id=mid, meta={"status": "gone"})
    assert _meta(store, mid)["status"] == "in-force"
    out = handler.edit(id=mid, meta={"status": "absorbed"})
    assert "meta: status" in out.body
    assert _meta(store, mid)["status"] == "absorbed"
    assert _meta(store, mid)["ruled_by"] == "Reto"


def test_edit_with_a_body_edit_still_drops_the_legacy_status_tag(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _ruling(handler, "body edit", body="old text here")
    handler.tag(id=mid, add=["status:in-force"])
    out = handler.edit(
        id=mid, mode="find-replace", find="old", text="new", meta={"status": "archived"}
    )
    assert "status:in-force" in out.body
    assert "status:in-force" not in _open_tags(store, mid)


def test_writing_status_drops_the_duplicate_status_tag(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _ruling(handler, "tagged ruling")
    handler.tag(id=mid, add=["status:in-force"])
    out = handler.edit(id=mid, meta={"status": "absorbed"})
    assert "dropped tag(s) ['status:in-force']" in out.body
    tags = _open_tags(store, mid)
    assert "status:in-force" not in tags and "ruling" in tags


def test_dry_run_edit_does_not_touch_the_status_tag(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _ruling(handler, "dry ruling", body="alpha beta")
    handler.tag(id=mid, add=["status:in-force"])
    handler.edit(
        id=mid,
        mode="find-replace",
        find="alpha",
        text="gamma",
        meta={"status": "absorbed"},
        dry_run=True,
    )
    assert "status:in-force" in _open_tags(store, mid)
    assert _meta(store, mid)["status"] == "in-force"


# ── the rulings view ────────────────────────────────────────────────────


def _rulings(handler: MemoryHandler, tags: list[str] | None = None, **kw: Any) -> str:
    return handler.search(tags=["ruling", *(tags or [])], view="rulings", **kw).body


def test_view_groups_by_status_and_hides_non_live_by_default(
    handler: MemoryHandler,
) -> None:
    live = _ruling(handler, "Live one")
    done = _ruling(handler, "Done one")
    handler.edit(id=done, meta={"status": "absorbed"})
    body = _rulings(handler)
    assert f"Live one (me{live})" in body
    assert "Done one" not in body
    assert "## in-force (1)" in body
    assert "1 absorbed hidden" in body and "'status': '*'" in body


def test_view_status_star_lists_every_status_in_group_order(
    handler: MemoryHandler,
) -> None:
    for title, status in (
        ("Retired r", "retired"),
        ("Archived a", "archived"),
        ("Absorbed b", "absorbed"),
        ("Live l", "in-force"),
    ):
        handler.edit(id=_ruling(handler, title), meta={"status": status})
    body = _rulings(handler, status="*")
    heads = [ln for ln in body.splitlines() if ln.startswith("## ")]
    assert heads == [
        "## in-force (1)",
        "## absorbed (1)",
        "## archived (1)",
        "## retired (1)",
    ]
    assert "hidden" not in body


def test_view_status_filter_names_one_status(handler: MemoryHandler) -> None:
    handler.edit(id=_ruling(handler, "Arch"), meta={"status": "archived"})
    _ruling(handler, "Live")
    body = _rulings(handler, status="archived")
    assert "Arch" in body and "Live" not in body


def test_view_unknown_status_is_refused_with_options(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput) as exc:
        _rulings(handler, status="stale")
    assert exc.value.options and "in-force" in exc.value.options


def test_view_orders_expiring_first_then_newest(handler: MemoryHandler) -> None:
    today = datetime.now(UTC).date()

    def day(offset: int) -> str:
        return (today + timedelta(days=offset)).isoformat()

    far = day(30)
    older = _ruling(handler, "Old far", ruled_on=day(-90), review_by=far)
    newer = _ruling(handler, "New far", ruled_on=day(-60), review_by=far)
    due = _ruling(handler, "Due soon", ruled_on=day(-50), review_by=day(1))
    late = _ruling(handler, "Overdue", ruled_on=day(-40), review_by=day(-2))
    body = _rulings(handler)
    positions = [body.index(f"(me{h})") for h in (late, due, newer, older)]
    assert positions == sorted(positions)
    assert "overdue 2d" in body and "in 1d, expiring" in body


def test_view_lists_governed_nodes_one_hop_out(
    handler: MemoryHandler, store: Store
) -> None:
    ruling = _ruling(handler, "Governs things", ruled_by="Reto", ruled_on="2026-11-01")
    target = id_of(handler.put(text="spec section body", title="Spec section").body)
    other = id_of(handler.put(text="unrelated", title="Merely related").body)
    handler.link(
        id=ruling,
        target=f"memory:{target}",
        rel="related-to",
        meta={"rel_intent": "decides"},
    )
    store.add_link(src_ref_id=ruling, dst_ref_id=other, relation="related-to")
    body = _rulings(handler)
    assert f"decides: me{target} 'Spec section'" in body
    assert "Merely related" not in body
    assert "ruled 2026-11-01 by Reto" in body


def test_view_refuses_what_it_cannot_apply(handler: MemoryHandler) -> None:
    for kw in ({"q": "words"}, {"mode": "lexical"}, {"scope": "title"}, {"page": 2}):
        with pytest.raises(BadInput, match="not applied by view='rulings'") as exc:
            _rulings(handler, **kw)
        assert exc.value.next


def test_view_extra_tags_narrow(handler: MemoryHandler) -> None:
    keep = id_of(handler.put(text="x", title="Kept", tags=["ruling", "factory"]).body)
    _ruling(handler, "Dropped")
    body = _rulings(handler, tags=["factory"])
    assert f"(me{keep})" in body and "Dropped" not in body


def test_view_reads_a_legacy_status_tag_before_migration(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _legacy(handler, "Legacy", "legacy", "status:absorbed")
    assert "Legacy" not in _rulings(handler)
    assert f"## absorbed (1)\n- Legacy (me{mid})" in _rulings(handler, status="*")


def test_view_empty_says_how_to_file_one(handler: MemoryHandler) -> None:
    assert "no rulings match" in _rulings(handler)


def test_view_through_the_runtime_dispatch(
    runtime_with_store: PrecisRuntime, handler: MemoryHandler
) -> None:
    mid = _ruling(handler, "Dispatch ruling")
    handler.edit(
        id=_ruling(handler, "Archived via dispatch"), meta={"status": "archived"}
    )
    out = runtime_with_store.dispatch(
        "search", {"kind": "memory", "tags": ["ruling"], "view": "rulings"}
    )
    assert "[error" not in out and f"(me{mid})" in out
    assert "Archived via dispatch" not in out
    wide = runtime_with_store.dispatch(
        "search",
        {
            "kind": "memory",
            "tags": ["ruling"],
            "view": "rulings",
            "status": "*",
        },
    )
    assert "[error" not in wide and "Archived via dispatch" in wide


# ── recall ──────────────────────────────────────────────────────────────


def test_default_search_leaves_out_archived_and_absorbed(
    handler: MemoryHandler,
) -> None:
    live = _ruling(handler, "Quokka live", body="quokka scheduler restart rule")
    arch = _ruling(handler, "Quokka arch", body="quokka scheduler restart rule old")
    absb = _ruling(handler, "Quokka absorbed", body="quokka scheduler restart rule x")
    handler.edit(id=arch, meta={"status": "archived"})
    handler.edit(id=absb, meta={"status": "absorbed"})
    body = handler.search(q="quokka scheduler").body
    assert f"memory {live}:" in body
    assert f"memory {arch}:" not in body and f"memory {absb}:" not in body


def test_index_search_leaves_out_archived_and_absorbed(
    handler: MemoryHandler,
) -> None:
    live = _ruling(handler, "Wombat live", body="wombat burrow policy")
    gone = _ruling(handler, "Wombat gone", body="wombat burrow policy old")
    handler.edit(id=gone, meta={"status": "absorbed"})
    body = handler.search(q="wombat burrow", view="index").body
    assert f"(me{live}" in body and f"(me{gone}" not in body
    listing = handler.search(view="index", tags=["ruling"]).body
    assert f"(me{live}" in listing and f"(me{gone}" not in listing


def test_recall_excludes_a_legacy_tagged_absorbed_ruling(
    handler: MemoryHandler,
) -> None:
    mid = _legacy(handler, "Ocelot", "ocelot habitat rule", "status:absorbed")
    assert f"memory {mid}:" not in handler.search(q="ocelot habitat").body


def test_flipping_back_to_in_force_restores_recall(handler: MemoryHandler) -> None:
    mid = _ruling(handler, "Lemur", body="lemur canopy rule")
    handler.edit(id=mid, meta={"status": "archived"})
    assert f"memory {mid}:" not in handler.search(q="lemur canopy").body
    handler.edit(id=mid, meta={"status": "in-force"})
    assert f"memory {mid}:" in handler.search(q="lemur canopy").body


def test_archived_ruling_stays_resolvable_by_get(
    handler: MemoryHandler,
) -> None:
    mid = _ruling(handler, "Kept for history", body="history text")
    handler.edit(id=mid, meta={"status": "archived"})
    assert "history text" in handler.get(id=mid).body


def test_non_ruling_memories_are_unaffected(handler: MemoryHandler) -> None:
    mid = id_of(handler.put(text="narwhal tusk note", tags=["status:absorbed"]).body)
    assert f"memory {mid}:" in handler.search(q="narwhal tusk").body


# ── the one-off migration CLI ───────────────────────────────────────────

_META_BODY = (
    "Land at once.\n\nMeta: ruled_by: Reto; ruled_on: 2026-11-01; status: absorbed"
)


def test_migrate_rulings_is_a_dry_run_by_default(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _legacy(handler, "Mig", _META_BODY, "status:absorbed")
    out = migrate_rulings(store)
    assert "dry run" in out and "would write" in out
    assert "status" not in _meta(store, mid)
    assert "status:absorbed" in _open_tags(store, mid)


def test_migrate_rulings_apply_writes_meta_and_is_idempotent(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _legacy(handler, "Mig", _META_BODY, "status:absorbed")
    bare = _legacy(handler, "Bare", "no meta line")
    out = migrate_rulings(store, apply=True)
    meta = _meta(store, mid)
    assert (meta["ruled_by"], meta["status"], meta["ruled_on"]) == (
        "Reto",
        "absorbed",
        "2026-11-01",
    )
    assert meta["review_by"] == "2026-11-15"
    assert "status:absorbed" not in _open_tags(store, mid)
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and "Meta:" in handler._body_text(ref)
    assert f"me{bare}" in out and "no 'Meta:' line found" in out
    again = migrate_rulings(store, apply=True)
    assert f"me{mid}" not in again
