"""Tests for bimodal skill injection (§2, ``docs/backlog/skill-question-
targets-and-injection.md``): :mod:`precis.skill_index.injection`'s
single-skill fallback and section injection, plus the two harness-controlled
prompt-assembly points wired to it — ``workers/planner_prompt.py`` and
``quest/tick.py``. ``workers/executors/claude_inproc.py`` builds no prompt
of its own (its ``plan_tick`` path *is* ``planner_prompt.py``, already
covered here; its other job type, ``fix_gripe``, runs a bug-fix coding
agent with no precis MCP tools wired at all, so a precis-skill injection
there would document affordances the agent can't reach — no sane seam).

Runs against the real skill corpus with :class:`~precis.embedder.
MockEmbedder` (the test-default embedder — see ``tests/conftest.py``'s
session-scoped ``_force_mock_embedder_for_tests``). The mock is hash-
based, not semantic, so match/no-match assertions use an EXACT copy of a
real skill's seeded front-matter question (self-similarity 1.0) versus an
arbitrary string unrelated to anything in the corpus (near-zero cosine
similarity to every real chunk) — the same technique
``tests/test_skill_index.py`` documents for its own MockEmbedder-backed
index tests.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.skill_index import injection


@pytest.fixture(autouse=True)
def _hermetic_injection_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> Iterator[None]:
    """A fresh cache dir + a fresh module-level index per test.

    Mirrors ``tests/test_skill.py``'s
    ``test_search_uses_semantic_index_when_embedder_wired`` (same
    ``PRECIS_CACHE_DIR`` swap) — the injection module memoises its
    :class:`~precis.skill_index.index.FileCorpusIndex` the same way
    ``SkillHandler`` does, so tests need an explicit reset between runs.
    """
    monkeypatch.setenv("PRECIS_CACHE_DIR", str(tmp_path))
    # 543814513: the prompt assemblers skip the bimodal block while
    # autoskill is active (enabled AND a store bound to route_log). A store
    # bound by an earlier test in the same worker would silently disable
    # the injection under test, so pin it off here.
    monkeypatch.setenv("PRECIS_AUTOSKILL", "off")
    injection._reset_index_for_tests()
    yield
    injection._reset_index_for_tests()


#: A real, stable question from ``precis-overview``'s seeded ``answers:``
#: front matter. Copied verbatim so MockEmbedder (hash-based, not
#: semantic — same input string always hashes to the same vector) scores
#: it at self-similarity 1.0 against the skill's own question_only chunk.
_KNOWN_QUESTION = "what verbs does precis support?"
_KNOWN_SKILL = "precis-overview"

#: A second seeded question, from ``precis-todo-help`` — also one of its
#: H2 headings, so the question twin resolves to a real section either
#: way. Used to prove two distinct sub-tasks both land in one injection.
_KNOWN_QUESTION_2 = "how do I file something I need to do?"
_KNOWN_SKILL_2 = "precis-todo-help"

#: An arbitrary string with no relationship to any shipped skill's
#: question-shaped chunks — a hash-derived vector has near-zero cosine
#: similarity to every unrelated hash-derived vector in high dimensions.
_UNRELATED_TEXT = "zzqxvbn plerdon frobnicate the ozymandias biryani doorknob"


def _mk_quest(store: Any, text: str) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


# ── match_skill ──────────────────────────────────────────────────────────


def test_match_skill_above_threshold_returns_whole_skill() -> None:
    match = injection.match_skill(_KNOWN_QUESTION)
    assert match is not None
    assert match.slug == _KNOWN_SKILL
    assert match.score >= 0.85


def test_match_skill_below_threshold_returns_none() -> None:
    assert injection.match_skill(_UNRELATED_TEXT) is None


def test_match_skill_off_short_circuits_without_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_SKILL_INJECT", "off")

    def _boom() -> Any:
        raise AssertionError("must not build an embedder when injection is off")

    monkeypatch.setattr(injection, "_default_embedder", _boom)
    assert injection.match_skill(_KNOWN_QUESTION) is None


def test_match_skill_empty_task_text_returns_none() -> None:
    assert injection.match_skill("") is None
    assert injection.match_skill("   ") is None


def test_threshold_env_override_blocks_a_perfect_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An impossible-to-clear threshold blocks even a 1.0-scored match —
    # proves the env override is actually read, not just the default.
    monkeypatch.setenv("PRECIS_SKILL_INJECT_THRESHOLD", "2.0")
    assert injection.match_skill(_KNOWN_QUESTION) is None


def test_threshold_env_override_admits_a_lowered_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A trivially-low threshold admits even the unrelated text's
    # best-scoring (near-zero, but > -1) hit.
    monkeypatch.setenv("PRECIS_SKILL_INJECT_THRESHOLD", "-1.0")
    assert injection.match_skill(_UNRELATED_TEXT) is not None


def test_get_index_caches_embedder_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOW pre-ship finding: a failed embedder construction wasn't cached,
    so every ``match_skill`` call retried the full (expensive, possibly
    model-loading) construction. The failure must be cached the first
    time (with one warning) so a subsequent call returns ``None`` cheaply,
    without invoking the embedder factory again."""
    calls = {"n": 0}

    def _boom() -> Any:
        calls["n"] += 1
        return None

    monkeypatch.setattr(injection, "_default_embedder", _boom)

    assert injection._get_index() is None
    assert calls["n"] == 1
    assert injection._get_index() is None
    assert calls["n"] == 1  # not retried — the failure sentinel short-circuits

    # The test-reset pattern the other tests rely on (autouse fixture,
    # per-test fresh index) must also clear the failure sentinel, not
    # just the index — otherwise a failure cached by one test would leak
    # into the next test's assertions.
    injection._reset_index_for_tests()
    assert injection._get_index() is None
    assert calls["n"] == 2


def test_match_skill_not_lost_behind_decoy_structural_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEDIUM pre-ship finding: ``match_skill`` ranks every chunk variant
    and only filters down to the question/heading-shaped
    ``_TARGET_VARIANTS`` *after* ranking — a fixed, small over-fetch
    window can be filled entirely by higher-or-tied-scoring structural
    chunks before the filter ever sees the real target-variant hit.

    Builds a synthetic corpus (patched into ``precis.handlers.skill``'s
    loaders — the same functions both ``injection._get_index`` and
    ``injection._search_page_size`` read from) of many decoy skills whose
    sole chunk is an EXACT copy of the query text (MockEmbedder scores an
    exact copy at self-similarity 1.0, same trick the module-level tests
    use) plus one target skill whose ``question_only`` twin is also an
    exact copy — tied at 1.0, decoys inserted first. ``list.sort`` is
    stable, so decoys sort strictly ahead of the tied target chunk. With
    the current corpus-proportional window the target still surfaces;
    monkeypatching ``_search_page_size`` down to the old fixed constant
    (50) with more than 50 decoys reproduces the bug this fix closes.
    """
    import precis.handlers.skill as skill_mod

    query = "the bespoke bimodal decoy regression phrase xyzzy plugh"
    n_decoys = 60
    target_slug = "zzz-decoy-regression-target"

    files: dict[str, str] = {}
    for i in range(n_decoys):
        # No H2 -> one structural chunk whose text is the body verbatim;
        # body == query exactly -> cosine self-similarity 1.0.
        files[f"decoy-{i:03d}"] = query
    files[target_slug] = f"---\nsummary: {query}\n---\n\n# Target\n\nunrelated body.\n"
    slugs = list(files)

    monkeypatch.setattr(skill_mod, "_list_skills", lambda: slugs)
    monkeypatch.setattr(skill_mod, "_load_skill", lambda slug: files.get(slug))

    # Fixed (corpus-proportional) window: comfortably covers every decoy
    # plus the target -> the target-variant hit is found.
    match = injection.match_skill(query)
    assert match is not None
    assert match.slug == target_slug

    # Simulate the pre-fix fixed window (_SEARCH_PAGE_SIZE = 50): the 60
    # tied decoys (inserted before the target, stable-sorted ahead of it)
    # fill the window completely and the real hit never surfaces.
    monkeypatch.setattr(injection, "_search_page_size", lambda: 50)
    assert injection.match_skill(query) is None


def test_task_injection_fuses_sections_across_sentences() -> None:
    selection = injection.match_sections(f"{_KNOWN_QUESTION} {_UNRELATED_TEXT}.")
    rendered = injection.render_section_injection(selection)
    assert "section matches across 2 phrasings" in rendered
    assert f"{_KNOWN_SKILL}~" in rendered
    assert "\tq1\t" in rendered
    assert "\tq2\t" not in rendered


def test_two_subtasks_both_winners_represented() -> None:
    """gr702892 acceptance: two distinct sub-tasks in one task text both
    land in the injection — each sub-question's top section is a winner
    and winners can't be cut before fisheye filler."""
    selection = injection.match_sections(f"{_KNOWN_QUESTION} {_KNOWN_QUESTION_2}")
    assert selection.n_queries == 2
    slugs = {m.slug for m in selection.matches}
    assert _KNOWN_SKILL in slugs
    assert _KNOWN_SKILL_2 in slugs
    winners = {m.slug for m in selection.matches if m.winner}
    assert _KNOWN_SKILL in winners
    assert _KNOWN_SKILL_2 in winners
    # Winners order before filler, so the byte budget can never drop one.
    first_filler = next(
        (i for i, m in enumerate(selection.matches) if not m.winner), None
    )
    if first_filler is not None:
        assert all(m.winner for m in selection.matches[:first_filler])
    rendered = injection.render_section_injection(selection)
    assert f"{_KNOWN_SKILL}~" in rendered
    assert f"{_KNOWN_SKILL_2}~" in rendered
    # The injected block stays under the whole-skill size cap.
    assert len(rendered.encode("utf-8")) <= injection._MAX_INJECT_BYTES


def test_render_section_injection_counts_bytes_not_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-land review (td707050): the byte budget was a BYTE budget
    (``DEFAULT_MAX_BODY_BYTES - 2048``, the value ``test_skill_size.py``
    enforces as ``st_size`` bytes) but capped on ``len(block)`` chars —
    non-ASCII bodies could push the encoded block past the whole-skill
    ceiling. Two 450-char bodies of ``é`` are 900 bytes each: a cap that
    admits exactly one under byte counting would admit both under a char
    count."""
    from precis.handlers.skill import _SkillSearchRow

    row = _SkillSearchRow(
        slug="s", score=1.0, source="semantic", section="~0 — H", snippet="word"
    )
    heavy = "é" * 450  # 450 chars, 900 UTF-8 bytes
    matches = tuple(
        injection.SectionMatch(
            slug="s", chunk_idx=0, row=row, attribution=f"q{i}", body=heavy, winner=True
        )
        for i in (1, 2)
    )
    selection = injection.SectionSelection(matches, 2)

    full = injection.render_section_injection(selection)
    assert full.count(heavy) == 2
    head_bytes = len(full.encode("utf-8")) - 2 * (len(heavy.encode("utf-8")) + 2)

    # Byte counting: one 900-byte body + its separator fits, two don't.
    # Char counting would fit both (2 × 452 chars < head + 1200 when the
    # head is ASCII), so a char-capped renderer emits two bodies here.
    monkeypatch.setattr(injection, "_MAX_INJECT_BYTES", head_bytes + 1200)
    rendered = injection.render_section_injection(selection)
    assert rendered.count(heavy) == 1
    assert len(rendered.encode("utf-8")) <= head_bytes + 1200


def test_match_sections_dedups_resolved_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-land review (td707050): deduping on (slug, heading) let one
    physical merged-alias section inject twice under different alias
    spellings — a heading_only/body_only hit keys it by one alias while
    a question_only→structural lookup keys it by the joined heading, and
    both resolve to the same chunk. Dedup runs on the resolved body, so
    two different keys whose renders are identical collapse to one
    injection."""
    import precis.handlers.skill as skill_mod
    from precis.skill_index.index import SearchHit

    class _FakeIndex:
        def is_available(self) -> bool:
            return True

        def search(
            self, q: str, page_size: int, build_wait_s: float | None = None
        ) -> list[SearchHit]:
            return [
                SearchHit(
                    slug="alias-skill",
                    chunk_idx=3,
                    heading="Search",
                    score=1.0,
                    snippet="body",
                    variant="heading_only",
                )
            ]

    class _FakeHandler:
        def __init__(self, hub: Any) -> None:
            pass

        def _fused_leg_rows(
            self,
            q: str,
            *,
            mode_norm: str,
            semantic_hits: list[SearchHit] | None = None,
        ) -> list[tuple[tuple[str, int | None], Any]]:
            idx = 0 if q.startswith("Alpha") else 1
            row = skill_mod._SkillSearchRow(
                slug="alias-skill",
                score=1.0,
                source="semantic",
                section=f"~{idx} — Search",
                snippet="body",
            )
            return [(("alias-skill", idx), row)]

        def get(self, id: str) -> Any:
            class _Resp:
                body = "the one physical section body"

            return _Resp()

    monkeypatch.setattr(injection, "_get_index", lambda: _FakeIndex())
    monkeypatch.setattr(skill_mod, "SkillHandler", _FakeHandler)

    selection = injection.match_sections("Alpha one? Beta two?")
    assert selection.n_queries == 2
    assert len(selection.matches) == 1
    assert selection.matches[0].body == "the one physical section body"
    rendered = injection.render_section_injection(selection)
    assert rendered.count("the one physical section body") == 1


# ── wiring: workers/planner_prompt.py ────────────────────────────────────


def test_planner_prompt_injects_matched_skill(
    monkeypatch: pytest.MonkeyPatch, hub: Hub
) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    marker = "MARKER-INJECTED-SKILL-BLOCK-PLANNER"
    monkeypatch.setattr(injection, "render_section_injection", lambda sel: marker)

    todo = hub.live_store.insert_ref(kind="todo", slug=None, title="do a thing")
    prompts = build_planner_prompts(hub.live_store, ref_id=todo.id, model="opus")
    assert marker in prompts.user
    # Injection lives in the per-tick (variable) layer, not the cached
    # system prefix shared across every tick.
    assert marker not in prompts.system


def test_planner_prompt_no_injection_when_no_match(
    monkeypatch: pytest.MonkeyPatch, hub: Hub
) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    monkeypatch.setattr(
        injection, "match_sections", lambda text: injection.SectionSelection((), 0)
    )

    todo = hub.live_store.insert_ref(kind="todo", slug=None, title="do a thing")
    prompts = build_planner_prompts(hub.live_store, ref_id=todo.id, model="opus")
    assert "Auto-matched skill" not in prompts.user


# ── wiring: quest/tick.py ─────────────────────────────────────────────────


def test_quest_tick_prompt_injects_matched_skill(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    from precis.quest.tick import build_tick_prompt

    marker = "MARKER-INJECTED-SKILL-BLOCK-QUEST"
    monkeypatch.setattr(injection, "render_section_injection", lambda sel: marker)

    qid = _mk_quest(store, "A striving toward a catalyst")
    qref = store.get_ref(kind="quest", id=qid)
    prompt = build_tick_prompt(store, qref)
    assert marker in prompt


def test_quest_tick_prompt_no_injection_when_no_match(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    from precis.quest.tick import build_tick_prompt

    monkeypatch.setattr(
        injection, "match_sections", lambda text: injection.SectionSelection((), 0)
    )

    qid = _mk_quest(store, "A striving toward a catalyst")
    qref = store.get_ref(kind="quest", id=qid)
    prompt = build_tick_prompt(store, qref)
    assert "Auto-matched skill" not in prompt


def test_quest_tick_prompt_survives_injection_failure(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    """HIGH pre-ship finding: injection.py's module docstring promises "a
    harness-controlled tick must never fail because injection couldn't
    run" — but the tick call site (``_skill_injection_section``) had no
    guard of its own, unlike the planner path (safe via the prompt
    assembler's module-level try/except around each ``build``). Any
    exception out of the section injection must degrade to
    no injection, not abort the tick."""
    from precis.quest.tick import build_tick_prompt

    def _boom(text: str) -> Any:
        raise RuntimeError("boom — simulated section injection failure")

    monkeypatch.setattr(injection, "match_sections", _boom)

    qid = _mk_quest(store, "A striving toward a catalyst")
    qref = store.get_ref(kind="quest", id=qid)
    prompt = build_tick_prompt(store, qref)
    assert prompt
    assert "Auto-matched skill" not in prompt
