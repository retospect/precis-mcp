"""``ci_watch`` job_type — a red nightly or soak run becomes one gripe
per failure signature (me580555).

Every tick reads completed ``check.yml`` runs through ``gh api`` — the
newest ``lookback_runs`` (default 3) ``event=schedule`` nightly runs on
main, and the newest :data:`SOAK_LIMIT` completed ``release/**`` runs
carrying a ``soak*`` job — and files ONE gripe per failure signature:
``ci:check.yml:<file>::<test>`` (the ``[param]`` suffix stripped; an
``ERROR`` node counts the same) for a pytest red, or
``ci:check.yml:infra:<job name>`` when a failed, cancelled or timed-out
job is the signal (a lint gate, a runner kill — tagged ``fix:infra``,
never ``fix:code``, so it stays out of the build lanes); a fail-fast
matrix's cancelled siblings share one infra gripe — the first down job
files, the rest are named in its body. Soak runs are judged on the
``soak*`` jobs only, never the whole run — a non-soak failure on
``release/**`` belongs to the release loop.

Dedupe is by the exact gripe title ``nightly red: <sig>`` /
``soak red: <sig>``: an open gripe gets a ``seen again: run <url> on
<sha>`` comment; a terminal one refiles — inside
:data:`REGRESSION_WINDOW` the new gripe names the old handle
(``regressed after gr<N>``). An open ``ci:mute`` tag on the gripe
silences it entirely (also under ``dry_run``). A red run that filed
nothing is NOT recorded in ``ci_watch.seen`` — the next tick re-judges
it rather than losing it.

The gripe body is what a builder without the MCP acts on: a ``Goal:``
line, the signature, run URL, head sha, job/step name, the
``scripts/ci-failed <run>`` digest line, the sibling gripes of the same
run (also ``related-to``-linked), and each failing node id with its
``reason:`` line. Every log-derived string is bounded (node id and
reason each capped, the whole body capped at :data:`_BODY_CAP`),
secrets masked, control characters stripped, and no address, home path
or hostname reaches the body (``<addr>``/``<home>``/``<host>``
placeholders) — log text is never interpreted.

The close side runs every pass over the open ``origin:ci-watch`` gripes:
one with no ``resolved:`` comment and no live todo ``related-to`` is
closed ``STATUS:done`` — ``closed by ci_watch: 2 consecutive green runs,
no fix landed (flake)`` — once its workflow's two newest runs are both
green; one carrying ``resolved: <sha>`` instead gets ``green on <url>``
when the newest green run's head contains the sha (the compare API).

``ci_watch.seen`` — a ``{run_id: {at, verdict, wf}}`` map capped at
:data:`SEEN_CAP` — is the idempotency ledger: a processed run is never
re-judged, so a re-tick converges, and a judged run's ledger verdict is
what the green streak and newest-green read (a run already judged
still counts). A probed ``release/**`` candidate with no soak artifact
at all is recorded ``verdict "no-soak"`` so it is not probed again; a
candidate whose soak matrix skipped wholesale — the jobs API reports
ONE job named by the literal ``name:`` expression, never ``soak
(PY, K)`` legs (gr586293) — is still a soak run and records ``verdict
"skipped"``: the legs exist but did not run, which is neither a gripe
nor a green the flake streak could count. GitHub
reads are bounded by :data:`_API_BUDGET` calls per tick; candidates
already in ``seen`` cost nothing, and soak-candidate probes are capped
at :data:`SOAK_PROBE_CAP` so the red-run job/log reads keep the rest.

Read-only while the factory kill switch holds (``PRECIS_FACTORY=off`` or
a ``service_config`` ``plan_dispatch`` row at prio 0) or under
``params.dry_run``: the summary lists ``would`` actions and nothing is
written — not even ``ci_watch.seen``. A ``gh`` failure ends the pass
cleanly: one ``job_event`` chunk (exit code + first stderr line), no
traceback. ``_gh_log`` asks ``gh api`` for ``--allow-escape-sequences``
and retries ONCE without it when gh predates the flag (added in gh
v2.97.0, GHSA-3m3g-3wcr-px46) — an old gh on a worker must not make the
tick dark; escapes are stripped in Python on that path. Every ``gh`` runs
with ``factory_git.git_auth_env()`` (the vault ``PRECIS_FACTORY_GIT_TOKEN``
as ``GH_TOKEN``), so a service account with no ``gh auth login`` still reads.

Filed gripes carry ``fix:code`` (``fix:infra`` for the infra signature),
``project:precis``, ``ci:nightly``/``ci:soak``, ``thread:factory``,
``size:s``, ``origin:ci-watch``, ``meta.dispatch_score``
``auto_gripe.AUTO_SCORE`` and ``meta.ci = {sig, wf}``. The ``meta.ci.sig`` + worker-stamped
``filed_by.principal = worker:ci_watch`` pair is the ``gripe_dispatch``
door: a ci_watch gripe is machine-filed from the repo's own test output
about its own code — not reader intake — so the untrusted-text
exclusion does not apply (see ``gripe_dispatch.candidates``). The
principal stamp is the ``job_inproc`` executor's in-process source
binding (``tick_context`` with ``source = spec.name`` →
``principal.current()`` → ``worker:<job_type>``); :data:`WORKER_PRINCIPAL`
is the one name for it — ``gripe_dispatch`` reads the constant, no
second literal.

Arming is an operator write and NOT in this slice — one recurring todo
(``every: '30m'``, ``job_inproc``); recipe:
``docs/runbooks/dark-factory-arming.md`` § CI watch.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.principal import worker_comment_meta
from precis.store.types import ChunkInsert, Tag
from precis.utils.secret_scan import mask_secrets
from precis.workers import factory_git as _factory_git
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

NAME = "ci_watch"
#: The ``filed_by.principal`` a real job_inproc tick stamps on this job's
#: writes — the gripe_dispatch door key. ONE constant; gripe_dispatch
#: reads it rather than spelling ``worker:ci_watch`` again.
WORKER_PRINCIPAL = f"worker:{NAME}"

#: The workflow whose runs are judged (same file ci_verdict_poll reads).
WORKFLOW_FILE = "check.yml"
#: ``lookback_runs`` default — completed schedule runs read per tick.
DEFAULT_LOOKBACK = 3
#: Completed ``release/**`` runs carrying a ``soak*`` job read per tick,
#: scanned out of the newest this many completed runs.
SOAK_LIMIT = 3
SOAK_SCAN = 20
#: A soak run is judged on jobs named this prefix only — never the run.
SOAK_JOB_NAME = "soak"
#: Run conclusions that count as green (ci_verdict_poll's set).
OK_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
#: Job conclusions that produce an infra signature — no test nodes, no
#: log read: a runner kill or operator cancel IS the failure.
BAD_JOB_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out"})
#: gh api calls one tick may spend, quiet night included: 2 run lists +
#: soak-candidate job probes + red-run job/log reads + compare calls.
_API_BUDGET = 15
#: Jobs-endpoint probes the soak scan may spend per tick — the red-run
#: job/log reads keep the rest of :data:`_API_BUDGET`. A probed run with
#: no soak artifact is ledgerecorded (``verdict "no-soak"``) so it is not
#: probed again next tick.
SOAK_PROBE_CAP = 5
#: The idempotency ledger setting (``{run_id: {at, verdict, wf}}``).
SEEN_SETTING = "ci_watch.seen"
SEEN_CAP = 50
#: A terminal gripe re-redded inside this window refiles as a regression
#: naming the old handle.
REGRESSION_WINDOW = timedelta(days=7)
#: Terminal gripe STATUSes (factory_rescue's set).
_TERMINAL: tuple[str, ...] = ("done", "won't-do", "abandoned", "wontfix")
_GH_TIMEOUT = 120.0
_TITLE_CAP = 200
#: Bounds for log-derived strings in the gripe body: one node id, one
#: reason, the node lines, and the whole body.
_NODE_CAP = 200
_REASON_CAP = 200
_MAX_NODE_LINES = 8
_BODY_CAP = 4000
#: ``pg_advisory_xact_lock`` key (the "ciwatch_" bytes) serialising each
#: run's find-or-file — the ``factory_rescue._SIG_LOCK_KEY`` shape.
_SIG_LOCK_KEY = 0x636977617463685F

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
#: ``2026-10-24T21:00:00.0000000Z `` leading a gh log line's text.
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\s+")
#: A pytest short-summary node: ``FAILED <id> - <why>`` / ``ERROR <id>``.
NODE_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$")
#: The ``[param]`` suffix on a parametrised node id.
PARAM_RE = re.compile(r"\[[^\]]*\]")
#: land_slice's ``resolved: <sha>`` comment line.
RESOLVED_RE = re.compile(r"^resolved:\s*([0-9a-f]{7,40})", re.MULTILINE)
#: Body hygiene for log-derived strings — no live control characters
#: (``\t`` is replaced by a space, not dropped).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: Addresses, home paths and hostnames never reach a body: dotted
#: quads; colon-joined hex groups masked only when they carry a hex
#: letter or a ``::`` (so a clock time ``12:34:56`` survives); the
#: ``~user`` / ``/Users|home|root`` shapes; and dotted names of two or
#: more labels unless the last label is a source/text extension.
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
#: Word/ colon boundaries (not hex-digit boundaries): ``std::cerr`` and
#: ``tests/x.py::decode_thing`` carry a word char before ``::`` and must
#: not match — ``::dec``/``::ce`` are hex-shaped and would otherwise mask.
_IPV6_RE = re.compile(r"(?<![\w:])[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,}(?![\w:])")
_HEX_LETTER_RE = re.compile(r"[a-fA-F]")
_HEX_DIGIT_RE = re.compile(r"[0-9a-fA-F]")
_HOME_RE = re.compile(r"~[/\w.-]+|/(?:Users|home|root)/\S*")
#: ``_`` is a label char (``\b`` does not split at it): without it
#: ``db_host.cluster.internal`` leaks its first label.
_HOST_RE = re.compile(r"(?i)\b[a-z0-9_-]+(?:\.[a-z0-9_-]+)+(?::\d+)?\b")
#: Last labels that make a dotted token a file name, not a hostname.
_HOST_EXT = frozenset(
    {
        "py",
        "json",
        "yml",
        "yaml",
        "md",
        "txt",
        "toml",
        "cfg",
        "ini",
        "lock",
        "xml",
        "html",
        "sh",
        "csv",
        "sql",
    }
)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Read and report 'would', write nothing (not even ci_watch.seen).
        "dry_run": {"type": "boolean"},
        # owner/repo; else release.repo, else the checkout's origin.
        "repo": {"type": "string"},
        # Completed schedule runs read per tick (default 3).
        "lookback_runs": {"type": "integer"},
    },
    "additionalProperties": False,
}

DESCRIPTION: str = (
    "CI watcher: judges each completed check.yml run once — a red "
    "event=schedule nightly on main or a red release/** run's soak job "
    "files one gripe per failure signature "
    "(ci:check.yml:<file>::<test>, ci:check.yml:infra:<job>), an open "
    "gripe gets a seen-again comment, a terminal one refiles as a "
    "regression under 7 days, and two consecutive green runs flake-close "
    "an unresolved gripe. Read-only while the factory kill switch is on."
)


class GhUnavailable(Exception):
    """A ``gh api`` call failed — carries ``exit <n>: <first stderr line>``."""


@dataclass
class WatchResult:
    """What one pass did (or, ``dry_run``, would have done)."""

    dry_run: bool
    reason: str | None = None
    repo: str | None = None
    blocker: str = ""
    gh_error: str | None = None
    processed: int = 0
    filed: list[int] = field(default_factory=list)
    seen_again: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    green_noted: list[int] = field(default_factory=list)
    would: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "processed": self.processed,
            "filed": len(self.filed),
            "seen_again": len(self.seen_again),
            "closed": len(self.closed),
            "green_noted": len(self.green_noted),
            "would": len(self.would),
            "blocked": bool(self.blocker),
            "blocker": self.blocker,
            "gh_error": bool(self.gh_error),
            "dry_run": self.dry_run,
            "reason": self.reason,
        }

    def summary(self) -> str:
        repo = self.repo or "(no repo)"
        if self.blocker:
            return f"{NAME} {repo}: blocked — {self.blocker}"
        if self.gh_error:
            return f"{NAME} {repo}: gh read failed — {self.gh_error}"
        if self.dry_run:
            why = f" ({self.reason})" if self.reason else ""
            return f"{NAME} {repo}: dry run — {len(self.would)} would-action(s){why}"
        return (
            f"{NAME} {repo}: {self.processed} new run(s); filed "
            f"{len(self.filed)}, seen-again {len(self.seen_again)}, closed "
            f"{len(self.closed)}, green-noted {len(self.green_noted)}"
        )

    def render(self) -> str:
        lines = [self.summary()]
        lines += [f"- {n}" for n in self.notes]
        lines += [f"- would: {w}" for w in self.would]
        return "\n".join(lines)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _gh_json(endpoint: str) -> Any:
    """Seam: ``gh api <endpoint>`` → parsed JSON. Raises
    :class:`GhUnavailable` (exit code + first stderr line)."""
    try:
        cp = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_factory_git.git_auth_env(),
            timeout=_GH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GhUnavailable(f"gh api {endpoint}: {exc}") from exc
    if cp.returncode != 0:
        raise GhUnavailable(
            f"gh api {endpoint}: exit {cp.returncode}: "
            f"{_first_line(cp.stderr or cp.stdout)}"
        )
    try:
        return json.loads(cp.stdout)
    except ValueError as exc:
        raise GhUnavailable(f"gh api {endpoint}: unparsable JSON ({exc})") from exc


_UNKNOWN_FLAG_RE = re.compile(r"unknown (flag|shorthand)", re.IGNORECASE)


def _gh_log(repo: str, job_id: int) -> str:
    """Seam: one job's log text. ``--allow-escape-sequences`` exists only
    on gh ≥2.97 — on an older gh the flag is an ``unknown flag`` error,
    so retry ONCE without it and strip escapes in Python on that path."""
    endpoint = f"repos/{repo}/actions/jobs/{job_id}/logs"
    retried = False
    while True:
        argv = ["gh", "api"]
        if not retried:
            argv.append("--allow-escape-sequences")
        argv.append(endpoint)
        try:
            cp = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=_factory_git.git_auth_env(),
                timeout=_GH_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GhUnavailable(f"gh api {endpoint}: {exc}") from exc
        if cp.returncode == 0:
            return ANSI_RE.sub("", cp.stdout) if retried else cp.stdout
        err = _first_line(cp.stderr or cp.stdout)
        if not retried and _UNKNOWN_FLAG_RE.search(err):
            retried = True
            continue
        raise GhUnavailable(f"gh api {endpoint}: exit {cp.returncode}: {err}")


def _take(budget: dict[str, int]) -> bool:
    """Spend one gh call against :data:`_API_BUDGET`; False when out."""
    if budget["left"] <= 0:
        return False
    budget["left"] -= 1
    return True


def _run_jobs(
    repo: str, run: dict[str, Any], budget: dict[str, int]
) -> list[dict[str, Any]] | None:
    """The run's jobs, cached on the run dict (a soak scan already paid
    for the read); ``None`` when the call budget is out."""
    jobs = run.get("_jobs")
    if jobs is None:
        if not _take(budget):
            return None
        data = _gh_json(f"repos/{repo}/actions/runs/{run.get('id')}/jobs")
        jobs = data.get("jobs") if isinstance(data, dict) else None
        jobs = [j for j in (jobs or []) if isinstance(j, dict)]
        run["_jobs"] = jobs
    return list(jobs)


def _nightly_runs(
    repo: str, lookback: int, budget: dict[str, int]
) -> list[dict[str, Any]]:
    """The newest ``lookback`` completed schedule runs on main."""
    if not _take(budget):
        return []
    data = _gh_json(
        f"repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs"
        f"?event=schedule&per_page={lookback}"
    )
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    out = [
        r
        for r in (runs or [])
        if isinstance(r, dict) and r.get("status") == "completed"
    ]
    return out[:lookback]


def _soak_placeholder(name: str) -> bool:
    """True for the unexpanded soak matrix's one placeholder job
    (gr586293): a matrix job skipped before expansion is never
    expanded — the jobs API reports it named by the literal ``name:``
    expression, and check.yml's test-linux job's holds the ``|| 'soak'``
    alternative. A ``${{``-led name mentioning the soak IS the soak
    legs, skipped wholesale."""
    return name.startswith("${{") and "soak" in name


def _soak_job_name(name: str) -> bool:
    """A soak leg by name: an expanded ``soak (PY, K)`` leg, or the
    placeholder :func:`_soak_placeholder` names (the sibling readers —
    ``scripts/lib/soak_verdict.py``, ``last_gated_main_sha.py`` — are
    coordinator-owned; this is the builder-lane half of gr586293)."""
    return name.startswith(SOAK_JOB_NAME) or _soak_placeholder(name)


def _soak_runs(
    repo: str, seen: dict[str, dict[str, Any]], budget: dict[str, int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """``(fresh, scope, probed_empty)`` out of the newest
    :data:`SOAK_SCAN` completed runs: ``fresh`` are the newest
    :data:`SOAK_LIMIT` unseen ``release/**`` runs carrying a soak leg —
    a ``soak*`` job or the skipped matrix's placeholder (what this tick
    judges); ``scope`` is every candidate a soak
    verdict is known for — fresh plus runs already judged in ``seen`` —
    what the green streak and newest-green read (a judged run must
    still count); ``probed_empty`` are candidates probed this tick that
    carry no soak artifact at all, for the caller to ledgerecord so they
    are not probed again. Probes spend at most :data:`SOAK_PROBE_CAP`
    calls — the red-run reads keep the rest of the budget."""
    if not _take(budget):
        return [], [], []
    data = _gh_json(
        f"repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs?per_page={SOAK_SCAN}"
    )
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    fresh: list[dict[str, Any]] = []
    scope: list[dict[str, Any]] = []
    probed_empty: list[dict[str, Any]] = []
    probes = 0
    for r in runs or []:
        if not isinstance(r, dict):
            continue
        if r.get("status") != "completed":
            continue
        if not str(r.get("head_branch") or "").startswith("release/"):
            continue
        known = seen.get(str(r.get("id") or ""))
        if known is not None:
            if known.get("wf") == "soak":
                scope.append(r)
            continue
        if len(fresh) >= SOAK_LIMIT or probes >= SOAK_PROBE_CAP:
            continue  # seen entries above still collect; the rest wait
        jobs = _run_jobs(repo, r, budget)
        if jobs is None:
            continue  # budget out — probes are done, unseen stays unseen
        probes += 1
        if any(_soak_job_name(str(j.get("name") or "")) for j in jobs):
            fresh.append(r)
            scope.append(r)
        else:
            probed_empty.append(r)
    return fresh, scope, probed_empty


def _ipv6_mask(m: re.Match[str]) -> str:
    """``<addr>`` when the colon-joined token is v6-shaped — a hex
    letter, or a ``::`` compression beside real hex content — else the
    token verbatim (a clock time ``12:34:56``, a bare ``::`` in
    ``test.py::test_node``)."""
    tok = m.group(0)
    if _HEX_LETTER_RE.search(tok) or ("::" in tok and _HEX_DIGIT_RE.search(tok)):
        return "<addr>"
    return tok


def _host_mask(m: re.Match[str]) -> str:
    """``<host>`` for a dotted name unless its last label is a
    source/text extension (``pyproject.toml``), it is ``localhost``, or
    the token is a version shape, not a name: all-digit labels
    (``1.2.3``, ``1.5``), a ``v``-prefixed label (``v0.8.37``), or a
    first label ending in a digit run followed by a numeric label
    (``python3.12``). A real host like ``node-a.cluster.internal``
    still masks."""
    tok = m.group(0)
    base = tok.split(":", 1)[0]
    if base == "localhost" or base.startswith("localhost."):
        return tok
    labels = base.split(".")
    if all(label.isdigit() for label in labels):
        return tok
    first = labels[0].lower()
    if len(first) > 1 and first.startswith("v") and first[1:].isdigit():
        return tok
    if len(labels) > 1 and labels[1].isdigit() and re.search(r"[a-zA-Z_-]\d+$", first):
        return tok
    return tok if labels[-1].lower() in _HOST_EXT else "<host>"


def _sanitize_log(text: str, cap: int) -> str:
    """Bound a log-derived string for the body: secrets masked, ANSI and
    control characters stripped (``\t`` → space), addresses, home paths
    and hostnames replaced with placeholders, hard character cap.
    Nothing is interpreted."""
    t = _CTRL_RE.sub("", ANSI_RE.sub("", mask_secrets(text or "") or ""))
    t = t.replace("\t", " ")
    t = _IPV6_RE.sub(_ipv6_mask, _IPV4_RE.sub("<addr>", t))
    t = _HOME_RE.sub("<home>", t)
    t = _HOST_RE.sub(_host_mask, t)
    return t.strip()[:cap]


def _sanitize_name(text: str, cap: int) -> str:
    """Bound a gh-supplied job/step NAME — the control/secret strip
    only, no address or hostname masking: a dotted name here is a
    matrix version (``test-linux (3.12, 1)``), never a host, and the
    hostname strip would fold ``3.12``/``3.13`` into one infra
    signature. Log-derived reason text and node ids keep the full
    :func:`_sanitize_log`."""
    t = _CTRL_RE.sub("", ANSI_RE.sub("", mask_secrets(text or "") or ""))
    return t.replace("\t", " ").strip()[:cap]


def _failed_nodes(log_text: str) -> list[tuple[str, str]]:
    """``(sanitised node id, sanitised reason)`` pairs — the pytest
    FAILED/ERROR nodes of one job log, parametrise stripped, order kept,
    deduped. The reason is ``""`` when the summary line has none."""
    out: list[tuple[str, str]] = []
    for raw in log_text.splitlines():
        line = TS_RE.sub("", ANSI_RE.sub("", raw), count=1)
        m = NODE_RE.match(line)
        if not m:
            continue
        node = _sanitize_log(PARAM_RE.sub("", m.group(2)), _NODE_CAP)
        reason = _sanitize_log(m.group(3) or "", _REASON_CAP)
        if node and node not in [n for n, _r in out]:
            out.append((node, reason))
    return out


def _infra_sig(job_name: str) -> str:
    return f"ci:{WORKFLOW_FILE}:infra:{_sanitize_name(job_name, _NODE_CAP)}"


def _run_signatures(
    repo: str, run: dict[str, Any], wf: str, budget: dict[str, int]
) -> dict[str, dict[str, Any]] | None:
    """``{signature: {"job": name, "nodes": [(node, reason)], "infra": b}}``
    for a red run — ``None`` when the call budget ran out mid-read (the
    run then stays unseen for the next tick). For ``soak`` only ``soak*``
    jobs count; a cancelled/timed-out job is an infra signature with no
    log read."""
    jobs = _run_jobs(repo, run, budget)
    if jobs is None:
        return None
    out: dict[str, dict[str, Any]] = {}
    down: list[tuple[str, str]] = []
    for job in jobs:
        concl = job.get("conclusion")
        name = str(job.get("name") or job.get("id") or "")
        if wf == "soak" and not name.startswith(SOAK_JOB_NAME):
            continue
        if concl in ("cancelled", "timed_out"):
            down.append((name, str(concl)))
            continue
        if concl != "failure":
            continue
        jid = job.get("id") or job.get("databaseId")
        if jid is None:
            continue
        if not _take(budget):
            return None
        nodes = _failed_nodes(_gh_log(repo, int(jid)))
        if not nodes:
            out.setdefault(_infra_sig(name), {"job": name, "nodes": [], "infra": True})
            continue
        for node, reason in nodes:
            info = out.setdefault(
                f"ci:{WORKFLOW_FILE}:{node}",
                {"job": name, "nodes": [], "infra": False},
            )
            if (node, reason) not in info["nodes"]:
                info["nodes"].append((node, reason))
    # A fail-fast matrix's cancelled siblings share one infra gripe:
    # the first down job's signature files, the rest are named in its
    # body (``with``).
    for i, (name, concl) in enumerate(down):
        if i == 0:
            out.setdefault(
                _infra_sig(name),
                {"job": name, "nodes": [], "infra": True, "with": []},
            )
        else:
            out[_infra_sig(down[0][0])].setdefault("with", []).append(
                f"{name} ({concl})"
            )
    return out


def _soak_verdict(jobs: list[dict[str, Any]]) -> str:
    """A release run's ci_watch verdict — ``red`` iff a ``soak*`` leg went
    down; ``skipped`` when the soak never ran (gr586293: every leg
    skipped, or the only soak artifact is the unexpanded matrix's
    placeholder — a skipped soak is not a gripe and not a green the
    flake streak could count); else ``green``. The rest of the run
    belongs to the release loop."""
    legs = [j for j in jobs if str(j.get("name") or "").startswith(SOAK_JOB_NAME)]
    if any(j.get("conclusion") in BAD_JOB_CONCLUSIONS for j in legs):
        return "red"
    if legs:
        return (
            "skipped"
            if all(j.get("conclusion") == "skipped" for j in legs)
            else "green"
        )
    return (
        "skipped"
        if any(_soak_placeholder(str(j.get("name") or "")) for j in jobs)
        else "green"
    )


def _verdict_of(wf: str, run: dict[str, Any], seen: dict[str, dict[str, Any]]) -> str:
    """The run's ci_watch verdict — the ledger's when it was judged on
    an earlier tick, else computed (soak: ``soak*`` legs plus the
    skipped matrix's placeholder; a fully skipped soak reads
    ``skipped``, not green)."""
    known = seen.get(str(run.get("id") or ""))
    if isinstance(known, dict):
        return str(known.get("verdict") or "")
    if wf == "soak":
        return _soak_verdict(list(run.get("_jobs") or []))
    return "green" if run.get("conclusion") in OK_CONCLUSIONS else "red"


def _green_streak(
    wf: str, runs: list[dict[str, Any]], seen: dict[str, dict[str, Any]]
) -> int:
    """Consecutive green verdicts counting down from the newest run."""
    streak = 0
    for r in sorted(runs, key=lambda r: int(r.get("id") or 0), reverse=True):
        if _verdict_of(wf, r, seen) != "green":
            break
        streak += 1
    return streak


def _newest_green(
    wf: str, runs: list[dict[str, Any]], seen: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    for r in sorted(runs, key=lambda r: int(r.get("id") or 0), reverse=True):
        if _verdict_of(wf, r, seen) == "green":
            return r
    return None


def _title(wf: str, sig: str) -> str:
    t = f"{wf} red: {sig}"
    return t if len(t) <= _TITLE_CAP else t[: _TITLE_CAP - 1].rstrip() + "…"


def _comment(store: Any, conn: Any, ref_id: int, text: str) -> None:
    """Append ``text`` as the gripe's next ``gripe_comment`` chunk (the
    ``gripe_dispatch._append_comment`` shape, conn-scoped)."""
    row = conn.execute(
        "SELECT coalesce(max(ord), -1) + 1 FROM chunks WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(
                ord=int(row[0]) if row else 0,
                text=text,
                meta={
                    "chunk_kind": "gripe_comment",
                    # worker-stamped — the G2 trust boundary (td575705).
                    **worker_comment_meta(),
                },
            )
        ],
        conn=conn,
    )


def _status_of(store: Any, conn: Any, ref_id: int) -> str:
    for t in store.tags_for(ref_id, conn=conn):
        if t.prefix == "STATUS":
            return str(t.value)
    return "open"


def _open_tags(store: Any, conn: Any, ref_id: int) -> set[str]:
    return {
        str(t.value) for t in store.tags_for(ref_id, conn=conn) if t.namespace == "open"
    }


def _title_matches(conn: Any, title: str) -> list[int]:
    rows = conn.execute(
        "SELECT ref_id FROM refs WHERE kind = 'gripe' "
        "AND retired_at IS NULL AND title = %s ORDER BY ref_id",
        (title,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _status_tagged_at(conn: Any, ref_id: int) -> datetime | None:
    """The newest terminal-STATUS tag time — when the gripe closed."""
    row = conn.execute(
        "SELECT max(rt.created_at) FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        "WHERE rt.ref_id = %s AND t.namespace = 'STATUS' AND t.value = ANY(%s)",
        (ref_id, list(_TERMINAL)),
    ).fetchone()
    return row[0] if row else None


def _body_text(
    sig: str,
    wf: str,
    run: dict[str, Any],
    info: dict[str, Any],
    regression: int | None,
    siblings: list[int],
) -> str:
    """The actionable body — structured fields only, every log-derived
    string sanitised and capped (module docstring)."""
    url = str(run.get("html_url") or "")
    sha9 = str(run.get("head_sha") or "")[:9]
    run_id = str(run.get("id") or "")
    job = _sanitize_name(str(info.get("job") or ""), _NODE_CAP)
    nodes: list[tuple[str, str]] = info.get("nodes") or []
    first_node = nodes[0][0] if nodes else ""
    if info.get("infra"):
        where = "nightly run" if wf == "nightly" else "release soak"
        lines = [
            f"Goal: check.yml's '{job}' job completes on the {where} "
            "again — an infra failure, not a test.",
            f"step: {job}",
        ]
    elif wf == "nightly":
        lines = [
            f"Goal: the test passes on main — {first_node} is green on "
            "the check.yml nightly."
        ]
    else:
        lines = [
            "Goal: the test passes on the release soak — "
            f"{first_node} is green on check.yml's soak job."
        ]
    lines += [
        f"signature: {sig}",
        f"run: {url}",
        f"head: {sha9}",
        f"job: {job}",
        f"digest: scripts/ci-failed {run_id}",
    ]
    others = [_sanitize_name(str(n), _NODE_CAP) for n in info.get("with") or []]
    if others:
        lines.append("also down: " + ", ".join(others))
    if regression is not None:
        lines.append(f"regressed after gr{regression}")
    if siblings:
        lines.append("siblings: " + ", ".join(f"gr{s}" for s in siblings))
    for node, reason in nodes[:_MAX_NODE_LINES]:
        lines.append(f"node: {node}")
        if reason:
            lines.append(f"reason: {reason}")
    text = "\n".join(lines)
    if len(text) > _BODY_CAP:
        text = text[: _BODY_CAP - 1].rstrip() + "…"
    return text


def _insert_gripe(store: Any, conn: Any, title: str, sig: str, wf: str) -> int:
    """The ref, tags and filing ``dispatch_score`` of a signature gripe
    (the ``factory_rescue._file_sig_gripe`` shape) — the body chunk is
    written separately once the run's sibling set is known."""
    from precis.utils.dispatch_score import SCORE_META_KEY
    from precis.workers.auto_gripe import AUTO_SCORE

    ref = store.insert_ref(
        kind="gripe",
        slug=None,
        title=title,
        meta={"ci": {"sig": sig, "wf": wf}, SCORE_META_KEY: AUTO_SCORE},
        conn=conn,
    )
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", "open"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    infra = sig.startswith(f"ci:{WORKFLOW_FILE}:infra:")
    for value in (
        "fix:infra" if infra else "fix:code",
        "project:precis",
        f"ci:{wf}",
        "thread:factory",
        "size:s",
        "origin:ci-watch",
    ):
        store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
    return int(ref.id)


def _note_run(
    store: Any,
    sigs: dict[str, dict[str, Any]],
    wf: str,
    run: dict[str, Any],
    now: datetime,
    *,
    dry_run: bool,
    res: WatchResult,
) -> None:
    """The dedupe rule for one run's signatures — comment an open gripe,
    refile a terminal one, else file; new gripes get the sibling set of
    the run in their body and pairwise ``related-to`` links. One
    transaction under the advisory lock."""
    url = str(run.get("html_url") or "")
    sha9 = str(run.get("head_sha") or "")[:9]
    if dry_run:
        with store.pool.connection() as conn:
            for sig in sorted(sigs):
                title = _title(wf, sig)
                open_gid = next(
                    (
                        gid
                        for gid in _title_matches(conn, title)
                        if _status_of(store, conn, gid) not in _TERMINAL
                    ),
                    None,
                )
                if open_gid is None:
                    res.would.append(f"file {title}")
                elif "ci:mute" in _open_tags(store, conn, open_gid):
                    res.notes.append(f"gr{open_gid} carries ci:mute — {sig} skipped")
                else:
                    res.would.append(f"comment seen again on gr{open_gid}")
        return
    with store.atomic() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SIG_LOCK_KEY,))
        to_file: list[tuple[str, dict[str, Any], int | None]] = []
        touched: list[int] = []
        for sig in sorted(sigs):
            title = _title(wf, sig)
            regression: int | None = None
            regression_at: datetime | None = None
            handled = False
            for gid in _title_matches(conn, title):
                if _status_of(store, conn, gid) in _TERMINAL:
                    closed_at = _status_tagged_at(conn, gid)
                    if closed_at is not None and (
                        regression_at is None or closed_at > regression_at
                    ):
                        regression, regression_at = gid, closed_at
                    continue
                if "ci:mute" in _open_tags(store, conn, gid):
                    res.notes.append(f"gr{gid} carries ci:mute — {sig} skipped")
                else:
                    _comment(store, conn, gid, f"seen again: run {url} on {sha9}")
                    res.seen_again.append(gid)
                    touched.append(gid)
                handled = True
                break
            if handled:
                continue
            if regression_at is None or regression_at < now - REGRESSION_WINDOW:
                regression = None
            to_file.append((sig, sigs[sig], regression))
        new: list[tuple[str, dict[str, Any], int | None, int]] = [
            (sig, info, reg, _insert_gripe(store, conn, _title(wf, sig), sig, wf))
            for sig, info, reg in to_file
        ]
        touched.extend(gid for _sig, _info, _reg, gid in new)
        for sig, info, reg, gid in new:
            siblings = [t for t in touched if t != gid]
            store.chunks.insert_chunks(
                gid,
                [
                    ChunkInsert(
                        ord=0,
                        text=_body_text(sig, wf, run, info, reg, siblings),
                        meta={"chunk_kind": "gripe_body"},
                    )
                ],
                conn=conn,
            )
            res.filed.append(gid)
        # One run's gripes are each other's evidence — pairwise.
        for i, a in enumerate(touched):
            for b in touched[i + 1 :]:
                store.add_link(
                    src_ref_id=a,
                    dst_ref_id=b,
                    relation="related-to",
                    set_by="system",
                    conn=conn,
                )


def _ci_gripes(conn: Any) -> list[tuple[int, str, str | None]]:
    """``(ref_id, status, wf)`` for every live ``origin:ci-watch`` gripe
    that is not muted (``ci:mute`` silences a gripe entirely)."""
    rows = conn.execute(
        """
        SELECT r.ref_id,
               COALESCE((SELECT s.value FROM ref_tags rs
                          JOIN tags s ON s.tag_id = rs.tag_id
                          WHERE rs.ref_id = r.ref_id AND s.namespace = 'STATUS'
                          LIMIT 1), 'open') AS status,
               (SELECT o.value FROM ref_tags ro
                 JOIN tags o ON o.tag_id = ro.tag_id
                 WHERE ro.ref_id = r.ref_id AND o.namespace = 'OPEN'
                   AND o.value = ANY(%s) LIMIT 1) AS wf
          FROM refs r
         WHERE r.kind = 'gripe' AND r.retired_at IS NULL
           AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                          AND t.value = 'origin:ci-watch')
           AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                            WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                              AND t.value = 'ci:mute')
         ORDER BY r.ref_id
        """,
        (["ci:nightly", "ci:soak"],),
    ).fetchall()
    # ``o.value`` is the tag value ``ci:<wf>``; the sweep keys on ``<wf>``.
    return [
        (int(r[0]), str(r[1]), None if r[2] is None else str(r[2]).rsplit(":", 1)[-1])
        for r in rows
    ]


def _comments_of(conn: Any, ref_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT text FROM chunks WHERE ref_id = %s "
        "AND chunk_kind = 'gripe_comment' ORDER BY ord",
        (ref_id,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _resolved_sha(comments: list[str]) -> str | None:
    """The newest ``resolved: <sha>`` the gripe's comments name."""
    sha = None
    for text in comments:
        for m in RESOLVED_RE.finditer(text):
            sha = m.group(1)
    return sha


def _live_todo_link(conn: Any, gripe_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM links l JOIN refs t ON t.ref_id = l.src_ref_id "
        "WHERE l.dst_ref_id = %s AND l.relation = 'related-to' "
        "AND t.kind = 'todo' AND t.retired_at IS NULL LIMIT 1",
        (gripe_id,),
    ).fetchone()
    return row is not None


def _contains(repo: str, resolved: str, head: str, budget: dict[str, int]) -> bool:
    """True when ``head`` contains ``resolved`` — compare status
    ``identical`` or ``ahead``."""
    if not _take(budget):
        return False
    data = _gh_json(f"repos/{repo}/compare/{resolved}...{head}")
    status = str(data.get("status") or "") if isinstance(data, dict) else ""
    return status in ("identical", "ahead")


def _sweep(
    store: Any,
    repo: str,
    streaks: dict[str, int],
    newest_green: dict[str, dict[str, Any] | None],
    budget: dict[str, int],
    *,
    dry_run: bool,
    res: WatchResult,
) -> None:
    """The close side: flake-close an unresolved gripe after two
    consecutive green runs of its workflow; comment ``green on <url>`` on
    a ``resolved:`` gripe once the newest green head contains its sha."""
    with store.pool.connection() as conn:
        gripes = _ci_gripes(conn)
        for gid, status, wf in gripes:
            if status in _TERMINAL or wf is None:
                continue
            comments = _comments_of(conn, gid)
            resolved = _resolved_sha(comments)
            if resolved is not None:
                run = newest_green.get(wf)
                if run is None:
                    continue
                url = str(run.get("html_url") or "")
                head = str(run.get("head_sha") or "")
                if any(url in c for c in comments if c.startswith("green on ")):
                    continue
                if not _contains(repo, resolved, head, budget):
                    continue
                if dry_run:
                    res.would.append(f"comment green on gr{gid}")
                    continue
                with store.atomic() as wconn:
                    _comment(store, wconn, gid, f"green on {url}")
                res.green_noted.append(gid)
                continue
            if streaks.get(wf, 0) < 2 or _live_todo_link(conn, gid):
                continue
            if dry_run:
                res.would.append(f"close gr{gid} (flake)")
                continue
            with store.atomic() as wconn:
                store.add_tag(
                    gid,
                    Tag.closed("STATUS", "done"),
                    set_by="system",
                    replace_prefix=True,
                    conn=wconn,
                )
                _comment(
                    store,
                    wconn,
                    gid,
                    "closed by ci_watch: 2 consecutive green runs, "
                    "no fix landed (flake)",
                )
            res.closed.append(gid)


def _load_seen(store: Any) -> dict[str, dict[str, Any]]:
    from precis import settings

    raw = settings.get_str(SEEN_SETTING, store=store)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("%s: %s unparsable — starting empty", NAME, SEEN_SETTING)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _save_seen(store: Any, seen: dict[str, dict[str, Any]]) -> None:
    from precis import settings

    items = sorted(
        seen.items(), key=lambda kv: str(kv[1].get("at") or ""), reverse=True
    )[:SEEN_CAP]
    settings.set_setting(SEEN_SETTING, json.dumps(dict(items)), store=store)


def _z(now: datetime) -> str:
    """ISO-8601 UTC labelled ``Z`` (never ``+00:00``)."""
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def run_pass(
    store: Any,
    *,
    repo: str | None = None,
    dry_run: bool = False,
    reason: str | None = None,
    lookback_runs: int = DEFAULT_LOOKBACK,
    now: datetime | None = None,
) -> WatchResult:
    """One watch pass (module docstring): judge each unseen run, then
    the close sweep."""
    from precis.plans import pr_class

    now = now or datetime.now(UTC)
    res = WatchResult(dry_run=dry_run, reason=reason)
    res.repo = pr_class.resolve_repo(store, repo)
    if not res.repo:
        res.blocker = (
            "no GitHub owner/repo (params.repo, release.repo and the "
            "checkout's origin all empty); next: set release.repo"
        )
        return res
    seen = _load_seen(store)
    changed = False
    budget = {"left": _API_BUDGET}
    try:
        nightly = _nightly_runs(res.repo, lookback_runs, budget)
        soak_fresh, soak_scope, probed_empty = _soak_runs(res.repo, seen, budget)
        # A probed candidate with no soak artifact at all is not a soak
        # run — record the probe (verdict "no-soak") so it is never
        # probed again.
        for r in probed_empty:
            rid = str(r.get("id") or "")
            if rid and rid not in seen:
                seen[rid] = {
                    "at": str(r.get("updated_at") or _z(now)),
                    "verdict": "no-soak",
                    "wf": "scan",
                }
                changed = True
        fetched = {"nightly": nightly, "soak": soak_fresh}
        scope = {"nightly": nightly, "soak": soak_scope}
        for wf, runs in fetched.items():
            for run in sorted(runs, key=lambda r: int(r.get("id") or 0)):
                rid = str(run.get("id") or "")
                if not rid or rid in seen:
                    continue
                verdict = _verdict_of(wf, run, seen)
                if verdict == "red":
                    sigs = _run_signatures(res.repo, run, wf, budget)
                    if sigs is None:
                        res.notes.append(
                            f"run {rid}: gh budget out mid-read — left unseen"
                        )
                        break
                    if not sigs:
                        res.notes.append(
                            f"run {rid} red but no {wf} signatures — left unseen"
                        )
                        continue
                    _note_run(store, sigs, wf, run, now, dry_run=dry_run, res=res)
                seen[rid] = {
                    "at": str(run.get("updated_at") or _z(now)),
                    "verdict": verdict,
                    "wf": wf,
                }
                changed = True
                res.processed += 1
        _sweep(
            store,
            res.repo,
            {wf: _green_streak(wf, runs, seen) for wf, runs in scope.items()},
            {wf: _newest_green(wf, runs, seen) for wf, runs in scope.items()},
            budget,
            dry_run=dry_run,
            res=res,
        )
    except GhUnavailable as exc:
        res.gh_error = str(exc)
    if changed and not dry_run:
        _save_seen(store, seen)
    return res


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher: one pass; a gh failure is one
    ``job_event`` chunk and a clean failure line, never a traceback."""
    del spec
    from precis.workers.job_types.plan_dispatch import read_only_reason

    params = dict(ctx.meta.get("params") or {})
    reason = read_only_reason(ctx.store, params)
    try:
        res = run_pass(
            ctx.store,
            repo=params.get("repo"),
            dry_run=reason is not None,
            reason=reason,
            lookback_runs=int(params.get("lookback_runs") or DEFAULT_LOOKBACK),
        )
    except Exception as exc:  # a failed pass must not wedge the lane
        log.warning("ci_watch: pass raised (%s)", type(exc).__name__, exc_info=True)
        ctx.record_failure(f"ci_watch: pass raised: {type(exc).__name__}: {exc}")
        return
    ctx.set_meta(ci_watch=res.counts())
    ctx.append_chunk("job_summary", res.render())
    log.info("%s", res.summary())
    if res.gh_error:
        ctx.append_chunk("job_event", f"ci_watch: gh read failed — {res.gh_error}")
        ctx.record_failure(f"ci_watch: gh read failed — {res.gh_error}")


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description=DESCRIPTION,
    dispatch=_dispatch,
    # A second watch while one is pending adds nothing.
    dedupe_pending=True,
    # It reads live DB/GitHub state at claim time; nothing enqueued rots.
    queue_ttl_exempt=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "BAD_JOB_CONCLUSIONS",
    "DEFAULT_LOOKBACK",
    "DESCRIPTION",
    "NAME",
    "OK_CONCLUSIONS",
    "PARAMS_SCHEMA",
    "REGRESSION_WINDOW",
    "SEEN_CAP",
    "SEEN_SETTING",
    "SOAK_JOB_NAME",
    "SOAK_LIMIT",
    "SOAK_PROBE_CAP",
    "SPEC",
    "WORKFLOW_FILE",
    "GhUnavailable",
    "WatchResult",
    "load",
    "run_pass",
]
