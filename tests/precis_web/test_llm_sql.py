"""Real-PG tests for the ``/llm/rules`` route's raw SQL (``routes/llm.py``).

The web ``FakeStore`` never parses SQL, so the INSERT/UPDATE/SELECT behind
the keyword-rule editor (migration 0237 ``llm_keyword_routes``) is only
proven here, against the live ``store`` fixture.
"""

from __future__ import annotations

from typing import Any

from precis_web.routes.llm import _add_rule, _move_rule, _toggle_rule


def _rows(store: Any) -> list[tuple]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT id, position, pattern, is_regex, target, sources, tiers, "
            "enabled, note FROM llm_keyword_routes ORDER BY position, id"
        ).fetchall()


def _add(store: Any, pattern: str, **kw: Any) -> None:
    _add_rule(
        store,
        pattern=pattern,
        is_regex=kw.get("is_regex", False),
        target=kw.get("target", {"model": "m"}),
        sources=kw.get("sources"),
        tiers=kw.get("tiers"),
        note=kw.get("note", ""),
    )


def test_add_rule_appends_with_next_position_and_roundtrips(store) -> None:
    _add(store, "alpha", target={"tier": "big"}, sources=["a", "b"], note="why")
    _add(store, "beta", is_regex=True, target={"chain": [{"model": "x"}]})

    first, second = _rows(store)
    assert first[2] == "alpha" and second[2] == "beta"
    assert second[1] == first[1] + 1
    assert first[3] is False and second[3] is True
    assert first[4] == {"tier": "big"}
    assert second[4] == {"chain": [{"model": "x"}]}
    assert first[5] == ["a", "b"] and first[6] is None
    assert first[7] is True
    assert first[8] == "why" and second[8] is None  # empty note -> NULL


def test_toggle_rule_flips_enabled_and_bumps_updated_at(store) -> None:
    _add(store, "gamma")
    rid = _rows(store)[0][0]
    _toggle_rule(store, rid, False)
    assert _rows(store)[0][7] is False
    _toggle_rule(store, rid, True)
    assert _rows(store)[0][7] is True


def test_move_rule_swaps_neighbours_and_renumbers_densely(store) -> None:
    for p in ("one", "two", "three"):
        _add(store, p)
    ids = {r[2]: r[0] for r in _rows(store)}

    _move_rule(store, ids["three"], "up")
    assert [r[2] for r in _rows(store)] == ["one", "three", "two"]
    assert [r[1] for r in _rows(store)] == [1, 2, 3]

    _move_rule(store, ids["one"], "up")  # already first: no-op
    assert [r[2] for r in _rows(store)] == ["one", "three", "two"]

    _move_rule(store, ids["one"], "down")
    assert [r[2] for r in _rows(store)] == ["three", "one", "two"]

    _move_rule(store, 10**9, "down")  # unknown id: no-op
    assert [r[2] for r in _rows(store)] == ["three", "one", "two"]
