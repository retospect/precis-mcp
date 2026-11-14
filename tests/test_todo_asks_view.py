"""Dark-factory step 1b (td540847): ``search(kind='todo', view='asks')`` —
the asks list in three classes, grouped by quest then thread — and td541979:
a question for the owner is an ``ask-user`` todo carrying ``meta.ask``."""

from __future__ import annotations

from typing import Any

import pytest

from precis import alerts as alerts_mod
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.gripe import GripeHandler
from precis.handlers.todo import _TREE_SEARCH_VIEWS, TodoHandler, TodoView
from precis.store import Store
from precis.store.types import Tag
from precis.todo import asks_view
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _tree(handler: TodoHandler, store: Store, hub: Hub) -> dict[str, Any]:
    """A quest-served root with four slices and a gripe, one per source."""
    quest = store.insert_ref(kind="quest", slug=None, title="Precis runs its own work")
    root = id_of(
        handler.put(text="Dark factory root.", meta={"rotation_root": True}).body
    )
    store.add_link(src_ref_id=root, dst_ref_id=quest.id, relation="serves")
    store.add_tag(root, Tag.open("thread:factory"))

    def slice_(text: str) -> int:
        return id_of(
            handler.put(text=text, parent_id=root, meta={"llm_tier": None}).body
        )

    stopped = slice_("Claimed slice stopped on Reto.")
    handler.tag(
        id=stopped,
        add=["claimed-by:slice-x", "STATUS:doing", "waiting-for:reto"],
    )
    asking = slice_("Slice with a question.")
    store.add_tag(asking, Tag.open("ask-user:which database?"))
    shape = slice_("Slice with a shape question.")
    store.add_tag(shape, Tag.open("discussion"))
    plain = slice_("Slice nobody waits on.")
    gripe = id_of(
        GripeHandler(hub=hub)
        .put(text="Needs a ruling on the lane rule.", tags=["needs:ruling"])
        .body
    )
    store.add_link(src_ref_id=gripe, dst_ref_id=quest.id, relation="serves")
    return {
        "quest": quest.id,
        "root": root,
        "stopped": stopped,
        "asking": asking,
        "shape": shape,
        "plain": plain,
        "gripe": gripe,
    }


def _row(model: asks_view.AsksModel, ref_id: int) -> asks_view.AskRow:
    return next(r for r in model.rows if r.ref_id == ref_id)


def _ask(
    handler: TodoHandler,
    text: str = "Should the pass cut on a red gate?",
    *,
    blocks: str = "nothing",
    parent_id: int | None = None,
    **ask: Any,
) -> int:
    meta = {
        "ask": {
            "kind": "ruling",
            "options": ["yes, cut", "no, warn only"],
            "blocks": blocks,
            "context": "gate went red twice",
            **ask,
        }
    }
    return id_of(
        handler.put(
            text=text,
            parent_id=parent_id,
            tags=["ask-user", "thread:factory"],
            meta=meta,
        ).body
    )


def _blocked_by(store: Store, src: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
            "AND relation = 'blocked-by' ORDER BY dst_ref_id",
            (src,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def test_asks_view_is_wired_in_the_vocabulary() -> None:
    assert TodoView("asks") is TodoView.ASKS
    assert TodoView.ASKS in _TREE_SEARCH_VIEWS


def test_each_source_lands_in_its_class_under_its_quest(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    model = asks_view.asks_model(store)

    assert _row(model, t["stopped"]).klass == asks_view.SHOWSTOPPER
    assert _row(model, t["asking"]).klass == asks_view.DECISION
    assert _row(model, t["gripe"]).klass == asks_view.DECISION
    assert _row(model, t["shape"]).klass == asks_view.DISCUSSION
    assert all(r.ref_id != t["plain"] for r in model.rows)
    assert len(model.rows) == 4
    # every row resolves to the quest over the root's / its own serves link
    assert {r.quest_id for r in model.rows} == {t["quest"]}
    assert _row(model, t["stopped"]).quest_title == "Precis runs its own work"
    # thread comes from the nearest ancestor that carries one
    assert _row(model, t["asking"]).thread == "factory"
    assert _row(model, t["gripe"]).thread == ""
    # the inline ask question travels with the row
    assert _row(model, t["asking"]).question == "which database?"
    assert _row(model, t["stopped"]).reasons == (
        "waiting-for:reto",
        "claimed-by:slice-x",
    )


def test_waiting_for_reto_unclaimed_is_a_decision(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    handler.tag(id=t["plain"], add=["waiting-for:reto"])
    model = asks_view.asks_model(store)
    assert _row(model, t["plain"]).klass == asks_view.DECISION


def test_a_closed_row_drops_out(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    handler.tag(id=t["asking"], add=["STATUS:done"])
    model = asks_view.asks_model(store)
    assert all(r.ref_id != t["asking"] for r in model.rows)


def test_quest_of_walks_parents_then_serves(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    assert asks_view.quest_of(store, t["asking"]) == t["quest"]
    assert asks_view.quest_of(store, t["root"]) == t["quest"]
    lone = id_of(handler.put(text="Serves nothing.", meta={"rotation_root": True}).body)
    assert asks_view.quest_of(store, lone) is None


def test_render_groups_by_class_then_quest_then_thread(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    out = handler.search(view="asks").body
    lines = out.splitlines()

    # gr652333: the header tails with the untriaged count (none of the
    # seeded rows carries an asks-triage stamp yet).
    assert (
        lines[0] == "# asks — 1 showstopper · 2 decision · 1 discussion · 4 untriaged"
    )
    heads = [ln for ln in lines if ln.split(" (")[0] in asks_view.CLASSES]
    assert [h.split(" (")[0] for h in heads] == [
        "showstopper",
        "decision",
        "discussion",
    ]
    # one quest header per class group it appears in, handle first
    assert sum(f"◆ qu{t['quest']} —" in ln for ln in lines) == 3
    stopped = next(ln for ln in lines if f"td{t['stopped']} " in ln)
    assert "waiting-for:reto claimed-by:slice-x — Claimed slice" in stopped
    assert stopped.startswith("      ")  # under a thread line
    assert "thread:factory" in out
    gripe = next(ln for ln in lines if f"gr{t['gripe']} " in ln)
    assert "needs:ruling" in gripe
    assert any(ln.strip() == "? which database?" for ln in lines)
    # the decision block sits between the showstopper and the discussion
    assert out.index(f"td{t['stopped']} ") < out.index(f"td{t['asking']} ")
    assert out.index(f"td{t['asking']} ") < out.index(f"td{t['shape']} ")


def test_render_when_nothing_waits(handler: TodoHandler) -> None:
    assert handler.search(view="asks").body.startswith("# asks — nothing waits on you")


# ── meta.ask (td541979) ───────────────────────────────────────────


def test_meta_ask_without_blocks_is_a_decision_with_its_options(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    ask = _ask(handler, parent_id=t["root"])
    row = _row(asks_view.asks_model(store), ask)

    assert row.klass == asks_view.DECISION
    assert row.reasons == ("ask-user",)
    assert row.ask_kind == "ruling"
    assert row.options == ("yes, cut", "no, warn only")
    assert row.context == "gate went red twice"
    assert row.blocks is None
    assert row.quest_id == t["quest"]
    assert row.thread == "factory"
    out = handler.search(view="asks").body
    assert "1. yes, cut" in out
    assert "ruling: gate went red twice" in out
    # a question for the owner is never auto-run
    assert "llm_tier" not in (store.fetch_refs_by_ids([ask])[ask].meta or {})


def test_blocks_writes_a_blocked_by_link_and_files_a_showstopper_under_the_quest(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    # parentless question: it sits under the quest of the slice it blocks
    ask = _ask(handler, blocks=f"td{t['plain']}")

    assert _blocked_by(store, t["plain"]) == [ask]
    row = _row(asks_view.asks_model(store), ask)
    assert row.klass == asks_view.SHOWSTOPPER
    assert row.reasons == ("ask-user", f"blocks:td{t['plain']}")
    assert row.blocks == t["plain"]
    assert row.quest_id == t["quest"]
    assert row.thread == "factory"
    # the blocked slice is parked until the question closes
    assert f"td{t['plain']}" in handler.search(view="blocked").body


def test_answering_closes_the_question_and_releases_the_slice(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    ask = _ask(handler, blocks=f"td{t['plain']}", parent_id=t["root"])

    handler.put(id=ask, text="answer: yes, cut; warn-only hides real reds")
    handler.tag(id=ask, add=["STATUS:done"], remove=["ask-user"])

    assert all(r.ref_id != ask for r in asks_view.asks_model(store).rows)
    assert f"td{t['plain']}" not in handler.search(view="blocked").body
    assert "answer: yes, cut" in handler.get(id=ask, view="comments").body


def test_edit_replaces_meta_ask_and_adds_the_link(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    ask = _ask(handler, parent_id=t["root"])
    assert _blocked_by(store, t["plain"]) == []

    handler.edit(
        id=ask,
        meta={"ask": {"kind": "approval", "blocks": f"td{t['plain']}"}},
    )

    assert _blocked_by(store, t["plain"]) == [ask]
    row = _row(asks_view.asks_model(store), ask)
    assert row.klass == asks_view.SHOWSTOPPER
    assert row.ask_kind == "approval"
    assert row.options == ()  # replaced, not merged


@pytest.mark.parametrize(
    ("ask", "fragment"),
    [
        ({"kind": "chat", "blocks": "nothing"}, "unknown kind"),
        ({"kind": "ruling", "blocks": "nothing", "options": "yes"}, "options"),
        ({"kind": "ruling", "blocks": "nothing", "options": ["ok", ""]}, "options"),
        ({"kind": "ruling", "blocks": "nothing", "context": 5}, "context"),
        ({"kind": "ruling", "blocks": "nothing", "colour": "red"}, "unknown"),
        ({"kind": "ruling", "blocks": "soon"}, "blocks"),
        ({"kind": "ruling", "blocks": "gr123456"}, "blocks"),
        ({"kind": "ruling"}, "blocks"),
        ("just a string", "dict"),
    ],
)
def test_malformed_meta_ask_is_refused_with_a_next_step(
    handler: TodoHandler, ask: Any, fragment: str
) -> None:
    with pytest.raises(BadInput) as exc:
        handler.put(text="Q?", tags=["ask-user"], meta={"ask": ask})
    assert fragment in exc.value.cause
    assert exc.value.next


def test_meta_ask_needs_the_ask_user_tag(handler: TodoHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.put(text="Q?", meta={"ask": {"kind": "ruling", "blocks": "nothing"}})
    assert "ask-user" in exc.value.cause
    assert exc.value.next


def test_blocks_must_name_a_live_other_todo(handler: TodoHandler) -> None:
    with pytest.raises(NotFound) as exc:
        handler.put(
            text="Q?",
            tags=["ask-user"],
            meta={"ask": {"kind": "ruling", "blocks": "td999999999"}},
        )
    assert exc.value.next
    ask = _ask(handler)
    with pytest.raises(BadInput):
        handler.edit(id=ask, meta={"ask": {"kind": "ruling", "blocks": f"td{ask}"}})


def test_a_refused_put_leaves_nothing_behind(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    before = len(store.list_refs(kind="todo", limit=1000))
    with pytest.raises(BadInput):
        handler.put(text="Q?", tags=["ask-user"], meta={"ask": {"kind": "chat"}})
    assert len(store.list_refs(kind="todo", limit=1000)) == before


def test_default_rows_hide_waiting_for_and_discussion(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    handler.tag(id=t["plain"], add=["waiting-for:reto"])
    ask = _ask(handler, blocks=f"td{t['shape']}")
    full = asks_view.asks_model(store)
    default = {r.ref_id for r in full.default_rows().rows}

    assert {t["stopped"], t["asking"], t["gripe"], ask} <= default
    assert t["plain"] not in default  # bare waiting-for:reto
    assert t["shape"] not in default  # discussion
    assert {r.ref_id for r in full.rows} - default == {t["plain"], t["shape"]}


# ── gr595189: the ask poses a decision; the digest shows age + blocks ──


def test_a_delegating_question_is_refused(handler: TodoHandler) -> None:
    """The lint: "scope / prioritize / own / decide the…" hands the
    work upward instead of posing a choice — rewrite as
    "approve X at cost Y, or Z"."""
    with pytest.raises(BadInput) as exc:
        handler.put(
            text="Scope and prioritize the proposed insight loop",
            tags=["ask-user"],
            meta={
                "ask": {
                    "kind": "ruling",
                    "options": ["yes", "no"],
                    "blocks": "nothing",
                }
            },
        )
    assert "delegates" in exc.value.cause
    assert "approve X at cost Y" in (exc.value.next or "")


def test_a_delegating_option_is_refused(handler: TodoHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.put(
            text="Grant the validation carve-out?",
            tags=["ask-user"],
            meta={
                "ask": {
                    "kind": "ruling",
                    "options": ["grant it — ~$20", "scope the initiative"],
                    "blocks": "nothing",
                }
            },
        )
    assert "delegates" in exc.value.cause


def test_decide_whether_is_not_a_delegation(handler: TodoHandler) -> None:
    """A "decide whether X or Y" question already poses the choice —
    only ``decide the <initiative>`` (an object, delegating) is refused."""
    ask = _ask(handler, text="Decide whether the pass cuts on red?")
    assert ask


def test_rows_show_age_and_sort_oldest_wait_first(
    handler: TodoHandler, hub: Hub
) -> None:
    """Anti-rot display (gr595189): every row shows its age and the
    digest orders the oldest wait first within its bucket."""
    store = hub.live_store
    t = _tree(handler, store, hub)
    older = _ask(handler, text="Older question?", parent_id=t["root"])
    newer = _ask(handler, text="Newer question?", parent_id=t["root"])
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '3 days' WHERE ref_id = %s",
            (older,),
        )
    model = asks_view.asks_model(store)
    pair = [r for r in model.rows if r.ref_id in (older, newer)]
    assert [r.ref_id for r in pair] == [older, newer]
    assert _row(model, older).age_days >= 3
    out = handler.search(view="asks").body
    line = next(ln for ln in out.splitlines() if f"td{older} " in ln)
    assert "· 3d" in line
    assert out.index(f"td{older} ") < out.index(f"td{newer} ")


def test_reto_queue_orders_by_blocked_then_age(
    handler: TodoHandler, hub: Hub, store: Store
) -> None:
    """gr648249: ``for-reto:<answer>`` rows join the queue; most blocked first."""
    assert TodoView("reto-queue") is TodoView.RETO_QUEUE
    gripe_h = GripeHandler(hub=hub)
    old = id_of(gripe_h.put(text="Spec the lane rule.", tags=["for-reto:spec"]).body)
    hot = id_of(gripe_h.put(text="Pick a database.", tags=["for-reto:ruling"]).body)
    plain = id_of(
        gripe_h.put(text="Agent-writable needs-spec.", tags=["needs-spec"]).body
    )
    for text in ("Slice A.", "Slice B."):
        sl = id_of(handler.put(text=text, meta={"llm_tier": None}).body)
        store.add_link(src_ref_id=sl, dst_ref_id=hot, relation="blocked-by")
    rows = asks_view.reto_queue(store)
    ids = [r.ref_id for r in rows]
    assert ids == [hot, old]
    assert plain not in ids
    assert (rows[0].answer, rows[0].blocked) == ("ruling", 2)
    body = asks_view.render_reto_queue(store).body
    assert "[ruling]" in body and "blocks 2" in body


def test_meta_ask_blocks_counts_the_slice_once(handler: TodoHandler, hub: Hub) -> None:
    """td648492: ``meta.ask.blocks`` already writes the blocked-by link —
    the slice counts once, not link + meta = 2."""
    store = hub.live_store
    t = _tree(handler, store, hub)
    ask = _ask(handler, blocks=f"td{t['plain']}")

    assert _row(asks_view.asks_model(store), ask).blocked == 1
    assert "blocks 1" in asks_view.render_reto_queue(store).body


def test_a_done_blocked_src_drops_out_of_the_count(
    handler: TodoHandler, hub: Hub
) -> None:
    """td648492: a terminal-STATUS src no longer waits on the answer."""
    store = hub.live_store
    t = _tree(handler, store, hub)
    ask = _ask(handler, blocks=f"td{t['plain']}")
    assert _row(asks_view.asks_model(store), ask).blocked == 1

    handler.tag(id=t["plain"], add=["STATUS:done"])
    assert _row(asks_view.asks_model(store), ask).blocked == 0


def test_an_unclaimed_waiting_for_reto_row_is_in_the_queue(
    handler: TodoHandler, hub: Hub
) -> None:
    """td648492: a bare ``waiting-for:reto`` row waits on Reto — the queue
    keeps it though ``default_rows()`` folds it behind ``?all=1``."""
    store = hub.live_store
    t = _tree(handler, store, hub)
    handler.tag(id=t["plain"], add=["waiting-for:reto"])

    assert t["plain"] not in {
        r.ref_id for r in asks_view.asks_model(store).default_rows().rows
    }
    assert t["plain"] in {r.ref_id for r in asks_view.reto_queue(store)}


# ── gr673370: q= / page_size= / page= on view='reto-queue' ────────


def test_reto_queue_q_narrows_on_the_row_text(
    handler: TodoHandler, hub: Hub, store: Store
) -> None:
    """gr673370: ``q=`` used to be dropped at the view dispatch — the
    full queue came back regardless. Every whitespace term must hit the
    row's rendered text (title, ask fields, class, reasons, handle)."""
    gripe_h = GripeHandler(hub=hub)
    ruling = id_of(gripe_h.put(text="Pick a database.", tags=["for-reto:ruling"]).body)
    spec = id_of(gripe_h.put(text="Spec the lane rule.", tags=["for-reto:spec"]).body)

    # the class vocabulary narrows — 'ruling' names the for-reto answer,
    # not any title text
    assert [r.ref_id for r in asks_view.reto_queue(store, q="ruling")] == [ruling]
    # title terms AND across the haystack
    assert [r.ref_id for r in asks_view.reto_queue(store, q="lane rule")] == [spec]
    # AND over terms: no row carries both
    assert asks_view.reto_queue(store, q="ruling lane") == []
    # a handle narrows to its own row
    assert [r.ref_id for r in asks_view.reto_queue(store, q=f"gr{spec}")] == [spec]

    body = handler.search(view="reto-queue", q="ruling").body
    assert f"gr{ruling}" in body
    assert f"gr{spec}" not in body
    assert "(q='ruling')" in body
    empty = handler.search(view="reto-queue", q="no such text anywhere").body
    assert empty.startswith("# reto-queue — nothing matching q=")


def test_reto_queue_q_matches_the_ask_question(
    handler: TodoHandler, hub: Hub, store: Store
) -> None:
    """The ask's question/context/options are searchable — they carry
    the decision, the title needn't repeat them."""
    t = _tree(handler, store, hub)
    ask = _ask(handler, text="Cut on red?")
    # the ask-user:<question> tag's resolved text is searchable
    assert [r.ref_id for r in asks_view.reto_queue(store, q="which database")] == [
        t["asking"]
    ]
    # so are meta.ask's context and options
    assert [r.ref_id for r in asks_view.reto_queue(store, q="gate went red")] == [ask]
    assert [r.ref_id for r in asks_view.reto_queue(store, q="warn only")] == [ask]


def test_reto_queue_pages_under_page_size(
    handler: TodoHandler, hub: Hub, store: Store
) -> None:
    """gr673370: ``page_size=`` caps the window and ``page=`` walks it —
    the only way past a flooded head of the queue."""
    gripe_h = GripeHandler(hub=hub)
    for i in range(3):
        gripe_h.put(text=f"Ruling {i}?", tags=["for-reto:ruling"])

    one = handler.search(view="reto-queue", page_size=2).body
    assert one.startswith("# reto-queue — 2 of 3 waiting")
    assert (
        "… 1 more — search(kind='todo', view='reto-queue', page_size=2, args={'page': 2})"
        in one
    )

    two = handler.search(view="reto-queue", page_size=2, page=2).body
    assert two.startswith("# reto-queue — rows 3-3 of 3 waiting")

    past = handler.search(view="reto-queue", page_size=2, page=9).body
    assert "page 9 is past the end" in past


def test_reto_queue_refuses_a_bad_page_and_stray_under(
    handler: TodoHandler, hub: Hub
) -> None:
    with pytest.raises(BadInput) as exc:
        handler.search(view="reto-queue", page_size=2, page=0)
    assert exc.value.next
    with pytest.raises(BadInput):
        handler.search(view="reto-queue", args={"under": 5})


def test_a_resolved_alert_leaves_the_model(hub: Hub, store: Store) -> None:
    """td648492: ``resolved_at`` set with no STATUS tag — a tagged resolved
    alert would sit on the asks list and the queue forever."""
    alert = store.insert_ref(kind="alert", slug=None, title="DB lagging")
    store.add_tag(alert.id, Tag.open("alert-state:open"), set_by="system")
    store.add_tag(alert.id, Tag.open("for-reto:review"))

    assert _row(asks_view.asks_model(store), alert.id).kind == "alert"
    assert alert.id in {r.ref_id for r in asks_view.reto_queue(store)}

    assert alerts_mod.resolve_alert(store, alert.id, resolved_by="test")
    assert all(r.ref_id != alert.id for r in asks_view.asks_model(store).rows)
    assert alert.id not in {r.ref_id for r in asks_view.reto_queue(store)}


def test_stamped_routed_hidden_and_untriaged_counted(
    handler: TodoHandler, hub: Hub, store: Store
) -> None:
    """gr652333: a row stamped ``asks-triage:resolved`` or ``:routed``
    leaves the model; a row with no stamp is counted as untriaged in
    both rendered headers."""
    kept = _ask(handler, "Approve the staged rollout?")
    hidden = _ask(handler, "Coordinator ticket, not a question")
    fresh = _ask(handler, "Brand new ask, never triaged")

    store.add_tag(kept, Tag.open("asks-triage:kept"), set_by="system")
    store.add_tag(hidden, Tag.open("asks-triage:routed"), set_by="system")
    store.remove_tag(hidden, Tag.open("ask-user"))
    store.add_tag(hidden, Tag.open("waiting-for:coordinator"), set_by="system")

    model = asks_view.asks_model(store)
    assert {r.ref_id for r in model.rows} == {kept, fresh}
    assert _row(model, kept).triage == "kept"
    assert _row(model, fresh).triage == ""

    # The headers report the one unstamped row.
    assert "· 1 untriaged" in handler.search(view="asks").body
    assert "· 1 untriaged" in handler.search(view="reto-queue").body


def test_coordinator_view_lists_routed_rows(
    handler: TodoHandler, hub: Hub, store: Store
) -> None:
    """gr652333 slice 4: ``search(kind='todo', view='coordinator')``
    lists ``waiting-for:coordinator`` rows — the queue ``asks_triage``
    routes coordinator-bound work onto."""
    assert TodoView("coordinator") is TodoView.COORDINATOR
    routed = _ask(handler, "A lane decision, not an owner question")
    other = _ask(handler, "A real owner question")
    store.remove_tag(routed, Tag.open("ask-user"))
    store.add_tag(routed, Tag.open("waiting-for:coordinator"), set_by="system")

    body = handler.search(view="coordinator").body
    assert body.startswith("# coordinator — 1 waiting")
    assert f"td{routed}" in body
    assert f"td{other}" not in body

    # And the routed row is off the owner-facing model.
    assert all(r.ref_id != routed for r in asks_view.asks_model(store).rows)
