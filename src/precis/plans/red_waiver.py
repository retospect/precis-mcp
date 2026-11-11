"""``red_waiver`` — the release gate's "deploy anyway and gripe" waiver.

Reto 2026-11-10 ("make it so"): one low-risk red test used to block the
whole release, and each fix cost a re-cut plus 35-50 min of CI (1-1.5 h per
release over the last three). With setting :data:`SETTING_KEY`
(``release.red_waiver``, ``on`` by default) the ``release-fix`` loop's
digest node (:mod:`precis.workers.job_types.ci_red_digest`) may instead
*waive* a TEST red — never an infra red, which that module rules first.

The decision is one pure function, :func:`waiver_decision`; the waiver
applies iff ALL of:

* the setting is explicitly on (:func:`waiver_enabled`; an unknown value
  or a failed read reads off);
* the red is a COMPLETE test red: the run's jobs all concluded success or
  failure (:func:`partial_run_problem`), no failed-first / skipped-by-
  design matrix, no collection error, every failed job's ids parsed and
  matching its own ``N failed`` count (:func:`parse_failures`);
* 1 to :data:`MAX_WAIVED_TESTS` failing test ids parse from the digest;
* no failing test id matches a :data:`TEST_HARD_STOPS` pattern;
* the cut range (fetched, :func:`range_paths`) touches no
  :func:`path_hard_stop` path;
* a culprit commit is attributable (:func:`culprit_commits`) — no
  attribution, no waiver;
* none of the failing ids sits on a waiver gripe still unfixed
  (:func:`open_waived_tests`) — a red cannot be carried forward forever.

Granted, the digest files a top-priority ``broken-build`` gripe
(:func:`file_waiver_gripe`: tags ``fix:code`` and ``release-waiver:r<N>``,
``refs.prio`` 1, ``meta.release_waiver`` carrying the ids), records the
cut's verdict as ``green`` with a ``waived`` marker
(:func:`precis.plans.release.record_ci_verdict`; the tests-green gate and
the loop's ``until`` read it as green, so the pass goes on to the deploy
ask with no new cut), and closes the iteration's nodes
(:func:`iteration_nodes`). ``gripe_dispatch`` mints the gripe's chain at
once and the chain claims in the release band.

Safety net: when a LATER release confirms and a waiver gripe is still
unfixed with no fix chain in flight, :func:`mint_reverts` mints a
``gripe-fix-direct`` chain, "Revert the culprits of gr<N>", once per
gripe.
"""

from __future__ import annotations

import ast
import fnmatch
import logging
import re
import subprocess
from collections.abc import Collection, Iterable, Sequence
from pathlib import Path
from typing import Any

from precis.plans import release as _release
from precis.store.types import ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES
from precis.utils import dispatch_score as _dscore
from precis.utils.secret_scan import mask_secrets

log = logging.getLogger(__name__)

#: Registry key (``precis.settings.REGISTRY``): ``on`` | ``off``.
SETTING_KEY = "release.red_waiver"
#: Failing tests one waiver may cover (Reto's refined ruling, 2026-11-10).
MAX_WAIVED_TESTS = 2

#: Tag prefix of the waiver gripe (``release-waiver:r<N>``); the same
#: literal ``gripe_dispatch.WAIVER_TAG_PREFIX`` keys its fast path on.
WAIVER_TAG_PREFIX = "release-waiver:"
#: ``refs.meta`` key of the waiver gripe's record.
WAIVER_META_KEY = "release_waiver"
#: Tag that marks the gripe a broken build.
BROKEN_BUILD_TAG = "broken-build"
#: ``meta`` key on a revert chain root: the waiver gripe it reverts for.
REVERT_META_KEY = "release_revert_of"

#: A failing test whose id (path + name, case-insensitive substring) names
#: one of these is never waived. Substring on purpose: over-matching
#: ("ground" for "round") only refuses a waiver, never grants one.
TEST_HARD_STOPS: tuple[str, ...] = (
    # Data safety: schema and the append-only store are irreversible.
    "migration",
    "migrat",
    "schema",
    "store",
    "chunk",
    "invariant",
    "backup",
    # Credentials and the outbound-fetch guard.
    "auth",
    "oauth",
    "vault",
    "credential",
    "secret",
    "safe_fetch",
    "ssrf",
    # The release machinery itself: a waived red here would hide a red in
    # the gate that decides waivers.
    "release",
    "round",
    "ship",
    "deploy",
    "gate",
    "bump",
    # CI plumbing and the waiver's own logic.
    "ci_",
    "waiver",
)

#: Path substrings (lower-case, matched ANYWHERE in the path; over-match
#: only refuses a waiver): the release/ship/deploy/round machinery and
#: gates, backups, migrations, version bumps, CI scripts and jobs, the
#: waiver itself and trust rules, auth and credential handling, the SSRF
#: guard, and the store layer.
PATH_HARD_STOP_SUBSTRINGS: tuple[str, ...] = (
    "release",
    "deploy",
    "round",
    "ship",
    "backup",
    "gate",
    "migrat",
    "bump",
    "ci-",
    "ci_",
    "waiver",
    "trust",
    "auth",
    "oauth",
    "vault",
    "credential",
    "secret",
    "safe_fetch",
    "store/",
)
#: Exact files: the SSRF guard and the release/gate/waiver logic.
PATH_HARD_STOP_FILES: tuple[str, ...] = (
    "src/precis/utils/safe_fetch.py",
    "src/precis/plans/release.py",
    "src/precis/plans/gates.py",
    "src/precis/plans/red_waiver.py",
)
#: Path prefixes: the release/ship/test harness (``scripts/release*``,
#: ``round*``, ``ship*``, ``lib/``, ``test``, ``ci-failed``), CI config,
#: deploy configuration, and the store (the append-only chunk layer).
PATH_HARD_STOP_PREFIXES: tuple[str, ...] = (
    "scripts/release",
    "scripts/round",
    "scripts/ship",
    "scripts/test",
    "scripts/ci-failed",
    "scripts/lib/",
    ".github/",
    "deploy/",
    "src/precis/store/",
)
#: Globs: forward-only migrations anywhere, and the release/CI job types.
PATH_HARD_STOP_GLOBS: tuple[str, ...] = (
    "*/migrations/*",
    "migrations/*",
    "src/precis/workers/job_types/release_*.py",
    "src/precis/workers/job_types/ci_*.py",
)

#: How many hard-stop hits a refusal reason names.
_REASON_HITS = 4


def waiver_enabled(store: Any) -> bool:
    """``release.red_waiver`` resolved FAIL-CLOSED: only an explicit
    stored ``on``, or no stored row at all (the registry default, ``on``),
    grants. ``off``, any unrecognised value and a failed DB read are all
    False (the lenient ``settings.get_str`` would swallow a read error
    into the default)."""
    from precis import settings

    try:
        raw = settings._db_read_strict(store, SETTING_KEY)
    except Exception:
        log.warning("%s: settings read failed; waiver off", SETTING_KEY, exc_info=True)
        return False
    if raw is None:
        raw = str(settings.REGISTRY[SETTING_KEY].default)
    return raw.strip().lower() == "on"


def hard_stop_hits(test_id: str) -> list[str]:
    """The :data:`TEST_HARD_STOPS` patterns ``test_id`` names."""
    low = test_id.lower()
    return [p for p in TEST_HARD_STOPS if p in low]


def path_hard_stop(path: str) -> str | None:
    """The hard-stop pattern ``path`` matches, else ``None``."""
    p = path.strip()
    while p.startswith("./"):
        p = p[2:]
    low = p.lower()
    for sub in PATH_HARD_STOP_SUBSTRINGS:
        if sub in low:
            return f"*{sub}*"
    if p in PATH_HARD_STOP_FILES:
        return p
    for pre in PATH_HARD_STOP_PREFIXES:
        if p.startswith(pre):
            return pre
    for glob in PATH_HARD_STOP_GLOBS:
        if fnmatch.fnmatch(p, glob):
            return glob
    return None


def waiver_decision(
    failing_tests: Sequence[str],
    range_paths: Sequence[str],
    prior_waived: Collection[str],
) -> tuple[bool, str]:
    """``(ok, reason)``: whether a test red may be waived (module
    docstring). Pure. ``failing_tests`` are the pytest node ids parsed
    from the digest, ``range_paths`` every path the cut range touches,
    ``prior_waived`` the ids on any waiver gripe still unfixed
    (:func:`open_waived_tests`). ``reason`` names the first rule that
    refused, or summarises the grant."""
    tests = list(dict.fromkeys(t.strip() for t in failing_tests if t.strip()))
    if not tests:
        return False, "no failing test id parsed from the digest (not a test red)"
    if len(tests) > MAX_WAIVED_TESTS:
        return (
            False,
            f"{len(tests)} failing tests exceed the waiver cap of {MAX_WAIVED_TESTS}",
        )
    for t in tests:
        hits = hard_stop_hits(t)
        if hits:
            return False, f"failing test {t} matches hard-stop pattern(s) {hits}"
    paths = [p for p in range_paths if p.strip()]
    if not paths:
        return False, "the cut range lists no paths (unreadable); cannot judge it"
    stops = [(p, h) for p in paths if (h := path_hard_stop(p)) is not None]
    if stops:
        shown = ", ".join(f"{p} [{h}]" for p, h in stops[:_REASON_HITS])
        more = (
            f" (+{len(stops) - _REASON_HITS} more)" if len(stops) > _REASON_HITS else ""
        )
        return False, f"the cut range touches hard-stop path(s): {shown}{more}"
    prior = set(prior_waived)
    again = [t for t in tests if t in prior]
    if again:
        return (
            False,
            f"already waived and still unfixed: {', '.join(again)} "
            "(a red is not carried forward twice)",
        )
    return (
        True,
        f"{len(tests)} failing test(s), {len(paths)} range path(s), no "
        "hard-stop hit, none on a still-unfixed waiver gripe",
    )


# ── the digest's failing ids ───────────────────────────────────────

_SECTION = re.compile(r"^== (.+?) ==$", re.MULTILINE)
_BLIND = "(no signal lines"
_IDS_HEAD = re.compile(r"^\s*failed ids \((\d+)\):\s*$")
_ID_LINE = re.compile(r"^ {4}(\S.*?)\s*$")
_MORE = re.compile(r"^\s*… \+(\d+) more")
#: ``ci-failed``'s own summary-line shape (``_SUMMARY_ID``) plus its
#: `` - reason`` tail, the assertion line.
_FAILED_LINE = re.compile(r"^\s*(?:FAILED|ERROR)\s+(.+?)(?:\s+-\s+(.*))?\s*$")
#: pytest's closing line (``=== 2 failed, 40 passed, 1 error in 3s ===``):
#: the count the parsed ids are checked against.
_SUMMARY_LINE = re.compile(r"^\s*=+\s*(.*\b\d+ (?:failed|errors?)\b.*?)\s*=+\s*$")
_N_FAILED = re.compile(r"(\d+) failed\b")
_N_ERRORS = re.compile(r"(\d+) errors?\b")
#: A collection error or an aborted session: nothing ran to completion.
_COLLECTION = re.compile(
    r"errors? during collection|^\s*Interrupted:|collection error|ERROR collecting",
    re.IGNORECASE | re.MULTILINE,
)
#: ``ci_red_digest`` appends this to a job's rescued lines when the
#: rescue cap cut some: the ids list is then known-incomplete.
RESCUE_TRUNCATED = "… rescued failure lines cut at the rescue cap"
#: The job whose red means the full matrix never ran, and ci-failed's
#: note for it.
_FAILED_FIRST = "failed-first"
_SKIPPED_BY_DESIGN = "skipped by design"


def parse_failures(
    log_text: str, rescued: dict[str, list[str]] | None = None
) -> tuple[dict[str, str], list[str]]:
    """``({test id: assertion line}, problems)`` from a ``scripts/ci-failed``
    digest. Per ``== job ==`` section the ids come from the ``failed ids``
    block, else the ``FAILED``/``ERROR`` summary lines; a blind section
    (no signal lines) takes ``rescued[job]`` instead (the jobs-logs read
    of ``ci_red_digest``). ``problems`` is non-empty — and the caller
    refuses the waiver — whenever the digest cannot be shown to list EVERY
    failing test of a complete run:

    * a section yields no ids, an id list or the rescue was truncated, or
      the ids parsed differ from the section's own ``N failed`` (+ errors)
      count or its ``failed ids (N)`` header, or that count is absent;
    * the red is the ``failed-first`` job (or the digest carries the
      "skipped by design" note): the full matrix never ran;
    * a collection error, an ``Interrupted:`` session, or an ``ERROR``
      line on a file id with no ``::test``."""
    rescued = rescued or {}
    marks = list(_SECTION.finditer(log_text))
    failures: dict[str, str] = {}
    problems: list[str] = []
    if not marks:
        return failures, ["the digest has no failed-job section"]
    if _SKIPPED_BY_DESIGN in log_text:
        problems.append("the matrix was skipped by design (failed-first red)")
    everything = log_text + "\n" + "\n".join(x for v in rescued.values() for x in v)
    if _COLLECTION.search(everything):
        problems.append("a collection error or an interrupted session")
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(log_text)
        name = m.group(1)
        if name.strip().lower().startswith(_FAILED_FIRST):
            problems.append(f"job {name}: the full test matrix never ran")
        body = log_text[m.end() : end]
        lines = body.splitlines()
        if _BLIND in body:
            lines = list(rescued.get(name) or [])
            if not lines:
                problems.append(f"job {name}: its log is blind (no failing ids)")
                continue
        if any(x.strip() == RESCUE_TRUNCATED for x in lines):
            problems.append(f"job {name}: rescued failure lines were cut at the cap")
        reasons: dict[str, str] = {}
        for line in lines:
            fm = _FAILED_LINE.match(line)
            if fm:
                reasons.setdefault(fm.group(1), (fm.group(2) or "").strip())
                if line.lstrip().startswith("ERROR") and "::" not in fm.group(1):
                    # An ERROR on a file (or a bare setup line), not a test:
                    # a collection / import error — nothing here ran.
                    problems.append(
                        f"job {name}: ERROR on {fm.group(1)!r}, not a test (collection?)"
                    )
        ids: list[str] = []
        header: int | None = None
        in_block = False
        for line in lines:
            hm = _IDS_HEAD.match(line)
            if hm:
                in_block = True
                header = int(hm.group(1))
                continue
            if in_block:
                more = _MORE.match(line)
                if more:
                    problems.append(
                        f"job {name}: id list truncated (+{more.group(1)} more)"
                    )
                    continue
                im = _ID_LINE.match(line)
                if im and not im.group(1).startswith(("FAILED", "ERROR")):
                    ids.append(im.group(1))
                    continue
                in_block = False
        if not ids:
            ids = list(reasons)
        if not ids:
            problems.append(f"job {name}: no failing test ids (not a pytest red)")
            continue
        ids = list(dict.fromkeys(ids))
        if header is not None and header != len(ids):
            problems.append(f"job {name}: {len(ids)} ids parsed, header says {header}")
        bare = [t for t in ids if "::" not in t]
        if bare:
            problems.append(
                f"job {name}: id(s) without a test name (collection error?): "
                + ", ".join(bare[:3])
            )
        expected = _summary_count(lines)
        if expected is None:
            problems.append(f"job {name}: no pytest 'N failed' count to check ids")
        elif expected != len(ids):
            problems.append(
                f"job {name}: {len(ids)} ids parsed but the summary counts {expected}"
            )
        for tid in ids:
            failures.setdefault(tid, reasons.get(tid, ""))
    return failures, problems


def _summary_count(lines: Sequence[str]) -> int | None:
    """``failed + errors`` from the section's pytest summary line, else
    ``None`` (absent or cut off)."""
    for line in reversed(lines):
        sm = _SUMMARY_LINE.match(line)
        if sm is None:
            continue
        text = sm.group(1)
        failed = sum(int(n) for n in _N_FAILED.findall(text))
        errors = sum(int(n) for n in _N_ERRORS.findall(text))
        return failed + errors
    return None


#: Job conclusions of a run whose picture is complete. ``skipped`` is
#: normal: check.yml always skips its conditional jobs (test-docs,
#: coverage-db, test-other, publish, failed-first, and the soak /
#: test-linux / verdict-soak legs after a gate-fast red). Anything else —
#: cancelled, timed_out, startup_failure, action_required, neutral, stale
#: or no conclusion — means a job that never reported.
COMPLETE_JOB_CONCLUSIONS = frozenset({"success", "failure", "skipped"})


def run_in_progress(state: dict[str, Any] | None) -> bool:
    """``True`` when ``state`` is a readable run that has not completed —
    the caller parks and re-reads instead of refusing for good."""
    return state is not None and str(state.get("status") or "") != "completed"


def partial_run_problem(state: dict[str, Any] | None) -> str | None:
    """Why the run is not a COMPLETE pass/fail picture, else ``None``:
    ``state`` is ``gh run view --json status,jobs`` (``ci_red_digest.
    _run_state``). Unreadable, not completed, jobless, or any job whose
    conclusion is outside :data:`COMPLETE_JOB_CONCLUSIONS` — a job that
    never reported may hold more reds."""
    if state is None:
        return "the run state is unreadable"
    if str(state.get("status") or "") != "completed":
        return f"the run is {state.get('status') or 'not completed'}, not completed"
    jobs = [j for j in state.get("jobs") or [] if isinstance(j, dict)]
    if not jobs:
        return "the run lists no jobs"
    odd = [
        f"{j.get('name') or '?'}={j.get('conclusion') or j.get('status') or '?'}"
        for j in jobs
        if str(j.get("conclusion") or "") not in COMPLETE_JOB_CONCLUSIONS
    ]
    if odd:
        shown = ", ".join(odd[:4]) + (
            f" (+{len(odd) - 4} more)" if len(odd) > 4 else ""
        )
        return f"job(s) neither success nor failure: {shown}"
    return None


# ── the cut range, from git ────────────────────────────────────────


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str | None:
    """One ``git -C root`` read; ``None`` on any failure."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout if cp.returncode == 0 else None


def range_paths(
    root: Path, rng: str, *, env: dict[str, str] | None = None
) -> list[str] | None:
    """Every path ``A..B`` touches (renames as delete + add), else ``None``
    when ``rng`` is not a two-sided range or git cannot read it.

    Fetches origin first, as ``release_cut`` does (``env`` is the factory
    git credential env), so a cut pushed from another checkout is
    present; a failed fetch is tolerated only when both ends are already
    here — a head that is still missing refuses. Paths come NUL-split
    from ``-z`` with ``core.quotePath=false``, so unusual names are not
    C-quoted into something a hard-stop match would miss."""
    if ".." not in rng:
        return None
    _git(root, "fetch", "-q", "origin", env=env)
    for end in (e for e in rng.split("..", 1) if e):
        if _git(root, "rev-parse", "--verify", "--quiet", f"{end}^{{commit}}") is None:
            return None
    out = _git(
        root,
        "-c",
        "core.quotePath=false",
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        rng,
    )
    return None if out is None else [p for p in out.split("\0") if p.strip()]


def _log(root: Path, rng: str, paths: Iterable[str] | None) -> list[tuple[str, str]]:
    args = ["log", "--no-merges", "--format=%h%x09%s", rng]
    if paths is not None:
        args += ["--", *paths]
    out = _git(root, *args) or ""
    rows = []
    for ln in out.splitlines():
        sha, _, subject = ln.partition("\t")
        if sha:
            rows.append((sha, subject))
    return rows


def file_of_test(test_id: str) -> str:
    """The file part of a pytest node id."""
    return test_id.split("::", 1)[0].strip()


def imported_src_paths(source: str) -> list[str]:
    """Candidate ``src/precis`` paths of the ``precis`` modules a test
    file's source imports (``import precis.a.b``, ``from precis.a import
    b`` — ``b`` may be a module or a name in ``a``)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module)
            mods.update(f"{node.module}.{a.name}" for a in node.names)
    paths: list[str] = []
    for mod in sorted(mods):
        if mod != "precis" and not mod.startswith("precis."):
            continue
        base = "src/" + mod.replace(".", "/")
        paths += [f"{base}.py", f"{base}/__init__.py"]
    return paths


def culprit_commits(
    root: Path, rng: str, test_ids: Iterable[str]
) -> list[tuple[str, str]]:
    """The range commits that touched a failing test file or a src module
    one imports (read at the range's end). NO fallback to the whole
    range: an empty list means the red is not attributable and the caller
    refuses the waiver."""
    end = rng.rsplit("..", 1)[-1]
    paths: list[str] = []
    for tf in dict.fromkeys(file_of_test(t) for t in test_ids):
        if not tf:
            continue
        paths.append(tf)
        src = _git(root, "show", f"{end}:{tf}")
        if src:
            paths += imported_src_paths(src)
    return _log(root, rng, dict.fromkeys(paths)) if paths else []


# ── prior waivers and the waiver gripe ─────────────────────────────


def _rn_num(rn: str) -> int | None:
    return int(rn[1:]) if re.fullmatch(r"r\d+", rn) else None


#: Gripe STATUS values that end a waiver's pressure: fixed (``done``;
#: ``verify``/``auto_verify``/``user_verify``/``in_review`` are fixes
#: awaiting confirmation) or ruled not-to-be-fixed (``wontfix``, a human
#: decision to keep the red).
GRIPE_FIXED_STATUSES = frozenset(
    {"done", "wontfix", "verify", "auto_verify", "user_verify", "in_review"}
)


def _gripes_tagged(store: Any, tag: str) -> list[tuple[int, dict[str, Any]]]:
    """``(gripe id, meta)`` of live gripes carrying open tag ``tag`` and a
    ``meta.release_waiver`` that is a JSON object."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta FROM refs r
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND jsonb_typeof(r.meta -> %s::text) = 'object'
               AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                              AND t.value = %s)
             ORDER BY r.ref_id
            """,
            (WAIVER_META_KEY, tag),
        ).fetchall()
    return [(int(i), dict(m or {})) for i, m in rows]


def waiver_gripe_for(store: Any, rn: str) -> int | None:
    """The waiver gripe already filed for release ``rn``, if any (a
    retried digest reuses it rather than filing a second)."""
    found = _gripes_tagged(store, f"{WAIVER_TAG_PREFIX}{rn}")
    return found[0][0] if found else None


def open_waived_tests(store: Any, rn: str) -> set[str]:
    """Test ids on ANY waiver gripe still unfixed (status outside
    :data:`GRIPE_FIXED_STATUSES`), whatever its release number — a red
    already waived and not yet fixed is never waived again. The gripe of
    release ``rn`` itself is excluded: a retried digest must converge on
    its own waiver, not refuse it."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta, t.value FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND jsonb_typeof(r.meta -> %s::text) = 'object'
               AND t.namespace = 'OPEN' AND t.value LIKE %s
            """,
            (WAIVER_META_KEY, f"{WAIVER_TAG_PREFIX}%"),
        ).fetchall()
    out: set[str] = set()
    for gid, meta, tag in rows:
        if str(tag) == f"{WAIVER_TAG_PREFIX}{rn}":
            continue
        if _gripe_status(store, int(gid)) in GRIPE_FIXED_STATUSES:
            continue
        tests = (dict(meta or {}).get(WAIVER_META_KEY) or {}).get("tests")
        if isinstance(tests, list):
            out.update(str(t) for t in tests)
    return out


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def waiver_body(
    *,
    rn: str,
    sha: str,
    rng: str,
    run_id: str,
    failures: dict[str, str],
    culprits: Sequence[tuple[str, str]],
) -> str:
    """The waiver gripe's body, masked."""
    lines = [
        f"release {rn} was deployed past a red check.yml run under the "
        f"red-test waiver ({SETTING_KEY}); a broken build until fixed.",
        f"run: {run_id} (digest: scripts/ci-failed {run_id})",
        f"cut: {sha[:12]} range {rng}",
        "",
        "failing tests:",
    ]
    for tid, reason in failures.items():
        lines.append(f"- {tid}")
        if reason:
            lines.append(f"    {_clip(reason, 300)}")
    lines.append("")
    lines.append(
        "culprit commits (touched a failing test file or a src module it imports):"
    )
    lines += [f"- {sha7} {subject}" for sha7, subject in culprits]
    lines += [
        "",
        "Make the failing tests green on main. If this gripe is still "
        "unfixed when the next release confirms, a revert of the "
        "culprit commits is minted automatically.",
    ]
    return mask_secrets("\n".join(lines))


def file_waiver_gripe(
    store: Any,
    *,
    rn: str,
    sha: str,
    rng: str,
    run_id: str,
    failures: dict[str, str],
    culprits: Sequence[tuple[str, str]],
    plan_root: int | None = None,
) -> int:
    """File the waiver gripe — hottest priority, ``fix:code`` +
    ``broken-build`` + ``release-waiver:r<N>``, ``meta.release_waiver`` —
    and return its id. A gripe already filed for ``rn`` is reused."""
    existing = waiver_gripe_for(store, rn)
    if existing is not None:
        return existing
    ids = list(failures)
    title = _clip(f"release {rn} shipped with known-red tests: {', '.join(ids)}", 200)
    body = waiver_body(
        rn=rn,
        sha=sha,
        rng=rng,
        run_id=run_id,
        failures=failures,
        culprits=culprits,
    )
    meta: dict[str, Any] = {
        WAIVER_META_KEY: {
            "rN": rn,
            "sha": sha[:12],
            "range": rng,
            "run_id": run_id,
            "tests": ids,
            "culprits": [c[0] for c in culprits],
            "plan": plan_root,
        },
        # The hottest dispatch score: the release class (a broken build).
        _dscore.SCORE_META_KEY: _dscore.RELEASE_SCORE,
    }
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="gripe", slug=None, title=title, meta=meta, conn=conn
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "gripe_body"})],
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in (
            "fix:code",
            "project:precis",
            BROKEN_BUILD_TAG,
            f"{WAIVER_TAG_PREFIX}{rn}",
            "origin:release-waiver",
        ):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.set_prio(ref.id, 1, conn=conn)
    return int(ref.id)


def record_waived_verdict(
    store: Any,
    *,
    sha: str,
    rng: str,
    run_id: str,
    attempt: int,
    gripe_id: int,
    tests: list[str],
) -> str:
    """Record the cut as ``green`` with the waiver marker; the status
    line (``ci verdict recorded: …`` on success). The marker is bound to
    ONE run — ``run_id`` and ``attempt`` — which the deploy gate
    (``scripts/round``) checks against the head's newest check.yml run: a
    newer run or a rerun voids the waiver."""
    return _release.record_ci_verdict(
        store,
        sha,
        rng,
        "green",
        run_id,
        waived={
            "gripe": gripe_id,
            "tests": tests,
            "run_id": str(run_id),
            "attempt": int(attempt),
        },
    )


def iteration_nodes(store: Any, digest_id: int) -> list[int]:
    """Live, non-terminal todos that transitively depend (``blocked-by``)
    on the digest node, under its loop — the iteration's fix, re-cut and
    poll nodes, which a waiver closes instead of running."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE dep(id) AS (
                SELECT l.src_ref_id FROM links l
                 WHERE l.relation = 'blocked-by' AND l.dst_ref_id = %s
                UNION
                SELECT l.src_ref_id FROM links l JOIN dep ON l.dst_ref_id = dep.id
                 WHERE l.relation = 'blocked-by'
            )
            SELECT r.ref_id FROM refs r JOIN dep ON dep.id = r.ref_id
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND r.parent_id IS NOT DISTINCT FROM
                   (SELECT parent_id FROM refs WHERE ref_id = %s)
               AND NOT EXISTS (SELECT 1 FROM ref_tags st JOIN tags s USING (tag_id)
                                WHERE st.ref_id = r.ref_id
                                  AND s.namespace = 'STATUS' AND s.value = ANY(%s))
             ORDER BY r.ref_id
            """,
            (digest_id, digest_id, sorted(TERMINAL_TODO_STATUSES)),
        ).fetchall()
    return [int(r[0]) for r in rows]


# ── the safety net: revert an unfixed waiver ───────────────────────

_REVERT_BRIEF = (
    "Release {rn} was deployed past known-red test(s) under the red-test "
    "waiver, and {handle} (the fix) is still not done after the next "
    "release confirmed. Revert the culprit commit(s) on main: {culprits}. "
    "Use `git revert` (one commit if they are adjacent), keep the revert "
    "minimal, and land it with scripts/ship --quick. If the fix for "
    "{handle} has already landed — the failing test(s) pass on origin/main "
    "({tests}) — revert nothing: finish with `moot: <sha>` and no commit."
)


#: A fix chain counts as in flight only if SOMETHING in it moved this
#: recently; a node left ``open`` and untouched longer is a stalled chain,
#: and a stalled chain no longer holds the revert back.
FIX_CHAIN_ACTIVE_HOURS = 24


def _fix_chain_live(store: Any, gripe_id: int) -> bool:
    """``True`` when the waiver gripe's OWN fix chain (a todo ``related-to``
    it that is not a revert chain) has a node ``open`` or ``doing`` AND the
    chain showed activity within :data:`FIX_CHAIN_ACTIVE_HOURS` — any
    node's ``updated_at``, any node STATUS stamp, or any job under a node.
    Both together mean the fix is in flight, so no revert."""
    with store.pool.connection() as conn:
        roots = [
            int(r[0])
            for r in conn.execute(
                """
                SELECT root.ref_id FROM links l
                  JOIN refs root ON root.ref_id = l.src_ref_id
                 WHERE l.dst_ref_id = %s AND l.relation = 'related-to'
                   AND root.kind = 'todo' AND root.retired_at IS NULL
                   AND root.meta ->> %s::text IS NULL
                """,
                (gripe_id, REVERT_META_KEY),
            ).fetchall()
        ]
        for root in roots:
            row = conn.execute(
                """
                WITH nodes AS (
                    SELECT n.ref_id, n.updated_at FROM refs n
                      JOIN ref_tags pt ON pt.ref_id = n.ref_id
                      JOIN tags p ON p.tag_id = pt.tag_id
                                 AND p.namespace = 'OPEN' AND p.value = %s
                     WHERE n.kind = 'todo' AND n.retired_at IS NULL
                )
                SELECT
                  EXISTS (SELECT 1 FROM nodes
                            JOIN ref_tags st ON st.ref_id = nodes.ref_id
                            JOIN tags s ON s.tag_id = st.tag_id
                                       AND s.namespace = 'STATUS'
                                       AND s.value IN ('open', 'doing')),
                  COALESCE((SELECT max(g) FROM (
                      SELECT updated_at AS g FROM nodes
                      UNION ALL
                      SELECT st.created_at FROM nodes
                        JOIN ref_tags st ON st.ref_id = nodes.ref_id
                        JOIN tags s ON s.tag_id = st.tag_id
                                   AND s.namespace = 'STATUS'
                      UNION ALL
                      SELECT j.updated_at FROM refs j
                        JOIN nodes ON j.parent_id = nodes.ref_id
                       WHERE j.kind = 'job'
                  ) a) > now() - %s * interval '1 hour', false)
                """,
                (f"plan:{root}", FIX_CHAIN_ACTIVE_HOURS),
            ).fetchone()
            if row is not None and row[0] and row[1]:
                return True
    return False


def _gripe_status(store: Any, gripe_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS' LIMIT 1",
            (gripe_id,),
        ).fetchone()
    return str(row[0]) if row else "open"


def _revert_minted(store: Any, gripe_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM refs WHERE kind = 'todo' AND retired_at IS NULL "
            "AND meta ->> %s = %s LIMIT 1",
            (REVERT_META_KEY, str(gripe_id)),
        ).fetchone()
    return row is not None


def mint_reverts(store: Any, confirmed_rn: str) -> list[tuple[int, int]]:
    """Release ``confirmed_rn`` just confirmed: for every waiver gripe of
    an EARLIER release that is unfixed (status outside
    :data:`GRIPE_FIXED_STATUSES`), has no fix chain in flight
    (:func:`_fix_chain_live`) and no revert chain yet, mint "Revert the culprits of gr<N>" — a
    ``gripe-fix-direct`` chain (the machinery ``gripe_dispatch`` uses) in
    the release claim band. Idempotent: at most one per gripe
    (``meta.release_revert_of`` on the chain root). Returns
    ``[(gripe id, chain root id)]`` minted now."""
    from precis.plans.templates import mint_gripe_fix_direct

    n = _rn_num(confirmed_rn)
    if n is None:
        return []
    minted: list[tuple[int, int]] = []
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta, t.value
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND jsonb_typeof(r.meta -> %s::text) = 'object'
               AND t.namespace = 'OPEN' AND t.value LIKE %s
             ORDER BY r.ref_id
            """,
            (WAIVER_META_KEY, f"{WAIVER_TAG_PREFIX}r%"),
        ).fetchall()
    for gid, meta, tag in rows:
        m = _rn_num(str(tag)[len(WAIVER_TAG_PREFIX) :])
        if m is None or m >= n:
            continue
        if _gripe_status(store, int(gid)) in GRIPE_FIXED_STATUSES:
            continue
        if _revert_minted(store, int(gid)) or _fix_chain_live(store, int(gid)):
            continue
        rec = dict(meta or {}).get(WAIVER_META_KEY) or {}
        culprits = ", ".join(str(c) for c in rec.get("culprits") or []) or "(see gr)"
        tests = ", ".join(str(t) for t in rec.get("tests") or []) or "(see gr)"
        handle = f"gr{gid}"
        mint = mint_gripe_fix_direct(
            store,
            gripe_id=int(gid),
            brief=_REVERT_BRIEF.format(
                rn=f"r{m}", handle=handle, culprits=culprits, tests=tests
            ),
            chain_title=f"Revert the culprits of {handle}",
            root_meta={REVERT_META_KEY: int(gid)},
            claim_band=_dscore.RELEASE_CLAIM_BAND,
        )
        minted.append((int(gid), mint.root_id))
    return minted


__all__ = [
    "BROKEN_BUILD_TAG",
    "GRIPE_FIXED_STATUSES",
    "MAX_WAIVED_TESTS",
    "PATH_HARD_STOP_FILES",
    "PATH_HARD_STOP_GLOBS",
    "PATH_HARD_STOP_PREFIXES",
    "PATH_HARD_STOP_SUBSTRINGS",
    "RESCUE_TRUNCATED",
    "REVERT_META_KEY",
    "SETTING_KEY",
    "TEST_HARD_STOPS",
    "WAIVER_META_KEY",
    "WAIVER_TAG_PREFIX",
    "culprit_commits",
    "file_of_test",
    "file_waiver_gripe",
    "hard_stop_hits",
    "imported_src_paths",
    "iteration_nodes",
    "mint_reverts",
    "open_waived_tests",
    "parse_failures",
    "partial_run_problem",
    "path_hard_stop",
    "range_paths",
    "record_waived_verdict",
    "waiver_decision",
    "waiver_enabled",
    "waiver_gripe_for",
]
