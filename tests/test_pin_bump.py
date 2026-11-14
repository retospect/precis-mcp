"""Pure helpers of the engine pin-bump job (td659522 step 4, R1a)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from precis.workers.job_types import pin_bump as pb
from precis.workers.job_types.pin_bump import PinError

REPO = Path(__file__).resolve().parents[1]
OLD = "2788a4569817bf9b9c11c0d4d083fb2b39a0c578"
NEW = "769e29a832195c74038f1e34fd818500c9179838"
URL = "https://github.com/retospect/catpath"
OTHER = "0123456789abcdef0123456789abcdef01234567"
KEY = "66c96e1b45bd24b3d0f7047eaee402ee63233337a0b857c8f0f22a7e4ed7a4bf"

PYPROJECT = f"""\
[project]
name = "demo"
version = "1.0.0"

[project.optional-dependencies]
# floors
catalyst = ["autocatpath>=0.49.0"]
catalyst-gpu = [
    "autocatpath[mace]>=0.32.0",
    "torch>=2",
]
other = ["autocatpathx>=0.49.0"]

[tool.uv.sources]
autocatpath = {{ git = "{URL}", rev = "{OLD}" }}  # v0.49.0 (old text, with }} brace)
elsewhere = {{ git = "{URL}2", rev = "{OLD}" }}  # keep me
"""

LOCK = f"""\
version = 1

[[package]]
name = "autocatpath"
version = "0.49.0"
source = {{ git = "{URL}?rev={OLD}#{OLD}" }}
dependencies = [
    {{ name = "ase" }},
]

[[package]]
name = "sibling"
version = "9.9.9"
source = {{ git = "{URL}?rev={OLD}#{OLD}" }}

[[package]]
name = "demo"
version = "1.0.0"
source = {{ editable = "." }}

[package.optional-dependencies]
catalyst = [
    {{ name = "autocatpath" }},
]

[package.metadata]
requires-dist = [
    {{ name = "autocatpath", marker = "extra == 'catalyst'", git = "{URL}?rev={OLD}" }},
    {{ name = "autocatpath", extras = ["mace"], marker = "extra == 'catalyst-gpu'", git = "{URL}?rev={OLD}" }},
    {{ name = "sibling", git = "{URL}?rev={OLD}" }},
]
"""

GROUP_VARS = """\
# comment: autocatpath_min_version: "0.1.0"
other_key: "0.49.0"
autocatpath_min_version: "0.49.0"
tail: 1
"""

ANCHORS = f"""\
import os

# the key, re-derived under 0.49.0
JO482106_KEY = "{"c" * 64}"  # trailing comment
OTHER_KEY = "{"d" * 64}"
JO482106_ENGINE = "0.49.0"


def f() -> str:
    return "JO482106_KEY"
"""


def _pin(text: str = PYPROJECT) -> str:
    return pb.set_pyproject_pin(text, "autocatpath", version="0.50.0", sha=NEW)


def _lock(text: str = LOCK, old: str = OLD) -> str:
    return pb.set_lock_pin(text, "autocatpath", version="0.50.0", sha=NEW, old_sha=old)


# --- oracle: the real bump commit -----------------------------------------

_BUMP = "bc4c8096f"


def _git_show(rev: str, path: str) -> str:
    try:
        res = subprocess.run(
            ["git", "show", f"{rev}:{path}"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        pytest.skip("git is unavailable")
    if res.returncode != 0:
        pytest.skip(f"commit {_BUMP} is unavailable (shallow checkout or no .git)")
    return res.stdout


def _norm_comment(text: str) -> str:
    return "\n".join(
        (ln.split("  # v0.50.0 (")[0] + "  # v0.50.0")
        if ln.startswith("autocatpath = { git") and "  # v0.50.0 (" in ln
        else ln
        for ln in text.split("\n")
    )


def test_oracle_pyproject_matches_the_bump_commit() -> None:
    """If this fails, set_pyproject_pin no longer reproduces the real
    four-file bump's pyproject.toml edit (sources comment aside)."""
    before = _git_show(f"{_BUMP}^", "pyproject.toml")
    after = _git_show(_BUMP, "pyproject.toml")
    got = pb.set_pyproject_pin(before, "autocatpath", version="0.50.0", sha=NEW)
    assert _norm_comment(got) == _norm_comment(after)
    assert "# v0.50.0\n" in got


def test_oracle_lock_matches_the_bump_commit() -> None:
    """If this fails, set_lock_pin no longer reproduces the real bump's
    uv.lock edit."""
    before = _git_show(f"{_BUMP}^", "uv.lock")
    after = _git_show(_BUMP, "uv.lock")
    got = pb.set_lock_pin(before, "autocatpath", version="0.50.0", sha=NEW, old_sha=OLD)
    assert got == after


def test_oracle_group_vars_matches_the_bump_commit() -> None:
    """If this fails, set_group_vars_floor no longer reproduces the real
    bump's group_vars edit."""
    before = _git_show(f"{_BUMP}^", "deploy/group_vars/all.yml")
    after = _git_show(_BUMP, "deploy/group_vars/all.yml")
    assert pb.set_group_vars_floor(before, "autocatpath_min_version", "0.50.0") == after


# --- compare_versions -----------------------------------------------------


def test_compare_versions_orders_numerically() -> None:
    """If this fails, compare_versions compares strings, not numbers."""
    assert pb.compare_versions("0.9.0", "0.10.0") == -1
    assert pb.compare_versions("1.0.0", "1.0.0") == 0
    assert pb.compare_versions("2.0.0", "1.99.99") == 1


def test_compare_versions_refuses_non_semver() -> None:
    """If this fails, a malformed version is compared instead of refused."""
    with pytest.raises(PinError):
        pb.compare_versions("1.0", "1.0.0")
    with pytest.raises(PinError):
        pb.compare_versions("1.0.0", "v1.0.0")


# --- pyproject ------------------------------------------------------------


def test_current_pin_reads_floor_rev_and_url() -> None:
    """If this fails, current_pin misreads the floor line or sources entry."""
    assert pb.current_pin(PYPROJECT, "autocatpath") == ("0.49.0", OLD, URL)


def test_current_pin_refuses_missing_and_duplicate_lines() -> None:
    """If this fails, a missing or doubled pin line is read anyway."""
    no_floor = PYPROJECT.replace('catalyst = ["autocatpath>=0.49.0"]\n', "")
    with pytest.raises(PinError, match="floor"):
        pb.current_pin(no_floor, "autocatpath")
    dup_floor = PYPROJECT.replace(
        "other = [", 'catalyst = ["autocatpath>=0.48.0"]\nother = ['
    )
    with pytest.raises(PinError, match="floor"):
        pb.current_pin(dup_floor, "autocatpath")
    two_in_line = PYPROJECT.replace(
        '["autocatpath>=0.49.0"]', '["autocatpath>=0.49.0", "autocatpath>=0.48.0"]'
    )
    with pytest.raises(PinError):
        pb.current_pin(two_in_line, "autocatpath")
    no_src = PYPROJECT.replace("autocatpath = {", "autocatpathy = {")
    with pytest.raises(PinError, match="sources"):
        pb.current_pin(no_src, "autocatpath")
    dup_src = PYPROJECT + f'autocatpath = {{ git = "{URL}", rev = "{OLD}" }}\n'
    with pytest.raises(PinError, match="sources"):
        pb.current_pin(dup_src, "autocatpath")


def test_current_pin_refuses_a_rev_that_is_not_40_lowercase_hex() -> None:
    """If this fails, a short or uppercase rev is accepted as the pin."""
    with pytest.raises(PinError, match="40 lowercase hex"):
        pb.current_pin(PYPROJECT.replace(OLD, OLD[:12], 1), "autocatpath")
    with pytest.raises(PinError, match="40 lowercase hex"):
        pb.current_pin(PYPROJECT.replace(OLD, OLD.upper(), 1), "autocatpath")


def test_set_pyproject_pin_moves_floor_rev_and_comment_only() -> None:
    """If this fails, the pyproject edit touched more than the floor, the
    rev and the sources comment."""
    got = _pin()
    want = PYPROJECT.replace('"autocatpath>=0.49.0"', '"autocatpath>=0.50.0"')
    want = want.replace(
        f'rev = "{OLD}" }}  # v0.49.0 (old text, with }} brace)',
        f'rev = "{NEW}" }}  # v0.50.0',
        1,
    )
    assert got == want
    assert pb.current_pin(got, "autocatpath") == ("0.50.0", NEW, URL)


def test_set_pyproject_pin_leaves_the_mace_floor_and_other_sources() -> None:
    """If this fails, the [mace] floor or another package's sources line
    was rewritten."""
    got = _pin()
    assert '"autocatpath[mace]>=0.32.0"' in got
    assert '"autocatpathx>=0.49.0"' in got
    assert f'elsewhere = {{ git = "{URL}2", rev = "{OLD}" }}  # keep me' in got


def test_set_pyproject_pin_is_idempotent() -> None:
    """If this fails, applying the same pin twice changes the text again."""
    once = _pin()
    assert _pin(once) == once


def test_set_pyproject_pin_adds_a_comment_when_there_is_none() -> None:
    """If this fails, a sources line without a comment loses its newline or
    gets no version comment."""
    bare = PYPROJECT.replace("  # v0.49.0 (old text, with } brace)", "")
    got = _pin(bare)
    assert f'rev = "{NEW}" }}  # v0.50.0\n' in got


def test_set_pyproject_pin_keeps_crlf_endings() -> None:
    """If this fails, a CRLF file's edited lines lose their carriage return."""
    got = _pin(PYPROJECT.replace("\n", "\r\n"))
    assert got == _pin().replace("\n", "\r\n")


def test_set_pyproject_pin_validates_every_argument() -> None:
    """If this fails, an unchecked value reaches the regex or the text."""
    for bad in ("0.50", "0.50.0.1", "v0.50.0", '0.50.0"]', "01.0.0"):
        with pytest.raises(PinError):
            pb.set_pyproject_pin(PYPROJECT, "autocatpath", version=bad, sha=NEW)
    for bad in (NEW[:39], NEW.upper(), NEW + "0", 'x" }'):
        with pytest.raises(PinError):
            pb.set_pyproject_pin(PYPROJECT, "autocatpath", version="0.50.0", sha=bad)
    with pytest.raises(PinError):
        pb.set_pyproject_pin(PYPROJECT, "auto.*", version="0.50.0", sha=NEW)
    with pytest.raises(PinError):
        pb.set_pyproject_pin(
            PYPROJECT, "autocatpath", version="0.50.0", sha=NEW, extra="a b"
        )


def test_set_pyproject_pin_refuses_trailing_text_after_the_table() -> None:
    """If this fails, a sources line with non-comment trailing text is
    rewritten blindly."""
    bad = PYPROJECT.replace("  # v0.49.0 (old text, with } brace)", " junk")
    with pytest.raises(PinError):
        _pin(bad)


# --- uv.lock --------------------------------------------------------------


def test_set_lock_pin_moves_block_and_dependency_lines() -> None:
    """If this fails, the lock edit missed the version, source or a
    dependency line of the pinned package."""
    got = _lock()
    want = LOCK.replace(
        'name = "autocatpath"\nversion = "0.49.0"',
        'name = "autocatpath"\nversion = "0.50.0"',
    )
    want = want.replace(
        f'source = {{ git = "{URL}?rev={OLD}#{OLD}" }}\ndependencies = [\n    {{ name = "ase" }}',
        f'source = {{ git = "{URL}?rev={NEW}#{NEW}" }}\ndependencies = [\n    {{ name = "ase" }}',
    )
    want = want.replace(
        f'{{ name = "autocatpath", marker = "extra == \'catalyst\'", git = "{URL}?rev={OLD}" }}',
        f'{{ name = "autocatpath", marker = "extra == \'catalyst\'", git = "{URL}?rev={NEW}" }}',
    ).replace(
        f'extras = ["mace"], marker = "extra == \'catalyst-gpu\'", git = "{URL}?rev={OLD}"',
        f'extras = ["mace"], marker = "extra == \'catalyst-gpu\'", git = "{URL}?rev={NEW}"',
    )
    assert got == want


def test_set_lock_pin_leaves_another_package_on_the_same_sha() -> None:
    """If this fails, a package that merely shares the old sha was moved."""
    got = _lock()
    assert (
        f'name = "sibling"\nversion = "9.9.9"\nsource = {{ git = "{URL}?rev={OLD}#{OLD}" }}'
        in got
    )
    assert f'{{ name = "sibling", git = "{URL}?rev={OLD}" }}' in got
    assert got.count(OLD) == 3  # sibling block, sibling dep, and its source twice
    assert got.count(NEW) == 4


def test_set_lock_pin_is_idempotent_when_old_sha_is_the_new_one() -> None:
    """If this fails, re-applying the pin changes an already-moved lock."""
    once = _lock()
    assert _lock(once, old=NEW) == once


def test_set_lock_pin_refuses_wrong_old_sha() -> None:
    """If this fails, the lock is rewritten though it carries another rev."""
    with pytest.raises(PinError, match="does not carry"):
        _lock(old=OTHER)


def test_set_lock_pin_refuses_missing_and_duplicate_blocks() -> None:
    """If this fails, a lock without exactly one package block is edited."""
    with pytest.raises(PinError, match="no package block"):
        pb.set_lock_pin(LOCK, "nothere", version="0.50.0", sha=NEW, old_sha=OLD)
    block = (
        f'[[package]]\nname = "autocatpath"\nversion = "0.49.0"\n'
        f'source = {{ git = "{URL}?rev={OLD}#{OLD}" }}\n\n'
    )
    with pytest.raises(PinError, match="more than one"):
        _lock(LOCK + "\n" + block)


def test_set_lock_pin_validates_every_argument() -> None:
    """If this fails, an unchecked value reaches the lock text."""
    with pytest.raises(PinError):
        pb.set_lock_pin(LOCK, "autocatpath", version="x", sha=NEW, old_sha=OLD)
    with pytest.raises(PinError):
        pb.set_lock_pin(LOCK, "autocatpath", version="0.50.0", sha="abc", old_sha=OLD)
    with pytest.raises(PinError):
        pb.set_lock_pin(LOCK, "autocatpath", version="0.50.0", sha=NEW, old_sha="abc")


# --- group_vars -----------------------------------------------------------


def test_set_group_vars_floor_rewrites_only_that_line() -> None:
    """If this fails, the group_vars edit touched a comment, another key or
    the line ending."""
    got = pb.set_group_vars_floor(GROUP_VARS, "autocatpath_min_version", "0.50.0")
    assert got == GROUP_VARS.replace(
        'autocatpath_min_version: "0.49.0"', 'autocatpath_min_version: "0.50.0"'
    )
    assert pb.set_group_vars_floor(got, "autocatpath_min_version", "0.50.0") == got


def test_set_group_vars_floor_refuses_missing_and_duplicate_keys() -> None:
    """If this fails, a group_vars without exactly one key line is edited."""
    with pytest.raises(PinError, match="no "):
        pb.set_group_vars_floor("a: 1\n", "autocatpath_min_version", "0.50.0")
    with pytest.raises(PinError, match="2 "):
        pb.set_group_vars_floor(
            GROUP_VARS + 'autocatpath_min_version: "0.1.0"\n',
            "autocatpath_min_version",
            "0.50.0",
        )
    with pytest.raises(PinError):
        pb.set_group_vars_floor(GROUP_VARS, "a.*", "0.50.0")
    with pytest.raises(PinError):
        pb.set_group_vars_floor(GROUP_VARS, "autocatpath_min_version", '0.50.0"')


# --- anchor literals ------------------------------------------------------


def test_set_anchor_literals_changes_only_the_two_values() -> None:
    """If this fails, the anchor edit touched a comment, another literal or
    any other byte."""
    got = pb.set_anchor_literals(ANCHORS, key=KEY, engine="0.50.0")
    want = ANCHORS.replace("c" * 64, KEY).replace(
        'JO482106_ENGINE = "0.49.0"', 'JO482106_ENGINE = "0.50.0"'
    )
    assert got == want
    assert "# trailing comment" in got
    assert pb.set_anchor_literals(got, key=KEY, engine="0.50.0") == got


def test_set_anchor_literals_honours_custom_names() -> None:
    """If this fails, key_name / engine_name are ignored."""
    got = pb.set_anchor_literals(
        ANCHORS,
        key=KEY,
        engine="1.2.3",
        key_name="OTHER_KEY",
        engine_name="JO482106_ENGINE",
    )
    assert f'OTHER_KEY = "{KEY}"' in got
    assert 'JO482106_KEY = "' + "c" * 64 in got


def test_set_anchor_literals_refuses_bad_values_and_missing_names() -> None:
    """If this fails, a malformed key or engine or an absent literal is
    accepted."""
    with pytest.raises(PinError):
        pb.set_anchor_literals(ANCHORS, key=KEY[:63], engine="0.50.0")
    with pytest.raises(PinError):
        pb.set_anchor_literals(ANCHORS, key=KEY.upper(), engine="0.50.0")
    with pytest.raises(PinError):
        pb.set_anchor_literals(ANCHORS, key=KEY, engine="0.50")
    with pytest.raises(PinError, match="0 top-level"):
        pb.set_anchor_literals(ANCHORS, key=KEY, engine="0.50.0", key_name="NOPE")
    with pytest.raises(PinError, match="2 top-level"):
        pb.set_anchor_literals(
            ANCHORS + 'JO482106_ENGINE = "0.1.0"\n', key=KEY, engine="0.50.0"
        )
    with pytest.raises(PinError, match="not a plain string"):
        pb.set_anchor_literals(
            ANCHORS.replace('"0.49.0"', "49"), key=KEY, engine="0.50.0"
        )
    with pytest.raises(PinError, match="does not parse"):
        pb.set_anchor_literals("x = (", key=KEY, engine="0.50.0")


# --- pin_is_coherent ------------------------------------------------------


def _coherent(py: str, lock: str, gv: str, sha: str = NEW, ver: str = "0.50.0") -> bool:
    return pb.pin_is_coherent(py, lock, gv, "autocatpath", version=ver, sha=sha)


def test_pin_is_coherent_true_only_when_all_three_carry_the_pin() -> None:
    """If this fails, coherence is judged from fewer than all three files."""
    py = _pin()
    lock = _lock()
    gv = pb.set_group_vars_floor(GROUP_VARS, "autocatpath_min_version", "0.50.0")
    assert _coherent(py, lock, gv)
    # old state
    assert not _coherent(PYPROJECT, LOCK, GROUP_VARS)
    # half-moved: each file left behind in turn
    assert not _coherent(PYPROJECT, lock, gv)
    assert not _coherent(py, LOCK, gv)
    assert not _coherent(py, lock, GROUP_VARS)
    # right version, wrong sha
    assert not _coherent(py, lock, gv, sha=OTHER)
    # pin missing in a text is not coherent (not an error)
    assert not _coherent("[project]\n", lock, gv)
    assert not _coherent(py, "version = 1\n", gv)
    assert not _coherent(py, lock, "a: 1\n")


def test_pin_is_coherent_validates_its_arguments() -> None:
    """If this fails, a malformed version or sha is silently 'not coherent'."""
    with pytest.raises(PinError):
        _coherent(PYPROJECT, LOCK, GROUP_VARS, ver="0.50")
    with pytest.raises(PinError):
        _coherent(PYPROJECT, LOCK, GROUP_VARS, sha="abc")
