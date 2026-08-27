"""Regression tests for gr263082 — a plain ``scripts/deploy`` failing partway
through on the ``autocatpath_plugin`` hosts because this repo's declared
``autocatpath`` version floor had outrun the newest wheel anyone had built.

autocatpath is release-gated off PyPI past 0.13.0, so a cluster host can only
resolve it from ``--find-links /opt/precis/wheels``, seeded by
``deploy/roles/autocatpath/tasks/wheelhouse_seed.yml`` from a controller-side
wheel handed over as ``-e autocatpath_wheel=<path>``. That variable defaulted
to ``""`` and nothing ever set it, so when pyproject moved to
``autocatpath>=0.18.0`` while ``dist/`` still topped out at 0.17.0, the seed
task was a documented no-op and the install died on castor + pollux — after
four other hosts had already moved, i.e. a mixed-version fleet.

The floor lives in this repo and the artifact lives in the catpath repo, so
they can always drift; ``scripts/lib/autocatpath-wheel.sh`` exists so
``scripts/deploy`` can spot the drift in preflight, before touching a host.
These tests cover the version arithmetic that decision rests on (kept
side-effect-free precisely so it can be tested here) plus the wiring that
makes it reachable.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped deploy helpers are POSIX shell",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "scripts" / "lib" / "autocatpath-wheel.sh"


def _sh(snippet: str) -> subprocess.CompletedProcess[str]:
    """Run `snippet` with the real lib sourced — never a reimplementation."""
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}"\n{snippet}'],
        capture_output=True,
        text=True,
    )


def _pyproject(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_floor_takes_the_lowest_of_several_declarations(tmp_path: Path) -> None:
    """pyproject declares the floor more than once — the ``catalyst`` extra's
    pure engine and ``catalyst-gpu``'s ``autocatpath[mace]``. A wheel only has
    to clear the LOWEST for some extra to resolve, so taking the first or the
    highest would abort deploys that would in fact have succeeded.
    """
    pyproject = _pyproject(
        tmp_path,
        'catalyst-gpu = ["autocatpath[mace]>=0.19.0"]\n'
        'catalyst = ["autocatpath>=0.18.0"]\n',
    )
    assert _sh(f'autocatpath_floor "{pyproject}"').stdout == "0.18.0"


def test_floor_reads_the_real_pyproject() -> None:
    """Pin the parse against the shipped file, not just synthetic input: the
    declaration's exact shape (extras list, the ``[mace]`` marker in the middle
    of the requirement string) is what the regex has to survive.
    """
    result = _sh(f'autocatpath_floor "{REPO_ROOT / "pyproject.toml"}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.count(".") == 2, f"not a release version: {result.stdout!r}"


def test_floor_reports_absence_rather_than_guessing(tmp_path: Path) -> None:
    """No floor declared must fail, not print an empty string that a caller
    would then compare against — scripts/deploy branches on this to mean
    "nothing to seed" and skip the whole preflight.
    """
    pyproject = _pyproject(tmp_path, 'dependencies = ["httpx>=0.27"]\n')
    assert _sh(f'autocatpath_floor "{pyproject}"').returncode == 1


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("0.18.0", "0.18.0", True),
        ("0.19.0", "0.18.0", True),
        ("0.17.0", "0.18.0", False),
        # The case a lexical compare gets wrong, and the one that actually
        # bites a project counting up minor versions one at a time.
        ("0.10.0", "0.9.0", True),
        ("0.9.0", "0.10.0", False),
        ("1.0.0", "0.99.0", True),
    ],
)
def test_version_ge_orders_releases_numerically(a: str, b: str, expected: bool) -> None:
    assert (_sh(f'version_ge "{a}" "{b}"').returncode == 0) is expected


def test_newest_wheel_is_highest_version_not_newest_file(tmp_path: Path) -> None:
    """Rebuilding an older version must not shadow a newer wheel already
    sitting in dist/ — an mtime-ordered pick would hand the cluster 0.17.0
    right after someone re-ran a build for it.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in (
        "autocatpath-0.18.0-py3-none-any.whl",
        "autocatpath-0.9.0-py3-none-any.whl",
        "autocatpath-0.17.0-py3-none-any.whl",  # written last, deliberately
    ):
        (dist / name).write_text("", encoding="utf-8")

    result = _sh(f'newest_autocatpath_wheel "{dist}"')
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout).name == "autocatpath-0.18.0-py3-none-any.whl"


def test_newest_wheel_ignores_unrelated_and_malformed_files(tmp_path: Path) -> None:
    """dist/ also holds sdists and other projects' artifacts; a non-numeric
    version segment must be skipped rather than sorted as if it were a release.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in (
        "autocatpath-0.17.0.tar.gz",
        "autocatpath-dev-py3-none-any.whl",
        "precis_mcp-8.31.0-py3-none-any.whl",
        "autocatpath-0.14.1-py3-none-any.whl",
    ):
        (dist / name).write_text("", encoding="utf-8")

    result = _sh(f'newest_autocatpath_wheel "{dist}"')
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout).name == "autocatpath-0.14.1-py3-none-any.whl"


def test_newest_wheel_reports_an_empty_or_missing_dist(tmp_path: Path) -> None:
    """Both "built nothing yet" and "no checkout at all" have to be
    distinguishable from a successful resolve, since deploy turns them into
    different operator-facing messages.
    """
    empty = tmp_path / "dist"
    empty.mkdir()
    assert _sh(f'newest_autocatpath_wheel "{empty}"').returncode == 1
    assert _sh(f'newest_autocatpath_wheel "{tmp_path / "absent"}"').returncode == 1


def test_wheel_version_extracts_the_release_segment() -> None:
    result = _sh(
        "autocatpath_wheel_version /some/dist/autocatpath-0.18.0-py3-none-any.whl"
    )
    assert result.stdout == "0.18.0"


def test_project_version_reads_a_checkouts_own_declaration(tmp_path: Path) -> None:
    """What a build from that tree would produce — compared against the floor
    so deploy never burns a build on a checkout too old to satisfy it.
    """
    pyproject = _pyproject(
        tmp_path,
        '[project]\nname = "autocatpath"\nversion = "0.18.0"\n',
    )
    assert _sh(f'autocatpath_project_version "{pyproject}"').stdout == "0.18.0"
    assert (
        _sh(f'autocatpath_project_version "{tmp_path / "absent.toml"}"').returncode == 1
    )


def test_deploy_resolves_the_wheel_before_it_touches_any_host() -> None:
    """The helpers are inert unless ``scripts/deploy`` uses them, and the
    ORDER is the whole point: the resolve has to sit in preflight, alongside
    the reachability ping, so an unsatisfiable floor aborts with the fleet
    intact instead of stopping halfway with hosts on different versions.
    """
    deploy = (REPO_ROOT / "scripts" / "deploy").read_text(encoding="utf-8")
    assert "scripts/lib/autocatpath-wheel.sh" in deploy
    assert "-e autocatpath_wheel=" in deploy

    # Anchored to an invocation LINE, not a bare substring — the script's own
    # header comment names the command too, and matching that would compare
    # against a position near the top of the file and pass no matter what.
    invocation = re.search(
        r"^\s*ansible-playbook redeploy-precis\.yml", deploy, re.MULTILINE
    )
    assert invocation is not None, "no ansible-playbook invocation found"

    resolve_at = deploy.index("autocatpath_floor")
    deploy_at = invocation.start()
    assert resolve_at < deploy_at, (
        "the autocatpath wheel resolve must run BEFORE the first playbook "
        "invocation, or a bad floor still half-deploys the fleet"
    )


def test_deploy_refuses_to_build_from_a_stale_or_dirty_checkout() -> None:
    """catpath reuses one version number across many commits — 0.18.0 already
    spans nine of them, including a minimum-image-convention fix — so two
    wheels both named ``autocatpath-0.18.0`` can carry different code and the
    wheelhouse simply keeps whichever landed last. Version comparison cannot
    see that; the checkout's git state is the only signal there is.

    Releases are cut on another machine, so the local tree being behind is the
    NORMAL case, not an edge one. Without these guards a deploy would quietly
    ship a wheel nobody can afterwards identify, which is worse than the
    missing-wheel failure this preflight was added to prevent.
    """
    deploy = (REPO_ROOT / "scripts" / "deploy").read_text(encoding="utf-8")
    build_at = deploy.index("uv build --wheel")

    for guard in ("status --porcelain", "HEAD..@{upstream}"):
        at = deploy.index(guard, deploy.index("autocatpath_floor"))
        assert at < build_at, f"{guard!r} must gate the build, not follow it"


def test_deploy_keeps_an_escape_hatch() -> None:
    """A fleet whose hosts were seeded by hand, or one with no
    autocatpath_plugin hosts at all, must not be blocked by a check that
    exists for someone else's benefit.
    """
    deploy = (REPO_ROOT / "scripts" / "deploy").read_text(encoding="utf-8")
    assert "PRECIS_DEPLOY_SKIP_CATPATH_WHEEL" in deploy
    assert "PRECIS_CATPATH_DIR" in deploy
