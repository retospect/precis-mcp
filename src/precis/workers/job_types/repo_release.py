"""``repo_release``: cut a release of a second repo (td659522 step 3).

The job will bump ``[project].version`` in the repo's ``pyproject.toml`` and
the project's own entry in ``uv.lock``, commit and push that, wait for the CI
run on the pushed commit, and tag it when the run is green. The profile
fields it reads are ``RepoProfile.releaser`` / ``release_check_cmds`` /
``ci_workflow`` / ``tag_prefix`` (``precis.plans.lanes``).

This round holds only the pure helpers: no I/O, no git, no network, no store,
and no job type is registered. The rule they serve: the committed bytes are
produced by these text edits and never by running a tool in the tree (no
``uv lock``, no ``uv version``), so the diff is exactly the version lines and
each edit is checked by re-parsing both texts with ``tomllib``.
"""

from __future__ import annotations

import copy
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "ReleaseError",
    "bump_version",
    "ci_verdict",
    "pick_bump",
    "pick_run",
    "project_name_version",
    "set_lock_version",
    "set_project_version",
    "tag_name",
]


class ReleaseError(ValueError):
    """A release helper refused its input; the message is one line."""


_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_FEAT = re.compile(r"feat[(:!]")
#: A TOML table header line: ``[a.b]`` or ``[[a.b]]`` with an optional
#: comment. No ``=`` and no nested brackets, so an array continuation such
#: as ``[1, 2],`` inside a multi-line value does not match.
_HEADER = re.compile(r"\s*(\[\[?)\s*([^\[\]=]*?)\s*\]\]?\s*(?:#.*)?")
_VERSION_LINE = re.compile(r"(\s*version\s*=\s*)([\"'])([^\"'\r\n]*)\2(.*)", re.DOTALL)
_NAME_LINE = re.compile(r"\s*name\s*=\s*\"([^\"\r\n]*)\"\s*(?:#.*)?")
_LOCAL_SOURCE = re.compile(
    r"\s*source\s*=\s*\{\s*(?:editable|virtual)\s*=\s*\"\.\"\s*\}\s*(?:#.*)?"
)


def _split_version(current: str) -> tuple[int, int, int]:
    m = _SEMVER.fullmatch(current)
    if m is None:
        raise ReleaseError(f"version {current!r} is not exactly X.Y.Z")
    return int(m[1]), int(m[2]), int(m[3])


def bump_version(current: str, bump: str) -> str:
    """``current`` (exactly ``X.Y.Z``) bumped: ``minor`` is X.Y+1.0, ``patch``
    is X.Y.Z+1. Never major."""
    x, y, z = _split_version(current)
    if bump == "minor":
        return f"{x}.{y + 1}.0"
    if bump == "patch":
        return f"{x}.{y}.{z + 1}"
    raise ReleaseError(f"bump {bump!r} is not 'minor' or 'patch'")


def pick_bump(subjects: Iterable[str]) -> str:
    """``minor`` when any subject is a conventional ``feat`` (``feat`` then
    ``(``, ``:`` or ``!``), else ``patch``."""
    return "minor" if any(_FEAT.match(s) for s in subjects) else "patch"


def tag_name(profile_tag_prefix: str, version: str) -> str:
    """The release tag for ``version`` (must be ``X.Y.Z``)."""
    _split_version(version)
    return f"{profile_tag_prefix}{version}"


def _parse(text: str, what: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(f"{what} does not parse as TOML: {exc}") from exc


def _replace_version(line: str, new: str) -> str | None:
    """``line`` with its quoted version value replaced, quote style, indent,
    trailing comment and line ending kept; ``None`` when it is no version
    line."""
    m = _VERSION_LINE.fullmatch(line)
    if m is None:
        return None
    rest = m[4]
    return f"{m[1]}{m[2]}{new}{m[2]}{rest}"


def set_project_version(pyproject_text: str, new: str) -> str:
    """``pyproject_text`` with the one static ``version`` line of the
    ``[project]`` table set to ``new``, every other byte kept."""
    _split_version(new)
    old = _parse(pyproject_text, "pyproject.toml")
    lines = pyproject_text.splitlines(keepends=True)
    in_project = False
    hits: list[int] = []
    for i, line in enumerate(lines):
        h = _HEADER.fullmatch(line.rstrip("\r\n"))
        if h is not None:
            in_project = h[1] == "[" and h[2] == "project"
            continue
        if in_project and _VERSION_LINE.fullmatch(line) is not None:
            hits.append(i)
    if not hits:
        raise ReleaseError("[project] has no static version line")
    if len(hits) > 1:
        raise ReleaseError("[project] has more than one version line")
    replaced = _replace_version(lines[hits[0]], new)
    assert replaced is not None  # matched above
    lines[hits[0]] = replaced
    out = "".join(lines)
    expect = copy.deepcopy(old)
    expect["project"]["version"] = new
    if _parse(out, "edited pyproject.toml") != expect:
        raise ReleaseError("pyproject edit changed more than project.version")
    return out


def project_name_version(pyproject_text: str) -> tuple[str, str]:
    """``([project].name, [project].version)`` as strings."""
    project = _parse(pyproject_text, "pyproject.toml").get("project")
    if not isinstance(project, dict):
        raise ReleaseError("pyproject.toml has no [project] table")
    name, version = project.get("name"), project.get("version")
    if not isinstance(name, str):
        raise ReleaseError("[project].name is missing or not a string")
    if not isinstance(version, str):
        raise ReleaseError("[project].version is missing or not a string")
    return name, version


def _norm(name: str) -> str:
    """PEP 503 name normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def set_lock_version(lock_text: str, package: str, new: str) -> str:
    """``lock_text`` (a uv lockfile) with the version line of the project's
    own ``[[package]]`` block (name equal to ``package`` after PEP 503
    normalisation, source ``{ editable = "." }`` or ``{ virtual = "." }``) set
    to ``new``, every other byte kept."""
    _split_version(new)
    old = _parse(lock_text, "uv.lock")
    want = _norm(package)
    lines = lock_text.splitlines(keepends=True)
    # (index of the [[package]] block among all of them, version line index)
    hits: list[tuple[int, int | None]] = []
    block = -1
    name: str | None = None
    local = False
    ver: int | None = None
    active = False  # inside the top-level keys of a [[package]] block

    def close() -> None:
        if active and name is not None and _norm(name) == want and local:
            hits.append((block, ver))

    for i, line in enumerate(lines):
        h = _HEADER.fullmatch(line.rstrip("\r\n"))
        if h is not None:
            close()
            active = h[1] == "[[" and h[2] == "package"
            if active:
                block += 1
            name, local, ver = None, False, None
            continue
        if not active:
            continue
        m = _NAME_LINE.fullmatch(line.rstrip("\r\n"))
        if m is not None:
            name = m[1]
        elif _LOCAL_SOURCE.fullmatch(line.rstrip("\r\n")) is not None:
            local = True
        elif _VERSION_LINE.fullmatch(line) is not None:
            ver = i
    close()
    if not hits:
        raise ReleaseError(f"uv.lock has no local package block for {package!r}")
    if len(hits) > 1:
        raise ReleaseError(f"uv.lock has more than one local block for {package!r}")
    block_no, ver = hits[0]
    if ver is None:
        raise ReleaseError(f"uv.lock block for {package!r} has no version line")
    replaced = _replace_version(lines[ver], new)
    assert replaced is not None  # matched above
    lines[ver] = replaced
    out = "".join(lines)
    expect = copy.deepcopy(old)
    try:
        expect["package"][block_no]["version"] = new
    except (KeyError, IndexError, TypeError) as exc:
        raise ReleaseError("uv.lock package list does not match its blocks") from exc
    if _parse(out, "edited uv.lock") != expect:
        raise ReleaseError("uv.lock edit changed more than one block's version")
    return out


def pick_run(
    runs: Sequence[Mapping[str, Any]], *, sha: str, workflow: str
) -> Mapping[str, Any] | None:
    """From a "list workflow runs" payload's ``workflow_runs``: the run on
    exactly ``sha`` of the workflow file ``workflow``; the highest
    ``run_attempt`` then the highest ``id`` when several match; ``None`` when
    none do."""
    found = [
        r
        for r in runs
        if r.get("head_sha") == sha
        and isinstance(r.get("path"), str)
        and (r["path"] == workflow or r["path"].endswith("/" + workflow))
    ]
    if not found:
        return None
    return max(
        found,
        key=lambda r: (int(r.get("run_attempt") or 1), int(r.get("id") or 0)),
    )


def ci_verdict(run: Mapping[str, Any] | None, *, rerun_attempt_before: int = 0) -> str:
    """``pending``, ``green``, ``cancelled`` or ``red`` for a picked run. A
    completed run whose ``run_attempt`` is at or below ``rerun_attempt_before``
    is still pending: a re-run was requested and its new attempt has not shown
    up (gr588028, see ``ci_verdict_poll``)."""
    if run is None or run.get("status") != "completed":
        return "pending"
    if int(run.get("run_attempt") or 1) <= rerun_attempt_before:
        return "pending"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "green"
    if conclusion == "cancelled":
        return "cancelled"
    return "red"
