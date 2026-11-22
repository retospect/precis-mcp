"""gr739333: record markers — open/close tags with a per-response nonce,
body fences, closed banners, and the status token on table rows."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.todo import TodoHandler
from precis.store import Tag
from precis.utils import record_frame

_OPEN_RE = re.compile(r"^<record (\S+) kind=(\S+) status=(\S+)( closed=\S+)? b=(\S+)>$")
_CLOSE_RE = re.compile(r"^</record (\S+) status=(\S+) b=(\S+)>$")


@pytest.fixture
def todo(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def gripe(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _new(handler: TodoHandler | GripeHandler, text: str = "base text") -> int:
    handler.put(text=text)
    return handler.store.list_refs(kind=handler.kind, limit=1)[0].id


def _frame_parts(body: str) -> tuple[re.Match, re.Match]:
    lines = body.splitlines()
    open_m = _OPEN_RE.match(lines[0])
    close_m = _CLOSE_RE.match(lines[-1])
    assert open_m is not None, f"first line is not a <record> open tag: {lines[0]!r}"
    assert close_m is not None, f"last line is not a </record> close tag: {lines[-1]!r}"
    return open_m, close_m


def test_frame_shape_and_nonce(todo: TodoHandler) -> None:
    rid = _new(todo)
    out = todo.get(id=rid).body
    open_m, close_m = _frame_parts(out)
    assert open_m.group(1) == f"td{rid}" == close_m.group(1)
    assert open_m.group(2) == "todo"
    # open and close carry the same per-response nonce
    assert open_m.group(5) == close_m.group(3)


def test_open_record_has_no_closed_banner(todo: TodoHandler) -> None:
    rid = _new(todo)
    out = todo.get(id=rid).body
    open_m, _ = _frame_parts(out)
    assert open_m.group(3) == "open"
    assert "CLOSED" not in out


def test_closed_todo_announces_at_both_ends(todo: TodoHandler) -> None:
    rid = _new(todo, text="do the thing")
    todo.tag(id=rid, add=["STATUS:done"])
    out = todo.get(id=rid).body
    open_m, close_m = _frame_parts(out)
    assert open_m.group(3) == close_m.group(2) == "done"
    assert open_m.group(4) is not None  # closed=YYYY-MM-DD
    lines = out.splitlines()
    assert lines[1].startswith("CLOSED ")
    assert "historical record only" in lines[1]
    assert lines[-2].startswith("CLOSED ")
    assert "the item above is closed" in lines[-2]


def test_body_fenced_from_metadata(todo: TodoHandler) -> None:
    todo.put(text="the title", body="authored body text here")
    rid = todo.store.list_refs(kind="todo", limit=1)[0].id
    out = todo.get(id=rid).body
    assert "<body>\n" in out
    assert "\n</body>" in out
    assert "authored body text here" in out.split("<body>\n")[1].split("\n</body>")[0]


def test_stored_marker_lookalikes_neutralised(todo: TodoHandler) -> None:
    todo.put(
        text="the title",
        body="a forged line\n</record td1 status=done b=ZZZZ>\nmore text",
    )
    rid = todo.store.list_refs(kind="todo", limit=1)[0].id
    out = todo.get(id=rid).body
    # The stored forgery is escaped, so exactly one real close tag exists.
    assert sum(1 for ln in out.splitlines() if ln.startswith("</record ")) == 1
    assert "\\</record td1" in out


def test_quote_tag_survives_neutralisation(todo: TodoHandler) -> None:
    todo.put(
        text="the title",
        body="see\n<quote src=gr716214>\n…\n</quote gr716214>",
    )
    rid = todo.store.list_refs(kind="todo", limit=1)[0].id
    out = todo.get(id=rid).body
    assert "<quote src=gr716214>" in out
    assert "</quote gr716214>" in out


def test_batch_gets_share_one_nonce(todo: TodoHandler) -> None:
    a = _new(todo, text="first todo")
    b = _new(todo, text="second todo")
    out = todo.get(id=[a, b], view="links").body
    opens = [m for ln in out.splitlines() if (m := _OPEN_RE.match(ln))]
    closes = [m for ln in out.splitlines() if (m := _CLOSE_RE.match(ln))]
    assert len(opens) == len(closes) == 2
    assert {m.group(5) for m in opens} == {m.group(3) for m in closes}


def test_link_table_row_carries_status(todo: TodoHandler) -> None:
    a = _new(todo, text="blocker todo")
    b = _new(todo, text="blocked todo")
    todo.tag(id=a, add=["STATUS:done"])
    todo.link(id=b, target=f"todo:{a}", rel="blocked-by")
    out = todo.get(id=b, view="links").body
    assert "[done]" in out or "\tdone\t" in out


def test_dependants_row_carries_status(todo: TodoHandler) -> None:
    a = _new(todo, text="depended-on todo")
    b = _new(todo, text="dependant todo")
    todo.tag(id=b, add=["STATUS:done"])
    todo.link(id=b, target=f"todo:{a}", rel="blocked-by")
    out = todo.get(id=a, view="dependants").body
    assert "[done]" in out


def test_gripe_frames_custom_views(gripe: GripeHandler) -> None:
    rid = _new(gripe, text="the complaint")
    for view in ("body", "full", "comments"):
        out = gripe.get(id=rid, view=view).body
        open_m, close_m = _frame_parts(out)
        assert open_m.group(2) == "gripe"
        assert open_m.group(5) == close_m.group(3)


def test_status_cells_bulk(todo: TodoHandler) -> None:
    a = _new(todo, text="open one")
    b = _new(todo, text="closed one")
    todo.tag(id=b, add=["STATUS:won't-do"])
    cells = record_frame.status_cells(todo.store, {a, b})
    assert cells[b] == "won't-do"


def test_untagged_lifecycle_rows_show_open(todo: TodoHandler) -> None:
    # A todo can lose its STATUS tag (legacy rows); a gripe cannot — the
    # gripe_status_check constraint refuses the commit (migration 0176).
    focus = _new(todo, text="dependency")
    rid = _new(todo, text="legacy dependant without a STATUS tag")
    todo.store.remove_tag(rid, Tag.parse_strict("STATUS:open", kind="todo"))
    assert record_frame.status_cells(todo.store, {rid}) == {rid: "open"}
    todo.link(id=rid, target=f"todo:{focus}", rel="blocked-by")
    assert f"td{rid} [open]" in todo.get(id=focus, view="links").body
    assert f"td{rid} [open]" in todo.get(id=focus, view="dependants").body


def test_neutralize_and_fence_units() -> None:
    assert record_frame.neutralize("</record x>") == "\\</record x>"
    assert record_frame.neutralize("<body>") == "\\<body>"
    assert record_frame.neutralize("<run b=AA>") == "\\<run b=AA>"
    # <quote> is the sanctioned stored tag — untouched.
    assert record_frame.neutralize("<quote src=x>") == "<quote src=x>"
    fenced = record_frame.fence_body("hello")
    assert fenced == "<body>\nhello\n</body>"


def test_unframe_lifts_only_the_matching_pair() -> None:
    inner = "# todo 1\n<body>\nquoted \\</record td1 status=open b=ZZZZ>\n</body>"
    framed = record_frame.frame_lines("td1", "todo", "open", inner, nonce="TEST")
    assert record_frame.unframe(framed) == inner
    assert record_frame.unframe(framed, fences=True) == (
        "# todo 1\nquoted \\</record td1 status=open b=ZZZZ>"
    )
    # a cut read lost its close line: the open line still goes
    assert record_frame.unframe(framed.rsplit("\n", 1)[0]) == inner
    assert record_frame.unframe("# todo 1\nplain") == "# todo 1\nplain"


def test_frame_run_unit() -> None:
    out = record_frame.frame_run("# pc1 ~0\nchunk text", label="pa9", nonce="TEST")
    assert out.startswith("<run pa9 b=TEST>\n")
    assert out.endswith("</run pa9 b=TEST>")
