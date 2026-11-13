"""gr475332: unknown ``args=`` keys are rejected on passthrough handlers.

A handler whose verb method declares ``args: dict`` receives the whole
extras dict instead of flattened kwargs, which used to skip the
unknown-key gate (``todo.search(args={'bogus': 1})`` silently dropped
``bogus``). Such handlers now declare ``ARGS_KEYS[verb]``; dispatch
rejects anything else with the same ``BadInput`` shape as paper.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import textwrap
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

import pytest

import precis.handlers as handlers_pkg
from precis.errors import BadInput
from precis.protocol import ArgsSpec, Handler, UndeclaredArgsKey
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.user_context import user_scope
from precis.users import PasswordRecord

#: (kind, verb) for every in-repo handler verb that declares ``args``.
PASSTHROUGH = [
    ("learning", "get"),
    ("learning", "put"),
    ("learning", "edit"),
    ("todo", "search"),
    ("todo", "delete"),
    ("gripe", "delete"),
    ("measure", "search"),
    # measure.put's recheck door reads args{'papers','dry_run'}
    # (gr506762 subset-4)
    ("measure", "put"),
    ("draft", "get"),
    ("python", "get"),
    ("pcb", "put"),
    ("pcb", "get"),
    ("cad", "get"),
    ("component", "put"),
    ("component", "get"),
    ("quest", "get"),
    ("paper", "get"),
    ("paper", "edit"),
    ("structure", "put"),
    ("structure", "edit"),
    ("structure", "get"),
    ("random", "get"),
    ("time", "get"),
    ("paper", "put"),
    ("paper", "delete"),
    ("datasheet", "put"),
    ("datasheet", "edit"),
    ("youtube", "put"),
    ("pres", "edit"),
    # llm.put's owner account door reads args{'account'} (0280, td568204)
    ("llm", "put"),
    # Plugin handlers (entry point group ``precis.handlers``).
    ("estimate", "get"),
    ("pathway", "get"),
    ("pathway", "put"),
    # pathway.search's meta filters (candidate/engine/status/tier) ride
    # args= (gr592660).
    ("pathway", "search"),
    ("se", "get"),
    ("se", "put"),
    ("se", "edit"),
]


def _handler_class(runtime: PrecisRuntime, kind: str) -> type[Handler]:
    return type(runtime.hub.handlers[kind])


def _call(runtime: PrecisRuntime, kind: str, verb: str, extras: dict) -> str:
    if kind == "learning":
        # Exercise the argument gate after a real account passes the private
        # kind's authentication boundary; anonymous calls stop before it.
        assert runtime.store is not None
        owner = runtime.store.create_web_user(
            login="args-learner",
            abbrev="arg",
            password=PasswordRecord("unused", "unused", "scrypt-v1"),
        )
        with user_scope(owner.id):
            return runtime.dispatch(verb, {"kind": kind, "__extras__": extras})
    return runtime.dispatch(verb, {"kind": kind, "__extras__": extras})


# ── gr524663: intercepted search shapes refuse args they can't apply ───
#
# The single-kind path vets payload keys against the resolved handler's
# signature; the dispatch-level interceptions (cross-kind fan-out,
# source search, the sprays/views) build their own kwarg set and used to
# silently drop everything else — ``search(q='nanobuds', kinds=[...])``
# ran the full ~34-kind fan-out under only a deprecation note. Each
# interception now refuses the keys it does not consume, naming the
# corrected call (``runtime._shared.refuse_unapplied_args``).


def test_cross_kind_search_refuses_kinds_naming_kind_form(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The gripe's exact call: ``kinds=[...]`` with no ``kind=`` meant
    "search these kinds" — the corrected form is ``kind='a,b'``."""
    out = runtime_with_store.dispatch(
        "search", {"q": "nanobuds", "kinds": ["memory", "finding"]}
    )
    assert "[error:BadInput]" in out, out
    assert "'kinds'" in out, out
    assert "kind='memory,finding'" in out, out
    assert "not applied by a cross-kind search" in out, out
    # the expensive default must NOT have run
    assert "matches" not in out.lower(), out


def test_cross_kind_search_refuses_per_kind_knobs_and_extras(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Every unapplied payload key is named — top-level and args= alike."""
    out = runtime_with_store.dispatch(
        "search",
        {
            "q": "x",
            "per_paper": 2,
            "status": "open",
            "__extras__": {"bogus_extra": 1},
        },
    )
    assert "[error:BadInput]" in out, out
    for name in ("per_paper", "status", "bogus_extra"):
        assert name in out, out


def test_cross_kind_search_page_gt_one_refused(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch("search", {"q": "x", "page": 3})
    assert "[error:BadInput]" in out, out
    assert "page" in out and "bounded top-N" in out, out


def test_intercepted_search_paging_spellings_refuse_as_aliases(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr530420: ``k``/``limit``/``offset`` are verb-level spellings the
    ``search`` wrapper consumes, not kind args — an intercepted shape
    refuses them naming the canonical call, never under the generic
    "not applied" list."""
    out = runtime_with_store.dispatch(
        "search", {"q": "x", "__extras__": {"limit": 5, "offset": 10}}
    )
    assert "[error:BadInput]" in out, out
    assert "not applied by" not in out, out
    assert "search-level paging spelling" in out, out
    assert "page_size" in out, out
    # mixed with a genuine unapplied key, the spelling gets the alias
    # hint while the stray keeps the unapplied report
    out = runtime_with_store.dispatch(
        "search",
        {"q": "x", "status": "open", "__extras__": {"k": 5}},
    )
    assert "[error:BadInput]" in out, out
    assert "'status'" in out and "not applied by" in out, out
    assert "'k'" not in out.split("not applied by")[0], out
    assert "page_size" in out, out


def test_cross_kind_search_still_runs_when_args_apply(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The gate fires only on unconsumed keys — a plain fan-out and a
    tags-only sweep are untouched."""
    out = runtime_with_store.dispatch("search", {"q": "zz_no_such_token_zz"})
    assert "[error" not in out, out
    out = runtime_with_store.dispatch("search", {"tags": ["topic:zz-none"]})
    assert "[error" not in out, out


def test_source_search_refuses_unapplied_keys(
    runtime_with_store: PrecisRuntime,
) -> None:
    """sort=/since=/until= route to the source-search primitive, which
    drops the same per-kind knobs the fan-out did."""
    out = runtime_with_store.dispatch(
        "search", {"q": "x", "sort": "recency", "kinds": ["memory"], "view": "abstract"}
    )
    assert "[error:BadInput]" in out, out
    assert "kinds" in out and "view" in out, out


def test_source_search_sort_still_applies(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch(
        "search", {"q": "zz_nothing_zz", "sort": "recency"}
    )
    assert "[error" not in out, out


def test_folder_scoped_search_refuses_sort(runtime_with_store: PrecisRuntime) -> None:
    """``folder=`` routes to the fan-out before the source-search
    interception, so ``folder= + sort=`` used to drop sort silently."""
    out = runtime_with_store.dispatch(
        "search", {"q": "x", "folder": "no-such-folder", "sort": "recency"}
    )
    assert "[error:BadInput]" in out, out
    assert "sort" in out, out


def test_angle_spray_refuses_unapplied_keys(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch(
        "search", {"q": "x", "angle": 0.5, "status": "open"}
    )
    assert "[error:BadInput]" in out, out
    assert "status" in out, out


def test_stubs_view_refuses_stray_keys(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The stubs view consumes ``q=``/``since=``/``until=``/the ref-id
    facets (gr608580) — any *other* unapplied key still refuses rather
    than silently dropping."""
    out = runtime_with_store.dispatch("search", {"view": "stubs", "status": "open"})
    assert "[error:BadInput]" in out, out
    assert "status" in out, out


def test_folder_scoped_search_next_is_a_working_call(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``search(kind='todo', q='x', folder=N, status='open')``: the caller
    already named one kind — "narrow to one kind" is the wrong hint.
    The refusal's ``next`` is built from the call shape: drop the refused
    key, or drop the interception trigger so the kind's own search sees
    it (mcp-authoring rule 2)."""
    out = runtime_with_store.dispatch(
        "search", {"kind": "todo", "q": "x", "folder": 12, "status": "open"}
    )
    assert "[error:BadInput]" in out, out
    assert "'status'" in out, out
    assert "narrow to one kind" not in out, out
    assert "search(kind='todo', q='x', args={'folder': 12})" in out, out
    assert "search(kind='todo', q='x', status='open')" in out, out


def test_source_search_next_is_a_working_call(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Same for the sort=/since=/until= route: drop the refused key, or
    drop the trigger so the kind's own listing applies it."""
    out = runtime_with_store.dispatch(
        "search", {"kind": "todo", "sort": "recency", "link": "gr1"}
    )
    assert "[error:BadInput]" in out, out
    assert "'link'" in out, out
    assert "search(kind='todo', sort='recency')" in out, out
    assert "search(kind='todo', args={'link': 'gr1'})" in out, out


def test_source_search_hints_spell_since_inside_args(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr608575: ``since=``/``until=`` have one documented location —
    inside ``args={…}`` (``sort=`` stays top-level, the one core
    trigger). The BadInput's shape and ``next=`` hints must say so:
    re-suggesting the top-level spelling aimed the caller back at the
    deprecation note the slim wrapper appends to it."""
    out = runtime_with_store.dispatch(
        "search",
        {"kind": "todo", "q": "x", "since": "2024-01-01", "bogus": 1},
    )
    assert "[error:BadInput]" in out, out
    # the shape names the locations: sort= top-level, since/until in args
    assert "args={'since'/'until'}" in out, out
    # the corrected call nests the date bound — never re-suggests it
    # top-level
    assert "args={'since': '2024-01-01'}" in out, out
    assert "since='2024-01-01'" not in out.replace("'since': '2024-01-01'", ""), out


def test_tags_only_sweep_refuses_ranking_knobs(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The tags-only sweep is a flat list_refs query — the fan-out's
    ranking knobs (``mode``, ``exclude``, ``view``) never reach it, so
    they refuse under its own narrower consumed set instead of silently
    dropping. ``top_k``/``n`` are the exception: they read as the page
    cap, matching angle/dreamable."""
    out = runtime_with_store.dispatch(
        "search",
        {"tags": ["topic:zz-none"], "mode": "semantic", "exclude": ["paper"]},
    )
    assert "[error:BadInput]" in out, out
    assert "'mode'" in out and "'exclude'" in out, out
    assert "tags-only" in out, out


def test_tags_only_sweep_reads_top_k_and_n(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``top_k``/``n`` cap the sweep's page size like ``page_size=`` —
    accepted, not refused."""
    for kw in ({"top_k": 20}, {"n": 5}):
        out = runtime_with_store.dispatch("search", {"tags": ["topic:zz-none"], **kw})
        assert "[error" not in out, (kw, out)


def test_tags_only_sweep_reads_top_k_from_args(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The MCP verb exposes ``n=`` but not ``top_k=`` — ``args={'top_k':
    N}`` is the only way a caller sends it, and it used to refuse as an
    unapplied extras key. Hoisted, it reads as the sweep's page cap
    with the same ``n`` > ``top_k`` > ``page_size`` precedence as the
    top-level spelling."""
    from tests.conftest import id_of

    for i in range(3):
        mid = id_of(
            runtime_with_store.dispatch("put", {"kind": "memory", "text": f"t{i}"})
        )
        runtime_with_store.dispatch(
            "tag", {"kind": "memory", "id": mid, "add": ["topic:zz-sweep-cap"]}
        )
    out = runtime_with_store.dispatch(
        "search",
        {"tags": ["topic:zz-sweep-cap"], "__extras__": {"top_k": 2}},
    )
    assert "[error" not in out, out
    assert "# 2 refs tagged" in out, out
    # extras ``n`` reads too, and outranks extras ``top_k``.
    out = runtime_with_store.dispatch(
        "search",
        {"tags": ["topic:zz-sweep-cap"], "__extras__": {"n": 1, "top_k": 2}},
    )
    assert "# 1 ref tagged" in out, out


def test_next_hints_render_caller_keys_not_resolved_ids(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``under=`` resolves into ``include_ref_ids`` before the refusal
    runs — the drop-keys hint must re-render the caller's own key, not
    the dispatch-internal id list, and must not render ``page=1``
    (a tolerated wrapper default, not caller intent)."""
    from precis.utils import handle_registry

    ref = store.insert_ref(kind="memory", slug=None, title="t", meta={})
    handle = handle_registry.format_handle("memory", ref.id)
    out = runtime_with_store.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "x",
            "sort": "recency",
            "under": handle,
            "bogus": 1,
            "page": 1,
        },
    )
    assert "[error:BadInput]" in out, out
    assert "'bogus'" in out, out
    assert "include_ref_ids" not in out, out
    assert f"'under': '{handle}'" in out, out
    assert "page=" not in out, out


def test_queryless_folder_call_raises_needs_q_not_strays(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``search(kind='todo', folder=12, status='open')``: the first
    problem is the missing filter — refusing ``status`` first would
    mislabel the call "a tags-only sweep" and offer a drop-keys hint
    whose rewritten call still has no q=/tags=. The refusal names the
    folder-scoped search (not "cross-kind" — the caller named one
    kind), and the q= hint keeps ``folder=``, which the fan-out
    applies. When dropping folder= lands on the kind's own search,
    that working call is the second hint."""
    out = runtime_with_store.dispatch(
        "search", {"kind": "todo", "folder": 12, "status": "open"}
    )
    assert "[error:BadInput]" in out, out
    assert "folder-scoped search requires q= or tags=" in out, out
    assert "not applied by" not in out, out
    assert "search(kind='todo', q='your query', args={'folder': 12})" in out, out
    assert "search(kind='todo', tags=['<tag>'], args={'folder': 12})" in out, out
    assert "search(kind='todo', status='open')" in out, out


def test_queryless_wildcard_mode_raises_needs_q_not_strays(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``search(kind='*', mode='lexical')``: same gate — the stray
    ``mode`` is not what fails first."""
    out = runtime_with_store.dispatch("search", {"kind": "*", "mode": "lexical"})
    assert "[error:BadInput]" in out, out
    assert "requires q= or tags=" in out, out
    assert "not applied by" not in out, out


def test_job_since_stays_on_the_job_listing(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A queryless ``search(kind='job', since=…)`` is the job listing's
    own ``created_at`` bound — it must not route to the cross-kind
    source-search primitive, which requires q=."""
    out = runtime_with_store.dispatch(
        "search", {"kind": "job", "since": "2024-01-01", "status": "*"}
    )
    assert "[error" not in out, out
    assert "matches across" not in out, out


def test_job_since_with_link_stays_on_the_job_listing(
    runtime_with_store: PrecisRuntime,
) -> None:
    """link= + since= composes on the job listing path — pre-fix the
    source-search route refused ``link`` as an unapplied key."""
    gripe = runtime_with_store.dispatch("put", {"kind": "gripe", "text": "x"})
    assert "[error" not in gripe, gripe
    out = runtime_with_store.dispatch(
        "search", {"kind": "job", "link": "gripe:1", "since": "2024-01-01"}
    )
    assert "[error" not in out, out
    assert "matches across" not in out, out


def test_job_since_with_explicit_sort_still_routes_to_source_search(
    runtime_with_store: PrecisRuntime,
) -> None:
    """An explicit ``sort=`` still asks for the source-search primitive —
    the job exemption covers the bare-window call shape only."""
    out = runtime_with_store.dispatch(
        "search",
        {"kind": "job", "q": "zz_no_such_token_zz", "sort": "recency"},
    )
    assert "[error" not in out, out
    assert "matches" in out.lower(), out


def test_job_since_with_q_routes_to_source_search(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``search(kind='job', q='…', since=…)`` is a ranked dated search —
    the listing exemption covers queryless calls only, so a ``q=``
    routes back to the source-search primitive's hybrid ranking."""
    out = runtime_with_store.dispatch(
        "search",
        {"kind": "job", "q": "zz_no_such_token_zz", "since": "2024-01-01"},
    )
    assert "[error" not in out, out
    assert "matches" in out.lower(), out
    assert "job entries" not in out, out


def test_dated_job_listing_refusals_do_not_name_job_type(
    runtime_with_store: PrecisRuntime,
) -> None:
    """mode=/view= refused on the plain dated listing must not read
    "job_type-filtered" — the caller passed no job_type — and the
    next= must be a call that works (add q= for the ranked path). An
    explicit sort= diverts to the source-search primitive upstream, so
    its wording is covered by a direct handler call in
    test_job_type_search."""
    out = runtime_with_store.dispatch(
        "search", {"kind": "job", "since": "2024-01-01", "mode": "semantic"}
    )
    assert "[error:BadInput]" in out, out
    assert "job_type-filtered" not in out, out
    out = runtime_with_store.dispatch(
        "search", {"kind": "job", "since": "2024-01-01", "view": "tree"}
    )
    assert "[error:BadInput]" in out, out
    assert "job_type-filtered" not in out, out


def test_job_search_nested_sort_routes_to_source_search(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr593340: ``search(kind='job', q='…', args={'sort': 'recency'})``
    is the top-level spelling misfiled inside args= — nested, the
    routing check never saw it and the extras gate refused it as
    ``keys ['sort'] not accepted by job.search`` (JobHandler.search
    forwards ``sort`` instead of declaring it). The hoist makes the
    nested call the top-level call: it routes to the source-search
    primitive like ``sort='recency'`` always did."""
    out = runtime_with_store.dispatch(
        "search",
        {
            "kind": "job",
            "q": "zz_no_such_token_zz",
            "__extras__": {"sort": "recency"},
        },
    )
    assert "[error" not in out, out
    assert "matches" in out.lower(), out
    assert "not accepted by job.search" not in out, out


def test_search_nested_folder_routes_to_folder_scope(
    runtime_with_store: PrecisRuntime,
) -> None:
    """td658272: ``search(kind='draft', q=…, args={'folder': …})`` is the
    top-level ``folder=`` spelling misfiled inside args= — the folder
    route reads ``args['folder']`` only, so the nested key died at the
    extras gate as ``keys ['folder'] not accepted by draft.search``.
    The hoist makes the nested call the top-level call: it reaches the
    folder-scope resolution (which refuses the unknown folder name) —
    never the handler's extras gate."""
    out = runtime_with_store.dispatch(
        "search",
        {"kind": "draft", "q": "x", "__extras__": {"folder": "no-such-folder"}},
    )
    assert "not accepted by draft.search" not in out, out
    assert "no-such-folder" in out or "folder" in out, out


def test_search_folder_passed_twice_refuses(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A ``folder=`` at both levels is ambiguous — refuse it rather than
    pick a winner (same shape as ``sort=``)."""
    out = runtime_with_store.dispatch(
        "search",
        {
            "kind": "draft",
            "q": "x",
            "folder": "a",
            "__extras__": {"folder": "b"},
        },
    )
    assert "[error:BadInput]" in out, out
    assert "folder= was passed twice" in out, out


def test_put_nested_reason_is_the_consumed_reason(
    runtime_with_store: PrecisRuntime,
) -> None:
    """td658272: ``put(kind='memory', text=…, args={'reason': …})`` is the
    revision-log ``reason=`` misfiled inside args= — the dispatcher
    consumes ``reason`` before the kwargs gate (memory.put does not
    declare it), so the nested key used to die at the extras gate. The
    hoist makes the nested call the top-level call: the put succeeds and
    the reason lands in the revision context."""
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "nested reason hoist",
            "__extras__": {"reason": "probe"},
        },
    )
    assert "[error" not in out, out
    assert "not accepted" not in out, out


def test_put_reason_passed_twice_refuses(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A ``reason=`` at both levels is ambiguous — refuse it rather than
    pick a winner."""
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "twice",
            "reason": "top",
            "__extras__": {"reason": "nested"},
        },
    )
    assert "[error:BadInput]" in out, out
    assert "reason= was passed twice" in out, out


def test_job_type_search_with_q_and_since_stays_on_job(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """gr608734: ``search(kind='job', q='…', since=…, args={'job_type':
    '…'})`` is the kind's own filtered search — the source-search
    primitive has no ``job_type`` concept and refused the call outright
    (``args['job_type'] is not applied by a source search``). The dated
    job listing applies all three filters itself."""
    store.insert_ref(
        kind="job",
        slug=None,
        title="land_slice td591266",
        meta={"job_type": "land_slice"},
    )
    store.insert_ref(
        kind="job",
        slug=None,
        title="land_slice td999999",
        meta={"job_type": "release_poll"},
    )
    out = runtime_with_store.dispatch(
        "search",
        {
            "kind": "job",
            "q": "land_slice td591266",
            "since": "2024-01-01",
            "status": "*",
            "__extras__": {"job_type": "land_slice"},
        },
    )
    assert "[error" not in out, out
    assert "job entr" in out, out
    assert "with job_type 'land_slice'" in out, out
    assert "td999999" not in out, out


def test_job_type_top_level_also_stays_on_job(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The top-level ``job_type=`` spelling takes the same carve-out."""
    store.insert_ref(kind="job", slug=None, title="t", meta={"job_type": "sweep"})
    out = runtime_with_store.dispatch(
        "search",
        {
            "kind": "job",
            "job_type": "sweep",
            "since": "2024-01-01",
            "status": "*",
        },
    )
    assert "[error" not in out, out
    assert "with job_type 'sweep'" in out, out


def test_job_type_search_refuses_sort_on_the_job_path(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A ``sort=`` the job_type path can't apply still refuses — now
    with the job listing's own message (its ordering is already
    recency), not the source-search field gate."""
    out = runtime_with_store.dispatch(
        "search",
        {
            "kind": "job",
            "sort": "recency",
            "__extras__": {"job_type": "land_slice"},
        },
    )
    assert "[error:BadInput]" in out, out
    assert "job_type-filtered" in out, out
    assert "not applied by a source search" not in out, out


def test_nested_sort_reaches_the_kind_browse_path(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The sibling shape (gr593341): a queryless ``args={'sort':
    'recency'}`` on a browse kind hoists to the handler's own ``sort``
    kwarg — ``numeric_browse`` keeps it off the source-search route —
    instead of dying at the extras gate on handlers that forward the
    kwarg without declaring it (todo) or declare an ``args`` dict
    without it (``ArgsSpec({'under'})``)."""
    out = runtime_with_store.dispatch(
        "search", {"kind": "todo", "__extras__": {"sort": "recency"}}
    )
    assert "[error" not in out, out
    assert "not accepted by todo.search" not in out, out
    out = runtime_with_store.dispatch(
        "search", {"kind": "gripe", "__extras__": {"sort": "drain"}}
    )
    assert "[error" not in out, out


def test_nested_sort_double_spelling_refused(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``sort=`` at both levels is ambiguous — refuse it rather than
    pick a winner (same contract as the cite-facet hoist)."""
    out = runtime_with_store.dispatch(
        "search",
        {
            "kind": "job",
            "q": "zz",
            "sort": "relevance",
            "__extras__": {"sort": "recency"},
        },
    )
    assert "[error:BadInput]" in out, out
    assert "passed twice" in out, out


def test_get_extent_refuses_stray_args(runtime_with_store: PrecisRuntime) -> None:
    """The ``get`` twin of the gripe: ``handler.eye`` takes only
    id/extent/q/ring, so any other payload or args= key was silently
    dropped on an eye read."""
    out = runtime_with_store.dispatch(
        "get",
        {
            "kind": "memory",
            "id": 1,
            "extent": "fisheye",
            "kinds": "x",
            "__extras__": {"bogus": 1},
        },
    )
    assert "[error:BadInput]" in out, out
    assert "kinds" in out and "bogus" in out, out
    assert "extent='fisheye'" in out, out


@pytest.mark.parametrize(("kind", "verb"), PASSTHROUGH)
def test_junk_args_key_rejected_naming_accepted_keys(
    runtime_with_store: PrecisRuntime, kind: str, verb: str
) -> None:
    if runtime_with_store.hub.handler_for(kind) is None:
        # Gated out in this build (e.g. pathway without autocatpath on CI).
        pytest.skip(f"kind {kind!r} disabled in this build")
    out = _call(runtime_with_store, kind, verb, {"zz_bogus_key": 1})
    assert "[error:BadInput]" in out, out
    assert f"not accepted by {kind}.{verb}" in out, out
    assert "zz_bogus_key" in out
    cls = _handler_class(runtime_with_store, kind)
    for key in cls.ARGS_KEYS[verb]:
        assert key in out, f"accepted key {key!r} missing from error: {out}"


def _declared_cases() -> list[tuple[str, str, str]]:
    # Static import-time view of the declarations (no runtime needed).
    cases: list[tuple[str, str, str]] = []
    for kind, verb in PASSTHROUGH:
        cls = _KIND_CLASS[kind]
        cases.extend((kind, verb, k) for k in sorted(cls.ARGS_KEYS[verb]))
    return cases


def _load_kind_classes() -> dict[str, type[Handler]]:
    out: dict[str, type[Handler]] = {}
    for mod in pkgutil.iter_modules(handlers_pkg.__path__):
        m = importlib.import_module(f"precis.handlers.{mod.name}")
        for _, cls in inspect.getmembers(m, inspect.isclass):
            spec = cls.__dict__.get("spec")
            if issubclass(cls, Handler) and spec is not None:
                out[spec.kind] = cls
    # Plugin packages register through the ``precis.handlers`` entry point.
    for ep in entry_points(group="precis.handlers"):
        cls = ep.load()
        if inspect.isclass(cls) and issubclass(cls, Handler):
            out[cls.spec.kind] = cls
    return out


_KIND_CLASS = _load_kind_classes()


@pytest.mark.parametrize(("kind", "verb", "key"), _declared_cases())
@pytest.mark.nightly(
    reason="133 s at run 42354225792 (me580555, gate-s2): tests >=30 s run on the nightly FULL shape (the gate's soak also deselects -m 'not nightly'), off the 5-minute gate"
)
def test_declared_args_key_not_rejected(
    runtime_with_store: PrecisRuntime, kind: str, verb: str, key: str
) -> None:
    if runtime_with_store.hub.handler_for(kind) is None:
        # Disabled kinds answer Unsupported, which would pass vacuously.
        pytest.skip(f"kind {kind!r} disabled in this build")
    out = _call(runtime_with_store, kind, verb, {key: 1})
    assert f"not accepted by {kind}.{verb}" not in out, out


def test_every_args_passthrough_verb_declares_its_keys() -> None:
    """A new ``args: dict`` verb without ``ARGS_KEYS`` fails here, loudly."""
    found = []
    for kind, cls in _KIND_CLASS.items():
        for verb in ("get", "put", "edit", "search", "delete"):
            fn = cls.__dict__.get(verb)
            if fn is None or "args" not in inspect.signature(fn).parameters:
                continue
            found.append((kind, verb))
            assert verb in cls.ARGS_KEYS, (
                f"{cls.__name__}.{verb} declares args but ARGS_KEYS lacks it"
            )
    assert sorted(found) == sorted(PASSTHROUGH)


def test_plugin_handlers_are_discovered() -> None:
    """Guard: the entry-point scan must actually see the plugin kinds, or
    the declaration check above passes vacuously for them."""
    assert {"estimate", "pathway", "se"} <= set(_KIND_CLASS)


def test_se_get_keys_are_union_of_view_args() -> None:
    from precis_se.handler import _GLOBAL_ARGS, _VIEW_ARGS, SeHandler

    # Every view's own inputs plus the display knobs every view accepts
    # (``prefunits``) — the same two sets ``_vet_view_args`` unions.
    union: set[str] = set().union(*_VIEW_ARGS.values()) | set(_GLOBAL_ARGS)
    assert SeHandler.ARGS_KEYS["get"] == union


def _se_dispatched_views() -> set[str]:
    """The view names ``SeHandler.get`` actually dispatches, read off the
    AST — every ``v == '…'`` / ``v in (…)`` / ``completion_view`` compare
    in ``_get_impl`` (``get`` is a display-preference shell that forwards
    to it). Static, so a new dispatch branch that forgets the tables
    fails the agreement test below instead of silently dropping its args
    (gr514467)."""
    from precis_se.handler import SeHandler

    node = _fn_node(SeHandler._get_impl)
    assert node is not None
    out: set[str] = set()
    for child in ast.walk(node):
        if not (
            isinstance(child, ast.Compare)
            and isinstance(child.left, ast.Name)
            and child.left.id in ("v", "completion_view")
        ):
            continue
        for op, comp in zip(child.ops, child.comparators, strict=True):
            if (
                isinstance(op, ast.Eq)
                and isinstance(comp, ast.Constant)
                and isinstance(comp.value, str)
            ):
                out.add(comp.value)
            elif isinstance(op, ast.In) and isinstance(comp, ast.Tuple | ast.List):
                out.update(
                    e.value
                    for e in comp.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )
    return out


def test_se_view_tables_agree_with_dispatch() -> None:
    """gr514467 (+ merged gr514468): the three places that describe the
    ``get(kind='se')`` view vocabulary — the dispatch itself,
    :data:`_VIEW_ARGS` (the args-vetting table) and
    ``SeHandler.spec.views`` (the advertised list) — had drifted:
    ``view='order'`` was dispatched but unlisted, so its ``args`` were
    silently dropped, and ~8 dispatched views were missing from
    ``spec.views``. ``_VIEWS_ALL`` (the unknown-view hint's roster) is
    held to the same set."""
    from precis_se.handler import _VIEW_ARGS, _VIEWS_ALL, SeHandler

    dispatched = _se_dispatched_views()
    assert dispatched == set(_VIEW_ARGS)
    advertised = dispatched - {""}
    assert advertised == set(SeHandler.spec.views)
    assert advertised == set(_VIEWS_ALL)


def test_acquire_kwargs_in_args_reach_paper_put(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr484234: ``args={'doi': …, 'verify': False}`` reaches
    ``paper.put`` — the acquire knobs are declared ``ARGS_KEYS`` and the
    handler maps them out of ``args=`` itself (cc1b88ffc). Genuinely
    unknown keys are still rejected."""
    from precis.handlers import paper as paper_mod
    from precis.ingest.semantic_scholar import S2ResolverError

    def _down(*_a: object) -> None:
        raise S2ResolverError("offline in test")

    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", _down)

    out = _call(
        runtime_with_store, "paper", "put", {"doi": "10.5555/acq-x", "verify": False}
    )
    assert "[error:BadInput]" not in out, out
    assert "minted stub" in out, out

    bad = _call(
        runtime_with_store, "paper", "put", {"doi": "10.5555/acq-y", "zz_nope": 1}
    )
    assert "not accepted by paper.put" in bad, bad
    assert "zz_nope" in bad


def test_args_key_naming_handler_kwarg_is_promoted_not_rejected(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr484234: an ``args=`` key naming a real handler kwarg is a misfiled
    top-level argument — dispatch promotes it instead of dying on the
    ``ARGS_KEYS`` gate. ``tags`` exercises it on paper.put: a real kwarg
    the ``ARGS_KEYS`` spec does not declare (the acquire knobs — ``doi``,
    ``verify`` — are declared since cc1b88ffc, so they ride ``args=`` as
    designed, covered above). A genuinely unknown key is still rejected."""
    from precis.handlers import paper as paper_mod
    from precis.ingest.semantic_scholar import S2ResolverError

    def _down(*_a: object) -> None:
        raise S2ResolverError("offline in test")

    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", _down)

    out = _call(
        runtime_with_store,
        "paper",
        "put",
        {"doi": "10.5555/acq-x", "verify": False, "tags": ["promotion-probe"]},
    )
    assert "[error:BadInput]" not in out, out
    assert "minted stub" in out, out
    rid = int(out.split("id=", 1)[1].split()[0])
    assert "promotion-probe" in {str(t) for t in store.tags_for(rid)}

    bad = _call(
        runtime_with_store, "paper", "put", {"doi": "10.5555/acq-y", "zz_nope": 1}
    )
    assert "not accepted by paper.put" in bad, bad
    assert "zz_nope" in bad


def test_args_key_naming_forwarded_kwarg_is_promoted_not_rejected(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr593341: ``search(kind='todo', args={'sort': 'recency'})``.

    ``sort`` is no explicit param of ``TodoHandler.search`` — its
    ``**_kw`` catch-all forwards to ``NumericRefHandler.search`` where
    ``sort=`` is real. The own-signature promotable check refused it on
    the ``ARGS_KEYS`` gate while gripe/quest take the same args=
    spelling through their lenient/explicit paths. The MRO-accepted set
    promotes it; an invalid value then surfaces the base browse-sort
    refusal (proof it reached ``sort=``, not a silent drop), and
    ``args={'id_min': …}`` — the same gripe's other misfiled key —
    promotes too."""
    out = _call(runtime_with_store, "todo", "search", {"sort": "recency"})
    assert "[error" not in out, out
    out = _call(runtime_with_store, "todo", "search", {"id_min": 0})
    assert "[error" not in out, out

    bad = _call(runtime_with_store, "todo", "search", {"sort": "bogus"})
    assert "not accepted by todo.search" not in bad, bad
    assert "sort= is supported" in bad, bad


@pytest.mark.parametrize("kind", ["gripe", "todo"])
def test_unknown_put_args_key_points_at_comment_idiom(
    runtime_with_store: PrecisRuntime, kind: str
) -> None:
    """gr478557: ``args={'comment': True}`` on put names the real comment door."""
    out = runtime_with_store.dispatch(
        "put", {"kind": kind, "id": 1, "text": "x", "__extras__": {"comment": True}}
    )
    assert "[error:BadInput]" in out, out
    assert f"to comment: put(kind='{kind}', id=N, text='...') with no args" in out, out


@pytest.mark.parametrize(
    ("kind", "verb", "params"),
    [
        ("memory", "put", {"text": "x"}),
        ("anki", "put", {"text": "x"}),
        ("gripe", "search", {"q": "x"}),
        ("gripe", "delete", {"id": 1}),
    ],
)
def test_unknown_args_key_no_comment_hint_off_comment_door(
    runtime_with_store: PrecisRuntime, kind: str, verb: str, params: dict
) -> None:
    """The 'to comment:' hint is only for put on a supports_comment kind."""
    out = runtime_with_store.dispatch(
        verb, {"kind": kind, **params, "__extras__": {"bogus_key": True}}
    )
    assert "[error:BadInput]" in out, out
    assert "to comment:" not in out, out


def test_args_keys_derive_from_args_spec() -> None:
    """Every ``ARGS_KEYS`` is built from the class's ``ARGS`` specs; none is
    a hand-written frozenset that can drift from them."""
    for kind, cls in _KIND_CLASS.items():
        if "ARGS" in cls.__dict__:
            assert {v: s.keys for v, s in cls.ARGS.items()} == cls.ARGS_KEYS, kind
        else:
            assert "ARGS_KEYS" not in cls.__dict__, f"{kind}: ARGS_KEYS without ARGS"


def test_args_view_rejects_undeclared_reads() -> None:
    view = ArgsSpec({"a": int, "b": str}).view({"a": 1}, "x.get args=")
    assert view["a"] == 1
    assert view.get("b") is None
    assert "b" not in view
    assert dict(view) == {"a": 1}
    for read in (
        lambda: view["c"],
        lambda: view.get("c"),
        lambda: "c" in view,
        lambda: view.pop("c", None),
        lambda: view.setdefault("c", 1),
    ):
        with pytest.raises(UndeclaredArgsKey):
            read()


def _is_args_expr(node: ast.AST, names: set[str]) -> bool:
    """Whether ``node`` evaluates to the args dict or an ``x or {}`` fallback
    on it — ``(args or {}).get('k')`` reads the same vocabulary
    (gr531204)."""
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return any(_is_args_expr(v, names) for v in node.values)
    return False


def _args_names(fn: ast.AST, name: str) -> set[str]:
    """``name`` plus locals bound to it or to ``name or {}`` —
    ``a = args or {}`` then ``'k' in a`` is a read all the same."""
    names = {name}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and _is_args_expr(node.value, names):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_args_expr(node.value, names)
            and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)
    return names


def _literal_args_reads(fn: ast.AST, name: str = "args") -> set[str]:
    """String literals a function body reads from its ``name`` parameter."""
    names = _args_names(fn, name)
    out: set[str] = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "pop")
            and _is_args_expr(node.func.value, names)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            out.add(node.args[0].value)
        elif (
            isinstance(node, ast.Subscript)
            and _is_args_expr(node.value, names)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            out.add(node.slice.value)
        elif (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops)
            and any(_is_args_expr(c, names) for c in node.comparators)
        ):
            out.add(node.left.value)
    return out


def _fn_node(fn: Callable[..., Any]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(fn))))
    except (OSError, TypeError):
        return None
    node = tree.body[0]
    return node if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) else None


def _reads_following_helpers(
    cls: type[Handler], fn: Callable[..., Any], name: str, seen: set[object]
) -> set[str]:
    """Literal keys read from ``name`` in ``fn`` and in every ``self.m(...)``
    / module-level helper it hands the whole dict to (``op='order'`` lives in
    ``_op_order(ref, args)``, not in ``put`` itself)."""
    node = _fn_node(fn)
    if node is None or (fn, name) in seen:
        return set()
    seen.add((fn, name))
    out = _literal_args_reads(node, name)
    module = inspect.getmodule(fn)
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        callee: object = None
        offset = 0
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")
        ):
            callee = inspect.getattr_static(cls, func.attr, None)
            offset = 0 if isinstance(callee, staticmethod) else 1
            callee = getattr(callee, "__func__", callee)
        elif isinstance(func, ast.Name) and module is not None:
            callee = getattr(module, func.id, None)
        if not inspect.isfunction(callee):
            continue
        params = list(inspect.signature(callee).parameters)
        passed: list[str] = []
        names = _args_names(node, name)
        for i, a in enumerate(call.args):
            if _is_args_expr(a, names) and i + offset < len(params):
                passed.append(params[i + offset])
        passed += [
            kw.arg for kw in call.keywords if kw.arg and _is_args_expr(kw.value, names)
        ]
        for p in passed:
            out |= _reads_following_helpers(cls, callee, p, seen)
    return out


@pytest.mark.parametrize(("kind", "verb"), PASSTHROUGH)
def test_verb_body_reads_only_declared_keys(kind: str, verb: str) -> None:
    """A literal ``args.get('k')`` / ``args['k']`` / ``'k' in args`` in the
    verb body, or in a helper the verb passes the whole dict to, must be a
    declared key (the static half of ``ArgsView``)."""
    cls = _KIND_CLASS[kind]
    reads = _reads_following_helpers(cls, cls.__dict__[verb], "args", set())
    undeclared = reads - cls.ARGS_KEYS[verb]
    assert not undeclared, f"{kind}.{verb} reads undeclared args keys {undeclared}"


def test_undeclared_args_key_is_badinput_envelope() -> None:
    """gr531204: an ARGS-spec gap renders ``[error:BadInput]`` naming the
    accepted keys, not ``[error:Internal]``."""
    assert issubclass(UndeclaredArgsKey, BadInput)
    assert issubclass(UndeclaredArgsKey, KeyError)
    err = UndeclaredArgsKey("probe", options=["level", "layers"], next="retry")
    assert isinstance(err, KeyError)
    s = str(err)
    assert "probe" in s and "level" in s and "retry" in s
