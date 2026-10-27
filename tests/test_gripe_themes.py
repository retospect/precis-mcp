"""Recurring-theme merge + observation filing (gr514477 batch follow-up).

``put(kind='gripe')`` create runs :mod:`precis.handlers._gripe_themes`:
one theme match + a live ``theme:<slug>`` keeper folds the filing into a
``+1`` comment; one match + no keeper tags the new gripe as the keeper;
several matches or ``args={'new': True}`` file normally.
``args={'observation': True}`` files the gripe already closed
(``STATUS:done`` + ``observation``) through the ordinary
resolved-comment + tag path.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import CallToolResult

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._gripe_themes import THEMES, match_themes
from precis.handlers.gripe import GripeHandler
from precis.runtime import PrecisRuntime
from precis.tools import core as tools_core

#: A create text that hits exactly the ``test-speed`` theme.
TEST_SPEED_TEXT = (
    "scripts/test tests/test_no_db.py still queues — and starts Postgres, "
    "named-file runs wait ~25s on container bring-up"
)

#: Real title + first body line per seeded example gripe (prod, fetched
#: 2026-10-24). Every example must hit exactly its own slug — a second
#: hit would mean the filing takes the no-merge multi-match path.
_EXAMPLES: dict[str, dict[int, str]] = {
    "test-speed": {
        514478: 'scripts/test single file waits ~25s container bring-up\ntesting | scripts/test <file> -q | actual: ~25 s of container bring-up ("bringing up nodes") before collection',
        514483: "named no-DB test file still queues and starts Postgres\ntesting | scripts/test tests/test_no_db.py | actual: named-file runs still queue and start Postgres",
        514502: "scripts/test tests/workspaces backgrounds on idle\nharness | scripts/test tests/workspaces | actual: backgrounds after ~30 s idle",
        514875: "uv run --frozen python -c takes ~17s vs 0.7s venv\nenv | uv run --frozen python -c … on the host | actual: ~17 s first call vs 0.7 s via .venv/bin/python",
    },
    "commit-heredoc": {
        514943: "Guard refusal on $(cat) commit reads as syntax error\n`git commit -m \"$(cat <<'EOF' ...)\"` is killed at parse time by the worktree guard",
        514972: "Canonical commit example trips worktree guard\nthe canonical commit example, `git commit -m \"$(cat <<'EOF' ...)\"`, is exactly the shape the worktree guard refuses",
        514895: 'prod-psql: commit msg with quotes fails heredoc EOF\nprod-psql | git commit -m "$(cat <<\'EOF\' ...)" with a body containing both " and \' fails with "unexpected EOF"',
        514617: 'git commit -m $(cat heredoc) fails on apostrophes\ntooling | git commit -m "$(cat <<\'EOF\' …)" with apostrophes | actual: bash "unexpected EOF"; needed git commit -F',
        514743: 'commit -m with backticks gets command-substituted\nworkflow | git commit -m "... ```text ..." | actual: the shell command-substituted the backticks and mangled the body',
    },
    "host-mypy": {
        514869: "host mypy reports import-untyped on precis_se.*\ntyping | uv run --frozen mypy tests/test_se_terminate.py on the host | actual: 13 import-untyped errors on precis_se.*",
        515069: "container mypy INTERNAL ERROR from shared .mypy_cache\nrunning host `uv run --frozen mypy` then `scripts/test --typecheck` makes container mypy exit 2: the bind-mounted worktree shares .mypy_cache",
        514897: "tests: host mypy false-fails on missing jinja2 stubs\ntests | host `uv run --frozen mypy` false-fails on missing jinja2 stubs; the container typecheck is fine.",
        515001: "host mypy phantom torch/transformers import-not-found\n`uv run --frozen mypy src/precis/ingest/marker.py` on the host reports phantom import-not-found errors",
    },
    "inflight-slow": {
        514898: "worktree: inflight slow and overflows to 140+ trees\nworktree | scripts/inflight takes >10s and prints 140+ worktrees",
        514784: "scripts/inflight takes >10s before output when cold\ninfra | scripts/inflight | actual: >10 s before output on a cold call",
        514947: "inflight takes >10s before first output\nscripts/inflight takes >10 s before first output.",
    },
    "rtk-rewrite": {
        514332: "rtk digests mangle code identifiers in rg/sed output\nrtk digests mangle code identifiers run via exec, e.g. revision became ren",
        514780: "rtk rewrites grep output, silently altering matches\nsearch | rg/grep output via rtk | actual: output silently rewritten (source_role -> n)",
        514944: "rtk rewrites identifiers in rg output\nrg output through rtk rewrites identifiers (geom becomes n)",
    },
    "skill-size-cap": {
        515088: "precis-paper-help near 32KB cap; trims needed per op\nskills | precis-paper-help sat at 31,883 of 32,768 bytes; the test_skill_size cap is brittle for growing skills.",
        486399: "precis-pcb-help is 33.6 KB, over the 32 KB skill cap\nallowlisted in tests/test_skill_size.py::_ALLOWLIST as a short-term fix.",
        472110: "precis-draft-help is 9 bytes under the 32 KB skill cap; the next sentence added to it reddens main.",
        514415: "precis-se-help at 32 KB cap; any pointer line overflows\nthe file was 4 bytes under the 32 KB cap on main",
        510535: "red main: precis-job-help over skill-size hard cap\n32956B vs FAIL_BYTES 32768 — tests/test_skill_size.py::test_skill_file_under_size_cap",
    },
    "devin-messaging": {
        514682: 'Legacy SendMessage footers in agent defs and brief\nlegacy footers still carry the old "SendMessage to coordinator" line',
        515003: "Devin builders lack a documented coordinator messaging path\nno documented way to message the coordinator; had to grep scripts/ for `scripts/fleet say -m`",
    },
    "brief-quality": {
        514903: 'Triage briefs omit each gripe\'s rulings\nthe gripe batch brief says "fixable without design" but the comments say Reto deferred the design',
        514870: "gripe-fix triage ignores parked-hexfold gripe state\nthe gripe-fix triage put it in a fix batch anyway",
        514946: "Brief ambiguous on where a refusal may live\nbriefs that constrain a file should name the allowed functions",
        514962: 'gripe-fix brief cites a design as an existing API\nthe brief said "must pass report_only=True" as though the affordance existed',
    },
    "mypy-noise": {
        514976: 'mypy prints 40-line pyproject unused-section note\nevery `uv run --frozen mypy <files>` prints a ~40-line pyproject "unused section(s)" note',
        514997: "ruff flags extensionless bash scripts as Python syntax\n`uv run ruff check scripts/prod-precis` reports 138 invalid-syntax errors",
    },
    "dev-db-env": {
        514999: 'precis post-migration-check cannot find dev DB URL\n`scripts/dev uv run precis post-migration-check` fails with "no database_url configured"',
    },
}

#: Five unrelated real gripe titles — none may match any theme.
_UNRELATED = [
    "se: _VIEW_ARGS and spec.views miss dispatched views",
    "quest-need: structured kinetics thermochemistry data lookup",
    "paper slug NotFound does not surface near-match options",
    "auth handler chokes on empty cookies",
    "Precis MCP responses carry repeated boilerplate",
]


@pytest.fixture
def gripe(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _ids(g: GripeHandler) -> set[int]:
    return {r.id for r in g.store.list_refs(kind="gripe", limit=200)}


def _tags(g: GripeHandler, gid: int) -> set[str]:
    return {str(t) for t in g.store.tags_for(gid)}


def _comments(g: GripeHandler, gid: int) -> list[str]:
    return [c.text for c in g._comments(gid)]


def _keeper(g: GripeHandler, slug: str = "test-speed") -> int:
    g.put(text="Synthetic keeper gripe", tags=[f"theme:{slug}"])
    return max(_ids(g))


# ── pattern coverage ─────────────────────────────────────────────────


def test_registry_covers_the_seeded_slugs() -> None:
    assert sorted(THEMES) == sorted(_EXAMPLES)


@pytest.mark.parametrize("slug", sorted(_EXAMPLES))
def test_each_slug_matches_its_examples_and_only_its(slug: str) -> None:
    for gid, text in _EXAMPLES[slug].items():
        assert match_themes(text) == [slug], f"gr{gid} matched {match_themes(text)}"


def test_unrelated_titles_match_nothing() -> None:
    for title in _UNRELATED:
        assert match_themes(title) == [], f"{title!r} matched {match_themes(title)}"


# ── merge / keeper behaviour ──────────────────────────────────────────


def test_no_keeper_files_gripe_tagged_as_keeper(gripe: GripeHandler) -> None:
    out = gripe.put(text=TEST_SPEED_TEXT)
    gid = next(iter(_ids(gripe)))
    assert "theme:test-speed" in _tags(gripe, gid)
    assert "theme keeper" in out.body


def test_match_with_open_keeper_merges_as_plus1_comment(gripe: GripeHandler) -> None:
    keeper = _keeper(gripe)
    before = _ids(gripe)
    out = gripe.put(text=TEST_SPEED_TEXT)
    assert _ids(gripe) == before, "merge must not mint a new ref"
    comments = _comments(gripe, keeper)
    # Single-line filing: no ``(first line)`` parenthetical — it would
    # repeat the whole text verbatim (gr515447).
    assert comments[-1] == f"+1: {TEST_SPEED_TEXT}"
    assert f"merged into gr{keeper} (theme test-speed)" in out.body
    assert "args={'new': True}" in out.body


def test_multi_line_merge_keeps_first_line_gist(gripe: GripeHandler) -> None:
    """A multi-line filing still gets the ``+1 (<first line>):`` shape —
    the parenthetical is the gist a skimming reader needs."""
    keeper = _keeper(gripe)
    text = (
        "scripts/test named-file runs wait on Postgres bring-up\n"
        "testing | scripts/test tests/test_no_db.py | actual: ~25s before collection"
    )
    assert match_themes(text) == ["test-speed"]
    gripe.put(text=text)
    comment = _comments(gripe, keeper)[-1]
    assert comment.startswith("+1 (scripts/test named-file runs wait")
    assert comment.endswith(text)


def test_multi_match_files_separately_and_names_themes(gripe: GripeHandler) -> None:
    text = "scripts/test --typecheck is slow; host mypy reports import-untyped"
    assert sorted(match_themes(text)) == ["host-mypy", "test-speed"]
    out = gripe.put(text=text)
    gid = next(iter(_ids(gripe)))
    assert "theme:test-speed" not in _tags(gripe, gid)
    assert "host-mypy" in out.body and "test-speed" in out.body
    assert "no merge" in out.body


def test_new_true_bypasses_matching(gripe: GripeHandler) -> None:
    keeper = _keeper(gripe)
    before = _ids(gripe)
    out = gripe.put(text=TEST_SPEED_TEXT, new=True)
    assert len(_ids(gripe)) == len(before) + 1
    gid = max(_ids(gripe) - before)
    assert "theme:test-speed" not in _tags(gripe, gid)
    assert not _comments(gripe, keeper)
    assert "created gripe" in out.body


def test_closed_keeper_means_new_filing_becomes_keeper(gripe: GripeHandler) -> None:
    keeper = _keeper(gripe)
    gripe.put(id=keeper, text="resolved: wontfix — parked", tags=["STATUS:wontfix"])
    out = gripe.put(text=TEST_SPEED_TEXT)
    gid = next(i for i in _ids(gripe) if i != keeper)
    assert "theme:test-speed" in _tags(gripe, gid)
    assert "theme keeper" in out.body


def test_merge_does_not_require_dogfood_or_agent_filing(gripe: GripeHandler) -> None:
    """A plain human filing (no dogfood tag) still merges — the rule is
    the same for everyone."""
    keeper = _keeper(gripe)
    gripe.put(text=TEST_SPEED_TEXT)
    assert _comments(gripe, keeper)[-1].startswith("+1: ")


def test_merge_refuses_prio_with_the_tag_door(gripe: GripeHandler) -> None:
    keeper = _keeper(gripe)
    with pytest.raises(BadInput) as ei:
        gripe.put(text=TEST_SPEED_TEXT, prio=3)
    assert f"tag(kind='gripe', id={keeper}, prio=3)" in str(ei.value.next)


def test_new_and_observation_are_create_only(gripe: GripeHandler) -> None:
    gid = _keeper(gripe)
    kws: list[dict[str, Any]] = [{"new": True}, {"observation": True}]
    for kw in kws:
        with pytest.raises(BadInput):
            gripe.put(id=gid, text="a comment", **kw)


# ── observation ───────────────────────────────────────────────────────


def test_observation_files_closed_and_tagged(gripe: GripeHandler) -> None:
    out = gripe.put(
        text="note: rtk hides pytest durations in folded output", observation=True
    )
    gid = next(iter(_ids(gripe)))
    tags = _tags(gripe, gid)
    assert "STATUS:done" in tags and "STATUS:open" not in tags
    assert "observation" in tags
    assert _comments(gripe, gid)[-1].startswith("resolved:")
    assert "auto-closed" in out.body and "reopen" in out.body


def test_observation_reopen_recipe_works(gripe: GripeHandler) -> None:
    gripe.put(text="note: something", observation=True)
    gid = next(iter(_ids(gripe)))
    gripe.tag(id=gid, add=["STATUS:open"], remove=["STATUS:done"])
    assert "STATUS:open" in _tags(gripe, gid)


# ── the MCP boundary: args= flattens into the put kwargs ─────────────


def _text(out: object) -> str:
    if isinstance(out, CallToolResult):
        first: Any = out.content[0]
        return str(first.text)
    return str(out)


def test_args_new_and_observation_reach_put(
    gripe: GripeHandler,
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools_core, "_runtime", runtime_with_store)
    out = tools_core.put(
        kind="gripe", text="note: observed once", args={"observation": True}
    )
    body = _text(out)
    assert "STATUS:done" in body and "auto-closed" in body

    keeper = _keeper(gripe)
    before = _ids(gripe)
    out = tools_core.put(kind="gripe", text=TEST_SPEED_TEXT, args={"new": True})
    body = _text(out)
    assert len(_ids(gripe)) == len(before) + 1, body
    assert "merged" not in body
