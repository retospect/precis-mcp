"""Short gripe titles — gripe system batch F slice 6.

``put`` always files; a first line over 60 chars gets a derived
``Area: slogan`` title (the body keeps the full text) and the ack says so.
``edit(kind='gripe', title=)`` rewrites the title only. The one-time
gardening pass (``precis gripes --shorten-titles``) lists by default and
records the old title in a comment when it writes.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from mcp.types import CallToolResult

from precis.cli.gripe import shorten_titles
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _gripe_title
from precis.handlers._gripe_title import TITLE_MAX, short_title
from precis.handlers.gripe import GripeHandler
from precis.runtime import PrecisRuntime
from precis.store.revision_context import revision_context
from precis.tools import core as tools_core
from tests._gripe import insert_gripe

LONG = (
    "pcb router: copper pours vanish after a reroute when the board has "
    "two ground nets, see the attached run"
)


@pytest.fixture
def gripe(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _latest(gripe: GripeHandler) -> int:
    return int(gripe.store.list_refs(kind="gripe", limit=1)[0].id)


def _title(gripe: GripeHandler, gid: int) -> str:
    ref = gripe.store.get_ref(kind="gripe", id=gid)
    assert ref is not None
    return str(ref.title)


def _comments(gripe: GripeHandler, gid: int) -> list[str]:
    return [
        c.text
        for c in gripe.store.chunks.list_chunks_for_ref(gid)
        if c.chunk_kind == "gripe_comment"
    ]


# ── the derivation ────────────────────────────────────────────────────────


def test_a_first_line_that_fits_is_kept() -> None:
    assert short_title("router: pours vanish\n\nlong detail " * 3) is None


def test_area_prefix_kept_and_cut_to_the_first_clause() -> None:
    assert (
        short_title(LONG) == "pcb router: copper pours vanish after a reroute when the"
    )
    title = short_title(
        "ship: the gate is red - every shard times out on the slow runner pool"
    )
    assert title == "ship: the gate is red"


def test_no_boundary_cuts_at_a_word_and_never_exceeds_the_cap() -> None:
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi"
    title = short_title(words)
    assert title is not None and len(title) <= TITLE_MAX
    assert words.startswith(title) and not title.endswith(" ")
    assert len(short_title("x" * 200) or "") == TITLE_MAX


def test_a_clause_boundary_inside_parens_or_quotes_is_not_a_cut() -> None:
    """gr500645: an MCP-shaped first line was cut mid-call at the first
    comma inside its own parentheses/quotes."""
    assert (
        short_title(
            "get(kind='job', id='/logs', q=) refused; the skill puts q inside the id"
        )
        == "get(kind='job', id='/logs', q=) refused"
    )
    # A comma inside quotes alone is likewise part of the clause.
    line = "logs say 'timeout, retrying' on every pass of the nightly sweep"
    title = short_title(line)
    assert title is not None and title.startswith("logs say 'timeout, retrying'")


# ── the MCP boundary (gr500645 comment) ─────────────────────────────────


def _text(out: Any) -> str:
    if isinstance(out, CallToolResult):
        return out.content[0].text  # type: ignore[union-attr]
    return out


def test_edit_title_via_the_verb_tolerates_the_injected_verdict(
    gripe: GripeHandler,
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tools_core.edit`` injects ``verdict='approved'`` on every call
    (``_WRAPPER_DEFAULTED_KWARGS``); a gripe title rewrite must not refuse
    it, and ``title`` rides ``args=`` under the slim schema."""
    monkeypatch.setattr(tools_core, "_runtime", runtime_with_store)
    gripe.put(text="router: pours vanish")
    gid = _latest(gripe)

    out = tools_core.edit(kind="gripe", id=gid, args={"title": "pcb: pours vanish"})
    assert not isinstance(out, CallToolResult) or not out.isError, _text(out)
    assert _title(gripe, gid) == "pcb: pours vanish"

    # mode='replace' (the spelling an agent reaches for) works too.
    out = tools_core.edit(
        kind="gripe", id=gid, mode="replace", args={"title": "pcb: pours gone"}
    )
    assert not isinstance(out, CallToolResult) or not out.isError, _text(out)
    assert _title(gripe, gid) == "pcb: pours gone"

    # And the top-level title= the slim verb promotes out of args=.
    out = tools_core.edit(kind="gripe", id=gid, title="pcb: pours really gone")
    assert not isinstance(out, CallToolResult) or not out.isError, _text(out)
    assert _title(gripe, gid) == "pcb: pours really gone"

    # A caller-supplied verdict is still refused — only the verb's
    # injected default is dropped.
    out = tools_core.edit(kind="gripe", id=gid, verdict="rejected", args={"title": "x"})
    assert "verdict" in _text(out) and _title(gripe, gid) == "pcb: pours really gone"


# ── put / ack ─────────────────────────────────────────────────────────────


def test_long_put_stores_a_short_title_and_the_full_body(gripe: GripeHandler) -> None:
    body = gripe.put(text=LONG).body
    gid = _latest(gripe)
    want = short_title(LONG)
    assert _title(gripe, gid) == want
    assert f'title shortened to "{want}"' in body
    assert f"edit(kind='gripe', id={gid}, args={{'title': '<new title>'}})" in body
    full = gripe.get(id=gid, view="full").body
    assert LONG in full


def test_short_put_keeps_its_title_and_says_nothing(gripe: GripeHandler) -> None:
    body = gripe.put(text="router: pours vanish").body
    assert _title(gripe, _latest(gripe)) == "router: pours vanish"
    assert "title shortened" not in body


def test_a_refused_title_write_still_files_the_gripe(
    gripe: GripeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The title UPDATE runs in a savepoint: when it fails (an agent_ro
    connection cannot UPDATE refs; here a NUL byte psycopg refuses), the
    gripe files with its full-text title and the ack says nothing."""
    monkeypatch.setattr(_gripe_title, "short_title", lambda _t: "bad\x00title")
    body = gripe.put(text=LONG).body
    gid = _latest(gripe)
    assert f"created gripe id={gid}" in body
    assert "title shortened" not in body
    assert _title(gripe, gid) == LONG


# ── edit ──────────────────────────────────────────────────────────────────


def test_edit_rewrites_the_title_only(gripe: GripeHandler) -> None:
    gripe.put(text=LONG)
    gid = _latest(gripe)
    dry = gripe.edit(id=gid, title="pcb: pours vanish", dry_run=True).body
    assert "nothing written" in dry
    assert _title(gripe, gid) == short_title(LONG)
    gripe.edit(id=gid, title="  pcb:   pours vanish ")
    assert _title(gripe, gid) == "pcb: pours vanish"
    assert LONG in gripe.get(id=gid, view="full").body


def test_edit_refuses_anything_but_a_title(gripe: GripeHandler) -> None:
    gripe.put(text="router: pours vanish")
    gid = _latest(gripe)
    with pytest.raises(BadInput, match="title only; text not accepted"):
        gripe.edit(id=gid, text="new body")
    with pytest.raises(BadInput, match="title= is empty"):
        gripe.edit(id=gid, title="   ")
    with pytest.raises(BadInput, match="title only"):
        gripe.edit(id=gid)


def test_edit_verdict_names_the_resolution_recipe(gripe: GripeHandler) -> None:
    """gr501173: ``verdict=`` is the draft chunk sign-off param — an agent
    generalising it to ``edit(kind='gripe')`` wants to record a ruling,
    so the refusal's ``next:`` names the resolved: comment + STATUS:done
    recipe rather than the generic title door. (A non-'approved' verdict
    is genuinely caller-supplied — 'approved' is the verb's injected
    default and is tolerated.)"""
    gripe.put(text="router: pours vanish")
    gid = _latest(gripe)
    with pytest.raises(BadInput, match="verdict not accepted") as exc:
        gripe.edit(id=gid, title="pcb: pours vanish", verdict="rejected")
    nxt = exc.value.next
    assert isinstance(nxt, str)
    assert "draft" in nxt
    assert f"put(kind='gripe', id={gid}" in nxt
    assert "resolved: <sha>" in nxt
    assert "STATUS:done" in nxt


# ── the gardening pass ────────────────────────────────────────────────────


def test_gardening_pass_lists_by_default_and_records_the_old_title(
    gripe: GripeHandler,
) -> None:
    ref = insert_gripe(gripe.store, LONG)
    gid = int(ref.id)
    want = short_title(LONG)

    dry = shorten_titles(gripe.store, apply=False, ids=[gid])
    assert dry == [
        f'gr{gid}: "{LONG[:100]}" → "{want}"',
        "1 open gripe title(s) to shorten; dry run, nothing written",
    ]
    assert _title(gripe, gid) == LONG

    done = shorten_titles(gripe.store, apply=True, ids=[gid])
    assert done[-1] == "1 open gripe title(s) to shorten; shortened 1"
    assert _title(gripe, gid) == want
    assert _comments(gripe, gid) == [f"title shortened from: {LONG}"]

    again = shorten_titles(gripe.store, apply=True, ids=[gid])
    assert again == ["0 open gripe title(s) to shorten; shortened 0"]


def _stamped_gripe(gripe: GripeHandler, title: str) -> int:
    """A gripe stamped generated/hunch, as a trust-converted agent files it."""
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        return int(insert_gripe(gripe.store, title).id)


def test_gardening_pass_shortens_a_trust_stamped_gripe(gripe: GripeHandler) -> None:
    gid = _stamped_gripe(gripe, LONG)

    done = shorten_titles(gripe.store, apply=True, ids=[gid])

    assert done[-1] == "1 open gripe title(s) to shorten; shortened 1"
    assert _title(gripe, gid) == short_title(LONG)
    assert _comments(gripe, gid) == [f"title shortened from: {LONG}"]


def test_gardening_pass_skips_and_reports_a_refused_row(
    gripe: GripeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _stamped_gripe(gripe, LONG)
    good = _stamped_gripe(gripe, LONG + " and a second one")
    real = gripe.store.update_ref

    def refuse(ref_id: int, **kw: Any) -> Any:
        if ref_id == bad:
            raise psycopg.errors.CheckViolation("trust: refused for the test")
        return real(ref_id, **kw)

    monkeypatch.setattr(gripe.store, "update_ref", refuse)

    out = shorten_titles(gripe.store, apply=True, ids=[bad, good])

    assert any(f"skipped 1: gr{bad} (CheckViolation" in line for line in out)
    assert out[-2] == "2 open gripe title(s) to shorten; shortened 1"
    assert _title(gripe, bad) == LONG
    assert _comments(gripe, bad) == []
    assert _title(gripe, good) == short_title(LONG + " and a second one")
