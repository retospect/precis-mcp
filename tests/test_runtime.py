"""Runtime dispatcher — verb routing, error rendering, hint integration."""

from __future__ import annotations

import pytest

from precis.hints import Hint, HintBus
from precis.runtime import PrecisRuntime
from precis.store import Store


def test_calc_through_dispatch(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("get", {"kind": "calc", "id": "2+3*4"})
    assert "14" in out


def test_pilcrow_id_routes_to_draft(runtime_with_store: PrecisRuntime) -> None:
    """A bare ``¶handle`` id infers kind='draft' (the terse chunk form
    the skill teaches), so ``get(id='¶…')`` no longer bounces as
    'missing kind=' — the #1 turn-burner in prod plan_ticks."""
    args: dict = {"id": "¶xPJ5NF"}
    runtime_with_store._maybe_split_prefixed_id(args)
    assert args["kind"] == "draft"
    assert args["id"] == "¶xPJ5NF"  # ¶ retained — DraftHandler needs it


def test_pilcrow_id_conflicting_kind_errors(
    runtime_with_store: PrecisRuntime,
) -> None:
    from precis.errors import BadInput

    try:
        runtime_with_store._maybe_split_prefixed_id({"id": "¶xPJ5NF", "kind": "paper"})
    except BadInput as e:
        assert "sigil implies kind" in str(e)
    else:
        raise AssertionError("expected BadInput on kind conflict")


def test_unknown_verb(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("frobnicate", {})
    assert "[error:BadInput]" in out
    assert "options:" in out


def test_missing_kind(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("get", {})
    assert "[error:BadInput]" in out
    assert "missing kind" in out


def test_unknown_kind(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("get", {"kind": "nope"})
    assert "[error:NotFound]" in out
    assert "next:" in out


def test_unsupported_verb_for_kind(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("put", {"kind": "calc", "mode": "replace", "text": "x"})
    assert "[error:Unsupported]" in out
    assert "calc does not support put" in out


def test_gripe_edit_body_refused_names_the_put_idiom(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr338669: an agent generalizing from todo's edit reaches for
    edit(kind='gripe', text=...) — gripe's edit is title-only (Reto
    ruling 2026-10-19), so text=/body= is refused and the next: hint
    must name the actual recovery (put-with-id append-comment)."""
    for kw in ("text", "body"):
        out = runtime_with_store.dispatch("edit", {"kind": "gripe", "id": 1, kw: "x"})
        assert "[error:BadInput]" in out
        assert "put(kind='gripe', id=N, text='...')" in out


def test_calc_bad_input_renders(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("get", {"kind": "calc", "id": "@@@"})
    assert "[error:BadInput]" in out
    assert "next:" in out


def test_cad_dsl_error_renders_grammar_teaching_not_internal_error(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A bad cad ``config`` string (``cad.dsl.DslError``) must render its
    grammar-teaching cause text through the normal error path — never
    flatten to an opaque ``internal error in put: DslError (see server
    log)``. ``DslError`` wasn't a ``PrecisError`` before this fix, so the
    dispatcher's generic ``except Exception`` swallowed the cause text
    (units-cutover prompt-surface audit, mechanism finding #3)."""
    out = runtime_with_store.dispatch(
        "put", {"kind": "cad", "id": "bad1", "text": "plate add blob:r25mm"}
    )
    assert "[error:DslError]" in out
    assert "internal error" not in out
    assert "unknown shape" in out and "blob" in out

    out = runtime_with_store.dispatch(
        "put", {"kind": "cad", "id": "bad2", "text": "plate add cyl:r25mm"}
    )
    assert "[error:DslError]" in out
    assert "internal error" not in out
    assert "cyl" in out and "missing" in out


def test_hints_appended_to_response(runtime: PrecisRuntime) -> None:
    """Verify hints emitted during a verb call land in the rendered output."""

    # Wrap calc.get to emit a hint mid-call
    calc = runtime.hub.handler_for("calc")
    assert calc is not None
    original = calc.get

    def wrapped(**kw):
        runtime.hints.emit(Hint("calc tip", topic="test.tip"))
        return original(**kw)

    calc.get = wrapped
    try:
        out = runtime.dispatch("get", {"kind": "calc", "id": "1+1"})
    finally:
        calc.get = original

    assert "2" in out
    assert "[tip] calc tip" in out


def test_kind_help_read_appends_skill_graph_hint(
    runtime_with_store: PrecisRuntime, monkeypatch
) -> None:
    """``get(kind=X)`` with no ``id=`` — the kind's own help/landing
    surface (here: ``memory``'s bare-get "list recent" fallback) —
    appends the kind's skill-graph breadcrumb (docs/backlog/skill-
    graph.md slice 4)."""
    from precis.skill_index import kind_skills

    monkeypatch.setattr(
        kind_skills,
        "kind_skill_hint",
        lambda kind, **kw: f"skills for kind={kind!r}: ...",
    )
    out = runtime_with_store.dispatch("get", {"kind": "memory"})
    assert "skills for kind='memory': ..." in out


def test_kind_help_read_omits_hint_when_kind_has_no_skills(
    runtime_with_store: PrecisRuntime, monkeypatch
) -> None:
    from precis.skill_index import kind_skills

    monkeypatch.setattr(kind_skills, "kind_skill_hint", lambda kind, **kw: None)
    out = runtime_with_store.dispatch("get", {"kind": "memory"})
    assert "skills for kind=" not in out


def test_kind_help_read_with_id_never_appends_hint(
    runtime_with_store: PrecisRuntime, monkeypatch
) -> None:
    """The breadcrumb is only for the no-``id=`` landing surface — a
    ``get(id=...)`` read for a nonexistent id still errors before ever
    reaching the footer seam, so it never gets appended either."""
    from precis.skill_index import kind_skills

    monkeypatch.setattr(
        kind_skills,
        "kind_skill_hint",
        lambda kind, **kw: f"skills for kind={kind!r}: ...",
    )
    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": "999999999"})
    assert "skills for kind=" not in out


def test_kind_help_read_degrades_silently_on_graph_failure(
    runtime_with_store: PrecisRuntime, monkeypatch
) -> None:
    """A broken skill-graph consumer must never break the kind-help
    read itself — the footer is simply absent."""
    from precis.skill_index import kind_skills

    def _boom(kind: str, **kw: object) -> str:
        raise RuntimeError("graph build blew up")

    monkeypatch.setattr(kind_skills, "kind_skill_hint", _boom)
    out = runtime_with_store.dispatch("get", {"kind": "memory"})
    assert "[error:" not in out


def test_search_never_appends_kind_skill_footer(
    runtime_with_store: PrecisRuntime, monkeypatch
) -> None:
    """The footer is a ``get()``-with-no-``id=`` affordance only — a
    ``search()`` call (which also has no ``id=`` in its args) must never
    pick up the breadcrumb. Kills the ``verb == "get" and ...`` → ``or``
    survivor: under ``or`` this fires on any no-id verb, including
    ``search``."""
    from precis.skill_index import kind_skills

    monkeypatch.setattr(
        kind_skills,
        "kind_skill_hint",
        lambda kind, **kw: f"skills for kind={kind!r}: ...",
    )
    out = runtime_with_store.dispatch("search", {"kind": "memory", "q": "anything"})
    assert "[error:" not in out
    assert "skills for kind=" not in out


def test_get_with_real_id_never_appends_kind_skill_footer(
    runtime: PrecisRuntime, monkeypatch
) -> None:
    """A ``get()`` that names a real, non-empty ``id=`` must never pick
    up the kind-skill footer — only the no-``id=`` landing surface does.
    Kills the ``verb == "get" and ...`` → ``or`` survivor: under ``or``
    this fires unconditionally for every ``get()``, id= or not."""
    from precis.skill_index import kind_skills

    monkeypatch.setattr(
        kind_skills,
        "kind_skill_hint",
        lambda kind, **kw: f"skills for kind={kind!r}: ...",
    )
    out = runtime.dispatch("get", {"kind": "calc", "id": "2+3*4"})
    assert "[error:" not in out
    assert "skills for kind=" not in out


# ── _maybe_append_kind_skill_footer at the method level (dispatch.py:1315) ──
# The dispatch-level tests above cover the "hint is None" branch only via a
# substring absence check, which a `is` → `is not` mutant on the early
# return would survive (the mutant appends "\n\nNone" to the body instead
# of the real hint text — no "skills for kind=" substring either way).
# These pin the exact body shape on both branches.


def test_footer_leaves_body_untouched_when_hint_is_none(monkeypatch) -> None:
    from precis.response import Response
    from precis.skill_index import kind_skills

    monkeypatch.setattr(kind_skills, "kind_skill_hint", lambda kind, **kw: None)
    response = Response(body="original body")
    out = PrecisRuntime._maybe_append_kind_skill_footer(
        object.__new__(PrecisRuntime), response, "memory"
    )
    # Not just "no hint text" — the body must be byte-for-byte unchanged.
    # A `is` → `is not` mutant instead appends "\n\nNone".
    assert out.body == "original body"


def test_footer_appends_hint_when_present(monkeypatch) -> None:
    from precis.response import Response
    from precis.skill_index import kind_skills

    monkeypatch.setattr(kind_skills, "kind_skill_hint", lambda kind, **kw: "SENTINEL")
    response = Response(body="original body")
    out = PrecisRuntime._maybe_append_kind_skill_footer(
        object.__new__(PrecisRuntime), response, "memory"
    )
    assert out.body == "original body\n\nSENTINEL"


# ── _is_id_empty (dispatch.py:1292-1293) ─────────────────────────────────


@pytest.mark.parametrize(
    "id_val, expected",
    [
        (None, True),
        ("", True),
        ("  ", True),
        ("/", True),
        ("abc", False),
        (5, False),
    ],
    ids=["none", "empty-str", "whitespace-str", "slash", "real-id", "non-str-int"],
)
def test_is_id_empty(id_val: object, expected: bool) -> None:

    assert PrecisRuntime._is_id_empty(id_val) is expected


def test_search_without_kind_in_stateless_runtime_errors_with_hint(
    runtime: PrecisRuntime,
) -> None:
    """In a stateless runtime (no store) with no search-supporting kinds
    available, ``search()`` without ``kind=`` falls through the cross-
    kind defaulting and surfaces a ``BadInput`` whose recovery hint
    enumerates the wildcard / comma-list forms.

    Pre-fix, the error message was a hard-coded "cross-kind search not
    yet implemented" stub from phase 1.  The cross-kind fan-out is now
    real, but stateless runtimes have no kinds to fan out to, so the
    error path stays — only the hint changed.
    """
    out = runtime.dispatch("search", {"q": "anything"})
    assert "[error:BadInput]" in out
    assert "no defensible default" in out or "no kinds available" in out
    # Hint must mention the new cross-kind affordances.
    assert "kind='*'" in out or "kind='paper,memory'" in out


def test_build_runtime_no_database() -> None:
    """Without PRECIS_DATABASE_URL set, build_runtime returns a
    stateless runtime (calc only, no store)."""
    import os

    from precis.runtime import build_runtime

    # Ensure the env var is unset for this test
    original = os.environ.pop("PRECIS_DATABASE_URL", None)
    try:
        rt = build_runtime()
        assert "calc" in rt.hub
        assert "memory" not in rt.hub
        assert rt.store is None
        assert isinstance(rt.hints, HintBus)
    finally:
        if original is not None:
            os.environ["PRECIS_DATABASE_URL"] = original


def test_live_store_narrows_the_delegated_store(
    runtime_with_store: PrecisRuntime, runtime: PrecisRuntime
) -> None:
    """gr515820: ``rt.live_store`` hands back ``Store`` (not
    ``Store | None``), so a test on ``runtime_with_store`` needs no
    second ``store`` fixture and no ``assert store is not None``. On a
    stateless runtime it raises — the same contract
    :attr:`precis.dispatch.Hub.live_store` carries."""
    assert runtime_with_store.live_store is runtime_with_store.hub.store
    with pytest.raises(RuntimeError, match="no store"):
        _ = runtime.live_store


def test_build_runtime_honors_embedder_config(fresh_db: str) -> None:
    """`PRECIS_EMBEDDER` selects the active embedder; mock is the default."""
    import os

    from precis.embedder import BoundedConcurrencyEmbedder, MockEmbedder
    from precis.runtime import build_runtime
    from precis.store import Migrator

    Migrator(fresh_db, _migrations_dir()).apply_all()

    original_db = os.environ.get("PRECIS_DATABASE_URL")
    original_emb = os.environ.get("PRECIS_EMBEDDER")
    os.environ["PRECIS_DATABASE_URL"] = fresh_db
    os.environ["PRECIS_EMBEDDER"] = "mock"
    try:
        rt = build_runtime()
        assert "paper" in rt.hub
        paper = rt.hub.handler_for("paper")
        assert paper is not None
        assert rt.store is not None
        # Default: mock embedder. Real backend is opt-in via config.
        # ``build_runtime`` is the request-path composition root, so what
        # the hub carries is the backend wrapped in the concurrency
        # bulkhead (gripe 244419) — the wrapper forwards ``dim`` / ``model``
        # and exposes the backend as ``.inner``.
        assert isinstance(paper.embedder, BoundedConcurrencyEmbedder)
        assert isinstance(paper.embedder.inner, MockEmbedder)
        assert paper.embedder.dim == rt.store.embedding_dim()
        rt.store.close()
    finally:
        if original_db is None:
            os.environ.pop("PRECIS_DATABASE_URL", None)
        else:
            os.environ["PRECIS_DATABASE_URL"] = original_db
        if original_emb is None:
            os.environ.pop("PRECIS_EMBEDDER", None)
        else:
            os.environ["PRECIS_EMBEDDER"] = original_emb


def test_build_runtime_falls_back_to_adopted_dsn_after_env_scrubbed(
    store, monkeypatch
) -> None:
    """A second ``build_runtime()`` after the first scrubbed the env must not
    come up storeless; it should reuse the DSN captured by the adopted store."""
    import os

    from precis import runtime as runtime_mod
    from precis import secrets as secrets_mod
    from precis.runtime import build_runtime

    monkeypatch.setattr(secrets_mod, "_STORE", None)
    monkeypatch.setattr(secrets_mod, "_ADOPTED_DSN", None)
    monkeypatch.setenv("PRECIS_DATABASE_URL", store.dsn)
    monkeypatch.setenv("PRECIS_EMBEDDER", "mock")

    # First build adopts the store and scrubs PRECIS_DATABASE_URL.
    rt1 = build_runtime()
    assert rt1.store is not None
    assert rt1.store.dsn == store.dsn
    assert "PRECIS_DATABASE_URL" not in os.environ

    # Second build: env is gone, but the adopted DSN should be recovered.
    rt2 = build_runtime()
    assert rt2.store is not None
    assert rt2.store.dsn == store.dsn

    rt1.store.close()
    rt2.store.close()

    # The fallback path should also be reachable via a fresh process state:
    # when build_runtime is called with an explicit config and no database_url,
    # it still consults the adopted DSN.
    monkeypatch.setattr(secrets_mod, "_ADOPTED_DSN", store.dsn)
    monkeypatch.delenv("PRECIS_DATABASE_URL", raising=False)
    rt3 = build_runtime(runtime_mod.PrecisConfig())
    assert rt3.store is not None
    assert rt3.store.dsn == store.dsn
    rt3.store.close()


# ── boot-time store connect: ride out a transient outage, then crash ──
#
# Regression for the 2026-07-14 asa incident: a `precis serve` spawned
# during a node reboot (DB briefly unreachable) must NOT come up
# storeless — it should retry within a bounded budget and, if the DB is
# still down, raise so the parent respawns rather than serving a
# memory/gripe-less surface. See PrecisConfig.db_connect_retry_seconds.


def test_connect_store_retries_then_succeeds(monkeypatch) -> None:
    import precis.store as store_mod
    from precis.runtime import _connect_store_or_raise

    monkeypatch.setattr("time.sleep", lambda _s: None)
    calls = {"n": 0}
    sentinel = object()

    def flaky_connect(dsn):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("Network is unreachable")
        return sentinel

    monkeypatch.setattr(store_mod.Store, "connect", staticmethod(flaky_connect))
    got = _connect_store_or_raise("postgresql://x", retry_seconds=30.0)
    assert got is sentinel
    assert calls["n"] == 3


def test_connect_store_raises_after_budget(monkeypatch) -> None:
    import precis.store as store_mod
    from precis.runtime import _connect_store_or_raise

    monkeypatch.setattr("time.sleep", lambda _s: None)
    # monotonic jumps past the deadline after the first failure so the
    # budget is exhausted immediately (no real waiting in the test).
    ticks = iter([0.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))

    def always_fail(dsn):
        raise ConnectionError("Network is unreachable")

    monkeypatch.setattr(store_mod.Store, "connect", staticmethod(always_fail))
    import pytest

    with pytest.raises(ConnectionError):
        _connect_store_or_raise("postgresql://x", retry_seconds=30.0)


def test_connect_store_zero_budget_fails_fast(monkeypatch) -> None:
    import precis.store as store_mod
    from precis.runtime import _connect_store_or_raise

    slept = {"n": 0}
    monkeypatch.setattr("time.sleep", lambda _s: slept.__setitem__("n", slept["n"] + 1))
    calls = {"n": 0}

    def always_fail(dsn):
        calls["n"] += 1
        raise ConnectionError("Network is unreachable")

    monkeypatch.setattr(store_mod.Store, "connect", staticmethod(always_fail))
    import pytest

    with pytest.raises(ConnectionError):
        _connect_store_or_raise("postgresql://x", retry_seconds=0.0)
    assert calls["n"] == 1  # single attempt, no retry
    assert slept["n"] == 0  # never slept


def _migrations_dir():
    from pathlib import Path

    return Path(__file__).parent.parent / "src" / "precis" / "migrations"


# ── cross-kind tag filter — backstop against silent ``**_kw`` drops ──
#
# The cross-kind dispatcher fans out ``search_hits(q=..., tags=...)``
# to every search-hits-capable handler. Handler signatures are
# uneven: numeric-ref kinds push ``tags=`` into SQL via
# ``Tag.normalize_filter``, but most slug-ref and block-level
# handlers take ``**_kw`` and silently drop unknown kwargs. That
# means ``tags=['workspace']`` in a cross-kind search was effectively
# a no-op for ``markdown`` / ``tex`` / ``plaintext`` / ``oracle`` /
# ``think`` / ``websearch`` / ``web`` — every stream
# returned unfiltered hits, the merger RRF'd them together, and the
# agent got the scope-to-workspace promise broken.
#
# The runtime post-filters after each stream: it fetches
# ``tags_for(ref_id)`` per hit and keeps only hits whose ref tags
# are a superset of the required filter. These tests pin that
# behaviour.


def test_cross_kind_search_honours_tag_filter(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Cross-kind ``search(tags=['topic-rhubarb'])`` MUST drop hits from
    refs that don't carry the tag, even when the owning handler's
    ``search_hits`` silently ignores ``tags=``.

    Reproduction: create two memory refs — one tagged
    ``topic-rhubarb``, one plain. A cross-kind search for a word
    both share, with that tag filter, should see only the tagged
    one.
    """
    # Create two memories sharing the word ``rhubarb-specific``.
    out1 = runtime_with_store.dispatch(
        "put",
        {"kind": "memory", "text": "first rhubarb-specific note"},
    )
    # The second memory carries the ``topic-rhubarb`` open tag.
    out2 = runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "second rhubarb-specific note",
            "tags": ["topic-rhubarb"],
        },
    )
    assert "[error:" not in out1
    assert "[error:" not in out2

    # Cross-kind search with the tag filter.
    filtered = runtime_with_store.dispatch(
        "search",
        {"kind": "*", "q": "rhubarb-specific", "tags": ["topic-rhubarb"]},
    )
    assert "[error:" not in filtered
    # Exactly one surviving hit — the tagged memory.
    assert "second rhubarb-specific note" in filtered
    assert "first rhubarb-specific note" not in filtered


def test_cross_kind_search_without_tags_still_returns_unfiltered(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Baseline: with no ``tags=`` in the query, the cross-kind
    fan-out returns every match (no accidental post-filter when
    the agent didn't ask for one)."""
    runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "aardvark-specific first"}
    )
    runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "aardvark-specific second",
            "tags": ["topic-aardvark"],
        },
    )
    out = runtime_with_store.dispatch("search", {"kind": "*", "q": "aardvark-specific"})
    assert "[error:" not in out
    # Both refs present — filter is off.
    assert "aardvark-specific first" in out
    assert "aardvark-specific second" in out


def test_cross_kind_search_with_unmatched_tag_returns_no_hits(
    runtime_with_store: PrecisRuntime,
) -> None:
    """When no ref in any kind carries the required tag, the
    cross-kind result must be empty — not a fallback to
    unfiltered results."""
    runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "kumquat-specific note"}
    )
    out = runtime_with_store.dispatch(
        "search",
        {"kind": "*", "q": "kumquat-specific", "tags": ["topic-nonexistent"]},
    )
    assert "[error:" not in out
    assert "kumquat-specific note" not in out
    # Cross-kind empty surfaces the searched-kinds list.
    assert "no matches" in out.lower()


def test_cross_kind_tag_filter_matches_flag_namespace(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The filter comparison is on the tag's canonical string form
    (``__str__``). A bare ``'workspace'`` in the filter matches
    the flag-namespace entry on the ref (the ``workspace`` flag
    seeded in ``0001_initial.sql``) regardless of whether the caller
    thought of it as a flag or an open tag. Pins the namespace-
    agnostic comparison in ``_filter_hits_by_tags``.
    """
    # Give a memory ref the workspace flag directly via the store —
    # memory handlers don't auto-apply it (only prose-file handlers
    # do), but the flag is registered in ``flag_names`` so we can
    # attach it for this test.
    out = runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "pineapple-specific note"}
    )
    assert "[error:" not in out
    # Extract ref id from the response so we can stamp the flag.
    import re

    match = re.search(r"id=(\d+)", out)
    assert match, f"could not parse ref id out of {out!r}"
    ref_id = int(match.group(1))
    from precis.store.types import Tag as _Tag

    assert runtime_with_store.store is not None
    runtime_with_store.store.add_tag(ref_id, _Tag.flag("workspace"), set_by="system")

    filtered = runtime_with_store.dispatch(
        "search",
        {"kind": "*", "q": "pineapple-specific", "tags": ["workspace"]},
    )
    assert "[error:" not in filtered
    assert "pineapple-specific note" in filtered


def test_cross_kind_tag_filter_requires_all_listed_tags(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Multiple tags in the filter list are AND-combined — a ref
    must carry every listed tag to survive. Single-tag matches
    don't leak through."""
    runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "guava-specific with-one-tag",
            "tags": ["topic-guava"],
        },
    )
    runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "guava-specific with-both",
            "tags": ["topic-guava", "topic-fruit"],
        },
    )
    out = runtime_with_store.dispatch(
        "search",
        {
            "kind": "*",
            "q": "guava-specific",
            "tags": ["topic-guava", "topic-fruit"],
        },
    )
    assert "[error:" not in out
    assert "with-both" in out
    assert "with-one-tag" not in out


# ── cross-kind TOON table — Change A links column, no kind column ────


def test_cross_kind_toon_table_surfaces_links_column(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The default cross-kind ``search(kind='*', ...)`` shape is TOON —
    it must carry a ``links`` column (batched via
    ``Store.link_rel_summary_for_refs``) and drop the ``kind`` column
    entirely (the id cell is kind-qualified instead)."""
    out1 = runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "kumquat-specific cited note"}
    )
    runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "kumquat-specific citing note"}
    )
    assert "[error:" not in out1
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'memory' ORDER BY ref_id DESC LIMIT 2"
        ).fetchall()
    citer_id, cited_id = int(rows[0][0]), int(rows[1][0])
    store.add_link(src_ref_id=citer_id, dst_ref_id=cited_id, relation="cites")

    out = runtime_with_store.dispatch("search", {"kind": "*", "q": "kumquat-specific"})
    assert "[error:" not in out
    assert "id\tsummary\tremaining_words\tlinks" in out
    assert "kind\t" not in out
    assert "1 · cited" in out


# ── kind= accepts a handle code ──────────────────────


def test_expand_kind_code_record(runtime: PrecisRuntime) -> None:
    """A 2-char record code resolves to its kind name."""
    assert runtime._expand_kind_code("dr") == "draft"
    assert runtime._expand_kind_code("me") == "memory"
    assert runtime._expand_kind_code("pa") == "paper"


def test_expand_kind_code_known_kind_wins(runtime: PrecisRuntime) -> None:
    """A literal that's already a registered kind is never shadowed."""
    assert runtime._expand_kind_code("calc") == "calc"
    assert runtime._expand_kind_code("draft") == "draft"


def test_expand_kind_code_chunk_codes_untouched(runtime: PrecisRuntime) -> None:
    """Chunk codes are address-only — left for the normal unknown-kind path."""
    assert runtime._expand_kind_code("dc") == "dc"
    assert runtime._expand_kind_code("pc") == "pc"


def test_expand_kind_code_unknown_passthrough(runtime: PrecisRuntime) -> None:
    assert runtime._expand_kind_code("zz") == "zz"
    assert runtime._expand_kind_code("draftish") == "draftish"


def test_dispatch_kind_code_equivalent_to_name(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``kind='dr'`` dispatches exactly like ``kind='draft'``."""
    by_code = runtime_with_store.dispatch("get", {"kind": "dr"})
    by_name = runtime_with_store.dispatch("get", {"kind": "draft"})
    assert by_code == by_name
    assert "unknown kind" not in by_code.lower()


def test_dispatch_chunk_code_rejected_as_kind(runtime: PrecisRuntime) -> None:
    """``kind='dc'`` is not a put/get kind — it stays unresolved and errors."""
    out = runtime.dispatch("get", {"kind": "dc"})
    assert "[error:NotFound]" in out


# ── retired kinds name their successor (dispatch._RETIRED_KINDS) ──────


def test_retired_kind_error_leads_with_where_the_work_moved(
    runtime: PrecisRuntime,
) -> None:
    """``nm`` merged into ``se`` as its atomic mode
    (docs/backlog/nm-se-merge.md's acceptance criterion: "``get(kind='nm',
    ...)`` fails with a kind-shaped hint pointing at se atomic mode"). The
    bare "unknown kind — pick from the options" is true but sends the
    caller re-discovering a capability that still exists."""
    out = runtime.dispatch("get", {"kind": "nm"})
    assert "[error:NotFound]" in out
    assert "unknown kind: nm" in out
    assert "merged into 'se'" in out
    assert "mode='atomic'" in out
    # the generic breadcrumb still trails it — the full kind table stays
    # one call away.
    assert "precis-help" in out


def test_an_ordinary_unknown_kind_gets_no_retired_pointer(
    runtime: PrecisRuntime,
) -> None:
    """The map is hand-curated, not a fallback: a typo must not be told
    it was retired into something."""
    out = runtime.dispatch("get", {"kind": "nanomachine"})
    assert "[error:NotFound]" in out
    assert "merged into" not in out


# ── kind='ref' names the typed shape (gr511442) ─────────────────────


def test_ref_kind_refuses_naming_the_typed_form(runtime: PrecisRuntime) -> None:
    """gr511442: ``kind='ref'`` is the generic-noun near-miss — agents use
    it meaning "any ref". The refusal names the typed call shape, not just
    "pick from the options"."""
    out = runtime.dispatch("get", {"kind": "ref", "id": 460128})
    assert "[error:NotFound]" in out
    assert "unknown kind: ref" in out
    assert "get(kind='paper', id='pa123')" in out
    # the generic breadcrumb still trails it
    assert "precis-help" in out


def test_ref_kind_names_the_real_kind_when_the_id_resolves(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A numeric id that resolves to a live ref gets the exact retry."""
    ref = store.insert_ref(kind="todo", slug=None, title="a task")
    out = runtime_with_store.dispatch("get", {"kind": "ref", "id": ref.id})
    assert "[error:NotFound]" in out
    assert f"get(kind='todo', id={ref.id})" in out
    assert f"get(id='td{ref.id}')" in out


def test_ref_kind_with_a_handle_id_hints_the_kind_free_call(
    runtime: PrecisRuntime,
) -> None:
    """A well-formed handle in ``id=`` already carries its kind — the hint
    is the kind-free call."""
    out = runtime.dispatch("get", {"kind": "ref", "id": "td504852"})
    assert "[error:NotFound]" in out
    assert "get(id='td504852')" in out


# ── complete-result budgets ───────────────────────────────────────────


def test_in_process_dispatch_skips_context_budget(
    runtime: PrecisRuntime, monkeypatch
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "1500")
    monkeypatch.setattr(runtime, "_render", lambda response: "x" * 5000)
    out, is_error = runtime.dispatch_with_status("get", {"kind": "calc", "id": "2+3"})
    assert is_error is False
    assert out == "x" * 5000


def test_agent_context_oversize_refuses_without_partial_body(
    runtime: PrecisRuntime, monkeypatch
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "1500")
    monkeypatch.setattr(runtime, "_render", lambda response: "x" * 5000)
    out, is_error = runtime.dispatch_with_status(
        "get", {"kind": "calc", "id": "2+3"}, fold=True
    )
    assert is_error is True
    assert "no partial result was returned" in out
    assert "search(kind='calc'" in out
    assert "xxxx" not in out
    assert "more(cursor=" not in out


def test_frame_cap_refuses_even_without_context_budget(
    runtime: PrecisRuntime, monkeypatch
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "200")
    monkeypatch.setattr(runtime, "_render", lambda response: "x" * 5000)
    out, is_error = runtime.dispatch_with_status("get", {"kind": "calc", "id": "2+3"})
    assert is_error is True
    assert "PRECIS_MAX_BODY_BYTES" in out
    assert "xxxx" not in out


def test_transient_live_response_also_refuses_oversize(runtime, monkeypatch):
    from precis.response import Response

    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "1000")
    monkeypatch.setattr(
        runtime,
        "_dispatch_inner",
        lambda *a: Response(body="live supplier fact\n" * 500, transient=True),
    )
    out, is_error = runtime.dispatch_with_status(
        "get", {"kind": "component", "id": "dogfood"}
    )
    assert is_error
    assert "no partial result was returned" in out
    assert "live supplier fact" not in out
