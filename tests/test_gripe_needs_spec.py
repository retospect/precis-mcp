"""``needs_spec`` (gr543698): triage parks an ill-specified gripe; the web
detail page's "move back to new" button returns it to the triage pickup
status (``STATUS:open``); a prod spec naming it closes it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers import gripe_triage as gt

pytestmark = pytest.mark.db


def _verdict(fix: str = "code", size: str = "S") -> Any:
    return gt.Verdict(
        fix=fix,
        cluster="misc",
        size=size,
        severity=2,
        verdict="bug",
        reason="r",
        recommend=None,
        dups=(),
        fix_sha=None,
    )


def _gripe(title: str = "t", body: str = "b") -> Any:
    return SimpleNamespace(title=title, body=body)


def test_needs_spec_signals() -> None:
    # fix=spec always parks (open design call / unclear expected behaviour).
    assert gt._needs_spec(_gripe(), _verdict(fix="spec"))
    # size L without a named spec parks; S/M do not.
    assert gt._needs_spec(_gripe(), _verdict(size="L"))
    assert not gt._needs_spec(_gripe(), _verdict(size="M"))
    # size L that already names a docs/backlog spec is not parked.
    g = _gripe(body="see docs/backlog/foo-spec.md for the shape")
    assert not gt._needs_spec(g, _verdict(size="L"))


def test_needs_spec_status_commits(store: Store) -> None:
    with store.tx() as conn:
        ref = store.insert_ref(kind="gripe", slug=None, title="x", meta={}, conn=conn)
        store.add_tag(
            ref.id, Tag.closed("STATUS", "needs_spec"), set_by="agent", conn=conn
        )
    assert "STATUS:needs_spec" in {str(t) for t in store.tags_for(ref.id)}


def test_needs_spec_hyphen_spelling_resolves(runtime_with_store: Any) -> None:
    """gr543698: ``status='needs-spec'`` (the hyphenated spelling) reads
    as canonical ``needs_spec`` in the search status filter."""
    store = runtime_with_store.hub.live_store
    handler = runtime_with_store.hub.handler_for("gripe")
    handler.put(text="ill-specified probe")
    gid = int(store.list_refs(kind="gripe", limit=1)[0].id)
    with store.tx() as conn:
        store.add_tag(
            gid,
            Tag.closed("STATUS", "needs_spec"),
            set_by="agent",
            replace_prefix=True,
            conn=conn,
        )
    body = handler.search(status="needs-spec").body
    assert f"gr{gid}" in body


def test_move_back_to_new_routes_through_put_with_comment() -> None:
    """The web move-back posts a comment + STATUS in one put (gr543698);
    every other status change stays a bare tag."""
    from precis_web.routes.gripes import _move_back_to_new_args

    verb, args = _move_back_to_new_args("needs_spec", "open", gid=7, actor="reto")
    assert verb == "put"
    assert args["tags"] == ["STATUS:open"] and "was needs_spec" in args["text"]
    assert "reto" in args["text"]
    verb, args = _move_back_to_new_args("needs_spec", "triaged", gid=7, actor="reto")
    assert verb == "tag" and args["add"] == ["STATUS:triaged"]
    verb, args = _move_back_to_new_args("triaged", "open", gid=7, actor="reto")
    assert verb == "tag" and args["add"] == ["STATUS:open"]


def test_web_status_template_labels_open_move_back_to_new() -> None:
    """The needs_spec detail control posts ``value=open`` labelled
    "move back to new" — the round-trip to the triage pickup status."""
    import jinja2

    from precis_web.routes.gripes import STATUS_VALUES

    assert "open" in STATUS_VALUES and "needs_spec" in STATUS_VALUES
    from pathlib import Path

    tpl = jinja2.Template(
        Path("src/precis_web/templates/gripes/_gripe_status.html.j2").read_text(
            encoding="utf-8"
        )
    )
    out = tpl.render(
        id=1, status="needs_spec", status_values=STATUS_VALUES, status_badge="x"
    )
    assert 'name="value" value="open"' in out
    assert "move back to new" in out
    # Other statuses keep the plain "→ open" label.
    out2 = tpl.render(
        id=1, status="triaged", status_values=STATUS_VALUES, status_badge="x"
    )
    assert "move back to new" not in out2
