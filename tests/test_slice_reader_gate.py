"""The request-boundary table for the slice grant scopes (gr574082,
td574161): ``slice_reader_scope.refusal`` maps a scope-stamped call to
``None`` (allowed) or a ``Denied`` with a ``next=``.

``reader`` sees python/md/skill freely plus its own todo and the gripes
it fixes (``refs``); ``triage`` adds the comment-2 write set — gripe
search/get free, comments on its own todo and on gripes inside the
effective refs (the grant's ``refs`` plus the gripe the todo
``fixes``-links live), STATUS:/claimed-by: tags on those same gripes
(never on the todo), and the one ``fixes`` link claim. Every other
verb, kind, ref or parameter shape refuses — and a scope outside the
known set fails closed.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis import slice_reader_scope
from precis.errors import Denied

REFS = frozenset({"td900", "gr800"})


def _ok(scope: str, verb: str, payload: dict[str, Any], fixes: Any = ()) -> None:
    assert slice_reader_scope.refusal(scope, verb, payload, REFS, fixes) is None


def _refused(scope: str, verb: str, payload: dict[str, Any], fixes: Any = ()) -> Denied:
    exc = slice_reader_scope.refusal(scope, verb, payload, REFS, fixes)
    assert isinstance(exc, Denied)
    assert exc.next  # every refusal names a next step
    return exc


# ── reader: the allowed read surface ────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "python", "id": "main::run_slice.submit"},
        {"kind": "python", "mode": "pattern", "q": "submit"},
        {"kind": "md", "id": "main/docs/codebase.md", "extent": "fisheye"},
        {"kind": "skill", "id": "precis-overview"},
        {"kind": "python,md", "q": "x"},  # a comma list of free kinds
        {"kind": "python", "q": "x", "extent": "fisheye+1hop", "args": {"a": 1}},
    ],
)
@pytest.mark.parametrize("verb", ["get", "search"])
def test_reader_free_kinds_pass_unrestricted(
    verb: str, payload: dict[str, Any]
) -> None:
    """python/md/skill are free reads: no parameter restrictions at all —
    extent, q and arbitrary args all pass."""
    _ok("reader", verb, payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "todo", "id": "td900"},
        {"kind": "todo", "id": 900},
        {"kind": "todo", "id": "todo:900"},
        {"kind": "todo", "id": ["td900"]},
        {"kind": "todo", "id": ["td900", 900]},
        {"kind": "todo", "id": "td900", "view": "comments"},
        {"kind": "todo", "id": "td900", "args": {"comment": 2}},
        {"kind": "todo", "id": "td900", "view": "comments", "args": {"last": 1}},
        {"kind": "gripe", "id": "gr800"},
        {"kind": "gripe", "id": "gripe:800"},
        {"kind": "gripe", "id": 800},
    ],
)
def test_reader_refs_bound_gets(payload: dict[str, Any]) -> None:
    """todo/gripe get passes when every named id resolves into ``refs`` —
    handles, ``<kind>:<int>`` link targets, bare ints and id lists."""
    _ok("reader", "get", payload)


# ── reader: everything else refuses ─────────────────────────────────


@pytest.mark.parametrize("verb", ["put", "edit", "delete", "tag", "link", "precis"])
def test_reader_only_get_and_search(verb: str) -> None:
    _refused("reader", verb, {"kind": "todo", "id": "td900"})


@pytest.mark.parametrize(
    "verb,payload",
    [
        ("", {}),  # a non-tool request (resources/list, prompts/get, …)
        ("get", {"id": "td900"}),  # kind required
        ("get", {"kind": ""}),
    ],
)
def test_reader_shape_minimums(verb: str, payload: dict[str, Any]) -> None:
    _refused("reader", verb, payload)


@pytest.mark.parametrize(
    "kind",
    [
        "memory",
        "paper",
        "web",
        "perplexity-research",
        "markdown",
        "plaintext",
        "agentlog",
        "llm",
        "job",
        "conv",
    ],
)
def test_reader_other_kinds_refused(kind: str) -> None:
    _refused("reader", "get", {"kind": kind, "id": "x"})
    _refused("reader", "get", {"kind": f"python,{kind}", "id": "x"})


@pytest.mark.parametrize("kind", ["todo", "gripe"])
def test_reader_no_search_on_refs_bound_kinds(kind: str) -> None:
    """Search on todo/gripe stays refused under reader — only the refs
    the grant named are readable there."""
    _refused("reader", "search", {"kind": kind, "q": "x"})


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "todo", "id": "td901"},  # a foreign todo handle
        {"kind": "todo", "id": 901},  # a foreign bare int
        {"kind": "todo", "id": ["td900", "td901"]},  # one foreign id sinks the list
        {"kind": "todo", "id": "nonsense"},  # unresolvable
        {"kind": "todo", "id": "gr800"},  # resolves, but to a gripe under todo
        {"kind": "gripe", "id": "gr801"},  # a foreign gripe under reader
        {"kind": "todo"},  # id required on the refs-bound kinds
        {"kind": "gripe"},
        {"kind": "todo", "id": "td900", "view": "fisheye"},
        {"kind": "todo", "id": "td900", "extent": "fisheye"},
        {"kind": "gripe", "id": "gr800", "q": "x"},
        {"kind": "todo", "id": "td900", "args": {"page": 2}},
        {"kind": "todo", "id": "td900", "meta": {"x": 1}},
    ],
)
def test_reader_refs_bound_refusals(payload: dict[str, Any]) -> None:
    _refused("reader", "get", payload)


# ── triage: the comment-2 write set ─────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "gripe", "q": "STATUS:open"},  # selecting a gripe
        {"kind": "gripe"},  # the unscoped list read
        {"kind": "gripe", "id": "/open"},
        {"kind": "gripe", "id": "gr800"},
        {"kind": "gripe", "id": "gr801"},  # any gripe, not just refs
        {"kind": "gripe", "id": "gr801", "view": "comments"},
    ],
)
@pytest.mark.parametrize("verb", ["get", "search"])
def test_triage_gripe_reads_free(verb: str, payload: dict[str, Any]) -> None:
    """Triage reads any gripe — it picks the one it will claim — though
    todo stays refs-bound (search excluded: search on todo refused)."""
    if verb == "search" and "id" in payload:
        pytest.skip("id belongs to get")
    if verb == "get" and payload.get("q"):
        pytest.skip("q belongs to search")
    _ok("triage", verb, payload)


def test_triage_keeps_the_reader_reads() -> None:
    """Everything reader may do, triage may do."""
    for payload in (
        {"kind": "python", "id": "main::x.y"},
        {"kind": "skill", "id": "precis-overview"},
        {"kind": "todo", "id": "td900"},
        {"kind": "todo", "id": "td900", "view": "comments"},
    ):
        _ok("triage", "get", payload)
    _ok("triage", "search", {"kind": "skill", "q": "x"})


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "todo", "id": "td900", "text": "progress note"},
        {"kind": "todo", "id": 900, "text": "progress note"},
        {"kind": "gripe", "id": "gr800", "text": "looking at this"},
        {"kind": "gripe", "id": "gripe:800", "text": "looking at this"},
    ],
)
def test_triage_put_comments(payload: dict[str, Any]) -> None:
    """A comment on the own todo or on a gripe inside the effective
    refs (the grant's refs plus the gripe the todo fixes-links);
    everything carries only kind/id/text and a single id."""
    _ok("triage", "put", payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "todo", "id": "td901", "text": "forged verdict"},
        {"kind": "todo", "id": "gr800", "text": "x"},  # resolves to a gripe
        {"kind": "gripe", "id": "td900", "text": "x"},  # resolves to a todo
        {"kind": "gripe", "id": "gr555", "text": "x"},  # outside the effective refs
        {"kind": "gripe", "id": "gripe:555", "text": "x"},  # same, link spelling
        {"kind": "gripe", "id": ["gr800", "gr555"], "text": "x"},  # never a list
        {"kind": "todo", "id": ["td900"], "text": "x"},  # never a list
        {"kind": "todo", "text": "x"},  # no create
        {"kind": "gripe", "text": "x"},  # no create
        {"kind": "todo", "id": "td900", "text": "x", "meta": {"a": 1}},
        {"kind": "todo", "id": "td900", "title": "x"},  # keys ⊆ {kind,id,text}
        {"$ref": "todo:900"},  # a $ref body is not a payload
        {"kind": "todo", "id": "td900"},  # no text — comments are strings
        {
            "kind": "todo",
            "id": "td900",
            "text": {"$ref": "gr800"},
        },  # a dict text bypasses the shape check — refused outright
        {"kind": "gripe", "id": "gr800", "text": {"$ref": "x"}},
        {"kind": "todo", "id": "td900", "text": 42},
    ],
)
def test_triage_put_refusals(payload: dict[str, Any]) -> None:
    _refused("triage", "put", payload)


@pytest.mark.parametrize(
    "text",
    [
        "review: LAND",
        "review: FIX — a.py misses the empty case",
        "REVIEW: REVERT — breaks the API",
        "landed: deadbee42",
        "  landed: deadbee42",
        "\nanswer: deploy r235",
        "Answer: hold",
        "answer:all example boards route",
        "handoff: review: LAND",
        "handoff:review: FIX — x",
        "Handoff: landed: deadbee42",
        " handoff : answer: deploy r235",
    ],
)
def test_triage_own_todo_verdict_headlines_refused(text: str) -> None:
    """An own-todo comment whose first line opens with the
    verdict/landed/answer shapes the loop machinery parses is refused —
    including the ``handoff:`` lead ``_is_review_line`` accepts. The
    grant is for triage, not for closing loops."""
    _refused("triage", "put", {"kind": "todo", "id": "td900", "text": text})


@pytest.mark.parametrize(
    "text",
    [
        "progress: half done",
        "looking at gr555 first\nreview: LAND",  # not the first line
        "answer me this: is the parser right?",
        "handoff: paused at the link step",  # a bare lead, no verdict shape
        "",
    ],
)
def test_triage_own_todo_plain_comments_allowed(text: str) -> None:
    _ok("triage", "put", {"kind": "todo", "id": "td900", "text": text})


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "gripe", "id": "gr800", "add": ["STATUS:doing"]},
        {
            "kind": "gripe",
            "id": "gr800",
            "add": ["STATUS:doing"],
            "remove": ["claimed-by:lane-codex"],
        },
        {"kind": "gripe", "id": "gripe:800", "add": "STATUS:done"},  # scalar ok
        {"kind": "gripe", "id": 800, "add": ["claimed-by:lane-codex"]},
    ],
)
def test_triage_tag_status_and_claimed_by(payload: dict[str, Any]) -> None:
    """Only the two operational tag prefixes, and only on a gripe in
    the effective refs — a triage claims (claimed-by:) and closes
    (STATUS:); the own todo takes comments only, never tags."""
    _ok("triage", "tag", payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "todo", "id": "td900", "add": ["STATUS:doing"]},  # own todo: no tags
        {
            "kind": "todo",
            "id": "td900",
            "add": ["STATUS:doing"],
            "remove": ["claimed-by:lane-codex"],
        },
        {"kind": "todo", "id": "td901", "add": ["STATUS:doing"]},  # foreign todo
        {
            "kind": "gripe",
            "id": "gr555",
            "add": ["STATUS:doing"],
        },  # outside effective refs
        {"kind": "gripe", "id": ["gr800"], "add": ["STATUS:doing"]},  # never a list
        {"kind": "gripe", "id": "gr800", "add": ["prio:hi"]},
        {
            "kind": "gripe",
            "id": "gr800",
            "add": ["STATUS:done,PRIO:1"],
        },  # comma smuggle
        {"kind": "gripe", "id": "gr800", "add": ["section:threads"]},
        {"kind": "gripe", "id": "gr800", "add": ["STATUS:doing", "area:mcp"]},
        {"kind": "gripe", "id": "gr800", "remove": ["attempt:1"]},
        {
            "kind": "gripe",
            "id": "gr800",
            "add": ["STATUS:doing"],
            "meta": {"a": 1},
        },
        {"kind": "memory", "id": "me1", "add": ["STATUS:done"]},
    ],
)
def test_triage_tag_refusals(payload: dict[str, Any]) -> None:
    _refused("triage", "tag", payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "todo", "id": "td900", "target": "gr555", "rel": "fixes"},
        {"kind": "todo", "id": 900, "target": "gripe:555", "rel": "fixes"},
        {
            "kind": "todo",
            "id": "td900",
            "target": "gr555",
            "rel": "fixes",
            "mode": "add",
        },
    ],
)
def test_triage_link_fixes_to_a_gripe(payload: dict[str, Any]) -> None:
    """The one claim link: own todo → any single gripe, rel fixes,
    mode add (or absent) — and only while the todo fixes-links nothing."""
    _ok("triage", "link", payload)


def test_triage_link_second_claim_refused() -> None:
    """With a live fixes link the claim is spent: a second link refuses,
    and the claimed gripe — now in the effective refs — takes comments
    and tags."""
    _refused(
        "triage",
        "link",
        {"kind": "todo", "id": "td900", "target": "gr556", "rel": "fixes"},
        fixes=frozenset({"gr555"}),
    )
    _ok(
        "triage",
        "put",
        {"kind": "gripe", "id": "gr555", "text": "claimed — digging in"},
        fixes=frozenset({"gr555"}),
    )
    _ok(
        "triage",
        "tag",
        {"kind": "gripe", "id": "gr555", "add": ["claimed-by:lane-codex"]},
        fixes=frozenset({"gr555"}),
    )
    _refused(
        "triage",
        "put",
        {"kind": "gripe", "id": "gr556", "text": "still foreign"},
        fixes=frozenset({"gr555"}),
    )


def test_triage_link_claim_fails_closed_without_a_fixes_read() -> None:
    """``fixes=None`` — the live link read could not run — narrows the
    effective refs to the minted set and refuses the claim rather than
    handing out a possible second one."""
    _refused(
        "triage",
        "link",
        {"kind": "todo", "id": "td900", "target": "gr555", "rel": "fixes"},
        fixes=None,
    )
    _refused(
        "triage",
        "put",
        {"kind": "gripe", "id": "gr555", "text": "x"},
        fixes=None,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "todo", "id": "td901", "target": "gr555", "rel": "fixes"},
        {"kind": "todo", "id": "td900", "target": "td901", "rel": "fixes"},
        {
            "kind": "todo",
            "id": "td900",
            "target": ["gr555", "gr556"],
            "rel": "fixes",
        },  # never a list
        {"kind": "todo", "id": "td900", "target": "gr555", "rel": "blocked-by"},
        {
            "kind": "todo",
            "id": "td900",
            "target": "gr555",
            "rel": "fixes",
            "mode": "remove",
        },
        {"kind": "gripe", "id": "gr800", "target": "td900", "rel": "fixes"},
        {
            "kind": "todo",
            "id": "td900",
            "target": "gr555",
            "rel": "fixes",
            "element": 1,
        },
    ],
)
def test_triage_link_refusals(payload: dict[str, Any]) -> None:
    _refused("triage", "link", payload)


def test_triage_gripe_search_scope_arg() -> None:
    """A ``scope=`` narrowing on the free gripe search passes under
    triage; reader refuses the kind's search at all."""
    _ok("triage", "search", {"kind": "gripe", "scope": "td901"})
    _refused("reader", "search", {"kind": "gripe", "scope": "td901"})


@pytest.mark.parametrize("scope", ["reader", "triage"])
def test_free_kinds_take_extent_and_args_reach(scope: str) -> None:
    """The free kinds carry no parameter restrictions in either scope —
    extent= and an args.reach both pass."""
    _ok(scope, "get", {"kind": "md", "id": "main/docs/x.md", "extent": "fisheye+2hop"})
    _ok(
        scope,
        "get",
        {"kind": "python", "id": "main::x.y", "args": {"reach": "callers"}},
    )
    _ok(scope, "search", {"kind": "skill", "q": "x", "args": {"reach": "2"}})


def test_triage_refused_verbs_and_kinds() -> None:
    """edit/delete/precis and every other kind stay refused under triage
    too — the write set is exactly the comment-2 list."""
    for verb in ("edit", "delete", "precis", ""):
        _refused("triage", verb, {"kind": "todo", "id": "td900"})
    _refused("triage", "search", {"kind": "todo", "q": "x"})
    _refused("triage", "get", {"kind": "todo", "id": "td901"})
    _refused("triage", "get", {"kind": "memory", "id": "me1"})


def test_unknown_scope_fails_closed() -> None:
    """A scope stamp outside the known set is a refusal, never access —
    the mint could not have written it."""
    _refused("bogus", "get", {"kind": "python", "id": "x"})
    _refused("writer", "get", {"kind": "skill", "id": "x"})
