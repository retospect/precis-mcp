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
