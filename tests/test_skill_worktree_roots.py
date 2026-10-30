"""Git-worktree skill roots — ``wt-<tree>/<slug>`` preview (gr541511).

Mirrors ``tests/test_md_worktree_roots.py``: the same
``WorktreeRegistry`` discovery, the same ``wt-<tree>`` aliases —
serving a checkout's ``src/precis/data/skills/`` corpus so an unlanded
skill's render (body, ``~N``, ``/toc``) is checkable before it ships.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._python_worktrees import WorktreeRegistry
from precis.handlers.skill import SkillHandler

HOST = "/hostprefix"

_SKILL = "---\ntitle: Worktree skill\n---\n# Wt\nunlanded body\n\n## One\nsec one\n\n## Two\nsec two\n"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _checkout(path: Path) -> None:
    skills = path / "src" / "precis" / "data" / "skills"
    skills.mkdir(parents=True)
    (path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (skills / "wt-only-help.md").write_text(_SKILL, encoding="utf-8")


def _register(main: Path, ident: str, host_path: str) -> None:
    d = main / ".git" / "worktrees" / ident
    d.mkdir(parents=True)
    (d / "gitdir").write_text(f"{host_path}/.git\n", encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path):
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    wts = tmp_path / "wts"
    return main, wts, tmp_path


def _make(env, names=("alpha",)):
    main, wts, tmp = env
    for n in names:
        _checkout(wts / n)
        _register(main, n, f"{HOST}/wts/{n}")
    reg = WorktreeRegistry("wt", main, gitdir_map=(HOST, str(tmp)), clock=Clock())
    return SkillHandler(hub=Hub(), worktrees=reg)


def test_worktree_skill_serve(env) -> None:
    h = _make(env)
    body = h.get(id="wt-alpha/wt-only-help").body
    assert "unlanded body" in body
    assert body.rstrip().splitlines()[-1].startswith("skill rev ")


def test_worktree_skill_chunks_and_toc(env) -> None:
    h = _make(env)
    chunk = h.get(id="wt-alpha/wt-only-help~2").body
    assert "sec two" in chunk
    assert "wt-alpha/wt-only-help/toc" in chunk  # trailer keeps the root
    toc = h.get(id="wt-alpha/wt-only-help/toc").body
    assert "wt-alpha/wt-only-help~0" in toc
    # The packaged corpus is untouched by the root — same slug, no file.
    with pytest.raises(NotFound):
        h.get(id="wt-only-help")


def test_worktree_alias_lists_slugs(env) -> None:
    h = _make(env)
    body = h.get(id="wt-alpha").body
    assert "wt-only-help" in body


def test_worktree_edits_visible_without_cache(env) -> None:
    """The corpus rebuilds per call — the point is previewing edits."""
    h = _make(env)
    h.get(id="wt-alpha/wt-only-help")
    main, wts, _tmp = env
    target = wts / "alpha" / "src" / "precis" / "data" / "skills" / "wt-only-help.md"
    target.write_text(_SKILL.replace("unlanded body", "edited body"), encoding="utf-8")
    assert "edited body" in h.get(id="wt-alpha/wt-only-help").body


def test_unknown_worktree_refusal(env) -> None:
    h = _make(env)
    with pytest.raises(NotFound, match="not configured"):
        h.get(id="wt-ghost/wt-only-help")


def test_no_registry_names_the_wiring(tmp_path: Path) -> None:
    """Without a WorktreeRegistry a wt-* id fails with the env var named."""
    h = SkillHandler(hub=Hub())
    with pytest.raises(NotFound, match="PRECIS_PYTHON_WORKTREES"):
        h.get(id="wt-alpha/wt-only-help")


def test_non_worktree_view_grammar_unchanged(env) -> None:
    h = _make(env)
    with pytest.raises(BadInput, match="unknown skill view"):
        h.get(id="precis-overview/bogus")


def test_tilde_placeholder_error_names_digits(env) -> None:
    h = _make(env)
    with pytest.raises(BadInput, match="~<n>"):
        h.get(id="precis-overview~N")
