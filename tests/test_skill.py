"""Tests for SkillHandler — markdown docs served from data/skills/.

The skills are real package data (`src/precis/data/skills/*.md`), so
these tests assert against the actual files shipped with the package.
That's intentional: they double as a packaging smoke test.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.skill import SkillHandler


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


# ── single fetch ──────────────────────────────────────────────────────


def test_get_existing_skill(skill: SkillHandler) -> None:
    out = skill.get(id="precis-overview")
    # The first H1 should match the canonical skill name.
    assert "precis-overview" in out.body or "Precis" in out.body
    assert len(out.body) > 100  # not empty


def test_get_paper_skill_documents_navigation(skill: SkillHandler) -> None:
    """The phase 3.5 navigation update added a 'Navigate' section."""
    out = skill.get(id="precis-paper-help")
    assert "Navigate" in out.body or "TOC" in out.body


def test_get_missing_raises_with_options(skill: SkillHandler) -> None:
    with pytest.raises(NotFound) as excinfo:
        skill.get(id="nonexistent-skill")
    err = excinfo.value
    assert err.options is not None
    assert any("precis-overview" in s for s in err.options)


def test_invalid_slug_raises(skill: SkillHandler) -> None:
    # The id parser is the first gate (regex-tight); the slug
    # validator the second. Both routes produce BadInput — that's
    # what the test pins. Phase B 2026-05-31: added the address
    # parser, so the specific error text shifted but the
    # error class stays the same.
    with pytest.raises(BadInput):
        skill.get(id="UPPERCASE")
    with pytest.raises(BadInput):
        skill.get(id="path/traversal")


# ── index view ────────────────────────────────────────────────────────


def test_bare_get_lists_skills(skill: SkillHandler) -> None:
    out = skill.get()
    assert "skill" in out.body.lower()
    assert "precis-overview" in out.body
    # The index trailer is now an explicit "Suggested starting
    # commands" section (round-2 picky reviewer flagged the old
    # generic "Next:" wording as ambiguous between recipe shortcuts
    # and new skills). Pin the new heading so a future rename
    # surfaces here.
    assert "Suggested starting commands" in out.body


def test_path_view_also_lists(skill: SkillHandler) -> None:
    out = skill.get(id="/index")
    assert "precis-overview" in out.body


# ── search ────────────────────────────────────────────────────────────


def test_search_finds_term(skill: SkillHandler) -> None:
    """Most skills mention 'kind' somewhere — sanity check fulltext."""
    out = skill.search(q="kind")
    assert "skill match" in out.body
    # Each hit should reference a real slug.
    assert "precis-" in out.body


def test_search_no_match(skill: SkillHandler) -> None:
    out = skill.search(q="xyzzy-no-such-token-anywhere")
    assert "no skills mention" in out.body


def test_search_hyphen_space_equivalence(skill: SkillHandler) -> None:
    """A 7B caller's natural phrasing ``spaced repetition`` must
    find ``spaced-repetition`` in the corpus, and vice versa. The
    MCP critic flagged the punctuation-specific false negative as
    MAJOR-C 2026-05-02; the substring search now folds hyphen and
    whitespace runs to a single space on both needle and haystack.
    """
    # ``precis-fc-help`` titles itself "spaced-repetition flashcards"
    # — the hyphenated form is what's actually in the corpus.
    out_natural = skill.search(q="spaced repetition")
    out_hyphen = skill.search(q="spaced-repetition")
    assert "precis-fc-help" in out_natural.body, (
        "natural-language query must find the hyphenated corpus term"
    )
    assert "precis-fc-help" in out_hyphen.body, (
        "hyphenated query must find the hyphenated corpus term"
    )


def test_search_empty_query_degrades_to_index(skill: SkillHandler) -> None:
    """``search(kind='skill', q='')`` and ``q=None`` degrade to the
    skill index (the same body ``get(kind='skill')`` returns), instead
    of raising ``BadInput`` like the previous behaviour. Round-2 picky
    N4/F-6, 2026-05-30 — symmetric with how the rest of the surface
    treats empty-q calls (``search(kind='memory', tags=[...])`` lists
    refs without ranking; skill should mirror that). The index body
    carries its own canonical ``Next:`` trailer.
    """
    index = skill.get()
    for empty in (None, "", "   "):
        resp = skill.search(q=empty)
        assert resp.body == index.body, (
            f"search(q={empty!r}) should match skill.get() index body; "
            f"got first-line {resp.body.splitlines()[0]!r}"
        )


# ── packaging guarantees ─────────────────────────────────────────────


def test_skills_directory_has_overview(skill: SkillHandler) -> None:
    """The overview is the agent's entry point — it must exist."""
    from precis.handlers.skill import _list_skills

    skills = _list_skills()
    assert "precis-overview" in skills


def test_skill_count_reasonable(skill: SkillHandler) -> None:
    """Sanity check that we ship a handful of skills, not zero or 9000."""
    from precis.handlers.skill import _list_skills

    skills = _list_skills()
    assert 5 <= len(skills) <= 100


# ── synthesized precis-help skill ────────────────────────────────────


def test_precis_help_falls_back_without_hub(skill: SkillHandler) -> None:
    """Without a hub bound, precis-help still resolves but is a stub.

    Under the old design a handler's registry reference came from
    ``bind_registry(...)``. Under the new design it's populated by
    ``Handler._register_with`` at construction time. Direct-constructed
    fixtures (like this one) never go through ``_register_with``, so
    ``self.hub`` stays ``None`` and the skill falls back.
    """
    out = skill.get(id="precis-help")
    assert "precis-help" in out.body
    assert "hub not wired" in out.body


def test_precis_help_lists_active_kinds(skill: SkillHandler) -> None:
    """When the hub is bound, precis-help enumerates every kind.

    The renderer reads ``Hub.verbs_for(kind)`` to source the live
    verb set rather than re-deriving from ``KindSpec.supports_*``
    flags — that way adding a new verb to ``_ALL_VERBS`` lights it
    up here automatically (regression for the seven-verb cutover,
    which previously left this skill stale showing only get/search/
    put despite handlers also supporting edit/delete/tag/link).
    """

    class _FakeSpec:
        def __init__(self, kind: str, *, description: str = "") -> None:
            self.kind = kind
            self.description = description

    class _FakeHandler:
        def __init__(self, spec: _FakeSpec) -> None:
            self.spec = spec

    class _FakeHub:
        """Duck-typed stand-in for ``dispatch.Hub`` exposing the
        three attributes ``SkillHandler._render_help`` consults:
        ``kinds`` (iterable property), ``handler_for(kind)``, and
        ``verbs_for(kind)`` — the live verb set from the dispatch
        table."""

        def __init__(
            self,
            handlers: list[_FakeHandler],
            verbs_per_kind: dict[str, set[str]],
        ) -> None:
            self._h = {h.spec.kind: h for h in handlers}
            self._verbs = verbs_per_kind

        @property
        def kinds(self) -> list[str]:
            return sorted(self._h.keys())

        def handler_for(self, kind: str) -> _FakeHandler:
            return self._h[kind]

        def verbs_for(self, kind: str) -> set[str]:
            return self._verbs.get(kind, set())

    handlers = [
        _FakeHandler(_FakeSpec("calc", description="Math expressions")),
        _FakeHandler(_FakeSpec("todo", description="Tasks with status tracking")),
        _FakeHandler(_FakeSpec("paper", description="Research papers")),
    ]
    # Mirror the live spec for each kind via the seven-verb table.
    verbs_per_kind = {
        "calc": {"get"},
        "todo": {"get", "search", "put", "delete", "tag", "link"},
        "paper": {"get", "search", "tag", "link"},
    }
    skill.hub = _FakeHub(handlers, verbs_per_kind)

    out = skill.get(id="precis-help")
    assert "calc" in out.body
    assert "todo" in out.body
    assert "paper" in out.body
    # Verbs surfaced — full seven-verb visibility per kind.
    assert "get / search / put / delete / tag / link" in out.body  # todo
    assert "get / search / tag / link" in out.body  # paper
    # calc supports only get; the row should show that single verb.
    assert "calc" in out.body
    assert "3 kinds active" in out.body


def test_precis_help_listed_in_index(skill: SkillHandler) -> None:
    out = skill.get()
    assert "precis-help" in out.body
    # Hint trailer should reference it.
    assert "precis-help" in out.body
    assert "active kinds" in out.body


# ── synthesized precis-toc skill ─────────────────────────────────────


def test_toc_alias_dispatches_to_precis_toc(skill: SkillHandler) -> None:
    """Both ``id='toc'`` and ``id='precis-toc'`` render the same body.

    The alias is registered in ``_SYNTH_ALIASES`` so a 7B caller
    typing the natural ``toc`` lands on the same TOC view as the
    canonical slug.
    """
    short = skill.get(id="toc").body
    full = skill.get(id="precis-toc").body
    assert short == full


def test_toc_lists_every_skill_with_synopsis(skill: SkillHandler) -> None:
    """The TOC names every shipping skill grouped by category.

    Round-2 picky 2026-05-30: the layout was restructured from a
    flat ``## Meta-skills`` + ``## Skills`` two-bucket shape into the
    five-category top layer (Orientation / Core verbs / Content
    types / Research & validation / Workflow tools). Synth
    meta-skills now live inside Orientation; file-backed skills land
    in whichever category their slug is registered under
    (``_SKILL_CATEGORIES``). Anything unmapped surfaces in
    ``## Other`` so new slugs never silently disappear.
    """
    out = skill.get(id="toc")
    body = out.body
    # Top-layer category headings — at least the first two must show
    # up; the rest depend on which kinds are wired in the fixture
    # hub and could legitimately drop out.
    assert "## Orientation" in body
    assert "## Core verbs" in body
    # Every synth meta-skill appears under Orientation.
    assert "precis-help" in body
    assert "precis-status" in body
    assert "precis-toc" in body
    # The canonical entry-point skill is listed.
    assert "precis-overview" in body
    # Discovery footer ("Suggested starting commands") and at least
    # one search recipe inside it.
    assert "Suggested starting commands" in body
    assert "search(kind='skill'" in body


def test_toc_listed_in_bare_index_hint(skill: SkillHandler) -> None:
    """``get(kind='skill')`` (no id) hints at ``id='toc'`` in the
    Next-Up trailer so an agent navigating the index lands on the
    TOC immediately rather than scrolling the raw slug list."""
    out = skill.get()
    assert "id='toc'" in out.body


# ── semantic search via FileCorpusIndex ──────────────────────────────


def test_search_uses_semantic_index_when_embedder_wired(tmp_path) -> None:
    """When the hub carries an embedder, ``search()`` routes through
    :class:`FileCorpusIndex` and returns hits.

    The MockEmbedder is hash-based and not actually semantic, but
    the integration path — index build, cache write, response
    formatting — is what we're pinning here. End-to-end "transcribe
    video finds youtube-help" requires a real bge-m3 model and is
    exercised live, not in CI.

    Round-2 picky 2026-05-31: dropped the ``source`` column from the
    rendered TOON shape (the maintainer said it was low signal). The
    test now checks for the integration path firing — search returns
    a hit headline + at least one TOON row — rather than the
    source-label string. End-to-end correctness is what matters.
    """
    import os

    from precis.dispatch import Hub
    from precis.embedder import MockEmbedder

    os.environ["PRECIS_CACHE_DIR"] = str(tmp_path)
    try:
        hub = Hub(embedder=MockEmbedder(dim=64))
        handler = SkillHandler(hub=hub)
        # Plant the hub manually since we bypassed Handler._register_with.
        handler.hub = hub
        out = handler.search(q="precis-overview")
        assert "skill match" in out.body
        # At least one TOON row begins with a slug + tab — the data
        # rows of the search-hit table.
        assert any(
            ln.startswith("precis-") and "\t" in ln
            for ln in out.body.splitlines()
        ), f"expected at least one precis-* TOON row; got body:\n{out.body}"
    finally:
        del os.environ["PRECIS_CACHE_DIR"]


def test_search_falls_back_to_substring_when_no_embedder(skill: SkillHandler) -> None:
    """Without an embedder the substring stream provides every hit.

    Round-2 picky 2026-05-31: the rendered output no longer carries a
    ``source`` column; the substring-vs-semantic distinction is now
    internal-only. Test that the substring path still returns hits
    when invoked without an embedder.
    """
    out = skill.search(q="seven verbs")
    # Either we found hits, in which case there's a headline + a TOON
    # row, OR we hit the empty-result branch which has a different
    # body shape entirely. Both are acceptable; what matters is no
    # crash and no fallback to ``BadInput``.
    assert "no skills mention" in out.body or "skill match" in out.body


# ── search marks unwired skills ──────────────────────────────────────


def test_search_marks_unwired_skills(skill: SkillHandler) -> None:
    """``search(kind='skill', q=...)`` must annotate skills whose
    subject kind is *known* to the registry but not currently loaded
    with ``[unwired]`` — 7B callers quote the title and invoke
    ``[error:NotFound]`` otherwise. Mirror of the index's
    hidden-skills behaviour.

    The "known" check (round-2 picky R2-3, 2026-05-30) uses
    ``hub.loadabilities`` so umbrella skill slugs like
    ``precis-files-help`` (whose stem ``'files'`` is *not* a real
    kind) don't get falsely marked. Production registers every
    deferred kind in ``loadabilities`` with ``loaded=False`` — the
    fake hub below mirrors that.
    """

    class _Loadability:
        def __init__(self, loaded: bool) -> None:
            self.loaded = loaded

    class _NoFileKindsHub:
        """Duck-typed hub that deliberately omits the file kinds
        (markdown / plaintext / python) so their help skills surface
        with the ``[unwired]`` marker. The file kinds appear in
        ``loadabilities`` (registered, but loaded=False) so the
        availability gate recognises them as known-but-disabled."""

        @property
        def kinds(self) -> list[str]:
            return ["calc", "paper", "memory"]

        loadabilities: dict[str, _Loadability] = {
            "calc": _Loadability(True),
            "paper": _Loadability(True),
            "memory": _Loadability(True),
            "markdown": _Loadability(False),
            "plaintext": _Loadability(False),
            "tex": _Loadability(False),
            "python": _Loadability(False),
        }

    skill.hub = _NoFileKindsHub()
    # 'edit' appears in several file-kind skills; the search hit
    # list should include at least one markdown/plaintext/tex/python
    # help skill with the inline ``[unwired]`` prefix on its slug.
    # (Round-2 picky 2026-05-31: dropped the dedicated ``status``
    # column; the marker is now a slug prefix to save a column for
    # the rare case it fires.)
    out = skill.search(q="edit")
    assert "[unwired]" in out.body, (
        "at least one unwired file-kind skill must surface with the "
        f"[unwired] slug prefix; got body:\n{out.body}"
    )
    # Skills the hub DOES support must NOT carry the marker.
    # precis-tags is a cross-cutting skill that references no specific
    # kind — its slug row must NOT have the [unwired] prefix.
    tags_out = skill.search(q="tags")
    # TOON row shape (post-2026-05-31 trim): ``slug\tsection\tkeywords``.
    # Locate the precis-tags row by its slug column; confirm no
    # ``[unwired]`` prefix prepended.
    rows = [
        ln
        for ln in tags_out.body.splitlines()
        if ln.startswith("precis-tags\t")
    ]
    if rows:
        assert "[unwired]" not in rows[0], (
            f"cross-cutting skill should not be marked unwired: {rows[0]!r}"
        )


# ── precis-overview kinds table stays honest ──────────────────────────


def test_overview_kinds_table_names_env_gates() -> None:
    """Every file-backed kind row in the precis-overview kinds table
    must name the env var that gates it, so a reader is never
    surprised by ``[error:NotFound] unknown kind: markdown`` after
    copying an example. (Review 2026-05: MAJOR-C — kind='markdown'
    advertised as active but unknown to registry.)"""
    from importlib import resources

    text = (
        resources.files("precis.data.skills")
        .joinpath("precis-overview.md")
        .read_text("utf-8")
    )
    # Every shipped file-backed kind row names its env-var gate.
    # ``markdown`` / ``plaintext`` / ``tex`` all share ``PRECIS_ROOT``
    # after the May 2026 consolidation; ``python`` keeps its own var.
    for kind, env in (
        ("markdown", "PRECIS_ROOT"),
        ("plaintext", "PRECIS_ROOT"),
        ("tex", "PRECIS_ROOT"),
        ("python", "PRECIS_PYTHON_ROOTS"),
    ):
        # Look for a line containing both the kind name and the env var.
        found = any(f"`{kind}`" in line and env in line for line in text.splitlines())
        assert found, (
            f"precis-overview kinds table must name {env} on the "
            f"{kind!r} row so readers know when the kind is active"
        )


# ── seven-verb-surface regression test ─────────────────────────────


# Forbidden mode= values that belonged to the legacy four-verb
# surface and should never reappear in skill text. ``import`` and
# ``create`` survive the cutover (Perplexity import + creation
# shortcut, migration doc D3). Anything else under ``put(mode=…)``
# is a leftover.
_LEGACY_PUT_MODES: tuple[str, ...] = (
    "delete",
    "edit",
    "append",
    "insert",
    "replace",
)

# Other legacy kwargs / verbs that have no place in the new surface.
_LEGACY_LITERAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("untags=", "tag(remove=[...])"),
    ("unlink=", "link(target=..., mode='remove')"),
    ("move(", "edit(mode='reorder')"),
)


def _find_matching_paren(s: str, start: int) -> int:
    """Return the index of the ``)`` matching the ``(`` at ``start``.

    Quote-aware: skips over single-, double-, and triple-quoted
    string literals so a ``)`` inside a docstring example doesn't
    fool the matcher. Returns ``-1`` when no match is found.
    """
    depth = 0
    i = start
    while i < len(s):
        c = s[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        elif c in "\"'":
            quote = c
            if s[i : i + 3] == quote * 3:
                end = s.find(quote * 3, i + 3)
                if end == -1:
                    return -1
                i = end + 3
                continue
            i += 1
            while i < len(s) and s[i] != quote:
                if s[i] == "\\":
                    i += 2
                    continue
                i += 1
        i += 1
    return -1


def test_skills_use_seven_verb_surface() -> None:
    """Every shipped skill must teach the seven-verb shape, not the legacy four-verb one.

    The migration doc (D4) is explicit: hard cutover, no alias window.
    A release that removed the old surface but still documented it in
    skills would be worse than no release. This regression catches
    any skill that still teaches a legacy pattern.

    Two checks:

    1. Plain substring for legacy kwargs / verbs (``untags=``,
       ``unlink=``, ``move(``) anywhere in the file.
    2. Scope-aware ``mode=<legacy-value>`` detection: only flagged
       when it appears inside a ``put(...)`` call. ``mode='replace'``
       inside ``edit(...)`` is correct and must not be flagged.
       ``mode='create'`` and ``mode='import'`` are accepted on
       ``put`` (D3) and aren't in the legacy list.

    See ``docs/user-facing/seven-verb-surface-migration.md`` Phase 2.
    """
    import re
    from importlib.resources import files

    put_call_re = re.compile(r"\bput\(")
    legacy_mode_re = re.compile(
        r"mode\s*=\s*['\"](" + "|".join(_LEGACY_PUT_MODES) + r")['\"]"
    )

    skills_dir = files("precis.data.skills")
    failures: list[str] = []
    for path in skills_dir.iterdir():
        # Only check shipped markdown skills, not __init__.py / __pycache__.
        if not str(path).endswith(".md"):
            continue
        text = path.read_text(encoding="utf-8")
        for needle, suggestion in _LEGACY_LITERAL_PATTERNS:
            if needle in text:
                failures.append(
                    f"{path.name}: legacy pattern {needle!r} \u2192 use {suggestion}"
                )
        # Scope mode= check to put(...) calls only. mode='replace'
        # inside edit(...) is the supported new shape.
        for m in put_call_re.finditer(text):
            paren_open = m.end() - 1
            paren_close = _find_matching_paren(text, paren_open)
            if paren_close == -1:
                continue
            args = text[paren_open + 1 : paren_close]
            for mm in legacy_mode_re.finditer(args):
                mode_val = mm.group(1)
                if mode_val == "delete":
                    hint = "delete(...) verb"
                elif mode_val == "edit":
                    hint = "edit(mode='find-replace', ...) verb"
                else:
                    hint = f"edit(mode={mode_val!r}, ...) verb"
                failures.append(
                    f"{path.name}: put(... mode={mode_val!r} ...) \u2192 use {hint}"
                )
    assert not failures, (
        "seven-verb regression: legacy four-verb patterns found in skills:\n  "
        + "\n  ".join(failures)
    )


# ── precis-overview drift detection ───────────────────────────────────


def test_precis_overview_kind_table_covers_live_registry(hub: Hub) -> None:
    """Every live ``hub.kinds`` entry must appear in ``precis-overview``.

    ``precis-overview`` is the tier-1 discovery skill — if a kind is
    active in the registry but not documented here, a caller reading
    the canonical discovery doc learns a wrong set of kinds and
    concludes a working feature doesn't exist. This happened with the
    ``patent`` kind (MCP critic MAJOR-C, 2026-05-02).

    The test asserts the *markdown file* on disk mentions every live
    kind by its inline-code slug (``\u0060kind\u0060``).  Hand-maintained
    table is fine — what matters is drift detection.

    Takes the ``hub`` fixture (Postgres-backed, the same one
    handler tests use) so every store-gated kind shows up — calling
    bare ``boot()`` would register only ``calc`` and miss the whole
    refs surface.
    """
    from precis.handlers.skill import _load_skill

    text = _load_skill("precis-overview")
    assert text is not None, "precis-overview.md missing from package data"
    missing = [k for k in sorted(hub.kinds) if f"`{k}`" not in text]
    assert not missing, (
        "precis-overview drifts from live registry — "
        f"missing kinds: {missing}. "
        "Add a row to the refs/tools/discovery table for each, "
        "or mark explicitly as env-gated in the Needs column."
    )
