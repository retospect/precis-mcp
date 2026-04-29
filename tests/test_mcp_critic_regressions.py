"""Regression tests for the MCP critic findings (April 2026).

Each test pins one fix: if any of these starts failing, the
corresponding agent-facing behaviour has regressed. Test names
match the critic's finding labels so failures point straight at the
matching changelog entry.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.handlers.paper import _normalise_view, _strip_jats
from precis.hints import HintBus
from precis.protocol import Handler
from precis.registry import Registry, builtins
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis.utils.slug import _first_author, mint_slug

# ── CRITICAL: search no longer surfaces a misleading score=0.016X ────


def test_search_render_omits_misleading_score(store: Store) -> None:
    """The MCP critic showed search exposing query-independent
    ``score=0.0164/0.0161/0.0159`` values that were just ``1/(k+rank)``.
    The render now uses position only — no numeric should leak."""
    from precis.handlers.paper import PaperHandler

    handler = PaperHandler(store=store)
    # Direct grep against the source — the format string is the
    # behaviour. Don't need a real corpus to assert it.
    import inspect

    src = inspect.getsource(handler.search)
    assert "score=" not in src, (
        "score= must not appear in the search render — RRF scores are "
        "rank-based and query-independent, so showing them mislead agents"
    )


# ── CRITICAL: move verb has zero implementing kinds ─────────────────


def test_move_no_implementer_friendly_error(runtime: PrecisRuntime) -> None:
    """When no kind in the active registry supports ``move``, the
    runtime says so explicitly rather than letting the error look
    like a per-kind quirk."""
    out = runtime.dispatch("move", {"kind": "calc", "id": "x", "after": "y"})
    assert "[error:Unsupported]" in out
    assert "no active kind currently supports move" in out
    assert "use put" in out


# ── MAJOR: cost trailer no longer double-prefixes "cost:" ───────────


def test_cost_trailer_not_double_prefixed(runtime_stateless: PrecisRuntime) -> None:
    """The runtime renderer used to prepend ``— cost: `` to the
    handler's already-formatted ``[cost: ...]`` string, producing
    ``— cost: [cost: ~$0.0020]``. Verify only one ``cost:`` appears."""

    # We use a fake handler to avoid pulling in a paid kind.
    from precis.protocol import KindSpec
    from precis.response import Response

    class _FakePaid(Handler):
        spec = KindSpec(
            kind="fakepaid",
            title="Fake Paid",
            description="test handler",
            supports_get=True,
        )

        def get(self, **_kw):  # type: ignore[no-untyped-def]
            return Response(body="hello", cost="[cost: ~$0.0020]")

    runtime_stateless.registry._by_kind["fakepaid"] = _FakePaid()  # type: ignore[attr-defined]
    try:
        out = runtime_stateless.dispatch("get", {"kind": "fakepaid", "id": "x"})
    finally:
        runtime_stateless.registry._by_kind.pop("fakepaid", None)  # type: ignore[attr-defined]

    # Exactly one occurrence of "cost:" — the one inside the brackets.
    assert out.count("cost:") == 1, f"expected single cost: occurrence, got: {out!r}"
    assert "[cost: ~$0.0020]" in out
    assert "— cost: [cost:" not in out


# ── MAJOR: cross-kind search hint enumerates real kinds ─────────────


def test_cross_kind_search_hint_enumerates_search_kinds(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Pre-fix the hint hard-coded ``add kind=<one of: calc>`` even
    though calc doesn't support search. Now the options list reflects
    every kind whose spec advertises ``supports_search=True``.

    Triggered against a fresh empty store so the runtime's
    ``most_recent_kind`` default returns ``None`` and we fall
    through to the BadInput. The default-kind path itself is
    exercised in :func:`test_search_defaults_to_most_recent_kind`.
    """
    out = runtime_with_store.dispatch("search", {"q": "anything"})
    assert "[error:BadInput]" in out
    assert "cross-kind search not yet implemented" in out
    # At least one search-supporting kind must surface in the hint.
    search_kinds = [
        k
        for k in runtime_with_store.registry.kinds()
        if runtime_with_store.registry.get(k).spec.supports_search
    ]
    assert search_kinds, "expected at least one search-supporting kind in the registry"
    for k in search_kinds:
        assert k in out, f"expected {k!r} in the hint options"
    # Comma-list form must not be advertised — it's not implemented.
    assert "paper,memory" not in out or "not supported" in out


def test_search_defaults_to_most_recent_kind(
    runtime_with_store: PrecisRuntime,
) -> None:
    """When the caller omits ``kind=`` and the store has at least
    one live ref in a search-supporting kind, the runtime defaults
    to the most recently touched kind and echoes the choice back
    in a ``(searched kind=...)`` annotation. This is the user-
    requested affordance for 7B callers that forget the kwarg.
    """
    # Create a memory ref so 'memory' becomes the most recently
    # touched search-supporting kind.
    runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "default-kind probe"}
    )
    out = runtime_with_store.dispatch("search", {"q": "default-kind probe"})
    # The annotation must surface — the agent has to know which
    # kind we picked so it can override on retry.
    assert "(searched kind='memory')" in out
    # It actually ran the search (i.e. the response isn't a
    # BadInput about missing kind=).
    assert "[error:BadInput]" not in out


def test_comma_list_kind_rejected_with_clear_hint(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch("search", {"kind": "paper,memory", "q": "test"})
    assert "[error:BadInput]" in out
    assert "comma-list kind not supported" in out


# ── MAJOR: paper view aliases (cite/bib ⇄ bibtex) symmetric ─────────


def test_view_kwarg_aliases_to_bibtex() -> None:
    """The kwarg ``view='cite/bib'`` resolves to the same canonical
    view as the path form ``id='slug/cite/bib'``."""
    assert _normalise_view("cite/bib") == "bibtex"
    assert _normalise_view("cite/bibtex") == "bibtex"
    assert _normalise_view("cite/ris") == "ris"
    assert _normalise_view("cite/endnote") == "endnote"
    # Bare names pass through.
    assert _normalise_view("bibtex") == "bibtex"
    assert _normalise_view("abstract") == "abstract"
    assert _normalise_view("toc") == "toc"
    # Unknown views pass through verbatim — the renderer surfaces
    # the supported-options Unsupported error.
    assert _normalise_view("garbage") == "garbage"
    # None stays None.
    assert _normalise_view(None) is None


# ── MAJOR: <jats:*> tags stripped from abstract bodies ──────────────


def test_strip_jats_removes_namespaced_tags() -> None:
    """Drops the redundant ``<jats:title>Abstract</jats:title>`` block
    entirely — the surrounding context (view name, header) already
    names the section, and keeping it caused the heading word to fuse
    with the body's first word ("AbstractMetal-organic"). MCP critic
    MINOR m1.
    """
    raw = (
        "<jats:title>Abstract</jats:title>"
        "<jats:p>Metal-organic frameworks (MOFs)…</jats:p>"
    )
    out = _strip_jats(raw)
    assert "<jats:" not in out
    assert "</jats:" not in out
    assert "Metal-organic frameworks" in out
    # The "Abstract" heading word is intentionally dropped — its
    # absence is the fix for the heading-mash bug.
    assert not out.lower().startswith("abstract")


def test_strip_jats_no_heading_mash() -> None:
    """Closing tag flanked by content gets a space separator so
    adjacent paragraphs don't fuse word-to-word."""
    raw = "<jats:p>First.</jats:p><jats:p>Second.</jats:p>"
    out = _strip_jats(raw)
    # Without the closing-tag-→-space substitution this would render
    # as "First.Second." — a trap for any agent that tokenises on
    # whitespace.
    assert "First. Second" in out or "First.\nSecond" in out


def test_strip_jats_idempotent() -> None:
    """Running the strip twice produces the same result — no double
    cleanup that would, say, eat plain text starting with '<'."""
    raw = "<jats:p>Hi.</jats:p> Plain prose. <jats:title>X</jats:title>"
    once = _strip_jats(raw)
    twice = _strip_jats(once)
    assert once == twice


def test_strip_jats_leaves_non_jats_html_alone() -> None:
    """Generic HTML that isn't JATS-prefixed is preserved (the
    renderer doesn't claim full HTML escaping)."""
    raw = "Plain <em>emphasised</em> text — no jats here."
    out = _strip_jats(raw)
    assert "<em>" in out
    assert "emphasised" in out


# ── MAJOR: slug minter strips glued-initials prefix ─────────────────


def test_slug_minter_strips_glued_initials() -> None:
    """``A.Clark`` produced ``aclark`` pre-fix because the regex
    silently dropped the dot. Now the minter recognises a leading run
    of single-letter dotted segments and drops them."""
    assert _first_author(["A.Clark"]) == "clark"
    assert _first_author(["A.B.Clark"]) == "clark"
    # Already-clean inputs stay untouched.
    assert _first_author(["Clark"]) == "clark"
    assert _first_author(["A. Clark"]) == "clark"
    assert _first_author(["Clark, A."]) == "clark"
    # Multi-letter prefixes (real surnames with dots) are preserved.
    assert _first_author(["St.Pierre"]) == "stpierre"


def test_mint_slug_round_trip_for_clark1998() -> None:
    """End-to-end: ``A.Clark`` + 1998 + 'extended' → ``clark1998extended``,
    not the buggy ``aclark1998extended``."""
    slug = mint_slug(
        authors=["A.Clark", "D.Chalmers"], year=1998, title="The Extended Mind"
    )
    assert slug == "clark1998extended"


# ── MAJOR: tag validation rejects bogus closed values + bare flags ──


def test_tag_strict_rejects_unknown_status() -> None:
    with pytest.raises(BadInput, match="invalid STATUS value"):
        Tag.parse_strict("STATUS:bogus")


def test_tag_strict_rejects_bare_flag_collision() -> None:
    with pytest.raises(BadInput, match="bare flag 'urgent'"):
        Tag.parse_strict("urgent")
    with pytest.raises(BadInput, match="bare flag 'done'"):
        Tag.parse_strict("done")


def test_tag_strict_accepts_canonical_status_values() -> None:
    for v in ("open", "doing", "blocked", "done", "won't-do"):
        tag = Tag.parse_strict(f"STATUS:{v}")
        assert tag.namespace == "closed"
        assert tag.value == v


def test_tag_strict_accepts_lowercase_and_open_tags() -> None:
    """Lowercase prefixes and non-colliding bare flags pass through."""
    Tag.parse_strict("topic:co2-capture")
    Tag.parse_strict("project:precis-v2")
    Tag.parse_strict("wip")
    Tag.parse_strict("star")
    # Sanity: the canonical bare flags coined by the docs.
    Tag.parse_strict("draft")


# ── MAJOR: the documented STATUS values match the runtime ───────────


def test_status_vocabulary_matches_todo_handler() -> None:
    """The STATUS values listed in the closed vocabulary are the same
    set the TodoHandler defaults and renders. If you add a new status,
    update both ``_CLOSED_VOCAB['STATUS']`` and the docstring + skill."""
    from precis.handlers.todo import TodoHandler
    from precis.store.types import _CLOSED_VOCAB

    expected = {"open", "doing", "blocked", "done", "won't-do"}
    assert _CLOSED_VOCAB["STATUS"] == expected
    # Default-on-create value lives in the closed vocabulary.
    for default in TodoHandler.default_tags_on_create:
        if default.startswith("STATUS:"):
            assert default[len("STATUS:") :] in expected


# ── MINOR: precis-overview no longer cites dead kinds ───────────────


def test_precis_overview_skill_no_dead_kinds() -> None:
    """The MCP critic flagged ``clock``, ``rng``, ``plot``, and ``ask``
    as documented kinds that don't exist in the runtime. Pin them out
    of the canonical entry-point doc.

    ``book`` is intentionally still mentioned — but only as a
    *reserved* kind (not yet wired). That's information, not bait,
    so it's allowed.
    """
    from importlib import resources

    text = (
        resources.files("precis.data.skills")
        .joinpath("precis-overview.md")
        .read_text("utf-8")
    )
    for dead in ("`clock`", "`rng`", "`plot`", "`ask`"):
        assert dead not in text, f"precis-overview still references dead kind {dead!r}"


def test_precis_overview_skill_has_no_wang2020state_example() -> None:
    """``wang2020state`` is a test fixture, not a real corpus slug;
    the docs were fooling first-time agents into NotFound errors."""
    from importlib import resources

    text = (
        resources.files("precis.data.skills")
        .joinpath("precis-overview.md")
        .read_text("utf-8")
    )
    assert "wang2020state" not in text


# ── MAJOR: put with bad tags must not leave a ghost ref behind ─────


class TestTransactionalPut:
    """The MCP critic flagged a state-drift bug: ``put(kind='memory',
    text='probe', tags=['urgent'])`` returned a BadInput about the
    bare-flag collision *and* still committed the ``memory`` row.
    Two probes leaked two ghost rows; the fix pre-validates every
    tag and runs the insert + tag writes inside one transaction.
    """

    def test_create_with_invalid_tag_writes_nothing(self, store: Store) -> None:
        from precis.handlers.memory import MemoryHandler

        h = MemoryHandler(store=store)
        before = len(store.list_refs(kind="memory", limit=200))
        with pytest.raises(BadInput):
            h.put(text="probe", tags=["urgent"])  # collides with PRIO:urgent
        after = len(store.list_refs(kind="memory", limit=200))
        assert after == before, "rejected create still wrote a ref row"

    def test_create_with_unknown_axis_writes_nothing(self, store: Store) -> None:
        from precis.handlers.memory import MemoryHandler

        h = MemoryHandler(store=store)
        before = len(store.list_refs(kind="memory", limit=200))
        with pytest.raises(BadInput):
            h.put(text="tx-test", tags=["DENSITY:bogus"])  # unknown closed axis
        after = len(store.list_refs(kind="memory", limit=200))
        assert after == before

    def test_update_with_invalid_tag_does_not_change_text(self, store: Store) -> None:
        from precis.handlers.memory import MemoryHandler

        h = MemoryHandler(store=store)
        out = h.put(text="original")
        ref_id = int(out.body.rsplit("=", 1)[1])
        with pytest.raises(BadInput):
            h.put(id=ref_id, text="should-not-stick", tags=["urgent"])
        # Text must be the original — the tag rejection should have
        # rolled back the title update too.
        got = h.get(id=ref_id)
        assert "original" in got.body
        assert "should-not-stick" not in got.body


# ── MAJOR: gibberish search returns no hits, not random top-K ──────


class TestSemanticRelevanceFloor:
    """``search_blocks_fused(max_distance=...)`` enforces a cosine-
    distance floor on the semantic CTE. The MCP critic flagged
    that a query like ``'xyzzy frobnicate quux'`` returned ranked
    semantic-only hits because pgvector ``<=>`` always finds
    *something*. With the floor in place, gibberish returns empty
    instead of arbitrary blocks. (Critic MAJOR #3.)
    """

    def test_gibberish_query_drops_distant_blocks(self, store: Store) -> None:
        from precis.embedder import MockEmbedder
        from precis.handlers.paper import PaperHandler
        from precis.store import BlockInsert

        e = MockEmbedder(dim=1024)
        cid = store.ensure_corpus("default")
        ref = store.insert_ref(
            corpus_id=cid, kind="paper", slug="p", title="P"
        )
        # Three blocks of meaningful text — none lexically or
        # semantically close to the gibberish query.
        store.insert_blocks(
            ref.id,
            [
                BlockInsert(
                    pos=0, text="alpha beta gamma", embedding=e.embed_one("alpha beta gamma")
                ),
                BlockInsert(
                    pos=1,
                    text="delta epsilon zeta",
                    embedding=e.embed_one("delta epsilon zeta"),
                ),
                BlockInsert(
                    pos=2, text="eta theta iota", embedding=e.embed_one("eta theta iota")
                ),
            ],
        )
        h = PaperHandler(store=store, embedder=e)
        out = h.search(q="xyzzy frobnicate quux")
        # The handler returns the empty-results envelope, not a
        # ranked top-K of irrelevant blocks.
        assert "no paper blocks match" in out.body


# ── Plumbing fixture for cross-kind search test ─────────────────────


@pytest.fixture
def runtime_with_store(store: Store) -> PrecisRuntime:
    handlers: list[Handler] = builtins(store=store)
    return PrecisRuntime(
        config=_default_config(),
        registry=Registry(handlers),
        hints=HintBus(),
        store=store,
    )


def _default_config():
    from precis.config import PrecisConfig

    return PrecisConfig(database_url=None, embedder="mock")
