"""Skill-declared write governance (gr657066 — gr657041 slice 3).

A skill's frontmatter may declare ``governs: factory``: the skill
teaches the factory's own procedure, so an in-branch edit needs Reto's
sign-off and is refused to every lane — builder and skill-editor alike.
The declaration is read two ways:

* :func:`governed_skill_paths_at_ref` resolves the tagged files in the
  slice's land base (``origin/main``, or a release fix's
  ``origin/release/r<N>``). The write gates — run_slice's brief/launch
  deny list and post-check (:func:`run_slice._deny_reason`) and
  land_slice's last gate — union these paths into the deny set.
  Resolving at the base makes the fence unbypassable from the branch:
  editing the ``governs:`` line out of a governed skill is itself a
  write to a governed path.
* :func:`governed_skill_paths` resolves the same set from the
  filesystem for advisory routing (plan_dispatch / gripe_dispatch
  pre-mint coordinator routing): a brief whose judged text names a
  governed skill routes to a coordinator ask instead of minting a
  builder.
"""

from __future__ import annotations

import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

from precis.utils.skill_common import FrontmatterError, parse_frontmatter

_BUILTIN_SKILLS = Path("precis") / "data" / "skills"

#: The git seam callers inject — the ``(args, cwd)`` shape of
#: ``run_slice._git``, so worker fakes and monkeypatches keep working.
GitRun = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _checkout_root() -> Path | None:
    """The repo checkout this module runs from (pyproject present beside
    ``src/``), else ``None`` — an installed tree has no repo layout."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / "pyproject.toml").is_file() else None


def _entry_point_targets(pyproject: str) -> tuple[str, ...]:
    """``precis.skills`` entry-point package names from pyproject text."""
    try:
        eps = tomllib.loads(pyproject)["project"]["entry-points"].get(
            "precis.skills", {}
        )
    except (KeyError, tomllib.TOMLDecodeError):
        return ()
    return tuple(sorted(eps.values()))


def _pkg_base(root: Path | None) -> Path:
    """The dir holding the ``precis`` package — ``<checkout>/src`` when
    ``root`` is a repo checkout, else this install's package parent."""
    if root is not None and (root / "src" / _BUILTIN_SKILLS).is_dir():
        return root / "src"
    return Path(__file__).resolve().parents[2]


def _skill_dirs(base: Path, pyproject: str | None) -> list[Path]:
    """Every skill corpus dir under package root ``base``: the built-in
    corpus plus each ``precis.skills`` entry-point package dir
    (``pyproject`` is the text at the ref/checkout being read, ``None``
    when unavailable — then the built-in corpus alone)."""
    dirs = [base / _BUILTIN_SKILLS]
    if pyproject is not None:
        for target in _entry_point_targets(pyproject):
            dirs.append(base / Path(*target.split(".")))
    return dirs


def governed_skill_paths(
    root: Path | None = None, *, governs: str = "factory"
) -> tuple[str, ...]:
    """Repo-relative paths of skills declaring ``governs: <domain>`` —
    the filesystem variant, for advisory routing. The write gates use
    :func:`governed_skill_paths_at_ref` instead: the fence a branch is
    measured against must come from its base, not from a checkout the
    branch itself edits. Returns ``()`` when the corpus is unreadable —
    routing fails open like its other classifier inputs; the run/land
    gates fail closed on their own scan."""
    root = root or _checkout_root()
    base = _pkg_base(root)
    pyproject = None
    if root is not None:
        try:
            pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        except OSError:
            pyproject = None
    out: list[str] = []
    for d in _skill_dirs(base, pyproject):
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            if p.name.startswith("__"):
                continue
            try:
                fm = parse_frontmatter(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, FrontmatterError):
                continue
            if governs in fm.governs:
                out.append("src/" + p.relative_to(base).as_posix())
    return tuple(sorted(out))


def governed_skill_paths_at_ref(
    repo: Path, ref: str, *, governs: str = "factory", git: GitRun = _git
) -> tuple[str, ...] | None:
    """Repo-relative paths of the skills that declare ``governs:
    <domain>`` in ``ref``'s tree, or ``None`` when git cannot answer —
    callers fail closed, matching :func:`run_slice.changed_paths`.

    ``ref`` is the slice's land base (``origin/main`` …): a branch
    cannot un-tag the fence because the set is enumerated from the base
    commit's blobs, never the branch's. ``git`` is the caller's own
    ``(args, cwd)`` git seam (``run_slice._git`` / ``land_slice._git``)
    so their fakes drive this scan too."""

    def run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return git(args, repo)
        except (OSError, subprocess.SubprocessError):
            return None

    # ``rev-parse <ref>`` (not ``--verify``): a real repo answers rc 0 +
    # the sha for a resolvable ref and rc 128 otherwise; worker fakes
    # leave the bare form permissive while their gated probes stay theirs.
    probe = run(["rev-parse", f"{ref}^{{commit}}"])
    if probe is None or probe.returncode != 0:
        return None
    spec = run(["show", f"{ref}:pyproject.toml"])
    if spec is None:
        return None
    pyproject: str | None
    if spec.returncode == 0:
        pyproject = spec.stdout
    elif "not in" in spec.stderr or "does not exist" in spec.stderr:
        # The ref's tree carries no pyproject — no precis.skills entry
        # points can be declared there, so the built-in corpus is the
        # whole governed surface.
        pyproject = None
    else:
        return None
    dirs = ["src/" + _BUILTIN_SKILLS.as_posix()] + [
        "src/" + "/".join(target.split("."))
        for target in _entry_point_targets(pyproject or "")
    ]
    # Prefilter to blobs mentioning the axis at all — one grep for the
    # whole corpus instead of a `git show` per skill file.
    proc = run(["grep", "-l", "-F", "-e", "governs:", ref, "--", *dirs])
    if proc is None or proc.returncode not in (0, 1):
        return None
    if proc.returncode == 1:
        return ()
    out: list[str] = []
    for line in proc.stdout.splitlines():
        _, _, path = line.partition(":")
        if not path.endswith(".md"):
            continue
        blob = run(["show", f"{ref}:{path}"])
        if blob is None or blob.returncode != 0:
            return None
        try:
            fm = parse_frontmatter(blob.stdout)
        except FrontmatterError:
            continue
        if governs in fm.governs:
            out.append(path)
    return tuple(sorted(set(out)))
