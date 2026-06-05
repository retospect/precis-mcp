"""Runtime dispatcher — verb routing, error rendering, hint integration."""

from __future__ import annotations

from precis.hints import Hint, HintBus
from precis.runtime import PrecisRuntime


def test_calc_through_dispatch(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("get", {"kind": "calc", "id": "2+3*4"})
    assert "14" in out


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


def test_calc_bad_input_renders(runtime: PrecisRuntime) -> None:
    out = runtime.dispatch("get", {"kind": "calc", "id": "@@@"})
    assert "[error:BadInput]" in out
    assert "next:" in out


def test_hints_appended_to_response(runtime: PrecisRuntime) -> None:
    """Verify hints emitted during a verb call land in the rendered output."""

    # Wrap calc.get to emit a hint mid-call
    calc = runtime.hub.handler_for("calc")
    original = calc.get

    def wrapped(**kw):  # type: ignore[no-untyped-def]
        runtime.hints.emit(Hint("calc tip", topic="test.tip"))
        return original(**kw)

    calc.get = wrapped  # type: ignore[method-assign]
    try:
        out = runtime.dispatch("get", {"kind": "calc", "id": "1+1"})
    finally:
        calc.get = original  # type: ignore[method-assign]

    assert "2" in out
    assert "[tip] calc tip" in out


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


def test_build_runtime_honors_embedder_config(fresh_db: str) -> None:
    """`PRECIS_EMBEDDER` selects the active embedder; mock is the default."""
    import os

    from precis.embedder import MockEmbedder
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
        # Default: mock embedder. Real backend is opt-in via config.
        assert isinstance(paper.embedder, MockEmbedder)  # type: ignore[attr-defined]
        assert paper.embedder.dim == rt.store.embedding_dim()  # type: ignore[attr-defined,union-attr]
        rt.store.close()  # type: ignore[union-attr]
    finally:
        if original_db is None:
            os.environ.pop("PRECIS_DATABASE_URL", None)
        else:
            os.environ["PRECIS_DATABASE_URL"] = original_db
        if original_emb is None:
            os.environ.pop("PRECIS_EMBEDDER", None)
        else:
            os.environ["PRECIS_EMBEDDER"] = original_emb


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
# ``quest`` / ``think`` / ``websearch`` / ``web`` — every stream
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
