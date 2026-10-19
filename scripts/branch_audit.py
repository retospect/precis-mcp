"""Landedness verdicts for local branches — the shared test behind
``scripts/branch-audit`` and ``scripts/fleet-wave close`` (gr497896).

Squash-merges defeat both ancestry checks (the squash commit is not an
ancestor of the branch) and per-commit patch-id checks (a multi-commit
squash's patch-id matches no single branch commit), so "did this branch's
work reach main" is answered by several signals, cheapest first:

  ahead      ``rev-list`` on the merge-base range is empty — merged outright
  tree       ``git diff --quiet origin/main branch`` — the branch's tree
             equals main's; nothing left to land regardless of history
  cherry     every ``git cherry`` line is ``-`` — each ahead commit landed
             patch-identical (single-commit squashes, cherry-picks)
  subjects   ahead commit subjects found in ``git log origin/main`` — a
             hint for ``partial`` only, never a landed proof (reused or
             generic subjects would make unlanded work look safe to drop)
  reverse-apply  the ``merge-base..branch`` diff applies in reverse on an
             index seeded from origin/main — the whole diff is on main

A non-empty ``merge-base..branch`` diff is required for the content signals:
an ``--allow-empty`` commit or a revert pair is ahead of main yet lands
nothing, and proves nothing — it stays ``unlanded``.

Verdicts: ``landed`` (any full-coverage signal), ``partial`` (some commits
match — a ``-`` cherry line or a subject hit — or the merge-base diff
applies in neither direction, meaning main holds part of it or has drifted
into its context), else ``unlanded``. Everything is read-only: no fetch, no
ref moves, and the apply checks run on a scratch ``GIT_INDEX_FILE`` — the
worktree is never touched.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Audit:
    """One branch's verdict plus the line's display fields."""

    branch: str
    verdict: str  # landed | partial | unlanded | error
    ahead: int  # commits on the branch side of the merge-base
    age: str  # tip commit's relative date (%cr)
    subject: str  # tip commit's subject (%s)
    matched: int = 0  # ahead commits a signal placed on base
    note: str = ""


class Auditor:
    """Landedness checks for branches against ``base`` in repo ``cwd``.

    The base's subject set is fetched lazily once and reused across audits.
    """

    def __init__(self, cwd: Path | str, base: str = "origin/main") -> None:
        self.cwd = Path(cwd)
        self.base = base
        self._subjects: set[str] | None = None

    def _g(
        self,
        *argv: str,
        inp: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *argv],
            cwd=self.cwd,
            input=inp,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )

    def base_subjects(self) -> set[str]:
        if self._subjects is None:
            self._subjects = set(
                self._g("log", self.base, "--format=%s").stdout.splitlines()
            )
        return self._subjects

    def _applies(self, diff: str, *, reverse: bool) -> bool:
        """Does ``diff`` (reverse-)apply cleanly on ``base``'s tree? A pure
        check against a scratch index — never the worktree."""
        with tempfile.TemporaryDirectory(prefix="branch-audit-") as d:
            env = {**os.environ, "GIT_INDEX_FILE": os.path.join(d, "index")}
            if self._g("read-tree", self.base, env=env).returncode:
                return False
            argv = ["apply", "--cached", "--check"] + (["-R"] if reverse else [])
            return self._g(*argv, "-", inp=diff, env=env).returncode == 0

    def audit(self, branch: str) -> Audit:
        if self._g("rev-parse", "--verify", "-q", f"{branch}^{{commit}}").returncode:
            return Audit(branch, "error", 0, "", "", note="does not resolve")
        tip = self._g("log", "-1", "--format=%cr%x09%s", branch).stdout.strip()
        age, _, subject = tip.partition("\t")
        if self._g("rev-parse", "--verify", "-q", f"{self.base}^{{commit}}").returncode:
            return Audit(
                branch, "error", 0, age, subject, note=f"{self.base} does not resolve"
            )
        mb = self._g("merge-base", self.base, branch).stdout.strip()
        if not mb:
            return Audit(
                branch,
                "unlanded",
                0,
                age,
                subject,
                note=f"no merge-base with {self.base}",
            )
        ahead = int(
            self._g("rev-list", "--count", f"{self.base}..{branch}").stdout.strip()
            or "0"
        )
        cherry = self._g("cherry", self.base, branch).stdout.splitlines()
        patch_marks = [ln.startswith("-") for ln in cherry]
        subjects = self._g(
            "log", f"{mb}..{branch}", "--no-merges", "--format=%s"
        ).stdout.splitlines()
        base_subs = self.base_subjects()
        subject_hits = sum(1 for s in subjects if s in base_subs)
        matched = max(sum(patch_marks), subject_hits)
        # An empty merge-base diff proves nothing by itself: an --allow-empty
        # commit or a revert pair is still unlanded work — every content
        # signal below is gated on a non-empty diff.
        diff = self._g("diff", mb, branch).stdout
        has_diff = bool(diff.strip())
        # LANDED needs a content proof — never subject lines alone. A reused
        # or generic subject ("wip") on an unlanded branch would otherwise
        # let fleet-wave close delete real work. Subjects only feed the
        # partial hint below.
        if (
            ahead == 0
            or (ahead > 0 and len(patch_marks) == ahead and all(patch_marks))
            or (
                has_diff
                and self._g("diff", "--quiet", self.base, branch).returncode == 0
            )
            or (has_diff and self._applies(diff, reverse=True))
        ):
            verdict = "landed"
        elif matched or (diff.strip() and not self._applies(diff, reverse=False)):
            verdict = "partial"
        else:
            verdict = "unlanded"
        return Audit(branch, verdict, ahead, age, subject, matched=matched)
