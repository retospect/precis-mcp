"""Release cut pairing for Change-Set and Requires trailers (gr687874,
gr763931).

Same rig as test_release_script: a throwaway repo with a bare origin and
stubbed gh/deploy. Declarations are commit-message trailers, which is also
how a real slice lands them (ship preserves the trailer block verbatim,
gr517803).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from precis.workers.job_types.release_cut import (
    REFUSAL_HOLDING,
    classify_cut_refusal,
)
from tests.test_release_script import (
    FAST,
    RELEASE,
    _load_release,
    _marker,
    _reject_release_pushes,
    _release,
)
from tests.test_round_gate import (
    Rig,
    _commit,
    _git,
    _open_round,
    _push_branch,
    _rel,
    _rels,
    _round_json,
)
from tests.test_round_gate import rig as rig

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs shebang'd scripts"
)


def _commit_trailer(rig: Rig, rel: str, subject: str, trailer: str) -> str:
    """A commit whose message body carries one trailer line."""
    path = rig.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(subject + "\n")
    _git(rig.root, "add", rel)
    _git(rig.root, "commit", "-q", "-m", subject, "-m", trailer)
    return _git(rig.root, "rev-parse", "HEAD")


def _push_main(rig: Rig) -> None:
    _git(rig.root, "push", "-q", "origin", "main")


def _hold(out: subprocess.CompletedProcess[str]) -> dict[str, str]:
    return classify_cut_refusal(out.stdout + out.stderr)


def test_change_set_pair_ships_together(rig: Rig) -> None:
    _commit_trailer(rig, "src/d1.py", "d1 half", "Change-Set: pair-1")
    d2 = _commit_trailer(rig, "src/d2.py", "d2 half", "Change-Set: pair-1")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RELEASE_CUT: r1 {d2}" in out.stdout.splitlines()
    assert rig.origin_ref("release/r1") == d2


def test_lone_change_set_member_holds_until_counterpart_lands(rig: Rig) -> None:
    """The review finding: a lone member is INCOMPLETE (the sibling has
    not landed) and must not be treated as a complete one-commit set."""
    _commit_trailer(rig, "src/d1.py", "d1 half", "Change-Set: pair-1")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 1
    refusal = _hold(out)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "Change-Set pair-1" in refusal["line"]
    assert "1 of 2" in refusal["line"]
    assert rig.origin_ref("release/r1") == ""

    d2 = _commit_trailer(rig, "src/d2.py", "d2 half", "Change-Set: pair-1")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RELEASE_CUT: r1 {d2}" in out.stdout.splitlines()
    assert rig.origin_ref("release/r1") == d2


def test_pinned_cut_moves_below_an_incomplete_set(rig: Rig) -> None:
    """A pinned cut below the tip drops below the lone member: the cut
    ships none of the set (the plain commit above it rides the next one)."""
    d1 = _commit_trailer(rig, "src/d1.py", "d1 half", "Change-Set: pair-1")
    _commit(rig.root, "src/d2.py", "d2 plain")
    _push_main(rig)
    out = _release(rig, "--cut-only", "--sha", d1, *FAST, PRECIS_RELEASE_PASS="")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "change-set guard" in out.stdout
    assert f"RELEASE_CUT: r1 {rig.shas['c3']}" in out.stdout.splitlines()
    assert rig.origin_ref("release/r1") == rig.shas["c3"]


def test_factory_pinned_cut_waits_for_an_incomplete_set(rig: Rig) -> None:
    member = _commit_trailer(rig, "src/d1.py", "first half", "Change-Set: pair-1")
    _commit(rig.root, "src/d2.py", "unrelated")
    _push_main(rig)
    out = _release(rig, "--cut-only", "--sha", member, *FAST, PRECIS_RELEASE_PASS="td5")
    assert out.returncode == 1
    assert _hold(out)["class"] == REFUSAL_HOLDING
    assert rig.origin_ref("release/r1") == ""


@pytest.mark.parametrize("mode", ["tip", "pinned", "factory"])
def test_expired_incomplete_set_allows_cut(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    with monkeypatch.context() as dates:
        dates.setenv("GIT_COMMITTER_DATE", "2000-01-01T00:00:00Z")
        member = _commit_trailer(rig, "src/d1.py", "d1 half", "Change-Set: pair-1")
    tip = _commit(rig.root, "src/d2.py", "unrelated")
    _push_main(rig)

    args = [] if mode == "tip" else ["--sha", member]
    out = _release(
        rig,
        "--cut-only",
        *args,
        *FAST,
        PRECIS_RELEASE_PASS="td5" if mode == "factory" else "",
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert rig.origin_ref("release/r1") == (tip if mode == "tip" else member)
    assert (
        out.stderr.count("Change-Set pair-1: 1 sibling never landed, no longer pairing")
        == 1
    )


def test_expired_incomplete_set_keeps_landed_members_together(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as dates:
        dates.setenv("GIT_COMMITTER_DATE", "2000-01-01T00:00:00Z")
        first = _commit_trailer(rig, "src/d1.py", "d1", "Change-Set: trio-1 3")
    second = _commit_trailer(rig, "src/d2.py", "d2", "Change-Set: trio-1 3")
    _push_main(rig)

    pinned = _release(rig, "--cut-only", "--sha", first, *FAST, PRECIS_RELEASE_PASS="")
    assert pinned.returncode == 1
    assert "straddles the cut" in _hold(pinned)["line"]
    assert rig.origin_ref("release/r1") == ""

    tip = _release(rig, "--cut-only", *FAST)
    assert tip.returncode == 0, tip.stdout + tip.stderr
    assert rig.origin_ref("release/r1") == second
    assert "Change-Set trio-1: 1 sibling never landed" in tip.stderr


def test_pending_cut_is_rechecked_at_its_recorded_base(rig: Rig) -> None:
    base = rig.shas["c3"]
    hook = _reject_release_pushes(rig)
    assert _release(rig, "--cut-only", "--sha", base, *FAST).returncode == 1
    hook.unlink()
    _commit_trailer(rig, "src/d1.py", "requires pending base", f"Requires: {base}")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 1
    refusal = _hold(out)
    assert refusal["class"] == REFUSAL_HOLDING
    assert f"pending cut at {base[:9]}" in refusal["line"]
    assert rig.origin_ref("release/r1") == ""


def test_recorded_cut_reports_when_the_new_tip_straddles_a_set(rig: Rig) -> None:
    """td695907 review: a cut validly recorded at C must still be reported
    when a lone member lands above C — the guard gates the selection of a
    cut, never a recorded release's resume."""
    c3 = rig.shas["c3"]
    out = _release(rig, "--cut-only", "--sha", c3, *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    _commit_trailer(rig, "src/d1.py", "d1 half", "Change-Set: pair-1")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "holding:" not in out.stdout + out.stderr
    assert f"RELEASE_CUT: r1 {c3}" in out.stdout.splitlines()


def test_pending_cut_below_an_incomplete_set_reconciles(rig: Rig) -> None:
    """td695907 review: the pending cut at C predates the lone member that
    landed above it — C ships neither member, so the rerun reconciles at
    the recorded base instead of holding on the incomplete set."""
    c3 = rig.shas["c3"]
    hook = _reject_release_pushes(rig)
    assert _release(rig, "--cut-only", "--sha", c3, *FAST).returncode == 1
    hook.unlink()
    _commit_trailer(rig, "src/d1.py", "d1 half", "Change-Set: pair-1")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RELEASE_CUT: r1 {c3}" in out.stdout.splitlines()
    assert rig.origin_ref("release/r1") == c3


def test_change_set_count_incomplete_holds(rig: Rig) -> None:
    """Explicit membership: two of a declared three-member set landed —
    the count in the trailer is the completion criterion."""
    _commit_trailer(rig, "src/d1.py", "d1", "Change-Set: trio-1 3")
    _commit_trailer(rig, "src/d2.py", "d2", "Change-Set: trio-1 3")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 1
    refusal = _hold(out)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "2 of 3" in refusal["line"]


def test_requires_pair_at_tip_ships(rig: Rig) -> None:
    b = _commit(rig.root, "src/b.py", "b base")
    a = _commit_trailer(rig, "src/a.py", "a on b", f"Requires: {b}")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RELEASE_CUT: r1 {a}" in out.stdout.splitlines()


def test_requires_already_on_prod_satisfies_the_pairing(rig: Rig) -> None:
    b = _commit(rig.root, "src/b.py", "b base")
    _push_branch(rig, "prod", b)
    a = _commit_trailer(rig, "src/a.py", "a on b", f"Requires: {b}")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RELEASE_CUT: r1 {a}" in out.stdout.splitlines()


def test_pinned_cut_at_b_holds_when_a_above_requires_it(rig: Rig) -> None:
    """The review finding, the other direction: main is B then A, A
    Requires B — a pinned cut at B would ship half the pair."""
    b = _commit(rig.root, "src/b.py", "b base")
    _commit_trailer(rig, "src/a.py", "a on b", f"Requires: {b}")
    _push_main(rig)
    out = _release(rig, "--cut-only", "--sha", b, *FAST)
    assert out.returncode == 1
    refusal = _hold(out)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "straddle the cut" in refusal["line"]
    assert rig.origin_ref("release/r1") == ""


def _not_enforced(out: subprocess.CompletedProcess[str], a: str) -> list[str]:
    return [
        ln
        for ln in out.stderr.splitlines()
        if ln.startswith(f"scripts/release: warning: {a[:9]} Requires ")
        and "not enforced" in ln
    ]


def test_requires_unresolvable_fails_open_with_one_warning(rig: Rig) -> None:
    """td759095 review: a Requires value naming no commit cannot be
    honoured. Holding on it would stop every release for good, so the cut
    goes out and names the commit and the value in one stderr line."""
    a = _commit_trailer(rig, "src/a.py", "a wants b", "Requires: " + "f" * 40)
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RELEASE_CUT: r1 {a}" in out.stdout.splitlines()
    lines = _not_enforced(out, a)
    assert len(lines) == 1, out.stderr
    assert "f" * 40 in lines[0]


def test_requires_non_sha_value_fails_open(rig: Rig) -> None:
    """`Requires: td123` (a handle, not a sha) is unenforceable too."""
    a = _commit_trailer(rig, "src/a.py", "a wants a todo", "Requires: td123")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RELEASE_CUT: r1 {a}" in out.stdout.splitlines()
    lines = _not_enforced(out, a)
    assert len(lines) == 1 and "'td123'" in lines[0], out.stderr


def test_requires_a_commit_off_main_fails_open(rig: Rig) -> None:
    """A sha that resolves here but is not on origin/main — a pre-squash
    builder commit, a sibling that never landed — never ships with this
    cut, so it is named and not held on."""
    _git(rig.root, "checkout", "-q", "-b", "side")
    side = _commit(rig.root, "src/side.py", "builder commit, squashed away")
    _git(rig.root, "checkout", "-q", "main")
    a = _commit_trailer(rig, "src/a.py", "a on side", f"Requires: {side}")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RELEASE_CUT: r1 {a}" in out.stdout.splitlines()
    assert len(_not_enforced(out, a)) == 1, out.stderr


def test_change_set_member_already_on_prod_counts_as_shipped(rig: Rig) -> None:
    """td759095 review: the first member shipped in an earlier release, so
    (prod..tip] holds only the second. Counting the prod one, the set is
    whole and the cut goes out instead of waiting forever."""
    d1 = _commit_trailer(rig, "src/d1.py", "d1 half", "Change-Set: pair-1")
    _push_main(rig)
    _push_branch(rig, "prod", d1)
    d2 = _commit_trailer(rig, "src/d2.py", "d2 half", "Change-Set: pair-1")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "holding:" not in out.stdout + out.stderr
    assert f"RELEASE_CUT: r1 {d2}" in out.stdout.splitlines()


def test_change_set_short_even_with_prod_members_still_holds(rig: Rig) -> None:
    """Prod's members count, but only as many as there are: one shipped
    plus one landed of a declared three is still incomplete."""
    d1 = _commit_trailer(rig, "src/d1.py", "d1", "Change-Set: trio-1 3")
    _push_main(rig)
    _push_branch(rig, "prod", d1)
    _commit_trailer(rig, "src/d2.py", "d2", "Change-Set: trio-1 3")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 1
    refusal = _hold(out)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "2 of 3" in refusal["line"]


def test_prod_declared_count_outranks_a_smaller_count_on_main(rig: Rig) -> None:
    """td768687 review: prod holds `Change-Set: x 3`; main adds one member
    declaring only the default pair. Two members are not the declared
    three, so the cut holds."""
    d1 = _commit_trailer(rig, "src/d1.py", "d1", "Change-Set: x-1 3")
    _push_main(rig)
    _push_branch(rig, "prod", d1)
    _commit_trailer(rig, "src/d2.py", "d2", "Change-Set: x-1")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 1
    refusal = _hold(out)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "2 of 3" in refusal["line"]


def test_move_below_never_reaches_prod(rig: Rig) -> None:
    """The lone member is the oldest unshipped commit: cutting below it
    would pass prod, so nothing is shippable and the cut holds."""
    _push_branch(rig, "prod", rig.shas["c3"])
    d1 = _commit_trailer(rig, "src/d1.py", "d1 half", "Change-Set: pair-1")
    _commit(rig.root, "src/d2.py", "d2 plain")
    _push_main(rig)
    out = _release(rig, "--cut-only", "--sha", d1, *FAST, PRECIS_RELEASE_PASS="")
    assert out.returncode == 1
    refusal = _hold(out)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "nothing to ship" in refusal["line"]


def test_tip_cut_holds_instead_of_moving_below(rig: Rig) -> None:
    """A pipelined cut can take only the tip: even with shippable commits
    below, an incomplete set at the tip holds for the missing member."""
    _commit_trailer(rig, "src/d1.py", "d1 half", "Change-Set: pair-1")
    _commit(rig.root, "src/d2.py", "d2 plain")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 1
    refusal = _hold(out)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "waiting for the rest to land" in refusal["line"]


def test_trailer_block_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    body = (
        "subject\n\n"
        "Prose mentioning Requires: nope inline.\n\n"
        "Change-Set: pair-1\n"
        "requires: abcdef1234567\n"
    )
    assert mod._trailers(body) == [
        ("Change-Set", "pair-1"),
        ("requires", "abcdef1234567"),
    ]
    # A last paragraph mixing prose and trailers is not a trailer block.
    assert mod._trailers("subject\n\nThanks.\nRequires: abcdef1\n") == []
    # A trailer block needs a blank line before it.
    assert mod._trailers("subject\nChange-Set: pair-1\n") == []


def test_change_set_declared_count_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_release(tmp_path, monkeypatch)
    walk = [  # newest first, as _first_parent_walk returns
        ("b" * 40, 2, "s\n\nChange-Set: trio-1\n"),
        ("a" * 40, 1, "s\n\nChange-Set: trio-1 3\n"),
    ]
    sets = mod._change_sets(walk)
    assert sets["trio-1"]["want"] == 3
    assert sets["trio-1"]["members"] == ["a" * 40, "b" * 40]


def test_unreadable_history_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review finding: a history/index read failure must not fall
    back to the requested cut unchecked — it holds, classed a wait."""
    mod = _load_release(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "rev", lambda name: "t" * 40 if "main" in name else "")
    monkeypatch.setattr(mod, "git", lambda *args: None)
    s, hold = mod._split_set_cut("a" * 40)
    assert s == "a" * 40
    assert hold is not None and "unreadable" in hold


# ── the publication snapshot, the already-pushed pending cut (td759095) ──

MAIN_MOVED = "origin/main moved since the pairing check"


def _racing_release(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    land: Callable[[], str],
    times: int = 1,
) -> tuple[ModuleType, list[str]]:
    """scripts/release in process, the REAL scripts/round underneath, with
    ``land`` run just before each of the first ``times`` publishing
    ``round cut`` calls — a lane landing on main after the pairing check,
    before round refreshes main and pushes. Returns the module and the
    list ``land``'s results go into."""
    for key, value in rig.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(rig.root)
    mod = _load_release(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "ROUND", RELEASE.parent / "round")
    # The marker the subprocess runs use (the work tree's toplevel).
    monkeypatch.setattr(mod, "marker_path", lambda: _marker(rig))
    real = mod.rnd
    landed: list[str] = []

    def rnd(*args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("cut",) and "--dry-run" not in args and len(landed) < times:
            landed.append(land())
        out: subprocess.CompletedProcess[str] = real(*args, **kw)
        return out

    monkeypatch.setattr(mod, "rnd", rnd)
    return mod, landed


def _lands_requiring(rig: Rig, b: str) -> Callable[[], str]:
    def land() -> str:
        a = _commit_trailer(rig, "src/a.py", "a on b", f"Requires: {b}")
        _push_main(rig)
        return a

    return land


def test_round_cut_pushes_nothing_when_main_moved_past_main_at(rig: Rig) -> None:
    """round cut --main-at: the refreshed origin/main authorizes the push,
    so it must be the snapshot the pairings were checked against."""
    _open_round(rig)
    c2, c3 = rig.shas["c2"], rig.shas["c3"]
    out = rig.round("cut", "--sha", c3, "--main-at", c2)
    assert out.returncode == 1
    assert MAIN_MOVED in out.stderr
    assert rig.origin_ref("release/r1") == ""
    assert _rels(_round_json(rig)) == []
    out = rig.round("cut", "--sha", c3, "--main-at", c3)
    assert out.returncode == 0, out.stdout + out.stderr
    assert rig.origin_ref("release/r1") == c3


def test_cut_rechecks_pairings_when_a_lane_lands_before_round_cut(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Review finding (1): a first cut pinned at B is checked, then A
    (Requires: B) lands before round cut refreshes main. Publishing B now
    would ship half the pair: round refuses on the moved snapshot, the
    re-check on the fresh fetch sees A straddle the cut, and it holds."""
    b = _commit(rig.root, "src/b.py", "b base")
    _push_main(rig)
    mod, landed = _racing_release(rig, tmp_path, monkeypatch, _lands_requiring(rig, b))
    rc = mod.main(["--cut-only", "--sha", b, *FAST])
    out = capsys.readouterr()
    assert len(landed) == 1
    assert rc == 1, out.out + out.err
    assert MAIN_MOVED in out.err
    refusal = classify_cut_refusal(out.out + out.err)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "straddle the cut" in refusal["line"]
    assert rig.origin_ref("release/r1") == ""


def test_cut_retries_on_moved_main_and_cuts_when_still_whole(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A moved main is a re-check, not a failure: an unrelated land in the
    window costs one retry, and the pinned cut still goes out."""
    c3 = rig.shas["c3"]

    def land() -> str:
        sha = _commit(rig.root, "src/x.py", "unrelated")
        _push_main(rig)
        return sha

    mod, landed = _racing_release(rig, tmp_path, monkeypatch, land)
    rc = mod.main(["--cut-only", "--sha", c3, *FAST])
    out = capsys.readouterr()
    assert len(landed) == 1
    assert rc == 0, out.out + out.err
    assert MAIN_MOVED in out.err
    assert f"RELEASE_CUT: r1 {c3}" in out.out.splitlines()
    assert rig.origin_ref("release/r1") == c3


def test_cut_holds_when_main_keeps_moving_past_the_retry_cap(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Heavy landing is transient: main moving on every one of the
    MAX_CUT_RACES + 1 tries ends in a `holding: ` wait, not an
    unclassified refusal, and nothing is pushed."""
    c3 = rig.shas["c3"]
    count = iter(range(100))

    def land() -> str:
        sha = _commit(rig.root, f"src/x{next(count)}.py", "unrelated")
        _push_main(rig)
        return sha

    mod, landed = _racing_release(rig, tmp_path, monkeypatch, land, times=100)
    rc = mod.main(["--cut-only", "--sha", c3, *FAST])
    out = capsys.readouterr()
    assert len(landed) == mod.MAX_CUT_RACES + 1
    assert rc == 1, out.out + out.err
    refusal = classify_cut_refusal(out.out + out.err)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "kept moving" in refusal["line"]
    assert "gave up" not in out.err
    assert rig.origin_ref("release/r1") == ""


def test_pending_cut_already_on_origin_finalizes_when_a_counterpart_lands(
    rig: Rig,
) -> None:
    """Review finding (2): B's push reached origin but the pending record
    was never finalized, then A (Requires: B) landed. Reconciling publishes
    nothing new, so it finalizes the branch origin holds instead of
    holding on a pairing that landed after B was cut."""
    b = rig.shas["c3"]
    out = _release(rig, "--cut-only", "--sha", b, *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert rig.origin_ref("release/r1") == b
    # A crash between round's push and its finalize write.
    path = rig.root / ".git" / "precis-round" / "round.json"
    cur = json.loads(path.read_text(encoding="utf-8"))
    _rel(cur)["cut_pending"] = True
    path.write_text(json.dumps(cur), encoding="utf-8")
    _commit_trailer(rig, "src/a.py", "a on b", f"Requires: {b}")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "holding:" not in out.stdout + out.stderr
    assert "already on origin" in out.stdout
    assert f"RELEASE_CUT: r1 {b}" in out.stdout.splitlines()
    assert "cut_pending" not in _rel(_round_json(rig))


def test_pending_reconcile_holds_when_a_lane_lands_before_round_cut(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pending cut whose reconcile would push is a publication: the
    pairing check pins its snapshot, and A landing in the window holds the
    reconcile with the intent (record + marker) kept."""
    base = rig.shas["c3"]
    hook = _reject_release_pushes(rig)
    assert _release(rig, "--cut-only", "--sha", base, *FAST).returncode == 1
    hook.unlink()
    mod, landed = _racing_release(
        rig, tmp_path, monkeypatch, _lands_requiring(rig, base)
    )
    rc = mod.main(["--cut-only", *FAST])
    out = capsys.readouterr()
    assert len(landed) == 1
    assert rc == 1, out.out + out.err
    assert MAIN_MOVED in out.err
    refusal = classify_cut_refusal(out.out + out.err)
    assert refusal["class"] == REFUSAL_HOLDING
    assert f"pending cut at {base[:9]}" in refusal["line"]
    assert rig.origin_ref("release/r1") == ""
    assert _rel(_round_json(rig))["cut_pending"] is True
    assert json.loads(_marker(rig).read_text(encoding="utf-8"))["base"] == base


def test_round_cut_holds_when_main_cannot_be_fetched(rig: Rig) -> None:
    """td759095 review: a fetch miss under --main-at is transient — a
    `holding: ` wait that costs no attempt, never a plain failure."""
    _open_round(rig)
    c3 = rig.shas["c3"]
    gone = rig.origin.with_name("origin.gone")
    rig.origin.rename(gone)
    try:
        out = rig.round("cut", "--sha", c3, "--main-at", c3)
    finally:
        gone.rename(rig.origin)
    assert out.returncode == 1
    refusal = classify_cut_refusal(out.stdout + out.stderr)
    assert refusal["class"] == REFUSAL_HOLDING, out.stderr
    assert "could not fetch origin/main" in refusal["line"]
    assert rig.origin_ref("release/r1") == ""
    assert _rels(_round_json(rig)) == []


def _pending_on_origin(rig: Rig) -> str:
    """A cut at c3 whose push reached origin while its record still says
    cut_pending (a crash between round's push and its finalize write)."""
    b = rig.shas["c3"]
    out = _release(rig, "--cut-only", "--sha", b, *FAST)
    assert out.returncode == 0, out.stdout + out.stderr
    path = rig.root / ".git" / "precis-round" / "round.json"
    cur = json.loads(path.read_text(encoding="utf-8"))
    _rel(cur)["cut_pending"] = True
    path.write_text(json.dumps(cur), encoding="utf-8")
    return b


def test_round_cut_require_pushed_never_pushes_a_pending_branch(rig: Rig) -> None:
    """--require-pushed: a pending cut is finalized only when origin holds
    its branch; an absent branch refuses with nothing pushed."""
    b = _pending_on_origin(rig)
    _git(rig.root, "push", "-q", "origin", "--delete", "release/r1")
    out = rig.round("cut", "--require-pushed")
    assert out.returncode == 1
    refusal = classify_cut_refusal(out.stdout + out.stderr)
    assert refusal["class"] == REFUSAL_HOLDING, out.stderr
    assert rig.origin_ref("release/r1") == ""
    assert _rel(_round_json(rig))["cut_pending"] is True
    _push_branch(rig, "release/r1", b)
    out = rig.round("cut", "--require-pushed")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "cut_pending" not in _rel(_round_json(rig))


def test_pushed_pending_branch_deleted_before_round_reads_it_is_not_repushed(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """td759095 review, the resume gap: release reads the pending branch
    on origin and skips the pairing check; the branch is deleted before
    round reads it. Round must not push it unchecked (A, Requires B, has
    landed meanwhile) — it holds, and the next run checks and holds on the
    split."""
    b = _pending_on_origin(rig)
    _commit_trailer(rig, "src/a.py", "a on b", f"Requires: {b}")
    _push_main(rig)

    def delete() -> str:
        _git(rig.root, "push", "-q", "origin", "--delete", "release/r1")
        return "deleted"

    mod, landed = _racing_release(rig, tmp_path, monkeypatch, delete)
    rc = mod.main(["--cut-only", *FAST])
    out = capsys.readouterr()
    assert landed == ["deleted"]
    assert rc == 1, out.out + out.err
    assert "is already on origin" in out.out
    refusal = classify_cut_refusal(out.out + out.err)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "left origin" in refusal["line"]
    assert rig.origin_ref("release/r1") == ""
    assert _rel(_round_json(rig))["cut_pending"] is True
    out2 = _release(rig, "--cut-only", *FAST)
    assert out2.returncode == 1
    assert _hold(out2)["class"] == REFUSAL_HOLDING
    assert "straddle the cut" in _hold(out2)["line"]
    assert rig.origin_ref("release/r1") == ""


def test_red_recut_holds_when_main_moves_before_round_cut(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The red re-cut path's MAIN_MOVED: a lane lands between the re-cut's
    pairing check and round's refresh. Nothing is pushed, the line is a
    `holding: ` wait, and the marker names the red release again so the
    next run resumes it."""
    from tests.test_release_script import _RED_RUNS

    c2 = rig.shas["c2"]
    first = _release(rig, "--cut-only", "--sha", c2, *FAST)
    assert first.returncode == 0, first.stdout + first.stderr
    (rig.gh_dir / c2).write_text(_RED_RUNS, encoding="utf-8")

    def land() -> str:
        sha = _commit(rig.root, "src/x.py", "unrelated")
        _push_main(rig)
        return sha

    mod, landed = _racing_release(rig, tmp_path, monkeypatch, land)
    rc = mod.main(["--cut-only", *FAST])
    out = capsys.readouterr()
    assert len(landed) == 1
    assert "is red and its head is on main" in out.out
    assert rc == 1, out.out + out.err
    assert MAIN_MOVED in out.err
    refusal = classify_cut_refusal(out.out + out.err)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "re-cut of release/r1" in refusal["line"]
    assert rig.origin_ref("release/r2") == ""
    mark = json.loads(_marker(rig).read_text(encoding="utf-8"))
    assert (mark["branch"], mark["base"]) == ("release/r1", c2)
