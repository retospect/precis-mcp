"""Pure helpers of the second-repo release job (td659522 step 3, R4)."""

from __future__ import annotations

import tomllib
from typing import Any

import pytest

from precis.plans import lanes
from precis.workers.job_types import repo_release as rr
from precis.workers.job_types.repo_release import ReleaseError

PYPROJECT = """\
# header comment
[project]
name = "My_Pkg"   # the name
version = "1.2.3"  # bumped by the release job
description = 'single quoted'
dependencies = [
    "left-pad>=1",
]

[tool.something]
version = "1.2.3"

[build-system]
requires = ["hatchling"]
"""

LOCK = """\
version = 1
revision = 3
requires-python = ">=3.10"

[[package]]
name = "my-pkg-extras"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "other"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "my-pkg", version = "1.2.3" },
]

[[package]]
name = "my-pkg"
version = "1.2.3"
source = { editable = "." }
dependencies = [
    { name = "other" },
]

[package.metadata]
requires-dist = [{ name = "other", specifier = ">=1" }]

[[package]]
name = "zzz"
version = "9.9.9"
source = { registry = "https://pypi.org/simple" }
"""


def _diff_lines(a: str, b: str) -> list[int]:
    la, lb = a.splitlines(keepends=True), b.splitlines(keepends=True)
    assert len(la) == len(lb)
    return [i for i, (x, y) in enumerate(zip(la, lb)) if x != y]


@pytest.mark.parametrize(
    ("current", "bump", "want"),
    [
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "patch", "1.2.4"),
        ("0.0.0", "patch", "0.0.1"),
        ("0.9.9", "minor", "0.10.0"),
        ("10.20.30", "patch", "10.20.31"),
    ],
)
def test_bump_version_ok(current: str, bump: str, want: str) -> None:
    """A wrong bump arithmetic would tag the wrong release number."""
    assert rr.bump_version(current, bump) == want


@pytest.mark.parametrize(
    ("current", "bump"),
    [
        ("1.2", "patch"),
        ("1.2.3rc1", "patch"),
        ("v1.2.3", "patch"),
        ("01.2.3", "patch"),
        ("1.2.3\n", "patch"),
        ("1.2.3.4", "patch"),
        ("", "patch"),
        ("1.2.3", "major"),
        ("1.2.3", ""),
    ],
)
def test_bump_version_refuses(current: str, bump: str) -> None:
    """A malformed version or a major bump must refuse, never guess."""
    with pytest.raises(ReleaseError):
        rr.bump_version(current, bump)


@pytest.mark.parametrize(
    ("subjects", "want"),
    [
        ([], "patch"),
        (["feat: x"], "minor"),
        (["feat(x): y"], "minor"),
        (["feat!: y"], "minor"),
        (["feature: y"], "patch"),
        (["fix: y"], "patch"),
        (["fix: a", "feat(z): b"], "minor"),
        (["chore: feat: nope"], "patch"),
    ],
)
def test_pick_bump(subjects: list[str], want: str) -> None:
    """A feature released as a patch (or the reverse) misstates the change."""
    assert rr.pick_bump(subjects) == want


def test_tag_name() -> None:
    """The tag is the profile prefix plus the version; a bad version refuses."""
    assert rr.tag_name("v", "1.3.0") == "v1.3.0"
    assert rr.tag_name("rel-", "0.1.0") == "rel-0.1.0"
    with pytest.raises(ReleaseError):
        rr.tag_name("v", "1.3")


def test_set_project_version_changes_only_that_line() -> None:
    """Touching any byte beyond the version value would pollute the release
    commit; the other table's version key must stay."""
    out = rr.set_project_version(PYPROJECT, "1.3.0")
    changed = _diff_lines(PYPROJECT, out)
    assert changed == [3]
    assert out.splitlines()[3] == 'version = "1.3.0"  # bumped by the release job'
    assert tomllib.loads(out)["tool"]["something"]["version"] == "1.2.3"


def test_set_project_version_keeps_single_quotes() -> None:
    """The quoting style of the line is the author's, not ours."""
    text = "[project]\nname = 'a'\nversion = '1.0.0'\n"
    assert rr.set_project_version(text, "1.0.1") == (
        "[project]\nname = 'a'\nversion = '1.0.1'\n"
    )


def test_set_project_version_keeps_crlf() -> None:
    """CRLF files must keep every CRLF, including the edited line's."""
    text = PYPROJECT.replace("\n", "\r\n")
    out = rr.set_project_version(text, "1.3.0")
    assert out == PYPROJECT.replace("\n", "\r\n").replace(
        'version = "1.2.3"  # bumped', 'version = "1.3.0"  # bumped'
    )
    assert out.count("\r\n") == text.count("\r\n")


def test_set_project_version_ignores_other_tables_version_only() -> None:
    """A version key in another table must not count as a second hit."""
    text = '[tool.x]\nversion = "9"\n[project]\nname = "a"\nversion = "1.0.0"\n'
    out = rr.set_project_version(text, "1.0.1")
    assert 'version = "9"' in out
    assert tomllib.loads(out)["project"]["version"] == "1.0.1"


@pytest.mark.parametrize(
    "text",
    [
        '[project]\nname = "a"\ndynamic = ["version"]\n[tool.x]\nversion = "1.0.0"\n',
        '[project]\nname = "a"\nversion = "1.0.0"\nversion = "1.0.1"\n',
        '[tool.x]\nversion = "1.0.0"\n',
        "[project\n",
    ],
)
def test_set_project_version_refuses(text: str) -> None:
    """No static line, two lines, no table or broken TOML must all refuse."""
    with pytest.raises(ReleaseError):
        rr.set_project_version(text, "1.0.1")


def test_set_project_version_refuses_bad_new() -> None:
    """The new value must itself be X.Y.Z."""
    with pytest.raises(ReleaseError):
        rr.set_project_version(PYPROJECT, "1.3")


def test_project_name_version() -> None:
    """The release job reads the project's own name and version from here."""
    assert rr.project_name_version(PYPROJECT) == ("My_Pkg", "1.2.3")


@pytest.mark.parametrize(
    "text",
    [
        '[tool.x]\nname = "a"\n',
        '[project]\nversion = "1.0.0"\n',
        '[project]\nname = "a"\ndynamic = ["version"]\n',
        '[project]\nname = "a"\nversion = 3\n',
        "[project]\nname = 1\nversion = '1'\n",
    ],
)
def test_project_name_version_refuses(text: str) -> None:
    """A missing or non-string name/version must refuse."""
    with pytest.raises(ReleaseError):
        rr.project_name_version(text)


def test_set_lock_version_changes_only_the_project_block() -> None:
    """The substring-named package and the same-version dependency must stay
    untouched; only the project's own version line changes."""
    out = rr.set_lock_version(LOCK, "my-pkg", "1.3.0")
    changed = _diff_lines(LOCK, out)
    assert len(changed) == 1
    assert out.splitlines()[changed[0]] == 'version = "1.3.0"'
    assert LOCK.splitlines()[changed[0] - 1] == 'name = "my-pkg"'
    pkgs = {p["name"]: p["version"] for p in tomllib.loads(out)["package"]}
    assert pkgs == {
        "my-pkg-extras": "1.2.3",
        "other": "1.2.3",
        "my-pkg": "1.3.0",
        "zzz": "9.9.9",
    }


@pytest.mark.parametrize("package", ["My_Pkg", "my.pkg", "MY--PKG", "my-pkg"])
def test_set_lock_version_normalises_names(package: str) -> None:
    """pyproject says My_Pkg, the lock says my-pkg: PEP 503 equality."""
    out = rr.set_lock_version(LOCK, package, "1.3.0")
    assert len(_diff_lines(LOCK, out)) == 1


def test_set_lock_version_accepts_virtual_and_crlf() -> None:
    """A virtual root is also the project; CRLF lockfiles keep their endings."""
    text = LOCK.replace('{ editable = "." }', '{ virtual = "." }').replace("\n", "\r\n")
    out = rr.set_lock_version(text, "my-pkg", "1.3.0")
    assert out.count("\r\n") == text.count("\r\n")
    assert len(_diff_lines(text, out)) == 1


def test_set_lock_version_refuses_no_local_block() -> None:
    """A registry package with the project's name is not the project."""
    with pytest.raises(ReleaseError):
        rr.set_lock_version(LOCK, "other", "1.3.0")
    with pytest.raises(ReleaseError):
        rr.set_lock_version(LOCK, "missing", "1.3.0")


def test_set_lock_version_refuses_two_blocks() -> None:
    """Two local blocks of the same name are ambiguous."""
    block = (
        '[[package]]\nname = "my-pkg"\nversion = "1.2.3"\nsource = { editable = "." }\n'
    )
    with pytest.raises(ReleaseError):
        rr.set_lock_version(LOCK + "\n" + block, "my-pkg", "1.3.0")


def test_set_lock_version_refuses_block_without_version() -> None:
    """A local block with no static version line cannot be edited."""
    text = (
        '[[package]]\nname = "my-pkg"\nsource = { editable = "." }\n'
        "[package.metadata]\nrequires-dist = []\n"
    )
    with pytest.raises(ReleaseError):
        rr.set_lock_version(text, "my-pkg", "1.3.0")


def _run(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "head_sha": "abc",
        "path": ".github/workflows/ci.yml",
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
    }
    base.update(kw)
    return base


def test_pick_run_filters_and_prefers_latest_attempt() -> None:
    """Judging the wrong sha or workflow would tag a commit CI never passed;
    a re-run's later attempt must win over the first."""
    runs = [
        _run(id=1, head_sha="zzz", run_attempt=9),
        _run(id=2, path=".github/workflows/other.yml", run_attempt=9),
        _run(id=3, path=".github/workflows/xci.yml", run_attempt=9),
        _run(id=4, run_attempt=1),
        _run(id=6, run_attempt=2),
        _run(id=5, run_attempt=2),
    ]
    assert rr.pick_run(runs, sha="abc", workflow="ci.yml") == runs[4]
    assert rr.pick_run(runs, sha="ab", workflow="ci.yml") is None
    assert rr.pick_run([], sha="abc", workflow="ci.yml") is None
    bare = [_run(path="ci.yml")]
    assert rr.pick_run(bare, sha="abc", workflow="ci.yml") == bare[0]


@pytest.mark.parametrize(
    ("run", "before", "want"),
    [
        (None, 0, "pending"),
        (_run(status="in_progress", conclusion=None), 0, "pending"),
        (_run(status="queued", conclusion=None), 0, "pending"),
        (_run(run_attempt=1), 1, "pending"),
        (_run(run_attempt=1, conclusion="failure"), 1, "pending"),
        (_run(run_attempt=2), 1, "green"),
        (_run(run_attempt=2, conclusion="failure"), 1, "red"),
        (_run(), 0, "green"),
        (_run(conclusion="cancelled"), 0, "cancelled"),
        (_run(conclusion="failure"), 0, "red"),
        (_run(conclusion="timed_out"), 0, "red"),
        (_run(conclusion="startup_failure"), 0, "red"),
        (_run(conclusion="action_required"), 0, "red"),
        (_run(conclusion="neutral"), 0, "red"),
        (_run(conclusion="skipped"), 0, "red"),
        (_run(conclusion="stale"), 0, "red"),
        (_run(conclusion=None), 0, "red"),
    ],
)
def test_ci_verdict(run: dict[str, Any] | None, before: int, want: str) -> None:
    """Each row of the verdict rule; the stale-attempt rows guard gr588028
    (a green first attempt must not pass a requested re-run)."""
    assert rr.ci_verdict(run, rerun_attempt_before=before) == want


def test_catpath_profile_release_fields() -> None:
    """The catpath profile carries the release data, and nothing can release
    it until the last round of step 3 sets a releaser."""
    p = lanes.REPO_PROFILES["catpath"]
    assert p.release_check_cmds == ("uv lock --check",)
    assert p.ci_workflow == "ci.yml"
    assert p.tag_prefix == "v"
    assert p.releaser is None
    assert lanes.releasable_repos() == ()
    default = lanes.REPO_PROFILES[lanes.DEFAULT_REPO]
    assert (default.releaser, default.release_check_cmds, default.ci_workflow) == (
        None,
        (),
        None,
    )
