"""Gripe STATUS vocabulary: app-level check + web live count.

Gripe STATUS is one of open, triaged, ready_for_fix, in_review, verify,
done, wontfix. ``refuted`` is a finding status and must not land on a
gripe; a gripe with no STATUS tag lists and counts as open.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.store import Store
from precis.store.types import Tag

pytestmark = pytest.mark.db


def test_parse_strict_rejects_refuted_on_gripe() -> None:
    with pytest.raises(BadInput, match="invalid STATUS value for kind 'gripe'"):
        Tag.parse_strict("STATUS:refuted", kind="gripe")


def test_parse_strict_status_error_teaches_search_wildcard() -> None:
    """gr484229: the STATUS-tag error is what agents hit after guessing
    ``status='any'`` or a comma list — it must name the ``'*'`` opt-out."""
    with pytest.raises(BadInput) as exc:
        Tag.parse_strict("STATUS:any", kind="gripe")
    nxt = exc.value.next
    assert isinstance(nxt, str) and "status='*'" in nxt


def _seed_gripe(store: Store, status: str) -> int:
    with store.tx() as conn:
        return _insert_gripe(store, conn, status=status)


def test_search_status_any_all_is_wildcard(hub: Any, store: Store) -> None:
    """gr484229: ``status='any'`` (and 'all') mean the ``'*'`` opt-out —
    include terminal statuses the default hides."""
    from precis.dispatch import Hub
    from precis.handlers.gripe import GripeHandler

    assert isinstance(hub, Hub)
    h = GripeHandler(hub=hub)
    open_id = _seed_gripe(store, "open")
    done_id = _seed_gripe(store, "done")
    default = h.search(tags=["STATUS:open"])
    assert str(open_id) in default.body and str(done_id) not in default.body
    for spelling in ("any", "all", "*"):
        out = h.search(status=spelling)
        assert str(open_id) in out.body, spelling
        assert str(done_id) in out.body, spelling


def test_search_status_comma_list_rejected_with_wildcard_hint(
    hub: Any, store: Store
) -> None:
    """``status='triaged,ready_for_fix'`` can't be expressed (a ref
    carries one STATUS; tags AND) — the error names ``status='*'``."""
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        h.search(status="triaged,ready_for_fix")
    assert "takes one value" in str(exc.value)
    nxt = exc.value.next
    assert isinstance(nxt, str) and "status='*'" in nxt


def test_search_status_pipe_list_rejected_naming_valid_values(
    hub: Any, store: Store
) -> None:
    """gr489410: the pipe-joined STATUS vocabulary in the kind description
    (``open|triaged|…``) gets pasted as one ``status=`` value — the error
    must say "one value" AND list what the kind accepts."""
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        h.search(status="open|triaged|ready_for_fix|in_review")
    assert "takes one value" in str(exc.value)
    assert "ready_for_fix" in str(exc.value)
    nxt = exc.value.next
    assert isinstance(nxt, str) and "status='*'" in nxt


def test_search_status_invalid_value_names_wildcard(hub: Any, store: Store) -> None:
    """An unknown explicit status= fails in ``_apply_status_default``
    with the opt-out in the hint, before tag normalisation would."""
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        h.search(status="bogus")
    assert "invalid status= value" in str(exc.value)
    nxt = exc.value.next
    assert isinstance(nxt, str) and "status='*'" in nxt


def test_parse_strict_accepts_refuted_on_finding_and_in_review_on_gripe() -> None:
    assert Tag.parse_strict("STATUS:refuted", kind="finding").value == "refuted"
    assert Tag.parse_strict("STATUS:in_review", kind="gripe").value == "in_review"


def test_parse_strict_accepts_verify_on_gripe_and_full_vocab() -> None:
    """pipeline v2 slice 1: ``verify`` joins the gripe vocabulary
    (migration 0247) — accepted on gripe and on kinds that take the
    full STATUS union (``finding``, like ``in_review`` today), refused
    on the kinds carrying their own subset."""
    assert Tag.parse_strict("STATUS:verify", kind="gripe").value == "verify"
    assert Tag.parse_strict("STATUS:verify", kind="finding").value == "verify"
    for kind in ("todo", "job", "quest"):
        with pytest.raises(BadInput, match=f"invalid STATUS value for kind '{kind}'"):
            Tag.parse_strict("STATUS:verify", kind=kind)


def test_web_ranked_values_match_gripe_vocab() -> None:
    from precis.store.types import _KIND_STATUS_VALUES
    from precis_web.routes.gripes import _RANKED_VALUES

    assert frozenset(_RANKED_VALUES) == _KIND_STATUS_VALUES["gripe"]


def _insert_gripe(store: Store, conn: Any, *, status: str | None) -> int:
    ref = store.insert_ref(kind="gripe", slug=None, title="x", meta={}, conn=conn)
    if status is not None:
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent", conn=conn)
    return int(ref.id)


def test_web_count_and_rows_treat_untagged_gripe_as_open(store: Store) -> None:
    from precis_web.nav import _gripes_count
    from precis_web.routes.gripes import _rows

    with store.tx() as conn:
        _insert_gripe(store, conn, status="open")
        _insert_gripe(store, conn, status="done")
        rid = _insert_gripe(store, conn, status="open")
    # Simulate legacy drift: strip the STATUS row from one gripe with the
    # triggers bypassed (replica role skips non-ALWAYS triggers).
    with store.tx() as conn:
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("DELETE FROM ref_tags WHERE ref_id = %s", (rid,))
    assert _gripes_count(store) == 2
    rows = _rows(store, status_filter="live")
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"open"}


def test_live_lists_include_verify(store: Store) -> None:
    """``verify`` is non-terminal: the web live queue and the nav badge
    count a verify gripe the same as open."""
    from precis_web.nav import _gripes_count
    from precis_web.routes.gripes import _rows

    with store.tx() as conn:
        _insert_gripe(store, conn, status="verify")
        _insert_gripe(store, conn, status="done")
    assert _gripes_count(store) == 1
    rows = _rows(store, status_filter="live")
    assert [r["status"] for r in rows] == ["verify"]


# ── status='!v' exclusion (gr496191) ────────────────────────────────


def test_search_status_bang_excludes(hub: Any, store: Store) -> None:
    """gr496191: ``status='!wontfix'`` is every status but wontfix —
    the exclusion the prod callers reached for via ``exclude=['wontfix']``
    (and as ``status='open|triaged|…'`` for the non-terminal cohort)."""
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    open_id = _seed_gripe(store, "open")
    done_id = _seed_gripe(store, "done")
    wontfix_id = _seed_gripe(store, "wontfix")
    out = h.search(status="!wontfix")
    assert f"gr{open_id}" in out.body and f"gr{done_id}" in out.body
    assert f"gr{wontfix_id}" not in out.body


def test_search_status_bang_list_unions_exclusions(hub: Any, store: Store) -> None:
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    open_id = _seed_gripe(store, "open")
    done_id = _seed_gripe(store, "done")
    wontfix_id = _seed_gripe(store, "wontfix")
    out = h.search(status="!done|!wontfix")
    assert f"gr{open_id}" in out.body
    assert f"gr{done_id}" not in out.body and f"gr{wontfix_id}" not in out.body


def test_search_status_bang_invalid_value_refused(hub: Any, store: Store) -> None:
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    with pytest.raises(BadInput) as exc:
        h.search(status="!bogus")
    assert "invalid status= value" in str(exc.value)


def test_search_status_bang_count_view(hub: Any, store: Store) -> None:
    from precis.handlers.gripe import GripeHandler

    h = GripeHandler(hub=hub)
    _seed_gripe(store, "open")
    _seed_gripe(store, "wontfix")
    out = h.search(status="!wontfix", view="count")
    assert "# 1 gripe entry" in out.body


def test_search_status_bang_reaches_chunk_q_path(hub: Any, store: Store) -> None:
    """The q= path searches gripe body chunks — the exclusion must reach
    the chunk-search SQL, not just the listing paths."""
    from precis.handlers.gripe import GripeHandler
    from precis.store.types import ChunkInsert

    h = GripeHandler(hub=hub)
    open_id = _seed_gripe(store, "open")
    wontfix_id = _seed_gripe(store, "wontfix")
    for rid in (open_id, wontfix_id):
        store.chunks.insert_chunks(
            rid, [ChunkInsert(ord=0, text="flaky ingest timeout", meta={})]
        )
    out = h.search(q="flaky", status="!wontfix")
    assert f"## gripe {open_id}" in out.body
    assert f"## gripe {wontfix_id}" not in out.body
