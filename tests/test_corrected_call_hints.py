"""gr613469: refusals lead with the corrected call, not just vocabulary.

Unknown-key gates (``args={}`` keys and top-level kwargs) and
unknown-view refusals name the caller's own call with the nearest
accepted spelling swapped in as the first ``  next:`` line — ahead of
the options/prose and the standing skill pointer — so the agent can
retry the runnable call instead of re-reading the docs.

DB-backed where the refusal path needs a resolved ref; the
``runtime_with_store`` fixture supplies it.
"""

from __future__ import annotations

import re

from precis.runtime import PrecisRuntime


def _next_lines(body: str) -> list[str]:
    return [
        line.removeprefix("  next: ")
        for line in body.splitlines()
        if line.startswith("  next: ")
    ]


def _first_concrete(body: str) -> str | None:
    """The first ``next:`` line that IS a call (the pointer excluded)."""
    for line in _next_lines(body):
        if re.match(
            r"^(get|search|put|edit|delete|tag|link)\(", line
        ) and not line.startswith("get(kind='skill'"):
            return line
    return None


# ── unknown top-level kwarg (slim-schema gate) ────────────────────────


def test_unknown_kwarg_names_the_swapped_call(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``scop`` on a paper search is ``scope`` one keystroke off — the
    refusal's first concrete next IS the caller's call with the key
    corrected (spec's own example)."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "paper", "q": "x", "scop": "pa1"}
    )
    assert is_error
    assert "does not accept ['scop']" in body
    nexts = _next_lines(body)
    assert nexts[0] == "search(kind='paper', q='x', scope='pa1')", body
    # The skill pointer still rides after it.
    assert any(line.startswith("get(kind='skill'") for line in nexts[1:])


def test_unknown_kwarg_without_a_near_match_names_no_call(
    runtime_with_store: PrecisRuntime,
) -> None:
    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "paper", "q": "x", "zznothing": 1}
    )
    assert is_error
    assert "does not accept ['zznothing']" in body
    assert _first_concrete(body) is None, body
    # …but the skill pointer still lands.
    assert any(line.startswith("get(kind='skill'") for line in _next_lines(body))


# ── unknown args={} key (extras gate) ─────────────────────────────────


def test_unknown_args_key_names_the_swapped_call(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``args={'job_typ': …}`` on a job search is ``job_type`` off by
    one — the corrected call keeps the args= shape with the key
    swapped."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {"kind": "job", "q": "ingest", "__extras__": {"job_typ": "ingest"}},
    )
    assert is_error
    assert "not accepted by job.search" in body
    first = _first_concrete(body)
    assert first is not None and first.startswith("search(kind='job'"), body
    assert "args={'job_type': 'ingest'}" in first, body


def test_unknown_args_key_without_a_near_match_names_no_call(
    runtime_with_store: PrecisRuntime,
) -> None:
    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {"kind": "job", "q": "ingest", "__extras__": {"zzznothing": 1}},
    )
    assert is_error
    assert "not accepted by job.search" in body
    assert _first_concrete(body) is None, body


# ── unknown views ─────────────────────────────────────────────────────


def test_todo_unknown_view_first_next_is_the_corrected_get(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The spec's slice-3 pin: ``view='comment'`` → ``view='comments'``."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "todo", "id": 1, "view": "comment"}
    )
    assert is_error
    nexts = _next_lines(body)
    assert nexts[0] == "get(kind='todo', id=1, view='comments')", body


def test_gripe_unknown_view_first_next_is_the_corrected_get(
    runtime_with_store: PrecisRuntime,
) -> None:
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "gripe", "id": 7, "view": "coments"}
    )
    assert is_error
    nexts = _next_lines(body)
    assert nexts[0] == "get(kind='gripe', id=7, view='comments')", body


def test_alert_unknown_view_first_next_is_the_corrected_get(
    runtime_with_store: PrecisRuntime,
) -> None:
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "alert", "id": 3, "view": "detal"}
    )
    assert is_error
    nexts = _next_lines(body)
    assert nexts[0] == "get(kind='alert', id=3, view='detail')", body


def test_memory_unknown_view_first_next_is_the_corrected_get(
    runtime_with_store: PrecisRuntime,
) -> None:
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": 1, "view": "bodi"}
    )
    assert is_error
    nexts = _next_lines(body)
    assert nexts[0] == "get(kind='memory', id=1, view='body')", body


def test_numeric_ref_unknown_view_first_next_is_the_corrected_get(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The shared ``NumericRefHandler.get`` gate: ``view='link'`` →
    ``view='links'`` on a kind with no view gate of its own (``job``);
    the gate runs after ref resolution, so the ref must exist."""
    store = runtime_with_store.store
    assert store is not None
    ref = store.insert_ref(kind="job", slug=None, title="view-gate probe", meta={})
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "job", "id": ref.id, "view": "link"}
    )
    assert is_error, body
    nexts = _next_lines(body)
    assert nexts[0] == f"get(kind='job', id={ref.id}, view='links')", body


def test_cache_kind_unknown_view_first_next_is_body(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``view='body'`` is a cache-backed get's only view — the corrected
    call leads unconditionally (no nearest-match needed)."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "wikipedia", "id": "deadbeef", "view": "wrong"}
    )
    assert is_error
    nexts = _next_lines(body)
    assert nexts[0] == "get(kind='wikipedia', id='deadbeef', view='body')", body


def test_todo_search_unknown_view_first_next_is_the_corrected_search(
    runtime_with_store: PrecisRuntime,
) -> None:
    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "todo", "q": "x", "view": "activ"}
    )
    assert is_error
    nexts = _next_lines(body)
    assert nexts[0] == "search(kind='todo', q='x', view='active')", body


# ── review td680653: no partial swaps, corrections keep caller args ──


def test_partial_top_level_match_yields_no_corrected_call(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``scop`` corrects to ``scope`` but ``zznothing`` has no near
    match — a corrected call naming both would still refuse, so the
    refusal names no call at all (mcp-authoring rule 2)."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "paper", "q": "x", "scop": "pa1", "zznothing": 1}
    )
    assert is_error
    assert "does not accept" in body
    assert _first_concrete(body) is None, body


def test_partial_args_key_match_yields_no_corrected_call(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Same suppression inside ``args=``: one key correctable, one
    not — no concrete ``next:``."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {
            "kind": "job",
            "q": "ingest",
            "__extras__": {"job_typ": "ingest", "zznothing": 1},
        },
    )
    assert is_error
    assert "not accepted by job.search" in body
    assert _first_concrete(body) is None, body


def test_todo_search_view_correction_keeps_caller_args(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The corrected search re-states the caller's filters — only
    ``view`` changes (review: the skeleton ``q=``-only hint dropped
    ``tags``/``page_size``/``args``)."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {
            "kind": "todo",
            "q": "x",
            "tags": ["t:one"],
            "page_size": 5,
            "view": "activ",
            "__extras__": {"page": 2},
        },
    )
    assert is_error
    nexts = _next_lines(body)
    assert nexts[0] == (
        "search(kind='todo', q='x', tags=['t:one'], page_size=5, "
        "view='active', args={'page': 2})"
    ), body


def test_alert_view_correction_keeps_context_filters(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``view='contex'`` → ``view='context'`` keeps the caller's
    host=/minutes= window — folded into ``args={…}``, the spelling the
    wire schema takes for get extras."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "get",
        {
            "kind": "alert",
            "id": 3,
            "view": "contex",
            "host": "web1",
            "minutes": 30,
        },
    )
    assert is_error
    nexts = _next_lines(body)
    assert nexts[0] == (
        "get(kind='alert', id=3, view='context', args={'host': 'web1', 'minutes': 30})"
    ), body


def test_checklist_view_correction_keeps_target(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``target``-addressed checklist gets keep their addressing —
    only the bad ``view`` is replaced."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "checklist", "target": "td680652", "view": "pge"}
    )
    assert is_error
    nexts = _next_lines(body)
    assert nexts[0] == (
        "get(kind='checklist', view='page', args={'target': 'td680652'})"
    ), body


def test_promoted_args_key_stays_nested_in_the_corrected_call(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``args={'status': …}`` lifts to a real search kwarg at dispatch
    (gr484234), but the corrected call replays the caller's own nesting:
    the promoted key stays inside ``args={…}``, not hoisted top-level
    (``status`` is even wire-declared — the hint preserves the caller's
    spelling, review td680653)."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {
            "kind": "todo",
            "q": "x",
            "__extras__": {"status": "open", "undre": "me1"},
        },
    )
    assert is_error, body
    assert "not accepted by todo.search" in body
    nexts = _next_lines(body)
    assert nexts[0] == (
        "search(kind='todo', q='x', args={'status': 'open', 'under': 'me1'})"
    ), body


def test_non_wire_top_level_kwarg_folds_into_args_in_the_corrected_call(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``group_by=`` is a real job.search kwarg but not a wire-declared
    ``search`` param — the corrected call folds it into ``args={…}`` so
    the suggested retry survives the slim schema (review td680653)."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {
            "kind": "job",
            "q": "ingest",
            "group_by": "state",
            "__extras__": {"job_typ": "ingest"},
        },
    )
    assert is_error, body
    nexts = _next_lines(body)
    assert nexts[0] == (
        "search(kind='job', q='ingest', "
        "args={'group_by': 'state', 'job_type': 'ingest'})"
    ), body


def test_numeric_ref_view_correction_keeps_extras(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The links-view ``rel=`` filter rides the corrected get — under
    ``args=``, since ``rel`` is not a wire-declared get param (the gate
    runs after ref resolution, so the ref must exist; ``concept`` takes
    the shared ``NumericRefHandler.get`` unchanged)."""
    store = runtime_with_store.store
    assert store is not None
    ref = store.insert_ref(kind="concept", slug=None, title="extras probe", meta={})
    body, is_error = runtime_with_store.dispatch_with_status(
        "get",
        {
            "kind": "concept",
            "id": ref.id,
            "view": "link",
            "__extras__": {"rel": "touched"},
        },
    )
    assert is_error, body
    nexts = _next_lines(body)
    assert nexts[0] == (
        f"get(kind='concept', id={ref.id}, view='links', args={{'rel': 'touched'}})"
    ), body
