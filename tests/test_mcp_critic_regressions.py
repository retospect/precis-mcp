"""Regression tests for the MCP critic findings (April 2026).

Each test pins one fix: if any of these starts failing, the
corresponding agent-facing behaviour has regressed. Test names
match the critic's finding labels so failures point straight at the
matching changelog entry.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from precis.dispatch import Hub, boot
from precis.errors import BadInput, Gone, NotFound
from precis.handlers.paper import _normalise_view, _strip_jats
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis.utils.slug import _first_author, mint_slug

# ── shape consistency: paper search renders score like every block kind ────


def test_paper_search_renders_score_like_block_kinds(store: Store) -> None:
    """The MCP critic 2026-05-02 flagged search-render shape drift:
    web/think/markdown/plaintext/conversation/python all annotate
    block hits with ``(score=X.XXXX)`` while paper alone omitted it,
    forcing agents into kind-specific parsing. The earlier rationale
    (RRF scores are query-independent) applies symmetrically to
    every block-level kind, so paper now aligns with the other
    six rather than standing alone — at the cost of one
    potentially-misleading float per hit, balanced by uniform
    downstream parsing.
    """
    from precis.handlers.paper import PaperHandler

    handler = PaperHandler(hub=Hub(store=store))
    import inspect

    src = inspect.getsource(handler.search)
    assert "score=" in src, (
        "paper search must annotate hits with (score=X.XXXX) like every "
        "other block-level handler — drift causes kind-specific parsing"
    )


# ── CRITICAL: unknown verb is rejected at the dispatcher boundary ────


def test_unknown_verb_rejected(runtime: PrecisRuntime) -> None:
    """The seven-verb migration removed the legacy ``move`` verb.
    The dispatcher must reject unknown verbs at the boundary with a
    BadInput naming the supported verbs — agents otherwise bounce
    against every kind hoping for one that works.
    """
    out = runtime.dispatch("move", {"kind": "calc", "id": "x", "after": "y"})
    assert "[error:BadInput]" in out
    assert "unknown verb: move" in out
    # Options should enumerate the live verb surface.
    for verb in ("get", "search", "put", "edit", "delete", "tag", "link"):
        assert verb in out


def test_unsupported_verb_lists_supported_verbs(runtime: PrecisRuntime) -> None:
    """When a kind doesn't support the requested verb, the dispatcher
    enumerates the verbs it *does* support so the LLM has a sharp
    recovery vocabulary to copy from.

    ``options:`` carries the comma-separated supported-verb list
    (the recovery vocabulary). ``next:`` carries one concrete call
    shape for a verb the kind supports — picking ``get`` when
    available, since every kind ships ``get``.
    """
    out = runtime.dispatch("link", {"kind": "calc", "id": "foo", "target": "x:y"})
    assert "[error:Unsupported]" in out
    assert "calc does not support link" in out
    # Options enumerate calc's supported verbs (only ``get``).
    assert "options: get" in out
    # ``next`` is a *callable* recovery shape — copy-paste-runnable.
    # No '...' placeholders that would prevent immediate retry.
    assert "next: try get(kind='calc')" in out
    assert "..." not in out


# ── MAJOR: cost trailer no longer double-prefixes "cost:" ───────────


def test_cost_trailer_not_double_prefixed(runtime_stateless: PrecisRuntime) -> None:
    """The runtime renderer used to prepend ``— cost: `` to the
    handler's already-formatted ``[cost: ...]`` string, producing
    ``— cost: [cost: ~$0.0020]``. Verify only one ``cost:`` appears."""

    # We use a fake handler to avoid pulling in a paid kind.
    from precis.protocol import Handler, KindSpec
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

    fake = _FakePaid()
    fake._register_with(runtime_stateless.registry)
    try:
        out = runtime_stateless.dispatch("get", {"kind": "fakepaid", "id": "x"})
    finally:
        reg = runtime_stateless.registry
        reg.abilities.pop(("fakepaid", "get", None), None)
        reg.handlers.pop("fakepaid", None)
        reg.overview.pop("fakepaid", None)

    # Exactly one occurrence of "cost:" — the one inside the brackets.
    assert out.count("cost:") == 1, f"expected single cost: occurrence, got: {out!r}"
    assert "[cost: ~$0.0020]" in out
    assert "— cost: [cost:" not in out


# ── MAJOR: cross-kind search hint enumerates real kinds ─────────────


def test_cross_kind_search_default_fans_out_when_store_is_empty(
    runtime_with_store: PrecisRuntime,
) -> None:
    """When the caller omits ``kind=`` against an empty store,
    ``most_recent_kind`` returns None and the runtime now falls
    through to a cross-kind ``kind='*'`` fan-out instead of the
    previous ``BadInput`` about "cross-kind search not yet
    implemented". The empty-result body must enumerate the kinds
    that were searched so the agent can narrow on retry.
    """
    out = runtime_with_store.dispatch("search", {"q": "anything"})
    # No error — the wildcard merge handles it.
    assert "[error:BadInput]" not in out
    # The "no matches across X, Y, Z" body must enumerate every
    # active kind that opted into cross-kind merge.
    search_hits_kinds = [
        k
        for k in sorted(runtime_with_store.hub.kinds)
        if runtime_with_store.hub.handler_for(k).spec.supports_search_hits
    ]
    assert search_hits_kinds, (
        "expected at least one cross-kind search-hits kind in the registry"
    )
    for k in search_hits_kinds:
        assert k in out, f"expected {k!r} listed in the no-matches body"


def test_search_defaults_to_cross_kind_fanout(
    runtime_with_store: PrecisRuntime,
) -> None:
    """When the caller omits ``kind=`` and the build has ≥2
    search-hits-capable kinds, the runtime fans out across every
    kind via reciprocal-rank fusion — not the most-recently-
    touched single kind. The MCP critic flagged the old default
    as a 7B affordance that hid useful answers in the other
    kinds (gripe:3681 #2, 2026-05-01); the new default is "what
    do I know about X" semantics. The single-kind annotation is
    now reserved for the single-kind fallback path
    (≤1 eligible kind).
    """
    runtime_with_store.dispatch("put", {"kind": "memory", "text": "default-kind probe"})
    out = runtime_with_store.dispatch("search", {"q": "default-kind probe"})
    assert "[error:BadInput]" not in out
    # Cross-kind fan-out: no single-kind annotation, hits tagged
    # with their source kind, and the body names the kinds it
    # searched (matching the empty-cross-kind regression above).
    assert "(searched kind=" not in out
    search_hits_kinds = [
        k
        for k in sorted(runtime_with_store.hub.kinds)
        if runtime_with_store.hub.handler_for(k).spec.supports_search_hits
    ]
    assert len(search_hits_kinds) >= 2, (
        "this test only meaningfully exercises the new default when ≥2 "
        "kinds are search-hits-capable in the test fixture"
    )


def test_comma_list_kind_dispatches_cross_kind_merge(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``kind='paper,memory'`` now fans out via ``search_hits`` and
    RRF-fuses the streams via :func:`merge_and_render`. The previous
    behaviour (a sharp ``BadInput`` saying multi-kind search was
    "not implemented yet, merge results client-side") is gone — the
    universal merge primitive handles it server-side now.

    Empty corpus → empty-body response, NOT an error.
    """
    out = runtime_with_store.dispatch(
        "search", {"kind": "paper,memory", "q": "qabsentword"}
    )
    assert "[error:BadInput]" not in out
    assert "comma-list kind not supported" not in out
    # Empty cross-kind result surfaces the kinds that were searched.
    assert "paper" in out and "memory" in out


def test_wildcard_kind_dispatches_cross_kind_merge(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``kind='*'`` is the canonical "search every kind" form."""
    out = runtime_with_store.dispatch("search", {"kind": "*", "q": "qabsentword"})
    assert "[error:BadInput]" not in out


@pytest.mark.parametrize("alias", ["all", "All", "ALL", "any", "*", "", "  all  "])
def test_kind_all_aliases_dispatch_cross_kind_merge(
    runtime_with_store: PrecisRuntime, alias: str
) -> None:
    """English shortcuts (``'all'`` / ``'any'``) and the empty
    string MUST behave identically to the canonical ``'*'`` —
    they expand to every search-hits-capable kind. The MCP critic
    flagged the missing alias as MAJOR-C 2026-05-02: a 7B caller
    hitting ``kind='all'`` previously got an unhelpful
    ``unknown kind: all`` error pointing at the kinds list.
    """
    out = runtime_with_store.dispatch("search", {"kind": alias, "q": "qabsentword"})
    assert "[error:BadInput]" not in out, (
        f"alias {alias!r} must dispatch as cross-kind, not error"
    )
    assert "[error:NotFound]" not in out, (
        f"alias {alias!r} must not be treated as an unknown kind"
    )


def test_cross_kind_unknown_kind_lists_eligible_options(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Unknown kind in a comma-list returns BadInput naming the kinds
    that DO opt into cross-kind merge — same shape as the pre-existing
    'unknown kind' error path so retries don't cascade."""
    out = runtime_with_store.dispatch(
        "search", {"kind": "paper,nosuchkind", "q": "test"}
    )
    assert "[error:BadInput]" in out
    assert "nosuchkind" in out


def test_cross_kind_search_forwards_exclude_to_supporting_kinds(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """Cross-kind dispatch forwards ``exclude=`` to handlers that
    accept it (paper today) and degrades cleanly for handlers that
    don't (memory) via the ``TypeError`` retry chain in
    ``_cross_kind_invoke_search_hits``.

    Pins the user-facing contract: ``search(kind='*',
    exclude=[paper-slug])`` drops the paper from the merged stream
    without crashing on memory's smaller signature. Without the
    retry chain, the whole fan-out would die on the first
    TypeError.
    """
    from precis.embedder import MockEmbedder
    from precis.store import BlockInsert

    e = MockEmbedder(dim=store.embedding_dim())
    # Paper that will match the query.
    paper = store.insert_ref(
        kind="paper",
        slug="paper-a",
        title="A",
    )
    store.insert_blocks(
        paper.id,
        [
            BlockInsert(
                pos=0,
                text="qabsent unique-marker xyz",
                embedding=e.embed_one("qabsent unique-marker xyz"),
            ),
        ],
    )
    # Memory with the same query word — proves the merge actually
    # crosses kinds, and that exclude doesn't blow up memory's path.
    runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "qabsent unique-marker memory side"}
    )

    # Cross-kind search without exclude: paper-a appears.
    out_full = runtime_with_store.dispatch(
        "search", {"kind": "*", "q": "unique-marker"}
    )
    assert "[error:" not in out_full
    assert "paper-a" in out_full

    # With exclude=['paper-a']: paper drops from the merged result;
    # memory side still appears (handler ignores the kwarg via the
    # TypeError fallback).
    out_excl = runtime_with_store.dispatch(
        "search",
        {"kind": "*", "q": "unique-marker", "exclude": ["paper-a"]},
    )
    assert "[error:" not in out_excl
    assert "paper-a" not in out_excl
    # Memory hit should still be there — exclude= is silently ignored
    # by handlers without the kwarg, not poisoning the whole call.
    assert "memory side" in out_excl


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


def test_chunk_range_accepts_dash_form() -> None:
    """``slug~N-M`` is accepted as a synonym for ``slug~N..M``.

    Workers reach for the dash form (``~20-30``) as the obvious
    range syntax. Until 2026-05-04 this hit ``BadInput: unparseable
    chunk selector after ~: '20-30'``. The handler still parses
    ``~N..M`` canonically; the dash is just an accepted variant.
    """
    from precis.handlers.paper import _RANGE_RE

    assert _RANGE_RE.match("20..30") is not None
    m = _RANGE_RE.match("20-30")
    assert m is not None
    assert (int(m.group(1)), int(m.group(2))) == (20, 30)
    # Single number still parsed by _CHUNK_RE, not _RANGE_RE.
    assert _RANGE_RE.match("20") is None
    # Trailing garbage still rejected.
    assert _RANGE_RE.match("20-30-40") is None


def test_edit_op_error_uses_external_mode_name() -> None:
    """``where=`` validation echoes ``mode='find-replace'``, not
    internal ``op='edit'``.

    Pre-fix, the error said ``got mode='edit'``, which made 7B
    callers retry with literal ``mode='edit'`` and hit
    ``unknown edit mode 'edit'`` from the handler dispatcher —
    a self-perpetuating error loop.
    """
    import pytest

    from precis.errors import BadInput
    from precis.utils.edit_resolve import EditOp

    with pytest.raises(BadInput) as exc_info:
        EditOp(op="edit", find="x", text="y", where="before")
    msg = str(exc_info.value)
    assert "find-replace" in msg
    assert "got mode='edit'" not in msg


def test_view_text_body_full_alias_to_default() -> None:
    """``view='text'`` (and 'body', 'full') is a no-op — same as no view.

    Workers naturally reach for ``view='text'`` to ask for chunk
    bytes, e.g. ``get(kind='paper', id='gerfen2011~13', view='text')``.
    Without this alias, that pattern raises BadInput ('cannot combine
    chunk selector with view='text''), which the worker then retries
    with view='toc' — burning two cycles per chunk read.
    """
    assert _normalise_view("text") is None
    assert _normalise_view("body") is None
    assert _normalise_view("full") is None


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
    """MCP critic (multiple rounds) + May 2026 policy tightening:
    the canonical entry-point doc must not reference any kind that
    isn't wired in the runtime. The stricter "unwired = unmentioned"
    discipline removes even *reserved* kinds (``book``, ``docx``,
    ``tex``, ``rmk``) that were previously tolerated as informational.

    The rule of thumb: if an agent reads this skill and copy-pastes
    a kind name into ``get(kind=X, …)``, X must succeed (or fail for
    reasons unrelated to X not existing). Today's wired set is the
    only set we advertise.
    """
    from importlib import resources

    text = (
        resources.files("precis.data.skills")
        .joinpath("precis-overview.md")
        .read_text("utf-8")
    )
    # Never-wired kinds (flagged by the original critic pass).
    for dead in ("`clock`", "`rng`", "`plot`", "`ask`"):
        assert dead not in text, f"precis-overview still references dead kind {dead!r}"
    # Reserved-but-unwired kinds (stripped under the May 2026
    # policy). If one of these lands, wire the handler AND re-add
    # the kind mention in the same commit.
    for reserved in ("`book`", "`docx`", "`rmk`"):
        assert reserved not in text, (
            f"precis-overview reintroduces unwired kind {reserved!r}"
        )


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


# ── MINOR-C: tag tool description documents per-kind axis gating ────


def test_tag_tool_description_documents_per_kind_gating() -> None:
    """MCP critic MINOR-C (round 1): the ``tag`` tool's description
    must teach the LLM about per-kind closed-prefix gating so a
    ``BadInput: axis not allowed on kind 'memory'`` reads as expected
    behaviour, not a bug. Agents that don't know the gating retry the
    same call with different values and burn tokens in confusion.

    The description is what the MCP client sees in ``tools/list`` —
    it's the LLM's only spec for the verb, so the gating must be
    discoverable there.
    """
    import inspect

    # The seven verbs moved into the shared tool registry; the
    # function FastMCP serialises into ``tools/list`` lives at
    # :mod:`precis.tools.core`. Reach for it there.
    from precis.tools.core import tag as tag_fn

    doc = inspect.getdoc(tag_fn) or ""
    # The gating phrase ("axis not allowed on kind") is the exact
    # error string the runtime emits — a docstring ↔ error pair.
    assert "axis not allowed on kind" in doc
    # Explicit per-kind matrix + pointer to the authoritative skill.
    assert "Per-kind closed-prefix gating" in doc
    assert "precis-tags" in doc
    # Pin a few known-correct axis assignments so a future rework of
    # _KIND_ALLOWED_AXES nudges the docstring too.
    for expected in (
        "todo",  # STATUS + PRIO
        "memory",  # no closed axes
        "paper",  # SRC + CACHE
        "CACHE",  # cache-only kinds
    ):
        assert expected in doc, f"tag docstring must mention {expected!r}"


# ── MINOR-C: soft-deleted refs distinguished from never-existed ────


class TestSoftDeleteGoneEnvelope:
    """MCP critic MINOR-C (round 1): ``delete memory id=N`` then
    ``get memory id=N`` previously returned an identical
    ``[error:NotFound]`` to a never-existed id, so the LLM couldn't
    tell whether it hit a typo (try a different id) or a tombstone
    (the row is gone, no MCP undo).

    The fix adds a distinct ``Gone`` error class; the rendering
    uses ``err.__class__.__name__`` so the envelope is
    ``[error:Gone]`` with a recovery hint pointing at the SQL
    layer (the only path that can resurrect a soft-deleted ref).
    """

    def test_never_existed_raises_notfound(self, store: Store) -> None:
        from precis.handlers.memory import MemoryHandler

        h = MemoryHandler(hub=Hub(store=store))
        with pytest.raises(NotFound, match="id=99999999 not found"):
            h.get(id=99999999)

    def test_soft_deleted_raises_gone_not_notfound(self, store: Store) -> None:
        from precis.handlers.memory import MemoryHandler

        h = MemoryHandler(hub=Hub(store=store))
        out = h.put(text="probe — will be deleted")
        # Ack shape is "created memory id=N …" (_render_create_ack).
        import re

        m = re.search(r"id=(\d+)", out.body)
        assert m is not None, f"unexpected put body shape: {out.body!r}"
        ref_id = int(m.group(1))

        # Soft-delete the ref.
        h.delete(id=ref_id)

        # NotFound is wrong — the row is still there, just flagged.
        # Gone carries an "SQL layer" recovery hint.
        with pytest.raises(Gone) as excinfo:
            h.get(id=ref_id)
        assert "soft-deleted" in excinfo.value.cause
        assert excinfo.value.next is not None
        assert "SQL" in excinfo.value.next

    def test_rendered_envelope_labelled_gone(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        """End-to-end through the dispatcher: the rendered string
        uses ``[error:Gone]`` (not ``[error:NotFound]``) so any
        wrapper parsing the envelope-prefix sees the distinction."""
        out_put = runtime_with_store.dispatch(
            "put", {"kind": "memory", "text": "envelope probe"}
        )
        import re

        m = re.search(r"id=(\d+)", out_put)
        assert m is not None
        ref_id = int(m.group(1))
        runtime_with_store.dispatch("delete", {"kind": "memory", "id": ref_id})

        out_get = runtime_with_store.dispatch("get", {"kind": "memory", "id": ref_id})
        assert "[error:Gone]" in out_get
        assert "[error:NotFound]" not in out_get
        assert "soft-deleted" in out_get


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

        h = MemoryHandler(hub=Hub(store=store))
        before = len(store.list_refs(kind="memory", limit=200))
        with pytest.raises(BadInput):
            h.put(text="probe", tags=["urgent"])  # collides with PRIO:urgent
        after = len(store.list_refs(kind="memory", limit=200))
        assert after == before, "rejected create still wrote a ref row"

    def test_create_with_unknown_axis_writes_nothing(self, store: Store) -> None:
        from precis.handlers.memory import MemoryHandler

        h = MemoryHandler(hub=Hub(store=store))
        before = len(store.list_refs(kind="memory", limit=200))
        with pytest.raises(BadInput):
            h.put(text="tx-test", tags=["DENSITY:bogus"])  # unknown closed axis
        after = len(store.list_refs(kind="memory", limit=200))
        assert after == before

    def test_update_with_invalid_tag_does_not_change_text(self, store: Store) -> None:
        from precis.handlers.memory import MemoryHandler

        h = MemoryHandler(hub=Hub(store=store))
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
        ref = store.insert_ref(kind="paper", slug="p", title="P")
        # Three blocks of meaningful text — none lexically or
        # semantically close to the gibberish query.
        store.insert_blocks(
            ref.id,
            [
                BlockInsert(
                    pos=0,
                    text="alpha beta gamma",
                    embedding=e.embed_one("alpha beta gamma"),
                ),
                BlockInsert(
                    pos=1,
                    text="delta epsilon zeta",
                    embedding=e.embed_one("delta epsilon zeta"),
                ),
                BlockInsert(
                    pos=2,
                    text="eta theta iota",
                    embedding=e.embed_one("eta theta iota"),
                ),
            ],
        )
        h = PaperHandler(hub=Hub(store=store, embedder=e))
        out = h.search(q="xyzzy frobnicate quux")
        # The handler returns the empty-results envelope, not a
        # ranked top-K of irrelevant blocks.
        assert "no paper blocks match" in out.body


# ── Plumbing fixture for cross-kind search test ─────────────────────


@pytest.fixture
def runtime_with_store(store: Store) -> PrecisRuntime:
    return PrecisRuntime(
        config=_default_config(),
        hub=boot(store=store),
    )


def _default_config():
    from precis.config import PrecisConfig

    return PrecisConfig(database_url=None, embedder="mock")


# ─────────────────────────────────────────────────────────────────────
# April-2026 second-pass critic findings
# ─────────────────────────────────────────────────────────────────────


def test_instructions_advertises_every_verb() -> None:
    """The MCP critic flagged ``Verbs: get, search, put, put.`` — the
    duplicate ``put`` hid ``move`` from any caller relying on
    serverInfo.instructions. The import-time assert in server.py now
    catches regressions, but pin it from a test too.

    Updated for the seven-verb surface: instructions advertise the
    seven agent-facing verbs. ``move`` is intentionally absent — it
    survives as a back-compat tool but D5 folds reorder semantics
    into ``edit(mode='reorder')``, so new callers shouldn't see it.
    """
    from precis import server

    for verb in ("get", "search", "put", "edit", "delete", "tag", "link"):
        assert verb in server._INSTRUCTIONS, (
            f"server _INSTRUCTIONS must list every verb; missing {verb!r}"
        )


def test_instructions_lead_with_skill_search_cta() -> None:
    """Phase 2 banner CTA (docs/design/mcp-cold-start-token-budget.md):
    the cold-start banner pushes agents to ``search(kind='skill', ...)``
    as the first action on a non-trivial request. Pin the CTA shape
    so future edits don't silently regress the discoverability story.
    """
    from precis import server

    text = server._INSTRUCTIONS
    assert "First action" in text
    assert "search(kind='skill', q=" in text
    # Full-index pointer remains so an agent can list rather than
    # search when it doesn't have a query in mind.
    assert "get(kind='skill', id='toc')" in text


def test_kinds_loaded_line_renders_sorted_set() -> None:
    """Phase-2 ``Kinds loaded:`` summary: the helper sorts the live
    set, joins with commas, and renders ``(none)`` when nothing is
    registered (stateless build / boot bug). Pin the shape so the
    agent always sees the truthful surface.
    """
    from precis import server

    rt_full = _runtime_with_root(None, file_kinds=("todo", "paper", "memory"))
    assert server._kinds_loaded_line(rt_full) == "Kinds loaded: memory, paper, todo."

    rt_empty = _runtime_with_root(None, file_kinds=())
    assert server._kinds_loaded_line(rt_empty) == "Kinds loaded: (none)"


# ── cold-start discoverability: sandbox preamble in serverInfo.instructions ─


def _runtime_with_root(
    root: str | None, *, file_kinds: tuple[str, ...]
) -> PrecisRuntime:
    """Build a PrecisRuntime whose config.root is ``root`` and whose hub
    reports ``file_kinds`` as registered.

    We fake the hub's ``kinds`` property rather than spinning up real file
    handlers — ``_build_instructions`` only needs the set membership test,
    and threading through the full handler stack (with its store
    dependency) would pull a DB fixture into a logic test.
    """
    from precis.config import PrecisConfig

    config = PrecisConfig(root=root) if root is not None else PrecisConfig()

    class _FakeHub:
        kinds = set(file_kinds)

    hub = _FakeHub()
    # PrecisRuntime is a dataclass with ``config`` + ``hub`` fields; any
    # object with a ``kinds`` attribute satisfies the call sites in
    # ``_build_instructions``.
    return PrecisRuntime(config=config, hub=hub)  # type: ignore[arg-type]


def test_build_instructions_returns_static_core_when_root_unset() -> None:
    """MCP critic MAJOR-C (cold-start discoverability): when
    ``PRECIS_ROOT`` is unset, the instructions still carry the
    pinned static verb blurb verbatim and a ``Kinds loaded:``
    summary, with no sandbox preamble (which would lie without a
    root).
    """
    from precis import server

    runtime = _runtime_with_root(None, file_kinds=())
    out = server._build_instructions(runtime)
    assert server._INSTRUCTIONS in out
    assert out.startswith(server._INSTRUCTIONS)
    # Phase-2 tail: live-kind summary appended to every banner.
    assert "Kinds loaded:" in out


def test_build_instructions_returns_static_core_when_root_set_but_no_file_kind_registered() -> (
    None
):
    """If ``root`` is set but no file-rooted handler registered, the
    preamble would lie (there's no ``get(kind='markdown')`` to call).
    Fall back to the static core + ``Kinds loaded:`` tail rather than
    advertise a dead file-surface.
    """
    from precis import server

    runtime = _runtime_with_root("/nonexistent", file_kinds=("paper", "todo"))
    out = server._build_instructions(runtime)
    assert out.startswith(server._INSTRUCTIONS)
    assert "Kinds loaded: paper, todo." in out


def test_build_instructions_announces_sandbox_when_root_has_files(tmp_path) -> None:
    """Canonical case: ``PRECIS_ROOT`` is set, file kinds are
    registered, files exist. The preamble precedes the static core,
    names the counts, and lists the per-kind ``get(kind='…')`` calls
    a cold-start agent would otherwise not know to try.
    """
    from precis import server

    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "meeting.md").write_text("# hi", encoding="utf-8")
    (tmp_path / "notes" / "ideas.md").write_text("# ideas", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "run.log").write_text("line", encoding="utf-8")
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "paper.tex").write_text(
        "\\documentclass{article}", encoding="utf-8"
    )

    runtime = _runtime_with_root(
        str(tmp_path), file_kinds=("markdown", "plaintext", "tex")
    )
    out = server._build_instructions(runtime)

    # Preamble prepends the static core; the live-kind summary trails it.
    assert server._INSTRUCTIONS in out
    assert out != server._INSTRUCTIONS
    assert "Kinds loaded: markdown, plaintext, tex." in out

    # Counts are visible.
    assert "2 markdown" in out
    assert "1 plaintext" in out
    assert "1 tex" in out

    # Per-kind index calls are named so agents can follow up.
    assert "get(kind='markdown')" in out
    assert "get(kind='plaintext')" in out
    assert "get(kind='tex')" in out

    # Workspace-tag hint carries a concrete, runnable example rather
    # than prose. ``search(tags=['workspace'])`` with empty ``q=`` is
    # currently rejected (cross-kind search requires q=), so the
    # banner teaches the keyword form — pin that exact shape.
    assert "`workspace`" in out
    assert "search(q='<keyword>', tags=['workspace'])" in out


def test_build_instructions_handles_empty_sandbox(tmp_path) -> None:
    """``PRECIS_ROOT`` set, file kinds registered, tree empty → preamble
    invites the agent to create a file instead of claiming nothing
    can be done.
    """
    from precis import server

    runtime = _runtime_with_root(
        str(tmp_path), file_kinds=("markdown", "plaintext", "tex")
    )
    out = server._build_instructions(runtime)

    assert server._INSTRUCTIONS in out
    assert "empty" in out
    assert "mode='create'" in out
    # The concrete search example lands in the empty branch too so
    # an agent starting from zero knows how to verify its first write.
    assert "search(q='<keyword>', tags=['workspace'])" in out
    # Phase-2 tail.
    assert "Kinds loaded: markdown, plaintext, tex." in out


def test_build_instructions_ignores_unregistered_kinds(tmp_path) -> None:
    """If only ``markdown`` is registered, a ``.tex`` file on disk
    doesn't surface in the counts — we must not advertise tools the
    build doesn't actually have.
    """
    from precis import server

    (tmp_path / "a.md").write_text("# a", encoding="utf-8")
    (tmp_path / "b.tex").write_text("\\documentclass{article}", encoding="utf-8")

    runtime = _runtime_with_root(str(tmp_path), file_kinds=("markdown",))
    out = server._build_instructions(runtime)

    assert "1 markdown" in out
    assert "tex" not in out.split(server._INSTRUCTIONS, 1)[0]
    assert "get(kind='markdown')" in out
    assert "get(kind='tex')" not in out


def test_build_instructions_handles_missing_root_directory() -> None:
    """``PRECIS_ROOT`` points at a path that doesn't exist on this
    host. We shouldn't crash — we should render the empty-sandbox
    preamble (the walker just yields nothing).
    """
    from precis import server

    runtime = _runtime_with_root("/this/path/does/not/exist", file_kinds=("markdown",))
    out = server._build_instructions(runtime)

    # Graceful: render the empty-sandbox preamble, never raise.
    assert server._INSTRUCTIONS in out
    assert "empty" in out
    assert "Kinds loaded: markdown." in out


def test_apply_instructions_mutates_underlying_mcp_server(tmp_path) -> None:
    """The ``_apply_instructions`` helper must write through to
    ``fastmcp._mcp_server.instructions`` so the handshake payload
    (``initialize`` response's ``serverInfo.instructions``) carries
    the dynamic text. Pin the integration since FastMCP exposes
    ``instructions`` as a read-only property with no setter.
    """
    from mcp.server.fastmcp import FastMCP

    from precis import server

    (tmp_path / "a.md").write_text("# a", encoding="utf-8")
    runtime = _runtime_with_root(str(tmp_path), file_kinds=("markdown",))

    fastmcp = FastMCP("test-server", instructions="placeholder")
    server._apply_instructions(fastmcp, runtime)

    # Property delegates to the lowlevel attribute we mutated.
    assert fastmcp.instructions is not None
    assert "Editable sandbox" in fastmcp.instructions
    assert "1 markdown" in fastmcp.instructions
    assert server._INSTRUCTIONS in fastmcp.instructions
    assert "Kinds loaded: markdown." in fastmcp.instructions


def test_server_name_is_precis_mcp() -> None:
    """``serverInfo.name`` should be ``precis-mcp`` (not the bare
    ``precis``) so log lines disambiguate against other tooling."""
    from precis import server

    assert server.mcp.name == "precis-mcp"


def test_dispatch_with_status_flags_errors() -> None:
    """``dispatch_with_status`` returns ``is_error=True`` on PrecisError
    so the MCP wrapper can flip the protocol-level ``isError`` flag.
    (MCP critic MAJOR — errors-as-strings without isError.)"""
    from precis.config import PrecisConfig

    rt = PrecisRuntime(
        config=PrecisConfig(database_url=None, embedder="mock"),
        hub=Hub(),
    )
    body, is_error = rt.dispatch_with_status("get", {"kind": "calc"})
    assert is_error is True
    assert "[error:" in body

    # Success path stays is_error=False.
    body, is_error = rt.dispatch_with_status("frobnicate", {})
    assert is_error is True
    assert "[error:BadInput]" in body


def test_chunk_view_combo_recovery_hint_is_parseable() -> None:
    """The recovery hint emitted when a caller mixes ``~A..B`` with
    ``view=`` must round-trip through ``ast.parse`` — the previous form
    quoted only the slug, leaving the chunk suffix outside the quotes
    and producing a SyntaxError when copied. (MCP critic MAJOR.)"""
    import ast
    import re

    from precis.handlers.paper import PaperHandler, _parse_paper_id  # noqa: F401

    # Drive the BadInput path directly via the parser/error renderer.
    pytest.importorskip("psycopg")
    # Construct a minimal handler stub — we don't need a real store
    # for this check; assemble the next= string the way the handler
    # does.
    slug = "wang2020state"
    chunk = (38, 38)
    recovery_id = f"{slug}~{chunk[0]}..{chunk[1]}/toc"
    next_hint = f"get(kind='paper', id={recovery_id!r})"
    # Strip the leading "get(" and trailing ")" via regex, keep the
    # arg list parseable.
    m = re.match(r"^get\((.+)\)$", next_hint)
    assert m, f"next hint should look like get(...): {next_hint!r}"
    # ast.parse on the arg list as a function call — it must parse.
    ast.parse(next_hint, mode="eval")


def test_calc_humanises_sympy_constants() -> None:
    """``1/0`` → ``zoo`` was opaque; now we surface plain English so a
    7B model doesn't misread it as a typo. (MCP critic MINOR.)"""
    from precis.handlers.calc import CalcHandler

    h = CalcHandler(hub=Hub())
    out = h.get(id="1/0").body
    assert "complex infinity" in out

    out = h.get(id="oo + 1").body
    assert "+infinity" in out or "infinity" in out


def test_toc_renders_single_block_section_as_tilde_n() -> None:
    """``~N..N`` was leaking through the TOC renderer even though the
    chunk renderer dropped it; both paths now agree. (MCP critic
    MINOR — ``~38..38`` would still surface as a model.)"""
    from precis.handlers._paper_toc import _format_block_range

    assert _format_block_range(38, 38) == "~38"
    assert _format_block_range(38, 50) == "~38..50"


def test_cache_backed_listing_hint_uses_caller_kind() -> None:
    """The empty-state hint must name the kind being called and the
    kind-specific example query — not the hardcoded
    ``kind='math', q='population of Ireland'``. (MCP critic MAJOR.)"""
    from precis.handlers.math import MathHandler
    from precis.handlers.web import WebHandler
    from precis.handlers.youtube import YouTubeHandler

    for cls, expected_query in (
        (MathHandler, "population of Ireland"),
        (WebHandler, "https://example.com/article"),
        (YouTubeHandler, "dQw4w9WgXcQ"),
    ):
        assert cls.example_query == expected_query, (
            f"{cls.__name__}.example_query must name a kind-shaped recovery query"
        )


def test_skill_index_hides_power_user_skill_for_unwired_kind() -> None:
    """``precis-patent-power`` is hostile when ``kind='patent'`` isn't
    wired (the skill examples all return [error:Unsupported]); the
    index must hide it the same way ``precis-patent-help`` is hidden.
    (MCP critic MAJOR.)

    Round-2 picky R2-3 (2026-05-30): the availability gate now
    requires the kind to be *known* to the registry — present in
    either ``hub.kinds`` or ``hub.loadabilities``. The fake hub
    below mirrors production behaviour: ``patent`` is registered as
    a deferred kind (``loaded=False``) so the gate recognises it
    and fires the banner. Before this change, the fake hub's lack
    of ``loadabilities`` made ``patent`` look like an unknown name,
    and the gate skipped the banner — producing the umbrella-skill
    false-positive that R2-3 was meant to prevent.
    """
    from precis.handlers.skill import _availability_gap

    class _Loadability:
        def __init__(self, loaded: bool) -> None:
            self.loaded = loaded

    class _NoPatentHub:
        @property
        def kinds(self) -> list[str]:
            return ["calc", "paper", "memory"]

        loadabilities: dict[str, _Loadability] = {
            "calc": _Loadability(True),
            "paper": _Loadability(True),
            "memory": _Loadability(True),
            "patent": _Loadability(False),
        }

    gap = _availability_gap("precis-patent-power", hub=_NoPatentHub())
    assert gap is not None, (
        "precis-patent-power references kind='patent' in its applies-to "
        "front-matter; the gate must hide it when patent isn't wired"
    )
    assert "patent" in gap


def test_bibtex_unescapes_html_entities() -> None:
    """Carbon Capture Science &amp; Technology should land as
    ``Carbon Capture Science \\& Technology`` in the BibTeX output —
    not the verbatim HTML entity. (MCP critic MINOR.)"""
    from datetime import datetime

    from precis.handlers.paper import _format_citation
    from precis.store.types import Ref

    _now = datetime(2026, 1, 1, tzinfo=UTC)
    ref = Ref(
        id=1,
        kind="paper",
        slug="ahmed2025revolutionary",
        title="Revolutionary CO<sub>2</sub> capture",
        provider=None,
        meta={
            "journal": "Carbon Capture Science &amp; Technology",
            "year": 2025,
        },
        created_at=_now,
        updated_at=_now,
        deleted_at=None,
    )
    bibtex = _format_citation(ref, style="bibtex")
    # &amp; must collapse to & then LaTeX-escape to \&.
    assert r"\&" in bibtex
    assert "&amp;" not in bibtex
    # JATS / HTML inline tags strip from the title.
    assert "<sub>" not in bibtex
    assert "CO2" in bibtex


def test_paper_list_view_strips_jats_markup() -> None:
    """List view titles must not leak ``Cu/ZnO<sub>x</sub>``; the
    cleanup helper strips inline JATS / HTML tags. (MCP critic MINOR.)"""
    from precis.handlers.paper import _clean_inline_text

    assert _clean_inline_text("Cu/ZnO<sub>x</sub> Nanoparticles") == (
        "Cu/ZnOx Nanoparticles"
    )
    # Double-escaped `<i>` survives entity unescape twice as a literal
    # `<i>` tag, then the inline-tag stripper drops it. Both layers
    # have to fire for the result to be agent-safe.
    assert (
        _clean_inline_text("Acidic Media&amp;lt;i&amp;gt;Active&amp;lt;/i&amp;gt;A")
        == "Acidic MediaActiveA"
    )


def test_figure_block_pairs_with_caption(store: Store) -> None:
    """When the caller asks for a single image-only block, the renderer
    auto-pulls the adjacent caption block so the agent sees both in one
    response. (MCP critic MAJOR — figure block returns image marker
    with no caption.)"""
    from precis.handlers.paper import (
        _is_image_only_block,
        _looks_like_caption,
    )

    # Pure-logic checks first.
    image_block = '<span id="page-19-0"></span>![](_page_19_Figure_1.jpeg)'
    caption_block = "**Fig. 16. (a)** Mechanism of photocatalytic NOx reduction."
    assert _is_image_only_block(image_block)
    assert _looks_like_caption(caption_block)
    assert not _is_image_only_block(caption_block)
    assert not _looks_like_caption(image_block)


def test_figure_block_renders_structured_placeholder() -> None:
    """Bare ``![](...)`` markers leak relative paths nothing serves;
    replace each with a structured ``[figure: slug~N — ...]``
    placeholder so an LLM citing the figure can't quote a dead URL.

    The MCP critic's April 2026 re-probe flagged the previous cut
    as STILL leaking ``_page_*_Figure`` because the placeholder
    text kept the asset path "for diagnostics".  Asset path is
    now dropped — a 7B caller reading ``asset: _page_3_Figure_3
    .jpeg`` treated the string as a real file, defeating the
    point of the substitution.
    """
    from precis.handlers.paper import _render_block_body

    image_block = '<span id="page-19-0"></span>![](_page_19_Figure_1.jpeg)'
    rendered = _render_block_body("ahmed2025revolutionary", 210, image_block)
    assert "![](" not in rendered, "bare image marker must not leak through"
    assert "[figure: ahmed2025revolutionary~210" in rendered
    assert "image not served" in rendered
    # Asset path must NOT appear — that was the re-probe regression.
    assert "_page_19_Figure_1.jpeg" not in rendered
    assert "_page_" not in rendered


def test_args_extras_unknown_key_rejected(runtime_with_store: PrecisRuntime) -> None:
    """``args={'depth': 3}`` against a kind that doesn't accept depth
    must raise rather than be silently swallowed by ``**_kw``.
    (MCP critic MINOR — args= silently consumed.)"""
    out = runtime_with_store.dispatch(
        "get",
        {"kind": "paper", "id": "doesnt-matter", "__extras__": {"depth": 3}},
    )
    assert "[error:BadInput]" in out
    assert "depth" in out
    assert "not accepted by paper.get" in out


# ── MAJOR: error returns from MCP tools must not blow up structured
#          output validation in mcp 1.27+ ─────────────────────────────


def test_search_error_path_survives_fastmcp_convert_result(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``mcp`` 1.27 added structured-output schema validation in
    ``FuncMetadata.convert_result``: when a tool annotated ``-> str``
    returns a ``CallToolResult`` (our error path), FastMCP validates
    ``result.structuredContent`` against the auto-generated schema.
    Our errors only set ``content`` + ``isError``, so
    ``structuredContent`` is ``None`` and the str-shaped model
    rejected it with a Pydantic ``model_type`` error.

    The fix disables structured output on every tool
    (``@mcp.tool(structured_output=False)``).  Pin both the success
    path (returns plain str through the boundary) and the error path
    (returns a ``CallToolResult`` with ``isError=True``) by going
    through the actual FastMCP ``call_tool`` entrypoint.
    """
    import asyncio

    from precis import server

    server._runtime = runtime_with_store
    try:
        # 1) success path — known kind, even if no hits.  Must come back
        #    as a list of ContentBlock with the rendered text body.
        out_ok = asyncio.run(
            server.mcp.call_tool(
                "search",
                {"q": "anything", "kind": "paper", "top_k": 3},
            )
        )
        assert isinstance(out_ok, list), (
            f"success path should return ContentBlock list, got {type(out_ok)}"
        )
        body_ok = "".join(b.text for b in out_ok if getattr(b, "type", None) == "text")
        # Either we got hits ("# N matches for") or the empty-result
        # template ("no paper blocks match"); either is fine — the
        # important thing is no exception leaked through convert_result.
        assert "match" in body_ok.lower(), f"unexpected success body: {body_ok!r}"

        # 2) error path — unknown kind in cross-kind list.  Must come
        #    back without raising and must carry ``[error:BadInput]``.
        from mcp.server.fastmcp.exceptions import ToolError

        try:
            out_err = asyncio.run(
                server.mcp.call_tool(
                    "search",
                    {"q": "anything", "kind": "paper,nosuchkind", "top_k": 3},
                )
            )
        except ToolError as e:  # pragma: no cover — fail loudly if it raises
            raise AssertionError(
                f"FastMCP rejected the error CallToolResult: {e}"
            ) from e

        # FastMCP returns the CallToolResult verbatim for tool-author
        # errors; older paths return a content list.  Either way, the
        # rendered body must mention the error class.
        if hasattr(out_err, "content"):
            blocks = out_err.content  # type: ignore[union-attr]
            assert getattr(out_err, "isError", False), (
                "error path must set isError=True on the protocol surface"
            )
        else:
            blocks = out_err
        body_err = "".join(b.text for b in blocks if getattr(b, "type", None) == "text")
        assert "[error:BadInput]" in body_err
        assert "nosuchkind" in body_err
    finally:
        server._runtime = None


# ── MAJOR (Apr 2026): searched-kind annotation must surface on errors ──


def test_search_default_kind_annotates_error_path(
    runtime_with_store: PrecisRuntime,
) -> None:
    """When ``search(kind='memory')`` is called and the handler
    raises, the rendered error must name the kind we tried — the
    annotation is the agent's only signal about which kind to
    retry against. The 2026-05-02 default flip moved kind-less
    ``search()`` to cross-kind fan-out (gripe:3681 #2), so this
    test now passes ``kind='memory'`` explicitly and verifies
    the annotation still surfaces on the explicit-single-kind
    error path. The cross-kind variant is covered by
    :func:`test_cross_kind_search_default_fans_out_when_store_is_empty`
    (no annotation needed — every hit is already source-tagged).
    """
    # Force a failure inside the memory handler's search() by
    # monkeypatching the bound method to raise.  We exercise the
    # PrecisError branch first…
    handler = runtime_with_store.hub.handler_for("memory")
    original = handler.search

    def _boom(**_kw):  # type: ignore[no-untyped-def]
        raise BadInput("synthetic search failure")

    try:
        handler.search = _boom  # type: ignore[method-assign]
        out = runtime_with_store.dispatch("search", {"kind": "memory", "q": "anything"})
    finally:
        handler.search = original  # type: ignore[method-assign]

    assert "[error:BadInput]" in out, f"expected error envelope, got: {out!r}"
    assert "synthetic search failure" in out, (
        "error envelope must surface the handler's message"
    )

    # …and the non-Precis branch (gets wrapped as Internal but the
    # rendered envelope must still preserve the kind context).
    def _explode(**_kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic non-precis failure")

    try:
        handler.search = _explode  # type: ignore[method-assign]
        out2 = runtime_with_store.dispatch(
            "search", {"kind": "memory", "q": "anything"}
        )
    finally:
        handler.search = original  # type: ignore[method-assign]

    assert "[error:Internal]" in out2


# ── MINOR (Apr 2026): calc recovery hint uses q=, not id= ─────────────


def test_calc_recovery_hint_uses_q_kwarg() -> None:
    """precis-overview / precis-help show ``q='2+3*4'`` as the canonical
    calc shape.  The handler still accepts ``id=`` for symmetry, but
    the recovery hint must teach ``q=`` so a caller scraping the
    next: trailer doesn't start emitting ``id=`` for tool-kinds and
    trip over the q= vs id= split everywhere else.  (MCP critic
    MINOR — calc recovery hint uses id= while canonical example
    uses q=.)
    """
    from precis.handlers.calc import CalcHandler

    handler = CalcHandler(hub=Hub())

    # Unparseable expression → BadInput with a recovery hint.
    with pytest.raises(BadInput) as exc_info:
        handler.get(q="2+")
    assert exc_info.value.next is not None
    assert "q=" in exc_info.value.next, (
        f"calc recovery hint must teach q=, got: {exc_info.value.next!r}"
    )
    assert "id=" not in exc_info.value.next, (
        f"calc recovery hint must not teach id= (canonical example uses q=), "
        f"got: {exc_info.value.next!r}"
    )

    # Missing-expression error path.
    with pytest.raises(BadInput) as exc_info2:
        handler.get()
    assert exc_info2.value.next is not None
    assert "q=" in exc_info2.value.next
    assert "id=" not in exc_info2.value.next


# ── MINOR (Apr 2026): empty numeric-ref search has Next: trailer ──────


def test_empty_numeric_ref_search_has_next_trailer(store: Store) -> None:
    """Empty searches on memory/todo/gripe/fc/quest must surface a
    Next: block — same shape as the very-good empty-list responses
    on get(kind='conv') / get(kind='gripe').  Without this, a small-
    model caller retries the same query, gives up, or guesses at
    the wrong kind.  (MCP critic MINOR — empty-result responses on
    search lack recovery hints.)
    """
    from precis.handlers.memory import MemoryHandler
    from precis.handlers.todo import TodoHandler

    hub = Hub(store=store)
    mem = MemoryHandler(hub=hub)
    mem.put(text="hello world")
    out = mem.search(q="frobnicate-zzz-quux")
    assert "no memory entries match" in out.body
    assert "Next:" in out.body, (
        "empty memory search must carry a Next: trailer with at least "
        "one runnable suggestion"
    )
    assert "search(kind='memory'" in out.body
    assert "/recent" in out.body

    todo = TodoHandler(hub=hub)
    todo.put(text="finish report")
    out_todo = todo.search(q="probe-zzz-quux")
    assert "no todo entries match" in out_todo.body
    assert "Next:" in out_todo.body
    assert "search(kind='todo'" in out_todo.body


def test_empty_numeric_ref_search_with_tags_suggests_dropping_filter(
    store: Store,
) -> None:
    """When the empty result was filtered by tags, the Next: block
    must surface a "drop the tag filter" affordance — otherwise a
    caller stuck at zero hits has no way to tell whether the corpus
    is empty or whether the filter is the reason.
    """
    from precis.handlers.memory import MemoryHandler

    handler = MemoryHandler(hub=Hub(store=store))
    handler.put(text="hello world")
    out = handler.search(q="hello", tags=["topic-no-such-thing"])
    assert "no memory entries match" in out.body
    assert "Next:" in out.body
    assert "drop the tag filter" in out.body


# ── MINOR (Apr 2026): paper view='fig/<N>' is reserved, not a typo ────


def test_paper_search_preview_strips_image_markers(store: Store) -> None:
    """The MCP critic's April 2026 re-probe flagged the search render
    as still leaking raw ``![](_page_3_Figure_3.jpeg)`` markers in
    its preview lines.  ``_render_chunks`` had the substitution
    wired but ``search()`` did not.  Centralising the strip in
    ``_scrub_block_text`` and applying it at both call sites keeps
    every excerpt path on the same contract.
    """
    from precis.handlers.paper import PaperHandler, _scrub_block_text

    # Pure-helper sanity: image marker + page anchor both go.
    raw = '<span id="page-3-0"></span>![](_page_3_Figure_3.jpeg)'
    scrubbed = _scrub_block_text(raw)
    assert "![](" not in scrubbed
    assert "_page_" not in scrubbed
    assert "<span" not in scrubbed
    assert "[figure]" in scrubbed
    # Idempotent — running twice yields the same string.
    assert _scrub_block_text(scrubbed) == scrubbed

    # End-to-end: seed a paper with an image-only block + a caption
    # block and run the search() rendering.  Neither preview must
    # carry the marker / asset path.
    ref = store.insert_ref(
        kind="paper",
        slug="markerleak2026probe",
        title="Search-preview marker leak regression",
        meta={"abstract": "minimal corpus to drive the search() render path"},
    )
    from precis.store import BlockInsert

    image_block_text = (
        '<span id="page-3-0"></span>![](_page_3_Figure_3.jpeg) photocatalytic'
    )
    caption_block_text = "**Fig. 3.** Photocatalytic NOx reduction mechanism on Cu/ZnO."
    store.insert_blocks(
        ref.id,
        [
            BlockInsert(pos=10, text=image_block_text),
            BlockInsert(pos=11, text=caption_block_text),
        ],
    )

    handler = PaperHandler(hub=Hub(store=store))
    out = handler.search(q="photocatalytic")
    body = out.body

    # The search hit MUST be present (this is the path under test).
    assert "markerleak2026probe" in body
    # Neither leak token is allowed in the rendered body.
    assert "![](" not in body, (
        f"search preview must scrub image markers; got body={body!r}"
    )
    assert "_page_" not in body, (
        f"search preview must not leak the asset path; got body={body!r}"
    )
    assert "<span" not in body


def test_paper_view_fig_n_is_reserved_not_unknown(store: Store) -> None:
    """precis-paper-help advertises ``view='fig/<N>'`` as a future-
    reserved affordance.  The handler currently lumps it into the
    generic "unknown view" enum, which makes a caller who has read
    the help skill assume the docs are wrong rather than the build
    being early.  Surface a deliberate "reserved view" error that
    cites the help skill.  (MCP critic MINOR — fig/<N> is documented
    but unrecognised view path returns the same enum as a typo.)
    """
    from precis.errors import Unsupported
    from precis.handlers.paper import PaperHandler

    ref = store.insert_ref(
        kind="paper",
        slug="testpaper2026figview",
        title="Test paper for fig/N reserved view",
        meta={},
    )
    assert ref.id is not None

    handler = PaperHandler(hub=Hub(store=store))

    with pytest.raises(Unsupported) as exc_info:
        handler.get(id="testpaper2026figview", view="fig/3")

    msg = str(exc_info.value.cause)
    assert "fig/3" in msg
    assert "reserved" in msg.lower(), (
        f"reserved-view error should call out the reservation; got: {msg!r}"
    )
    assert exc_info.value.next is not None
    assert "precis-paper-help" in exc_info.value.next
    # Don't conflate with a typo: a typo in the canonical view enum
    # (e.g. 'biibtex') still routes to the original "unknown view"
    # message, which is the right shape for a typo.
    with pytest.raises(Unsupported) as exc_typo:
        handler.get(id="testpaper2026figview", view="biibtex")
    assert "unknown view" in str(exc_typo.value.cause).lower()
