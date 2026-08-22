"""Tests for bimodal skill injection (§2, ``docs/backlog/skill-question-
targets-and-injection.md``): :mod:`precis.skill_index.injection`'s
``match_skill``/``render_injection`` pair, plus the two harness-controlled
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
    injection._reset_index_for_tests()
    yield
    injection._reset_index_for_tests()


#: A real, stable question from ``precis-overview``'s seeded ``answers:``
#: front matter. Copied verbatim so MockEmbedder (hash-based, not
#: semantic — same input string always hashes to the same vector) scores
#: it at self-similarity 1.0 against the skill's own question_only chunk.
_KNOWN_QUESTION = "what verbs does precis support?"
_KNOWN_SKILL = "precis-overview"

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


# ── match_skill / render_injection ──────────────────────────────────────


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


def test_render_injection_is_whole_skill_no_truncation() -> None:
    match = injection.match_skill(_KNOWN_QUESTION)
    assert match is not None
    rendered = injection.render_injection(match)
    assert rendered.startswith(f"# Auto-matched skill: {_KNOWN_SKILL}")
    assert "auto" in rendered.lower() and "matched" in rendered.lower()
    # The skill's own body content survives verbatim, not just a snippet.
    assert "seven verbs" in rendered


# ── wiring: workers/planner_prompt.py ────────────────────────────────────


def test_planner_prompt_injects_matched_skill(
    monkeypatch: pytest.MonkeyPatch, hub: Hub
) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    marker = "MARKER-INJECTED-SKILL-BLOCK-PLANNER"
    fake_match = injection.SkillMatch(slug=_KNOWN_SKILL, title="t", score=0.99)
    monkeypatch.setattr(injection, "match_skill", lambda text: fake_match)
    monkeypatch.setattr(injection, "render_injection", lambda m: marker)

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

    monkeypatch.setattr(injection, "match_skill", lambda text: None)

    todo = hub.live_store.insert_ref(kind="todo", slug=None, title="do a thing")
    prompts = build_planner_prompts(hub.live_store, ref_id=todo.id, model="opus")
    assert "Auto-matched skill" not in prompts.user


# ── wiring: quest/tick.py ─────────────────────────────────────────────────


def test_quest_tick_prompt_injects_matched_skill(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    from precis.quest.tick import build_tick_prompt

    marker = "MARKER-INJECTED-SKILL-BLOCK-QUEST"
    fake_match = injection.SkillMatch(slug=_KNOWN_SKILL, title="t", score=0.99)
    monkeypatch.setattr(injection, "match_skill", lambda text: fake_match)
    monkeypatch.setattr(injection, "render_injection", lambda m: marker)

    qid = _mk_quest(store, "A striving toward a catalyst")
    qref = store.get_ref(kind="quest", id=qid)
    prompt = build_tick_prompt(store, qref)
    assert marker in prompt


def test_quest_tick_prompt_no_injection_when_no_match(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    from precis.quest.tick import build_tick_prompt

    monkeypatch.setattr(injection, "match_skill", lambda text: None)

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
    exception out of ``match_skill``/``render_injection`` must degrade to
    no injection, not abort the tick."""
    from precis.quest.tick import build_tick_prompt

    def _boom(text: str) -> Any:
        raise RuntimeError("boom — simulated match_skill failure")

    monkeypatch.setattr(injection, "match_skill", _boom)

    qid = _mk_quest(store, "A striving toward a catalyst")
    qref = store.get_ref(kind="quest", id=qid)
    prompt = build_tick_prompt(store, qref)
    assert prompt
    assert "Auto-matched skill" not in prompt
