"""DraftHandler regex grep (search mode='regex') + substitute (edit sub=)
— scope levels, dry-run/apply, backrefs, derived-chunk skip."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store


def _proj(hub: Hub, text: str = "Project root") -> int:
    t = TodoHandler(hub=hub).put(text=text, tags=["level:strategic"])
    return int(t.body.split("id=")[1].split()[0].rstrip(",.()"))


def _h(put_body: str) -> str:
    m = re.search(r"dc\d+", put_body)
    assert m is not None, f"no dc handle in {put_body!r}"
    return m.group(0)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


@pytest.fixture
def seeded(draft: DraftHandler, hub: Hub) -> dict[str, str]:
    """A draft with two sections; some prose carries **bold** and an
    em-dash so the regex ops have something to find."""
    proj = _proj(hub, "regex proj")
    slug = "rxdoc"
    draft.put(id=slug, title="Regex doc", project=proj)
    s1 = draft.put(id=slug, chunk_kind="heading", text="Intro", at={"last": True})
    s1h = _h(s1.body)
    p1 = draft.put(
        id=slug,
        chunk_kind="paragraph",
        text="This has **bold** text and an em-dash—here.",
        at={"into": s1h, "last": True},
    )
    s2 = draft.put(id=slug, chunk_kind="heading", text="Methods", at={"last": True})
    s2h = _h(s2.body)
    p2 = draft.put(
        id=slug,
        chunk_kind="paragraph",
        text="Methods section also has **bold** emphasis.",
        at={"into": s2h, "last": True},
    )
    return {"slug": slug, "s1": s1h, "p1": _h(p1.body), "s2": s2h, "p2": _h(p2.body)}


# ── find (grep) ──────────────────────────────────────────────────────


def test_find_whole_draft(draft: DraftHandler, seeded: dict[str, str]) -> None:
    r = draft.search(q=r"\*\*\w+\*\*", mode="regex", scope=seeded["slug"])
    assert "2 match(es) in 2 chunk(s)" in r.body
    assert "»**bold**«" in r.body
    assert seeded["p1"] in r.body and seeded["p2"] in r.body


def test_find_section_scope_excludes_other_section(
    draft: DraftHandler, seeded: dict[str, str]
) -> None:
    # scope to the Methods heading subtree → only p2's bold is found
    r = draft.search(q=r"\*\*\w+\*\*", mode="regex", scope=seeded["s2"])
    assert seeded["p2"] in r.body
    assert seeded["p1"] not in r.body


def test_find_no_match(draft: DraftHandler, seeded: dict[str, str]) -> None:
    r = draft.search(q="zzznope", mode="regex", scope=seeded["slug"])
    assert "no draft chunk matches" in r.body


def test_find_flags_case_fold(draft: DraftHandler, seeded: dict[str, str]) -> None:
    assert (
        "no draft chunk"
        in draft.search(q="METHODS", mode="regex", scope=seeded["slug"]).body
    )
    r = draft.search(q="METHODS", mode="regex", scope=seeded["slug"], flags="i")
    assert "1 match" in r.body or "2 match" in r.body


def test_find_bad_regex_is_badinput(
    draft: DraftHandler, seeded: dict[str, str]
) -> None:
    with pytest.raises(BadInput):
        draft.search(q="(unclosed", mode="regex", scope=seeded["slug"])


# ── substitute (s///) ────────────────────────────────────────────────


def test_sub_dryrun_writes_nothing(
    draft: DraftHandler, seeded: dict[str, str], hub: Hub
) -> None:
    out = draft.edit(id=seeded["slug"], sub={"find": "—", "replace": ", "})
    assert "DRY RUN" in out.body
    assert "1 replacement(s) across 1 chunk(s)" in out.body
    # original text untouched
    chunk = draft.get(id=seeded["p1"]).body
    assert "em-dash—here" in chunk


def test_sub_apply_rewrites(draft: DraftHandler, seeded: dict[str, str]) -> None:
    out = draft.edit(id=seeded["slug"], sub={"find": "—", "replace": ", "}, apply=True)
    assert "1 replacement(s) across 1 chunk(s)" in out.body
    assert "em-dash, here" in draft.get(id=seeded["p1"]).body


def test_sub_backref_strip_bold(draft: DraftHandler, seeded: dict[str, str]) -> None:
    out = draft.edit(
        id=seeded["slug"],
        sub={"find": r"\*\*(\w+)\*\*", "replace": r"\1"},
        apply=True,
    )
    assert "2 replacement(s) across 2 chunk(s)" in out.body
    assert "has bold text" in draft.get(id=seeded["p1"]).body
    assert "**" not in draft.get(id=seeded["p2"]).body


def test_sub_section_scope(draft: DraftHandler, seeded: dict[str, str]) -> None:
    # confine the bold-strip to the Methods section only
    draft.edit(
        id=seeded["s2"],
        sub={"find": r"\*\*(\w+)\*\*", "replace": r"\1"},
        apply=True,
    )
    assert "**bold**" in draft.get(id=seeded["p1"]).body  # intro untouched
    assert "**" not in draft.get(id=seeded["p2"]).body


def test_sub_sed_string_form(draft: DraftHandler, seeded: dict[str, str]) -> None:
    out = draft.edit(id=seeded["slug"], sub="s/—/, /", apply=True)
    assert "1 replacement(s)" in out.body
    assert "em-dash, here" in draft.get(id=seeded["p1"]).body


def test_sub_requires_scope(draft: DraftHandler, seeded: dict[str, str]) -> None:
    with pytest.raises(BadInput):
        draft.edit(id=None, sub={"find": "a", "replace": "b"})


def test_sub_no_match(draft: DraftHandler, seeded: dict[str, str]) -> None:
    out = draft.edit(id=seeded["slug"], sub={"find": "zzznope", "replace": "x"})
    assert "no substitutable matches" in out.body


def test_sub_apply_raises_on_concurrent_edit(
    draft: DraftHandler,
    seeded: dict[str, str],
    hub: Hub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr176088: `_substitute` reads chunks via `_scope_chunks`, computes the
    per-chunk rewrite, then writes each back via `edit_text` — a concurrent
    edit landing between that read and the write must raise, not silently
    clobber the concurrent writer's text. Simulate the race by mutating p1
    (force edit_text, no base_sha — a second agent's write) right after
    `_substitute`'s own read returns, before it reaches the write loop."""
    real_scope_chunks = draft.__class__._scope_chunks

    def _racing_scope_chunks(
        self: DraftHandler, raw_scope: str | int | None, *, allow_all: bool
    ):
        pairs, where = real_scope_chunks(self, raw_scope, allow_all=allow_all)
        # a concurrent writer changes p1's content right after our read
        # (edit_text wants the legacy base-58 handle, not the dc<id> address)
        p1_handle = hub.store.get_draft_chunk(seeded["p1"]).handle
        hub.store.edit_text(p1_handle, "raced in first")
        return pairs, where

    monkeypatch.setattr(draft.__class__, "_scope_chunks", _racing_scope_chunks)
    with pytest.raises(BadInput, match="changed since you read"):
        draft.edit(id=seeded["slug"], sub={"find": "—", "replace": ", "}, apply=True)
    # the concurrent writer's text survives untouched — no clobber
    assert hub.store.get_draft_chunk(seeded["p1"]).text == "raced in first"


def test_regex_mode_reaches_draft_handler_via_search_boundary(
    seeded: dict[str, str], store: Store
) -> None:
    """gr178511: the unified search() tool-boundary validator whitelisted
    only hybrid|lexical|semantic|verbatim and rejected mode='regex' for
    EVERY kind before dispatch — so the draft handler's regex grep (which
    IS implemented, backed by draft_regex.py) was unreachable through the
    MCP search tool. The validator now permits 'regex' when kind=='draft'
    and still rejects it for other kinds. Exercised through the real
    ``tools.core.search`` boundary, not the handler directly."""
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.runtime import PrecisRuntime
    from precis.tools import core as tools_core

    def _body(out: object) -> str:
        content = getattr(out, "content", None)
        return content[0].text if content is not None else str(out)

    rt = PrecisRuntime(config=PrecisConfig(), hub=boot(store=store))
    prev = tools_core._runtime
    tools_core._runtime = rt
    try:
        # draft + regex passes the boundary and reaches the handler, which
        # finds the two **bold** spans in the seeded draft.
        ok = _body(
            tools_core.search(
                kind="draft", q=r"\*\*\w+\*\*", mode="regex", scope=seeded["slug"]
            )
        )
        assert "unknown search mode" not in ok
        assert "match(es)" in ok
        # a non-draft kind still rejects regex at the boundary
        bad = _body(tools_core.search(kind="paper", q="x", mode="regex"))
        assert "unknown search mode 'regex'" in bad
    finally:
        tools_core._runtime = prev


def test_sub_skips_table_chunk(draft: DraftHandler, seeded: dict[str, str]) -> None:
    # a table whose derived markdown contains the word 'bold' must be skipped
    draft.put(
        id=seeded["slug"],
        chunk_kind="table",
        table={"header": ["k", "v"], "rows": [["bold", "1"]]},
        caption="bold caption",
        at={"last": True},
    )
    out = draft.edit(
        id=seeded["slug"], sub={"find": "bold", "replace": "BOLD"}, apply=True
    )
    # the two prose chunks change; the table is reported as skipped
    assert "skipped" in out.body
