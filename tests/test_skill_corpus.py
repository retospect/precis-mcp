"""SkillHandler corpus — overview/includes/personas, precis-help, verb-surface and overview kind tables (split out of test_skill.py, gr515135)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


# ── packaging guarantees ─────────────────────────────────────────────


def test_skills_directory_has_overview(skill: SkillHandler) -> None:
    """The overview is the agent's entry point — it must exist."""
    from precis.handlers.skill import _list_skills

    skills = _list_skills()
    assert "precis-overview" in skills


def test_skill_count_reasonable(skill: SkillHandler) -> None:
    """Sanity check that we ship a handful of skills, not zero or 9000.

    The ceiling is deliberately loose (gr514416): a hard cap meant every
    new skill needed a test edit, and sibling +1 bumps collided on rebase
    (gr514731). 400 keeps the catastrophe check — the recursive walk
    suddenly picking up a foreign tree — without per-add churn."""
    from precis.handlers.skill import _list_skills

    skills = _list_skills()
    assert 5 <= len(skills) <= 400


# ── subdirectory discovery + include expansion ───────────────────────


def test_personas_subdir_is_discoverable() -> None:
    """Persona files under ``data/skills/personas/`` are findable.

    The recursive walk in ``_load_skills_map`` means subdirectory
    organisation (personas/, refs/, runbooks/, …) is transparent to
    callers; every ``*.md`` whose stem matches the slug regex is
    reachable via ``_load_skill`` regardless of its directory.
    """
    from precis.handlers.skill import _list_skills

    slugs = _list_skills()
    assert "precis-adversarial-reviewer" in slugs
    assert "precis-citation-reviewer" in slugs


def test_load_skill_expands_includes(skill: SkillHandler) -> None:
    """``_load_skill`` resolves ``{{include doc:…}}`` directives.

    The adversarial-reviewer persona pulls 6 sections from
    ``precis-common-reviewer.md`` via include directives. The body
    returned by ``_load_skill`` must have those expanded inline —
    no HTML-comment markers, no raw directive tokens.
    """
    from precis.handlers.skill import _load_skill

    body = _load_skill("precis-adversarial-reviewer")
    assert body is not None
    # No raw {{include directives left in the expanded text.
    assert "{{include" not in body
    # Substitution is verbatim — no HTML-comment markers so MCP-
    # served bodies don't carry low-signal tokens.
    assert "<!-- inlined-from" not in body
    # Content from the included section actually appears, inline.
    assert "Picky reviewer stance" in body
    assert "Quote verbatim" in body


def test_common_reviewer_fenced_example_not_expanded(skill: SkillHandler) -> None:
    """Regression for gr311346.

    ``precis-common-reviewer.md`` teaches personas the ``{{include}}``
    syntax inside a fenced code example — and that example happens to
    be a syntactically valid, self-referential directive (it names
    sections of ``precis-common-reviewer`` itself). Fence-unaware
    expansion spliced the resolved sections mid-fence, duplicating
    "Picky reviewer stance" / "Output findings table format" and
    leaving a dangling ``` fence. The fenced example must survive
    verbatim and each section must render exactly once.
    """
    from precis.handlers.skill import _load_skill

    body = _load_skill("precis-common-reviewer")
    assert body is not None
    assert "{{include doc:precis-common-reviewer#picky-reviewer-stance}}" in body
    assert "{{include doc:precis-common-reviewer#output-findings-table-format}}" in body
    assert body.count("## Picky reviewer stance") == 1
    assert body.count("## Output findings table format") == 1
    # Fences balance -- no dangling ``` left by a mid-fence splice.
    assert body.count("```") % 2 == 0


def test_get_skill_returns_expanded_body(skill: SkillHandler) -> None:
    """`get(kind='skill', id='precis-adversarial-reviewer')` returns
    expanded content — agents fetching via MCP see resolved includes,
    not raw directive tokens."""
    out = skill.get(id="precis-adversarial-reviewer")
    assert "{{include" not in out.body
    assert "Picky reviewer stance" in out.body


def test_personas_are_searchable_by_unique_term(skill: SkillHandler) -> None:
    """End-to-end: personas under ``data/skills/personas/`` show up
    in ``search(kind='skill', q=…)`` when the query matches text
    unique to that persona.

    Without an embedder wired (the fixture builds SkillHandler with a
    bare Hub), search falls through to the substring/lexical path of
    FileCorpusIndex. The persona's body must therefore be in the
    files dict — proving that the recursive walk picked it up AND
    that include expansion produced content (the included sections
    are part of what makes these queries hit).
    """
    out = skill.search(q="adversarial")
    assert "precis-adversarial-reviewer" in out.body, (
        "adversarial-reviewer persona under personas/ should be "
        "searchable; query 'adversarial' did not find it"
    )

    out = skill.search(q="citation discipline")
    assert "precis-citation-reviewer" in out.body, (
        "citation-reviewer persona under personas/ should be "
        "searchable; query 'citation discipline' did not find it"
    )


def test_personas_search_finds_included_content(skill: SkillHandler) -> None:
    """A query matching text from ``precis-common-reviewer``'s shared
    blocks (which personas pull in via ``{{include}}``) must hit the
    personas — proves include expansion ran *before* the search index
    sampled their content, not just at body-render time."""
    # 'no benefit of the doubt' lives only in
    # precis-common-reviewer#picky-reviewer-stance, and personas pull
    # it in. If expansion ran, the personas should also match.
    out = skill.search(q="no benefit of the doubt")
    body = out.body
    # Either the common-reviewer file itself surfaces (also valid),
    # or one of the personas does. With include expansion working,
    # personas carrying the same text should appear.
    assert (
        "precis-adversarial-reviewer" in body or "precis-citation-reviewer" in body
    ), (
        "query matching included shared text should hit at least "
        "one persona — include expansion may not have run during "
        "index build"
    )


def test_skill_without_includes_is_unchanged(skill: SkillHandler) -> None:
    """Skills with no ``{{include}}`` directives skip the expander
    entirely and return identical bytes — fast path for the 90 % of
    skills that don't yet use the include mechanism."""
    from precis.handlers.skill import _load_skill, _load_skills_map

    raw = _load_skills_map().get("precis-overview")
    expanded = _load_skill("precis-overview")
    if raw is not None and "{{include" not in raw:
        assert expanded == raw


def test_skill_corpus_texts_is_public_mirror_of_private_map() -> None:
    """``skill_corpus_texts`` (docs/backlog/skill-graph.md slice 1, item
    3) is the public accessor a caller outside this module — the
    server-seam graph build — uses instead of reaching through the
    leading-underscore ``_load_skills_map``."""
    from precis.handlers.skill import _load_skills_map, skill_corpus_texts

    public = skill_corpus_texts()
    private = _load_skills_map()
    assert public == private
    # A fresh copy each call — mutating it must not poison the cache.
    public["bogus-slug"] = "junk"
    assert "bogus-slug" not in _load_skills_map()


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
        .read_text(encoding="utf-8")
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


def test_precis_overview_ref_kinds_table_matches_registry(hub: Hub) -> None:
    """The "ref kinds" table specifically must list every kind that
    supports the full get/search/tag/link surface.

    The looser sibling test above passes when a kind is mentioned
    anywhere in the markdown, which let ``finding``/``job``/
    ``provenance``/``tag`` drop out of the canonical table even though
    they were referenced elsewhere — broad-pass usability finding #8.

    This test extracts the rows under the "## The ref kinds" heading
    and asserts every full-verb-surface kind in ``hub.kinds`` is
    listed as a row by its inline-code slug in the first column.
    """
    import re

    from precis.handlers.skill import _load_skill

    text = _load_skill("precis-overview")
    assert text is not None, "precis-overview.md missing from package data"

    m = re.search(
        r"^## The ref kinds[^\n]*\n(.*?)(?=\n## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "precis-overview missing the 'ref kinds' table heading"
    table_block = m.group(1)
    listed = set(re.findall(r"^\|\s*`([^`]+)`\s*\|", table_block, re.MULTILINE))

    full_surface = (
        hub.kinds_supporting("get")
        & hub.kinds_supporting("search")
        & hub.kinds_supporting("tag")
        & hub.kinds_supporting("link")
    )
    missing = sorted(full_surface - listed)
    assert not missing, (
        "precis-overview 'ref kinds' table drifts from registry — "
        f"missing rows for: {missing}. Add a row each, or change the "
        "table's stated contract."
    )
