"""Pin: uv git fetches run with the GitHub fast path disabled (gr514979).

uv's GitHub fast path resolves git dependencies through api.github.com /
raw.githubusercontent.com UNAUTHENTICATED. For the private catpath repo
(``[tool.uv.sources]`` → ``retospect/catpath``) those calls 404 before uv
falls back to the git transport, and on public repos they burn the
60-req/h anonymous API quota for nothing. ``UV_NO_GITHUB_FAST_PATH=1``
(uv >= 0.7.13) forces every git fetch through plain git, where the host's
credential helper applies.

These tests pin that every docker/Dockerfile stage capable of fetching a
git dep (``uv sync``, or ``uv pip install`` of a ``git+``/``autocatpath``
URL) sees the variable — set in the stage itself or inherited from a
``FROM`` ancestor — and that the cluster autocatpath install task exports
it too. A full-sha ``rev`` in [tool.uv.sources] skips the API on its own;
the env is what protects non-sha refs like ``AUTOCATPATH_REV=main``.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO_ROOT / "docker" / "Dockerfile"
_ANSIBLE_TASKS = _REPO_ROOT / "deploy" / "roles" / "autocatpath" / "tasks" / "main.yml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

_FROM_RE = re.compile(r"^FROM\s+\S+\s+AS\s+([\w.-]+)", re.MULTILINE)
_PARENT_RE = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+\w+)?", re.MULTILINE)
_UV_GIT_FETCH_RE = re.compile(r"uv sync|uv pip install[^\n]*(?:git\+|autocatpath)")


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _stages() -> dict[str, tuple[str | None, str]]:
    """stage name -> (parent stage/image ref, stage body including its FROM).

    Bodies are de-commented and ``\\``-continuations joined, so regexes see
    logical instructions only.
    """
    text = _dockerfile()
    text = re.sub(r"\\\n", " ", text)
    text = re.sub(r"^#.*$", "", text, flags=re.MULTILINE)
    matches = list(_FROM_RE.finditer(text))
    stages: dict[str, tuple[str | None, str]] = {}
    for i, m in enumerate(matches):
        name = m.group(1)
        parent = _PARENT_RE.search(text, m.start())
        assert parent is not None
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        stages[name] = (parent.group(1), text[m.start() : end])
    return stages


def _env_pairs(body: str) -> dict[str, str]:
    """KEY=VALUE pairs set by ENV instructions in one stage body."""
    pairs: dict[str, str] = {}
    for m in re.finditer(r"^ENV\s+(.+)$", body, re.MULTILINE):
        pairs.update(re.findall(r"([A-Z_][A-Z0-9_]*)=([^\s]+)", m.group(1)))
    return pairs


def _fast_path_disabled(stage: str, stages: dict[str, tuple[str | None, str]]) -> bool:
    seen: set[str] = set()
    cur: str | None = stage
    while cur and cur in stages and cur not in seen:
        seen.add(cur)
        parent, body = stages[cur]
        if _env_pairs(body).get("UV_NO_GITHUB_FAST_PATH") == "1":
            return True
        cur = parent
    return False


def test_every_git_fetching_stage_disables_fast_path() -> None:
    """Any stage that can resolve a git dep must see UV_NO_GITHUB_FAST_PATH —
    set or inherited — so a private source 404s nowhere in the build."""
    stages = _stages()
    fetchers = [
        name for name, (_, body) in stages.items() if _UV_GIT_FETCH_RE.search(body)
    ]
    assert fetchers, "expected at least one uv-syncing stage in the Dockerfile"
    missing = [n for n in fetchers if not _fast_path_disabled(n, stages)]
    assert not missing, (
        f"stage(s) {missing} can fetch git deps without "
        "UV_NO_GITHUB_FAST_PATH set or inherited (gr514979)"
    )


def test_autocatpath_install_stage_covered() -> None:
    """The ``uv pip install ... catpath@${AUTOCATPATH_REV}`` layer must
    inherit the var — its ``@main`` fallback is the non-sha ref that
    actually consults the GitHub API."""
    stages = _stages()
    for name, (_, body) in stages.items():
        if "catpath@${AUTOCATPATH_REV}" in body:
            assert _fast_path_disabled(name, stages), (
                f"stage {name} installs autocatpath from git without "
                "UV_NO_GITHUB_FAST_PATH"
            )
            return
    raise AssertionError("no Dockerfile stage installs autocatpath from git")


def test_ansible_autocatpath_install_exports_var() -> None:
    """The cluster install task fetches precis-mcp over git; it must export
    the var so uv never leans on the anonymous GitHub API."""
    tasks = yaml.safe_load(_ANSIBLE_TASKS.read_text(encoding="utf-8"))
    installs = [
        t
        for t in tasks
        if "git+https://github.com/retospect" in str(t.get("ansible.builtin.shell", ""))
    ]
    assert installs, "expected a uv install of a github git URL in main.yml"
    for task in installs:
        env = task.get("environment") or {}
        assert env.get("UV_NO_GITHUB_FAST_PATH") == "1", (
            f"task {task.get('name')!r} must set UV_NO_GITHUB_FAST_PATH=1"
        )


def test_pyproject_documents_the_fetch_caveat() -> None:
    """The [tool.uv.sources] comment must name the escape hatch — this is
    where the next person adds the second git-sourced dep."""
    pyproject = _PYPROJECT.read_text(encoding="utf-8")
    idx = pyproject.index("\nautocatpath = { git")
    region = pyproject[max(0, idx - 3000) : idx]
    assert "UV_NO_GITHUB_FAST_PATH" in region, (
        "pyproject's uv.sources comment must document UV_NO_GITHUB_FAST_PATH (gr514979)"
    )
