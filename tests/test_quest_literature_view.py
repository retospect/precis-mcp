"""``view='literature'`` — the weekly "what literature arrived" read
(gr592952): inbound ``serves`` links inside a ``[since, until)`` window,
newest first, each row dated (linked_at + the ref's created_at) and
carrying the lit-search query that found it (edge ``meta.found_by``, else
the matching lit-search logbook line, else ``-``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.errors import BadInput
from precis.quest.logbook import append_entry


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    return QuestHandler(hub=Hub(store=store))


def _quest(store: Any) -> tuple[Any, int]:
    h = _handler(store)
    resp = h.put(text="A striving with literature")
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None
    return h, int(m.group(1))


def _serve(
    store: Any,
    src_id: int,
    qid: int,
    *,
    linked_at: datetime,
    found_by: str | None = None,
) -> None:
    """A serves edge with a controlled link timestamp (add_link stamps
    now(), so backdate the row afterwards)."""
    meta = {"found_by": found_by} if found_by is not None else None
    store.add_link(
        src_ref_id=src_id,
        dst_ref_id=qid,
        relation="serves",
        set_by="agent",
        meta=meta,
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET created_at = %s "
            "WHERE src_ref_id = %s AND dst_ref_id = %s AND relation = 'serves'",
            (linked_at, src_id, qid),
        )
        conn.commit()


def _paper(store: Any, slug: str, title: str, *, days_ago: float = 30.0) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=title)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '1 day' * %s "
            "WHERE ref_id = %s",
            (days_ago, ref.id),
        )
        conn.commit()
    return ref.id


T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


class TestLiteratureWindow:
    def test_rows_in_window_only(self, store: Any) -> None:
        h, qid = _quest(store)
        recent = _paper(store, "recent-one", "A recent arrival")
        old = _paper(store, "old-one", "An old arrival")
        _serve(store, recent, qid, linked_at=T0 + timedelta(days=1))
        _serve(store, old, qid, linked_at=T0 - timedelta(days=30))

        body = h.get(
            id=qid,
            view="literature",
            args={"since": "2026-11-01T00:00:00Z", "until": "2026-11-16T00:00:00Z"},
        ).body
        assert "1 linked" in body
        assert "A recent arrival" in body
        assert f"pa{old}" not in body
        assert "An old arrival" not in body

    def test_until_is_exclusive_since_inclusive(self, store: Any) -> None:
        h, qid = _quest(store)
        edge = _paper(store, "edge-one", "On the boundary")
        _serve(store, edge, qid, linked_at=T0)

        body = h.get(
            id=qid,
            view="literature",
            args={"since": "2026-11-10T12:00:00Z"},
        ).body
        assert f"pa{edge}" in body
        body = h.get(
            id=qid,
            view="literature",
            args={"until": "2026-11-10T12:00:00Z"},
        ).body
        assert f"pa{edge}" not in body

    def test_kind_filter(self, store: Any) -> None:
        h, qid = _quest(store)
        paper = _paper(store, "k-paper", "A paper")
        finding = store.insert_ref(kind="finding", slug=None, title="A finding")
        _serve(store, paper, qid, linked_at=T0 + timedelta(days=1))
        _serve(store, finding.id, qid, linked_at=T0 + timedelta(days=1))

        body = h.get(id=qid, view="literature", args={"kind": "finding"}).body
        assert f"fi{finding.id}" in body
        assert f"pa{paper}" not in body

    def test_default_view_excludes_non_literature_kinds(self, store: Any) -> None:
        # td702287 review: the default is papers and findings — a
        # structure or concept in the quest's service must not inflate
        # the arrival rows or the acquisition count.
        h, qid = _quest(store)
        paper = _paper(store, "def-paper", "A paper arrival")
        concept = store.insert_ref(
            kind="concept", slug=None, title="A concept in service"
        )
        structure = store.insert_ref(
            kind="structure", slug="def-struct", title="A structure in service"
        )
        _serve(store, paper, qid, linked_at=T0 + timedelta(days=1))
        _serve(store, concept.id, qid, linked_at=T0 + timedelta(days=1))
        _serve(store, structure.id, qid, linked_at=T0 + timedelta(days=1))

        body = h.get(id=qid, view="literature").body
        assert "A paper arrival" in body
        assert "A concept in service" not in body
        assert "A structure in service" not in body
        assert "1 linked" in body
        assert "1 of them newly acquired" in body

    def test_bad_kind_refused(self, store: Any) -> None:
        h, qid = _quest(store)
        with pytest.raises(BadInput, match="kind"):
            h.get(id=qid, view="literature", args={"kind": "todo"})

    def test_newly_acquired_summary_counts_ref_created_in_window(
        self, store: Any
    ) -> None:
        h, qid = _quest(store)
        fresh = _paper(store, "fresh", "Freshly minted", days_ago=0.5)
        held = _paper(store, "held", "Held for a year", days_ago=400)
        _serve(store, fresh, qid, linked_at=T0 + timedelta(days=1))
        _serve(store, held, qid, linked_at=T0 + timedelta(days=1))

        body = h.get(
            id=qid,
            view="literature",
            args={"since": "2026-11-01T00:00:00Z"},
        ).body
        assert "2 linked, 1 of them newly acquired in the window" in body


class TestLiteratureFoundBy:
    def test_found_by_from_edge_meta(self, store: Any) -> None:
        h, qid = _quest(store)
        paper = _paper(store, "meta-one", "Found by meta")
        _serve(
            store,
            paper,
            qid,
            linked_at=T0 + timedelta(days=1),
            found_by="perovskite water splitting",
        )
        body = h.get(id=qid, view="literature").body
        assert 'found by "perovskite water splitting"' in body

    def test_found_by_logbook_fallback(self, store: Any) -> None:
        h, qid = _quest(store)
        paper = _paper(store, "legacy-one", "Found by the logbook")
        _serve(store, paper, qid, linked_at=T0 + timedelta(days=1))
        append_entry(
            store,
            qid,
            text=f'lit-search: "NO NH3 Pd catalyst" → linked 1 source(s): pa{paper}',
            entry_type="result",
            by="agent",
            extra_meta={"signal": "lit-search"},
        )
        body = h.get(id=qid, view="literature").body
        assert 'found by "NO NH3 Pd catalyst"' in body

    def test_found_by_dash_when_nothing(self, store: Any) -> None:
        h, qid = _quest(store)
        paper = _paper(store, "plain-one", "No provenance")
        _serve(store, paper, qid, linked_at=T0 + timedelta(days=1))
        body = h.get(id=qid, view="literature").body
        assert 'found by "-"' in body


class TestLiteraturePaging:
    def test_next_page_when_more_than_100(self, store: Any) -> None:
        h, qid = _quest(store)
        ids = [_paper(store, f"pg-{i}", f"Paper number {i}") for i in range(105)]
        for i, rid in enumerate(ids):
            _serve(store, rid, qid, linked_at=T0 + timedelta(minutes=i))

        body = h.get(id=qid, view="literature").body
        assert "105 linked" in body
        assert "next:" in body
        assert "'offset': 100" in body

        page2 = h.get(id=qid, view="literature", args={"offset": 100}).body
        assert "5 linked" in page2
        assert "next:" not in page2

    def test_newest_first_ordering(self, store: Any) -> None:
        h, qid = _quest(store)
        older = _paper(store, "ord-old", "Older link")
        newer = _paper(store, "ord-new", "Newer link")
        _serve(store, older, qid, linked_at=T0)
        _serve(store, newer, qid, linked_at=T0 + timedelta(days=2))

        body = h.get(id=qid, view="literature").body
        assert body.index(f"pa{newer}") < body.index(f"pa{older}")


class TestLiteratureBudgetFit:
    """td702287 review: an over-cap literature page used to lose its
    ``next:`` trailer to the generic line-fit, leaving the rest of the
    window unreachable. The response now carries a row-granular
    ``budget_fit``: whole rows only, resuming at the first omitted row."""

    def _ten_arrivals(self, store: Any) -> tuple[Any, int]:
        h, qid = _quest(store)
        ids = [_paper(store, f"bf-{i}", f"Budget row {i}") for i in range(10)]
        for i, rid in enumerate(ids):
            _serve(store, rid, qid, linked_at=T0 + timedelta(minutes=i))
        return h, qid

    def test_under_cap_fit_returns_the_full_page(self, store: Any) -> None:
        h, qid = self._ten_arrivals(store)
        resp = h.get(id=qid, view="literature")
        assert resp.budget_fit is not None
        assert resp.budget_fit(len(resp.body.encode("utf-8"))) == resp.body
        assert resp.budget_fit(10_000_000) == resp.body

    def test_over_cap_fit_cuts_whole_rows_and_keeps_next(self, store: Any) -> None:
        h, qid = self._ten_arrivals(store)
        resp = h.get(id=qid, view="literature")
        full = resp.body
        cap = len(full.encode("utf-8")) // 2
        fitted = resp.budget_fit(cap)
        assert fitted != full
        rows = [ln for ln in fitted.splitlines() if ln.startswith("- ")]
        assert 0 < len(rows) < 10
        # whole rows only — every surviving row is a complete line
        assert all("found by " in ln for ln in rows)
        # the trailer survives the cut and resumes at the first omitted row
        assert "next:" in fitted
        assert f"'offset': {len(rows)}" in fitted
        # following the resume offset shows exactly the omitted tail
        page2 = h.get(id=qid, view="literature", args={"offset": len(rows)}).body
        page2_rows = {ln for ln in page2.splitlines() if ln.startswith("- ")}
        assert not page2_rows.intersection(rows)

    def test_fit_where_nothing_fits_raises_with_a_next(self, store: Any) -> None:
        from precis.errors import ResultBudgetExceeded

        h, qid = self._ten_arrivals(store)
        resp = h.get(id=qid, view="literature")
        with pytest.raises(ResultBudgetExceeded, match="even one arrival"):
            resp.budget_fit(10)
