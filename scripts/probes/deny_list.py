"""deny_list boundary probe (td574934 A4, gr596928).

The deny list is the fence a slice may not write past. Two halves:

* every glob on the deny lists refuses a path —
  :func:`precis.workers.job_types.run_slice.deny_violations` must
  return a witness path built from each glob of
  :data:`lanes.FACTORY_DENY_WRITE_PATHS`, :data:`lanes.HUMAN_ONLY_PATHS`
  and :data:`lanes.INTERIM_DENY` under every resolved set that carries
  it, and a human-only glob must refuse even under an empty lane set
  (never relaxable);
* the lane deny set agrees with the registry —
  :func:`lanes.deny_paths_for` resolves :data:`STRICT_DENY` with no
  setting and the relaxed ``HUMAN_ONLY_PATHS + INTERIM_DENY`` with the
  flag on; :data:`STRICT_DENY` / :data:`ENGINE_PATHS` equal their
  documented derivations; a fresh :class:`lanes.Lane` defaults to
  ``STRICT_DENY``.

When A2's ``precis.plans.boundary`` lands it becomes the hit-list
registry; this probe already prefers its sets when importable
(TODO A2: drop the lanes fallback then).

Contract: exit 0 pass, 1 boundary breach (stdout = the finding), else
cannot-run (exit 2 here). ``argv[1]`` names the tree root (default: cwd).
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from types import SimpleNamespace


def _witness(glob: str) -> str:
    """One concrete path a ``fnmatch`` glob must match: ``**`` → a
    nested pair, ``*``/``?``/``[…]`` → literal fill."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob[i : i + 2] == "**":
            out.append("zz/zz")
            i += 2
        elif glob[i] == "*":
            out.append("zz")
            i += 1
        elif glob[i] == "?":
            out.append("z")
            i += 1
        elif glob[i] == "[":
            j = glob.find("]", i)
            if j < 0:
                raise ValueError(f"unclosed class in glob {glob!r}")
            cls = glob[i + 1 : j]
            out.append(cls[1] if cls.startswith("!") and len(cls) > 1 else cls[0])
            i = j + 1
        else:
            out.append(glob[i])
            i += 1
    return "".join(out)


def _deny_sets() -> dict[str, tuple[str, ...]]:
    """The resolved sets to verify — A2's registry when it exists, else
    the lanes.py tuples (TODO A2)."""
    try:
        from precis.plans import boundary as _b  # A2
    except ImportError:
        _b = None
    if _b is not None and hasattr(_b, "LANE_DENY_SETS"):
        return dict(_b.LANE_DENY_SETS)
    from precis.plans import lanes

    return {
        "strict": lanes.STRICT_DENY,
        "relaxed": lanes.HUMAN_ONLY_PATHS + lanes.INTERIM_DENY,
    }


class _Conn:
    """A store double whose app_settings row reads "1" (relaxed mode)."""

    def execute(self, *_a, **_k):
        return self

    def fetchone(self):
        return ("1",)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Pool:
    def connection(self):
        return _Conn()


def _agreement(lanes) -> list[str]:
    """The lane deny set agrees with the registry."""
    findings: list[str] = []
    strict = lanes.deny_paths_for(None)
    if strict != lanes.STRICT_DENY:
        findings.append(
            "deny_paths_for(None) is not STRICT_DENY — the default deny set "
            "drifted from the registry"
        )
    relaxed = set(lanes.deny_paths_for(SimpleNamespace(pool=_Pool())))
    want_relaxed = set(lanes.HUMAN_ONLY_PATHS) | set(lanes.INTERIM_DENY)
    if relaxed != want_relaxed:
        findings.append("the relaxed deny set is not HUMAN_ONLY_PATHS + INTERIM_DENY")
    want_strict = tuple(
        dict.fromkeys(lanes.FACTORY_DENY_WRITE_PATHS + lanes.HUMAN_ONLY_PATHS)
    )
    if want_strict != lanes.STRICT_DENY:
        findings.append(
            "STRICT_DENY ≠ dedup(FACTORY_DENY_WRITE_PATHS + HUMAN_ONLY_PATHS)"
        )
    want_engine = tuple(
        dict.fromkeys(
            g
            for g in lanes.FACTORY_DENY_WRITE_PATHS + lanes.INTERIM_DENY
            if g not in lanes.HUMAN_ONLY_PATHS
        )
    )
    if want_engine != lanes.ENGINE_PATHS:
        findings.append("ENGINE_PATHS ≠ dedup(FACTORY + INTERIM) − HUMAN_ONLY")
    lane = lanes.Lane(
        name="probe",
        adapter="claude_p",
        credential_pool="probe",
        cap=1,
        task_classes=frozenset(),
    )
    if lane.deny_write_paths != lanes.STRICT_DENY:
        findings.append("a lane's default deny_write_paths is not STRICT_DENY")
    return findings


def check(root: Path) -> list[str]:
    """Every finding; empty = pass."""
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from precis.plans import lanes
    from precis.workers.job_types import run_slice

    findings: list[str] = []
    for label, globs in _deny_sets().items():
        for glob in dict.fromkeys(globs):
            witness = _witness(glob)
            if not fnmatch.fnmatchcase(witness, glob):
                raise ValueError(
                    f"probe bug: witness {witness!r} does not match {glob!r}"
                )
            hits = run_slice.deny_violations([witness], tuple(globs))
            if witness not in hits:
                findings.append(
                    f"deny glob {glob!r} ({label} set) does not refuse {witness!r}"
                )
    for glob in lanes.HUMAN_ONLY_PATHS:
        witness = _witness(glob)
        if witness not in run_slice.deny_violations([witness], ()):
            findings.append(
                f"human-only glob {glob!r} escapes an empty lane deny set "
                f"({witness!r} passed)"
            )
    findings += _agreement(lanes)
    return findings


def main(argv: list[str] | None = None) -> int:
    """Probe entry: ``[tree_root]``; 0 pass, 1 breach, 2 cannot-run."""
    argv = list(sys.argv if argv is None else argv)
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    try:
        findings = check(root)
    except Exception as exc:
        print(f"cannot-run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
