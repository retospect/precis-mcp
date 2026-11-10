"""``search(kind='quest', scope='qu<N>')`` + ``view='logbook'``'s
``args={'q': …}`` — ranked retrieval inside ONE quest's logbook
(gr592279).

Before this, no door searched inside one quest's logbook: ``scope=`` was
refused at the kwargs gate and a ``chunk_scope='qu<N>'`` was silently
ignored, returning the quest ref instead of its entries.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.errors import BadInput, NotFound


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    return QuestHandler(hub=Hub(store=store))


def _created_id(resp: Any) -> int:
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, f"no quest handle in ack: {resp.body!r}"
    return int(m.group(1))


def _quest_with_log(store: Any, entries: list[str]) -> tuple[Any, int]:
    h = _handler(store)
    qid = _created_id(h.put(text="A quest whose notebook is searched"))
    for text in entries:
        h.put(id=qid, text=text, entry="observation")
    return h, qid


def _ql_ids(store: Any, qid: int) -> list[int]:
    with store.pool.connection() as conn:
        return [
            int(r[0])
            for r in conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id=%s "
                "AND chunk_kind='quest_log' ORDER BY ord",
                (qid,),
            ).fetchall()
        ]


class TestScopedLogbookSearch:
    def test_scope_returns_ranked_entries_not_the_quest(self, store: Any) -> None:
        h, qid = _quest_with_log(
            store,
            [
                "Trusted anchor: Ta2O5 on Ir subsurface looks best",
                "The solvent rinse failed again",
                "Leading candidate remains the Ta Ir alloy",
            ],
        )
        out = h.search(q="trusted anchor Ta Ir subsurface", scope=f"qu{qid}").body
        # entries, not the quest ref itself
        assert "Trusted anchor" in out
        assert "Ta Ir alloy" in out
        assert "solvent rinse" not in out
        assert "logbook match" in out
        # handles are the linkable ql<chunk_id> form
        assert re.search(r"ql\d+ ~\d+", out)

    def test_scope_excludes_other_quests(self, store: Any) -> None:
        h, qid = _quest_with_log(store, ["manganese perovskite note"])
        h2 = _handler(store)
        other = _created_id(h2.put(text="Another quest"))
        h2.put(id=other, text="manganese perovskite note", entry="note")
        out = h.search(q="manganese perovskite", scope=qid).body
        ids = _ql_ids(store, qid)
        assert any(f"ql{cid}" in out for cid in ids)
        other_ids = _ql_ids(store, other)
        assert not any(f"ql{cid}" in out for cid in other_ids)

    def test_scope_spellings(self, store: Any) -> None:
        h, qid = _quest_with_log(store, ["vanadium redox flow entry"])
        cid = _ql_ids(store, qid)[0]
        for scope in (qid, f"qu{qid}", f"ql{cid}", f"quest:{qid}"):
            out = h.search(q="vanadium redox", scope=scope).body
            assert f"ql{cid}" in out, f"scope={scope!r}: {out}"
        # the args= spelling the gripe asked for
        out = h.search(q="vanadium redox", within=f"qu{qid}").body
        assert f"ql{cid}" in out
        # and the chunk_scope shape agents reached for first
        out = h.search(q="vanadium redox", chunk_scope=f"qu{qid}").body
        assert f"ql{cid}" in out

    def test_scope_disagreement_refused(self, store: Any) -> None:
        h, qid = _quest_with_log(store, ["x"])
        with pytest.raises(BadInput, match="disagree"):
            h.search(q="x", scope=f"qu{qid}", within="qu999")

    def test_scope_requires_q(self, store: Any) -> None:
        h, qid = _quest_with_log(store, ["x"])
        with pytest.raises(BadInput, match="still needs q="):
            h.search(scope=f"qu{qid}")

    def test_scope_dead_handle_is_notfound(self, store: Any) -> None:
        h, _qid = _quest_with_log(store, ["x"])
        with pytest.raises(NotFound, match="qu999999999"):
            h.search(q="x", scope="qu999999999")

    def test_scope_wrong_kind_refused(self, store: Any) -> None:
        h, _qid = _quest_with_log(store, ["x"])
        td = store.insert_ref(kind="todo", slug=None, title="a todo")
        with pytest.raises(BadInput, match="resolves to a todo"):
            h.search(q="x", scope=f"td{td.id}")

    def test_ref_filters_refused_loudly(self, store: Any) -> None:
        h, qid = _quest_with_log(store, ["x"])
        with pytest.raises(BadInput, match="not applied by a scoped"):
            h.search(q="x", scope=f"qu{qid}", status="active")
        with pytest.raises(BadInput, match="not applied by a scoped"):
            h.search(q="x", scope=f"qu{qid}", since="2026-01-01")

    def test_compact_views(self, store: Any) -> None:
        h, qid = _quest_with_log(store, ["zinc-air cycle note"])
        cid = _ql_ids(store, qid)[0]
        out = h.search(q="zinc-air", scope=f"qu{qid}", view="ids").body
        assert out.strip().endswith(f"ql{cid}")
        out = h.search(q="zinc-air", scope=f"qu{qid}", view="list").body
        assert f"ql{cid} ~0" in out and "zinc-air cycle note" in out

    def test_no_match_offers_recovery(self, store: Any) -> None:
        h, qid = _quest_with_log(store, ["vanadium note"])
        out = h.search(q="xyzzy frobnicate quux", scope=f"qu{qid}").body
        assert "no logbook entries" in out
        assert "view='logbook'" in out

    def test_unscoped_search_unchanged(self, store: Any) -> None:
        h, qid = _quest_with_log(store, ["a notebook entry"])
        out = h.search(q="notebook is searched").body
        assert f"qu{qid}" in out or str(qid) in out


class TestLogbookViewQ:
    def test_logbook_args_q_filters_entries(self, store: Any) -> None:
        h, qid = _quest_with_log(
            store,
            ["trusted anchor candidate Ta2O5", "the pump broke again"],
        )
        body = h.get(id=qid, view="logbook", args={"q": "trusted anchor"}).body
        assert "entries match" in body
        assert "trusted anchor candidate" in body
        assert "pump broke" not in body
        assert re.search(r"### ql\d+ ~\d+", body)

    def test_logbook_args_q_composes_with_window(self, store: Any) -> None:
        h, qid = _quest_with_log(
            store,
            ["anchor entry old", "anchor entry new"],
        )
        # age the first entry out of a '1h' window
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET created_at = created_at - interval '2 days' "
                "WHERE ref_id=%s AND ord=0",
                (qid,),
            )
            conn.commit()
        body = h.get(id=qid, view="logbook", args={"q": "anchor", "since": "1h"}).body
        assert "anchor entry new" in body
        assert "anchor entry old" not in body

    def test_logbook_args_q_empty_refused(self, store: Any) -> None:
        h, qid = _quest_with_log(store, ["x"])
        with pytest.raises(BadInput, match="is empty"):
            h.get(id=qid, view="logbook", args={"q": "  "})
