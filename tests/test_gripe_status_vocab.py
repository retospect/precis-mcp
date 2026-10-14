"""Gripe STATUS vocabulary: app-level check + web live count.

Gripe STATUS is one of open, triaged, ready_for_fix, in_review, done,
wontfix. ``refuted`` is a finding status and must not land on a gripe; a
gripe with no STATUS tag lists and counts as open.
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
