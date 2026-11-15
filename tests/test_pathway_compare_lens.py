"""Tests for the table lens on ``view='compare'`` (gr592637).

The compare view pages its sibling table through the shared lens
(:mod:`precis.format.table_lens`): ``args={'where': …, 'columns':
[...], 'limit': N, 'cursor': token}`` cut the ranked rows server-side
and the footer names the exact call that continues — a long sibling set
answers in ~4 kB instead of cutting positionally at the transport cap.

DB-free: ``PathwayHandler._compare`` only touches the store through one
sibling-scan query and ``trust_gate.sibling_evidence_batch`` — both are
faked here, with hand-built reaction graphs (the same shape
:mod:`tests.test_reaction_graph_window` uses).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from precis.utils import trust_gate
from precis_pathway.handler import PathwayHandler

_N = 30


def _graph(i: int) -> dict[str, Any]:
    """A→B→T chain; barrier rises with ``i`` so the compare rank
    (RATE ascending) is exactly ``i`` ascending."""
    b = 0.30 + 0.05 * i
    return {
        "nodes": [
            {"id": "A", "rel_energy": 0.0, "n_H": 0},
            {"id": "B", "rel_energy": 0.10, "n_H": 0},
            {"id": "T", "rel_energy": -0.20 - 0.01 * i, "n_H": 0},
        ],
        "links": [
            {"source": "A", "target": "B", "kind": "reaction", "barrier": b},
            {"source": "B", "target": "T", "kind": "reaction", "barrier": 0.2},
        ],
    }


def _rows() -> list[tuple[Any, ...]]:
    """The sibling-scan row shape: ref_id, slug, meta, cand_id,
    cand_title, cand_meta."""
    out = []
    for i in range(_N):
        meta = {
            "status": "ready",
            "results": {
                "substrate": "NO",
                "target": "NO3",
                "pathway": ["A", "B", "T"],
            },
            "graph": _graph(i),
            "config": {"slab": {"element": "Pd" if i % 2 == 0 else "Pt"}},
        }
        out.append((1000 + i, f"sib-{i:02d}", meta, None, None, None))
    return out


class _FakeConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def execute(self, *_a: Any, **_k: Any) -> _FakeConn:
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None


class _FakePool:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def connection(self) -> _FakeConn:
        return _FakeConn(self._rows)


_CURSOR_RE = re.compile(r"args=\{'cursor': '([^']+)'\}")


def _compare(handler: PathwayHandler, store: Any, **kw: Any) -> str:
    ref = SimpleNamespace(id=1)
    meta = {"results": {"substrate": "NO", "target": "NO3"}}
    return handler._compare(store, ref, meta, **kw)


@pytest.fixture
def handler(monkeypatch: pytest.MonkeyPatch) -> PathwayHandler:
    monkeypatch.setattr(trust_gate, "sibling_evidence_batch", lambda *_a, **_k: {})
    return object.__new__(PathwayHandler)


def test_compare_limit_and_cursor(handler: PathwayHandler) -> None:
    store = SimpleNamespace(pool=_FakePool(_rows()))
    first = _compare(handler, store, limit=10)
    assert "sib-00" in first  # best RATE ranks first
    assert "sib-29" not in first
    assert "next: get(kind='pathway', id='pw1', view='compare'" in first
    m = _CURSOR_RE.search(first)
    assert m is not None, first

    seen = set(re.findall(r"sib-\d\d", "\n".join(line for line in first.splitlines())))
    cursor: str | None = m.group(1)
    bodies = [first]
    while cursor is not None:
        body = _compare(handler, store, cursor=cursor)
        bodies.append(body)
        handles = set(re.findall(r"sib-\d\d", "\n".join(body.splitlines())))
        assert not handles & seen, "cursor page overlaps a previous one"
        seen |= handles
        nxt = _CURSOR_RE.search(body)
        cursor = nxt.group(1) if nxt else None
    assert seen == {f"sib-{i:02d}" for i in range(_N)}, seen
    # every row visited exactly once, in rank order across the pages
    rank = [h for b in bodies for h in re.findall(r"sib-\d\d", b)]
    assert len(rank) == len(set(rank)) == _N

    # the lens footer rides every page
    for body in bodies[:-1]:
        assert "lens:" in body
        assert "hidden-row aggregates:" in body


def test_compare_where_and_columns(handler: PathwayHandler) -> None:
    store = SimpleNamespace(pool=_FakePool(_rows()))
    body = _compare(handler, store, where={"lever": "Pd"}, columns=["cand", "RATE"])
    pd = {f"sib-{i:02d}" for i in range(_N) if i % 2 == 0}
    pt = {f"sib-{i:02d}" for i in range(_N) if i % 2 == 1}
    found = set(re.findall(r"sib-\d\d", "\n".join(body.splitlines())))
    assert pd <= found
    assert not pt & found
    assert "SPAN" not in body  # columns= narrowed the scalar schema
    assert "RATE" in body


def test_compare_refuses_bad_lens_args(handler: PathwayHandler) -> None:
    from precis.errors import BadInput

    store = SimpleNamespace(pool=_FakePool(_rows()))
    with pytest.raises(BadInput):
        _compare(handler, store, where={"nosuch": "x"})
    with pytest.raises(BadInput):
        _compare(handler, store, columns=["nosuch"])
    # sort is refused before it reaches the view: compare ranks by
    # RATE (SPAN at U) and a caller-side sort would scramble the
    # ranking across pages.
    from precis_pathway.handler import _compare_lens_args

    with pytest.raises(BadInput):
        _compare_lens_args({"sort": "-RATE"})
