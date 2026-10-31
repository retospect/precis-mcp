"""Inbound pull requests: facts, class and priority, decided in code (td543749).

Spec: ``docs/backlog/dark-factory.md`` § "9. Inbound pull requests". The
``pr-intake`` template (:func:`precis.plans.templates.mint_pr_intake`) mints
one plan per open PR; which nodes it mints depends on the class this module
gives the PR. No prompt decides the class:

- ``harness`` — any touched path (old or new name) matches
  :data:`HARNESS_PATTERNS`, the ``HARNESS_DIFF`` case of ``scripts/ship``
  plus ``.github/**`` (Dependabot's actions bumps land here). Checked first,
  whoever the author is: an ask for Reto's ``/go`` and nothing else.
- ``dependabot`` — author ``dependabot[bot]`` (user type ``Bot``), head
  branch in the base repo (a fork is never ``dependabot``), at least one
  commit and every commit authored by ``dependabot[bot]`` with
  ``commit.verification.verified`` true, files within
  :data:`DEPENDABOT_FILES`. A Dependabot-authored PR that fails any of these
  is ``external`` with the failed checks in the reason (the spoof guard:
  login and signature both, plus the files allowlist).
- ``factory`` — the author is a known builder (the ``pr.factory_builders``
  setting, default :data:`DEFAULT_BUILDERS`), the head is in the base repo
  and the title's trailing handle list names a ``(td<id>)``: it is a slice,
  lander plus reviewer, no analysis.
- ``external`` — everything else: read-only analysis, then a human answer.

Priority is the plan root's ``prio`` (spec: 2 security bump, 4 feature
slice, 6 routine bump, 8 PR analysis): ``dependabot`` 2 when security-linked
(:func:`is_security`) else 6, ``factory`` 4, ``harness`` 5, ``external`` 8.

GitHub reads go through :func:`precis.utils.safe_fetch.safe_get` (the public
API; ``GH_TOKEN`` from the env is sent when set). The repo is the
``release.repo`` setting, else the slice checkout's ``origin`` remote.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

DEPENDABOT = "dependabot"
HARNESS = "harness"
FACTORY = "factory"
EXTERNAL = "external"
CLASSES: tuple[str, ...] = (DEPENDABOT, HARNESS, FACTORY, EXTERNAL)

DEPENDABOT_LOGIN = "dependabot[bot]"
#: The only paths a ``dependabot`` PR may touch.
DEPENDABOT_FILES: frozenset[str] = frozenset({"pyproject.toml", "uv.lock"})
#: ``scripts/ship``'s ``HARNESS_DIFF`` case (``scripts/ship | scripts/test |
#: scripts/lib/* | .github/*``) as fnmatch globs (``*`` spans ``/``, so
#: ``.github/*`` is the subtree). ``tests/test_pr_intake.py`` pins the parity.
HARNESS_PATTERNS: tuple[str, ...] = (
    "scripts/ship",
    "scripts/test",
    "scripts/lib/*",
    ".github/*",
)
#: Known builder logins when the ``pr.factory_builders`` setting is unset.
DEFAULT_BUILDERS: frozenset[str] = frozenset({"devin-ai-integration[bot]"})
BUILDERS_SETTING = "pr.factory_builders"

PRIO: dict[str, int] = {DEPENDABOT: 6, FACTORY: 4, HARNESS: 5, EXTERNAL: 8}
SECURITY_PRIO = 2

_API = "https://api.github.com/repos/{repo}"
_TIMEOUT_S = 20.0
_PAGE = 100
_MAX_PAGES = 10
_TD_RE = re.compile(r"\(([^()]*)\)\s*$")
_TD_HANDLE_RE = re.compile(r"\btd(\d+)\b")
_BUMP_RE = re.compile(
    r"\b(?:bump|update)\s+(\S+)(?:\s+requirement)?\s+from\s+(\S+)\s+to\s+(\S+)",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?")
_SECURITY_RE = re.compile(r"\b(?:CVE-\d{4}-\d+|GHSA(?:-[0-9a-z]{4}){3})\b", re.I)
_FEATURE_RE = re.compile(r"^\s*feat(?:\([^)]*\))?!?:", re.I)
_SUBJECT_RE = re.compile(
    r"^(feat|fix|docs|test|chore|build|refactor|perf|ci|release|revert)"
    r"(\([a-z0-9/_.-]+\))?!?: \S"
)
_PREFIX_RE = re.compile(r"^\s*[A-Za-z]+(?:\([^)]*\))?!?:\s*")


@dataclass
class PrFacts:
    """What the class rules read from one PR (``meta.pr`` on the plan root)."""

    number: int
    repo: str
    title: str
    author: str
    author_type: str = ""
    head_sha: str = ""
    head_ref: str = ""
    head_repo: str = ""
    base_repo: str = ""
    draft: bool = False
    labels: list[str] = field(default_factory=list)
    body: str = ""
    url: str = ""
    #: Touched paths, renames' old names included.
    files: list[str] = field(default_factory=list)
    #: ``{sha, author, verified}`` per commit.
    commits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def fork(self) -> bool:
        """The head lives outside the base repo (a deleted fork counts)."""
        return not self.head_repo or self.head_repo != self.base_repo

    def to_meta(self, cls: str, why: str) -> dict[str, Any]:
        """The plan root's ``meta.pr`` (body clipped, commits summarised)."""
        out = asdict(self)
        out["body"] = self.body[:2000]
        out["class"] = cls
        out["why"] = why
        bump = bump_of(self.title)
        if bump is not None:
            out["bump"] = asdict(bump)
        out["security"] = is_security(self)
        return out


@dataclass(frozen=True)
class Bump:
    """A Dependabot title's ``<name> from <old> to <new>``."""

    name: str
    old: str
    new: str
    #: ``True`` when the leading version number changes (or, below 1.0, the
    #: minor), or when either side does not parse (fail toward the ask).
    major: bool


# ── facts ──────────────────────────────────────────────────────────


def facts_from_api(
    repo: str,
    pull: dict[str, Any],
    files: list[dict[str, Any]],
    commits: list[dict[str, Any]],
) -> PrFacts:
    """:class:`PrFacts` from the REST shapes of ``pulls/<n>``,
    ``pulls/<n>/files`` and ``pulls/<n>/commits``."""
    user = pull.get("user") or {}
    head = pull.get("head") or {}
    base = pull.get("base") or {}
    paths: list[str] = []
    for f in files:
        for key in ("filename", "previous_filename"):
            name = f.get(key)
            if isinstance(name, str) and name and name not in paths:
                paths.append(name)
    summary = []
    for c in commits:
        verification = (c.get("commit") or {}).get("verification") or {}
        summary.append(
            {
                "sha": str(c.get("sha") or ""),
                "author": str((c.get("author") or {}).get("login") or ""),
                "verified": verification.get("verified") is True,
            }
        )
    return PrFacts(
        number=int(pull.get("number") or 0),
        repo=repo,
        title=str(pull.get("title") or ""),
        author=str(user.get("login") or ""),
        author_type=str(user.get("type") or ""),
        head_sha=str(head.get("sha") or ""),
        head_ref=str(head.get("ref") or ""),
        head_repo=str((head.get("repo") or {}).get("full_name") or ""),
        base_repo=str((base.get("repo") or {}).get("full_name") or ""),
        draft=bool(pull.get("draft")),
        labels=[str(lb.get("name") or "") for lb in pull.get("labels") or []],
        body=str(pull.get("body") or ""),
        url=str(pull.get("html_url") or ""),
        files=paths,
        commits=summary,
    )


# ── class rules ────────────────────────────────────────────────────


def is_harness(path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in HARNESS_PATTERNS)


def factory_todo(title: str) -> int | None:
    """The ``td<id>`` in the title's trailing handle list, else ``None``."""
    m = _TD_RE.search(title.strip())
    if m is None:
        return None
    hit = _TD_HANDLE_RE.search(m.group(1))
    return int(hit.group(1)) if hit else None


def dependabot_problems(f: PrFacts) -> list[str]:
    """Why a Dependabot-authored PR is not a certified ``dependabot`` one
    (empty: it is)."""
    out: list[str] = []
    if f.author_type and f.author_type != "Bot":
        out.append(f"user type {f.author_type!r}, not 'Bot'")
    if f.fork:
        out.append(f"head repo {f.head_repo or '(deleted)'!r} is not {f.base_repo!r}")
    if not f.commits:
        out.append("no commits")
    unsigned = [c["sha"][:9] for c in f.commits if not c.get("verified")]
    if unsigned:
        out.append("unverified commit(s) " + ", ".join(unsigned))
    others = [c["sha"][:9] for c in f.commits if c.get("author") != DEPENDABOT_LOGIN]
    if others:
        out.append(
            f"commit(s) not authored by {DEPENDABOT_LOGIN}: " + ", ".join(others)
        )
    if not f.files:
        out.append("no files")
    stray = sorted(set(f.files) - DEPENDABOT_FILES)
    if stray:
        out.append("files outside pyproject.toml/uv.lock: " + ", ".join(stray[:10]))
    return out


def classify(
    f: PrFacts, builders: frozenset[str] = DEFAULT_BUILDERS
) -> tuple[str, str]:
    """``(class, why)`` per the module docstring's rules, in that order."""
    harness = [p for p in f.files if is_harness(p)]
    if harness:
        return HARNESS, "touches the landing harness: " + ", ".join(harness[:10])
    if f.author == DEPENDABOT_LOGIN:
        problems = dependabot_problems(f)
        if not problems:
            return DEPENDABOT, (
                "dependabot[bot], every commit verified, files within "
                "pyproject.toml/uv.lock"
            )
        return EXTERNAL, "dependabot[bot] but " + "; ".join(problems)
    td = factory_todo(f.title)
    if f.author in builders and td is not None and not f.fork:
        return FACTORY, f"builder {f.author} on td{td}"
    if f.author in builders:
        why = "fork" if f.fork else "no (td<id>) in the title"
        return EXTERNAL, f"builder {f.author} but {why}"
    return EXTERNAL, f"author {f.author or '(unknown)'}"


def _major_minor(version: str) -> tuple[int, int | None] | None:
    m = _VERSION_RE.search(version)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)) if m.group(2) is not None else None


def bump_of(title: str) -> Bump | None:
    """The ``bump|update <name> [requirement] from <old> to <new>`` in a
    Dependabot title, else ``None``."""
    m = _BUMP_RE.search(title)
    if m is None:
        return None
    name, old, new = m.group(1), m.group(2), m.group(3)
    a, b = _major_minor(old), _major_minor(new)
    if a is None or b is None or a[0] != b[0]:
        major = True
    else:
        major = a[0] == 0 and a[1] != b[1]
    return Bump(name=name, old=old, new=new, major=major)


def is_major(f: PrFacts) -> bool:
    """A ``dependabot`` PR needs the major gate: the title's bump is major,
    or the title names no single bump (a grouped update)."""
    bump = bump_of(f.title)
    return bump is None or bump.major


def is_security(f: PrFacts) -> bool:
    """Security-labelled, or the body links an advisory (CVE / GHSA id)."""
    if any("security" in lb.lower() for lb in f.labels):
        return True
    return bool(_SECURITY_RE.search(f.body or ""))


def prio_of(cls: str, f: PrFacts) -> int:
    if cls == DEPENDABOT and is_security(f):
        return SECURITY_PRIO
    return PRIO[cls]


def ask_kind(f: PrFacts) -> str:
    """``design-verdict`` for a feature (a ``feat`` title or an
    ``enhancement``/``feature`` label), else ``approval`` (a fix)."""
    labels = {lb.lower() for lb in f.labels}
    if _FEATURE_RE.match(f.title) or labels & {"enhancement", "feature"}:
        return "design-verdict"
    return "approval"


def land_subject(title: str, cls: str) -> str:
    """The squash subject for a PR's land: the title when it already passes
    ``scripts/ship``'s subject lint, else its text under ``build(deps):``
    (Dependabot) or ``chore(pr):``. ``land_slice`` appends ``(td<id>)``."""
    title = " ".join(title.split())
    if _SUBJECT_RE.match(title):
        return title
    head = _PREFIX_RE.sub("", title) or title
    head = head[:1].lower() + head[1:]
    return f"{'build(deps)' if cls == DEPENDABOT else 'chore(pr)'}: {head}"


def builders(store: Store | None = None) -> frozenset[str]:
    """Known builder logins: the ``pr.factory_builders`` setting
    (comma-separated), else :data:`DEFAULT_BUILDERS`."""
    from precis import settings

    raw = settings.get_str(BUILDERS_SETTING, store=store) or ""
    names = frozenset(n.strip() for n in raw.split(",") if n.strip())
    return names or DEFAULT_BUILDERS


# ── the plan's PR ──────────────────────────────────────────────────


def existing_intake(store: Store, number: int) -> int | None:
    """The oldest live todo tagged ``pr:<number>`` (any status), else
    ``None``: one intake plan per PR, ever."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
             ORDER BY r.ref_id LIMIT 1
            """,
            (f"pr:{int(number)}",),
        ).fetchone()
    return int(row[0]) if row else None


def pr_of(store: Store, todo_id: int) -> dict[str, Any] | None:
    """The plan root's ``meta.pr`` for a node of a ``pr-intake`` plan."""
    from precis.plans import release as _release

    ref = store.get_ref(kind="todo", id=_release.plan_root(store, todo_id))
    pr = (ref.meta or {}).get("pr") if ref is not None else None
    if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
        return None
    return pr


# ── GitHub reads ───────────────────────────────────────────────────


class GitHubUnreadable(Exception):
    """GitHub did not answer usefully (network, rate limit, 5xx, 404)."""


def _send(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
    """One GET through the SSRF-pinned client (the backoff's thunk)."""
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_get

    with http_client(timeout=_TIMEOUT_S, headers=headers) as client:
        return safe_get(client, url, params=params)


def _http_get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
    """Seam: one polite GET (the ``github`` policy, the api.github.com
    limiter row); returns the response."""
    from precis.utils.http import with_backoff

    return with_backoff(lambda: _send(url, params, headers), service="github", url=url)


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(url: str, params: dict[str, Any]) -> Any:
    try:
        resp = _http_get(url, params, _headers())
    except Exception as exc:
        raise GitHubUnreadable(f"GET {url}: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise GitHubUnreadable(f"GET {url}: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GitHubUnreadable(f"GET {url}: not JSON") from exc


def _get_pages(url: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        data = _get_json(url, {**(params or {}), "per_page": _PAGE, "page": page})
        if not isinstance(data, list):
            raise GitHubUnreadable(f"GET {url}: expected a list")
        out += [d for d in data if isinstance(d, dict)]
        if len(data) < _PAGE:
            break
    return out


def fetch_open_pulls(repo: str) -> list[dict[str, Any]]:
    """``GET /repos/<repo>/pulls?state=open`` (every page)."""
    return _get_pages(f"{_API.format(repo=repo)}/pulls", {"state": "open"})


def fetch_facts(repo: str, number: int, pull: dict[str, Any] | None = None) -> PrFacts:
    """The PR's facts: ``pulls/<n>`` (unless ``pull`` is given), its files
    and its commits. Raises :class:`GitHubUnreadable`."""
    base = f"{_API.format(repo=repo)}/pulls/{int(number)}"
    if pull is None:
        pull = _get_json(base, {})
        if not isinstance(pull, dict):
            raise GitHubUnreadable(f"GET {base}: expected an object")
    files = _get_pages(f"{base}/files")
    commits = _get_pages(f"{base}/commits")
    return facts_from_api(repo, pull, files, commits)


def resolve_repo(store: Store | None, explicit: Any = None) -> str | None:
    """``explicit``, else the ``release.repo`` setting, else the slice
    checkout's ``origin`` remote (``owner/repo``)."""
    from precis import settings
    from precis.plans import release as _release

    repo = _release.valid_repo(explicit) or _release.valid_repo(
        settings.get_str("release.repo", store=store)
    )
    if repo:
        return repo
    try:
        from precis.workers.job_types import release_cut, run_slice

        return release_cut.origin_repo(run_slice.slice_root())
    except Exception:
        return None


__all__ = [
    "CLASSES",
    "DEFAULT_BUILDERS",
    "DEPENDABOT",
    "DEPENDABOT_FILES",
    "DEPENDABOT_LOGIN",
    "EXTERNAL",
    "FACTORY",
    "HARNESS",
    "HARNESS_PATTERNS",
    "Bump",
    "GitHubUnreadable",
    "PrFacts",
    "ask_kind",
    "builders",
    "bump_of",
    "classify",
    "dependabot_problems",
    "existing_intake",
    "factory_todo",
    "facts_from_api",
    "fetch_facts",
    "fetch_open_pulls",
    "is_harness",
    "is_major",
    "is_security",
    "land_subject",
    "pr_of",
    "prio_of",
    "resolve_repo",
]
