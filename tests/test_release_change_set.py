"""gr687874 — the coupled-commit guard: a release never splits a declared
Change-Set/Requires pairing (Reto 2026-11-15).

Same rig as test_release_script: a throwaway repo with a bare origin and
stubbed gh/deploy. Declarations are commit-message trailers, which is also
how a real slice lands them (ship preserves the trailer block verbatim,
gr517803).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from precis.workers.job_types.release_cut import (
    REFUSAL_HOLDING,
    classify_cut_refusal,
)
from tests.test_release_script import (
    FAST,
    _load_release,
    _reject_release_pushes,
    _release,
)
from tests.test_round_gate import Rig, _commit, _git, _push_branch
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


def test_requires_unresolvable_holds_the_cut(rig: Rig) -> None:
    """A Requires value that is not a resolvable sha cannot be verified —
    the cut holds rather than shipping a maybe-split pair."""
    _commit_trailer(rig, "src/a.py", "a wants b", "Requires: " + "f" * 40)
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 1
    refusal = _hold(out)
    assert refusal["class"] == REFUSAL_HOLDING
    assert "resolves to nothing" in refusal["line"]


def test_requires_non_sha_value_holds_the_cut(rig: Rig) -> None:
    _commit_trailer(rig, "src/a.py", "a wants a gripe", "Requires: gr123")
    _push_main(rig)
    out = _release(rig, "--cut-only", *FAST)
    assert out.returncode == 1
    assert _hold(out)["class"] == REFUSAL_HOLDING


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
        ("b" * 40, "s\n\nChange-Set: trio-1\n"),
        ("a" * 40, "s\n\nChange-Set: trio-1 3\n"),
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
