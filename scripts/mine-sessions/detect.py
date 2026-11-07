#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Friction detectors over the normalized event stream (`schema.Event`).

Each detector is a pure function ``list[Event] -> list[Candidate]``,
registered under a short id via ``@detector(...)`` so adding one later is
one function plus one decorator line. A ``Candidate`` is a pointer, never a
payload: ``evidence`` holds ``(session, seq)`` refs that ``cards.py``
resolves back to text. Detectors never write to disk and never call
``redact.scrub`` themselves — the CLI at the bottom of this file does that
once, on the way out, so every writer obeys the "scrub on write" contract in
``redact.py`` exactly once per byte.

**Read the calibration fact before trusting any of this.** The measured
error rate on the precis MCP surface is 0.8% (609 errors in 73,472 calls,
see ``docs/runbooks/surface-review.md``). Errors are not where the waste is.
The error-shaped detectors here (D1, D2, D8, D9, D11) exist because a badly
worded error is expensive when it fires, not because errors are frequent;
the byte- and retry-shaped detectors (D4, D5, D6, D7) are the dominant
signal and should be read first.

**D0 (`exec_class`) must run, and be read, before any of the others.** It
separates harness noise (a denied permission prompt, a rejected tool call, a
harness hang) from actual surface behaviour. An earlier audit that skipped
this step let that noise dominate its stats and inverted its conclusions —
see the runbook's calibration section. Every rate computed by another
detector or by ``stats.py`` implicitly assumes D0's noise classes have
already been set aside; ``exec_class()`` is exported so ``stats.py`` reuses
the exact same rule rather than a second, driftable copy.

Each detector's docstring states what a hit *means* and which fix class it
implies, using the vocabulary from ``docs/runbooks/surface-review.md``:
skill edit, error-message fix, MCP capability, render diet, doc fix,
harness-hook fix. That text is read by a `forensics` agent triaging the
cards, not by a human skimming this file — write it for that reader.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import outdir
from redact import scrub
from schema import Event, from_json

# ---------------------------------------------------------------------------
# Shared shapes
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class EvidenceRef:
    """A pointer to one event, never its payload."""

    session: str
    seq: int


@dataclass(slots=True)
class Candidate:
    """One finding. ``metrics`` carries whatever extra numbers a detector
    needs beyond the shared shape (byte percentiles, retry means, ...);
    ``caveat`` is set by detectors (D7) whose signal is a proxy rather than
    a direct observation, and must be read alongside the numbers, not
    dropped."""

    detector: str
    signature: str
    n: int
    n_sessions: int
    first_ts: str | None
    last_ts: str | None
    evidence: list[EvidenceRef]
    metrics: dict[str, Any] = field(default_factory=dict)
    caveat: str = ""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def is_precis(ev: Event) -> bool:
    """A call is a precis call iff extract.py resolved a verb for it — the
    invariant ``schema.Event.verb`` documents."""
    return ev.verb is not None


_ARG_VALUE_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _arg_value(ev: Event, name: str) -> str | None:
    """Lift one argument's VALUE out of ``arg_digest``, or ``None``.

    Pre-0209 only payload-bearing corpora had a digest at all — the
    ledger recorded argument names and never values — so a ``None``
    here means "not knowable", not "not passed". Callers comparing two
    events must treat ``None`` as a non-match rather than as equality,
    or every ledger pair looks identical.

    Two digest dialects exist: the local extractor's JSON dump
    (``{"id": 366639, "kind": "gripe"}``) and the ``id=5`` rendering (jobs
    transcripts, hand-built cards) — the ledger's 0209 ``arg_sample``
    JSONB serializes to the same JSON shape, so the dialect reader
    covers it for free. Read both — the JSON form is the whole local
    corpus, so a single-dialect reader is silently blind there (it is
    why ``skill_taught_wrong`` reported ``skill:?`` for an id that was
    right in front of it).
    """
    if name not in ev.arg_keys or not ev.arg_digest:
        return None
    pat = _ARG_VALUE_RE_CACHE.get(name)
    if pat is None:
        pat = re.compile(
            rf"\b{re.escape(name)}\s*=\s*['\"]?([\w./:-]+)"
            rf"|\"{re.escape(name)}\"\s*:\s*\"?([\w./:-]+)"
        )
        _ARG_VALUE_RE_CACHE[name] = pat
    m = pat.search(ev.arg_digest)
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def has_real_session(ev: Event) -> bool:
    """Whether ``ev.session`` names a real conversational thread rather
    than a synthesized bucket.

    Post-0209 the ledger *does* carry one: ``tool_calls.session_id``
    (minted per MCP session) or the ``agentlog:`` fallback for inproc
    ticks — ``extract._ledger_session`` resolves both into ``session``.
    What remains synthetic is the ``<prefix>:YYYY-MM-DD`` day bucket
    (and ``:unknown``) used for pre-0209 rows and for llmlog, whose rows
    still have no thread identity at all.
    """
    s = ev.session or ""
    return bool(s) and not s.startswith(("ledger:", "llmlog:"))


def by_session(events: Sequence[Event]) -> dict[str, list[Event]]:
    """Group events by session, each list sorted by ``seq`` — **only for
    corpora whose ``session`` is a real conversational thread.**

    Synthetic ``ledger:YYYY-MM-DD`` / ``llmlog:YYYY-MM-DD`` buckets are
    not threads: one "session" would be the entire fleet's calls for a
    calendar day, interleaved across dozens of unrelated concurrent
    jobs. Every adjacency rule ("within 3 turns", "3+ consecutive
    calls") is meaningless there and fires constantly: on the first
    real pass this reported ``get|quest`` reformulating 26 times inside
    a **19-second** range — 26 different jobs each making one call,
    read as one agent hunting for an argument shape.

    Admission = ``has_payload`` (local/codex/jobs) **or**
    ``has_real_session`` — post-0209 ledger rows earn admission via
    their real ``session_id``/``agentlog:`` key even though they carry
    no result payload (migration 0209 added ``session_id``/``seq``/
    ``arg_sample``/``error_head`` for exactly this).

    The filter lives here rather than in each detector because every
    adjacency detector reaches for this function, and a future one would
    otherwise have to remember a rule it cannot see. Aggregate detectors
    (``hard_error``, ``detour_census``, ``vocab_near_miss``, ``spin``) walk
    ``events`` directly and so keep the full fleet-wide denominator.
    """
    out: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        if not (has_payload(ev) or has_real_session(ev)):
            continue
        out[ev.session].append(ev)
    for sess in out:
        out[sess].sort(key=lambda e: e.seq)
    return out


def percentile(sorted_vals: Sequence[int], p: float) -> float:
    """Linear-interpolation percentile over an already-sorted sequence.
    No numpy dependency — this is the one copy; ``stats.py`` imports it."""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    if lo == hi:
        return float(sorted_vals[lo])
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_DIGITS_RE = re.compile(r"\d+")
_PATHISH_RE = re.compile(r"(?:/[\w.\-]+){2,}")


def normalize_err_head(text: str) -> str:
    """Collapse an error head to a family signature: paths -> ``<path>``,
    quoted ids -> ``<id>``, digit runs -> ``#``. So ``no paper with slug
    'a'`` and ``no paper with slug 'b'`` land in the same D1/D2/D11 bucket
    instead of two singleton ones each too small to act on."""
    t = (text or "").strip()
    t = _PATHISH_RE.sub("<path>", t)
    t = _QUOTED_RE.sub("<id>", t)
    t = _DIGITS_RE.sub("#", t)
    return t[:160]


def _levenshtein(a: str, b: str) -> int:
    """Local edit-distance helper (D9) — no rapidfuzz/python-Levenshtein
    dependency for one small check."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def _candidate(
    detector_id: str,
    signature: str,
    evs: Sequence[Event],
    metrics: dict[str, Any] | None = None,
    caveat: str = "",
) -> Candidate:
    evs_sorted = sorted(evs, key=lambda e: (e.ts or "", e.session, e.seq))
    sessions = {e.session for e in evs}
    tss = sorted(e.ts for e in evs if e.ts)
    return Candidate(
        detector=detector_id,
        signature=signature,
        n=len(evs),
        n_sessions=len(sessions),
        first_ts=tss[0] if tss else None,
        last_ts=tss[-1] if tss else None,
        evidence=[EvidenceRef(e.session, e.seq) for e in evs_sorted],
        metrics=metrics or {},
        caveat=caveat,
    )


# ---------------------------------------------------------------------------
# D0 — execution-class segmentation (shared by every other stat)
# ---------------------------------------------------------------------------

_BLOCKED_MARKERS = ("claude requested permissions", "haven't granted")
_REJECTED_MARKERS = ("tool use was rejected",)
_HANG_MARKERS = ("sent no response", "aborting")
_ZERO_MARKERS_RE = re.compile(
    r"\bno (results?|matches?|hits?|rows?|items?)\b|^\[\]$|^0 results", re.I
)


def _is_zero(ev: Event) -> bool:
    if ev.is_error:
        return False
    if ev.result_bytes == 0:
        return True
    text = (ev.result_head or "").strip()
    return bool(text) and len(text) < 200 and bool(_ZERO_MARKERS_RE.search(text))


#: Corpora whose events carry a result payload. The ``tool_calls`` ledger and
#: ``llm_call_log`` deliberately carry none (migration 0133: "no payload
#: content, ever"), so every one of their rows has ``result_bytes == 0`` and
#: would be classified ``zero`` — an artifact that swamped the real mix
#: 34382:3052 on the first real pass. Anything reading a payload must gate on
#: this set, not on the event alone.
PAYLOAD_CORPORA = frozenset({"local", "codex", "devin", "jobs"})


def has_payload(ev: Event) -> bool:
    return ev.corpus in PAYLOAD_CORPORA


def exec_class(ev: Event) -> str | None:
    """Classify one precis call's outcome: ``blocked`` > ``rejected`` >
    ``hang`` > ``zero`` > ``ok``, in that priority order. Returns ``None``
    for non-precis events and for payload-free corpora (see
    :data:`PAYLOAD_CORPORA`) — a ledger row is not an empty result, it is a
    row that never had a result to begin with.

    ``blocked``/``rejected``/``hang`` are harness noise — a denied
    permission prompt, a user-rejected tool call, a turn that got no
    response — not surface behaviour; every rate elsewhere in this module
    and in ``stats.py`` is computed only over the remaining ``ok``/``zero``
    population. ``zero`` (successful call, empty or no-match result) is
    real surface-quality signal on its own: the caller had a plausible
    query and the corpus/index had nothing for it. Fix class for
    blocked/rejected/hang is harness-hook fix (nothing here to fix in the
    MCP itself); fix class for a recurring ``zero`` is doc fix or MCP
    capability depending on whether the query shape was reasonable.
    """
    if not is_precis(ev) or not has_payload(ev):
        return None
    text = f"{ev.err_head or ''} {ev.result_head or ''}".lower()
    if any(m in text for m in _BLOCKED_MARKERS):
        return "blocked"
    if any(m in text for m in _REJECTED_MARKERS):
        return "rejected"
    if any(m in text for m in _HANG_MARKERS):
        return "hang"
    if _is_zero(ev):
        return "zero"
    return "ok"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

DETECTORS: dict[str, Callable[[Sequence[Event]], list[Candidate]]] = {}


def detector(
    id_: str,
) -> Callable[
    [Callable[[Sequence[Event]], list[Candidate]]],
    Callable[[Sequence[Event]], list[Candidate]],
]:
    def deco(
        fn: Callable[[Sequence[Event]], list[Candidate]],
    ) -> Callable[[Sequence[Event]], list[Candidate]]:
        DETECTORS[id_] = fn
        return fn

    return deco


@detector("exec_class")
def detect_exec_class(events: Sequence[Event]) -> list[Candidate]:
    """One candidate per non-``ok`` execution class found among precis
    calls (see ``exec_class``). Run and read this first: it is the guard
    that keeps every other detector's rate from being contaminated by
    harness noise. Fix class: harness-hook fix for blocked/rejected/hang;
    doc fix / MCP capability for a large ``zero`` bucket.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        cls = exec_class(ev)
        if cls and cls != "ok":
            groups[cls].append(ev)
    return [_candidate("exec_class", cls, evs) for cls, evs in groups.items()]


@detector("hard_error")
def detect_hard_error(events: Sequence[Event]) -> list[Candidate]:
    """Errored precis calls grouped by ``(verb, kind, err_type,
    normalized err_head)``. A large group names a single recurring failure
    family, not 40 unrelated one-offs — the normalization
    (``normalize_err_head``) is what makes that visible. Fix class:
    error-message fix when the wording is the problem, MCP capability when
    ``err_type`` shows the call asked for something the surface cannot do.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        if not is_precis(ev) or not ev.is_error:
            continue
        sig = f"{ev.verb}|{ev.kind}|{ev.err_type}|{normalize_err_head(ev.err_head)}"
        groups[sig].append(ev)
    return [_candidate("hard_error", sig, evs) for sig, evs in groups.items()]


@detector("retry_to_success")
def detect_retry_to_success(events: Sequence[Event]) -> list[Candidate]:
    """Same ``(verb, kind)`` re-called within 3 turns after an errored
    call, until a success — grouped by the originating error signature,
    with the mean retry count as ``metrics['mean_retries']``. This is the
    error-MESSAGE-quality metric, not the error-rate metric: a message that
    tells the caller exactly what to fix gets retried zero or one times; a
    message that takes three tries to work around is a badly worded error
    even though the call "worked eventually". Ranked by
    ``n * mean_retries`` (``metrics['rank']``). Fix class: error-message
    fix.
    """
    groups: dict[str, list[tuple[Event, int]]] = defaultdict(list)
    for sess_events in by_session(events).values():
        precis_evs = [e for e in sess_events if is_precis(e)]
        for i, err_ev in enumerate(precis_evs):
            if not err_ev.is_error:
                continue
            sig = f"{err_ev.verb}|{err_ev.kind}|{normalize_err_head(err_ev.err_head)}"
            retries = 0
            for cand in precis_evs[i + 1 :]:
                if cand.seq - err_ev.seq > 3:
                    break
                if cand.verb == err_ev.verb and cand.kind == err_ev.kind:
                    retries += 1
                    if not cand.is_error:
                        groups[sig].append((err_ev, retries))
                        break
    cands = []
    for sig, items in groups.items():
        evs = [e for e, _ in items]
        retries_list = [r for _, r in items]
        mean_retries = sum(retries_list) / len(retries_list)
        cands.append(
            _candidate(
                "retry_to_success",
                sig,
                evs,
                metrics={
                    "mean_retries": round(mean_retries, 2),
                    "rank": round(len(items) * mean_retries, 2),
                },
            )
        )
    cands.sort(key=lambda c: c.metrics.get("rank", 0), reverse=True)
    return cands


@detector("reformulation")
def detect_reformulation(events: Sequence[Event]) -> list[Candidate]:
    """3+ consecutive calls to the same ``(verb, kind)`` with differing
    ``arg_keys`` shapes and no intervening non-precis tool call in between
    (assistant text and user turns don't break the streak; another tool
    call does). The agent is hunting for the argument shape by trial and
    error. Fix class: doc fix (the call's expected args aren't documented
    clearly enough) or skill edit when a skill is what taught the wrong
    shape.
    """
    groups: dict[str, list[Event]] = defaultdict(list)

    def flush(streak: list[Event]) -> None:
        if len(streak) < 3:
            return
        if len({tuple(e.arg_keys) for e in streak}) < 2:
            return
        sig = f"{streak[0].verb}|{streak[0].kind}"
        groups[sig].extend(streak)

    for sess_events in by_session(events).values():
        streak: list[Event] = []
        for ev in sess_events:
            if is_precis(ev):
                if streak and ev.verb == streak[-1].verb and ev.kind == streak[-1].kind:
                    streak.append(ev)
                else:
                    flush(streak)
                    streak = [ev]
            elif ev.tool is not None and ev.tool != "__user__":
                flush(streak)
                streak = []
            # assistant text (tool is None) / user turns leave the streak intact
        flush(streak)
    return [_candidate("reformulation", sig, evs) for sig, evs in groups.items()]


@detector("abandon_detour")
def detect_abandon_detour(events: Sequence[Event]) -> list[Candidate]:
    """An error or ``zero`` on ``kind`` K, then within 5 turns a ``Bash``
    call that *invokes* a detour tool in command position
    (``detour_bucket``: psql/prod-psql, the precis CLI, a precis-surface
    curl) or whose command text mentions K's own name. The agent gave up
    on the MCP surface and went around it. A line merely *containing*
    ``prod-psql`` — an ``echo`` of a quoted example, say — does not count,
    same rule as ``detour_census``. Fix class: MCP capability — the
    surface is missing the verb/view the agent needed.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    for sess_events in by_session(events).values():
        for i, ev in enumerate(sess_events):
            if not is_precis(ev) or not ev.kind:
                continue
            if not (ev.is_error or exec_class(ev) == "zero"):
                continue
            k = ev.kind
            for cand in sess_events[i + 1 :]:
                if cand.seq - ev.seq > 5:
                    break
                if cand.tool == "Bash":
                    cmd = (cand.arg_digest or "").lower()
                    if detour_bucket(cand.arg_digest or "") is not None or (
                        k.lower() in cmd
                    ):
                        sig = f"{ev.verb}|{k}"
                        groups[sig].extend([ev, cand])
                        break
    return [_candidate("abandon_detour", sig, evs) for sig, evs in groups.items()]


def _shell_segments(cmd: str) -> list[str]:
    """Split a shell command into pipeline/sequence segments at ``|``,
    ``;``, ``&``, newlines, ``$(`` and backticks — but only OUTSIDE
    quotes. A separator inside ``'…'`` or ``"…"`` is literal text, not a
    command boundary, so ``echo 'example; prod-psql'`` is one segment
    whose command word is ``echo`` — not a psql call (td580721). A
    backslash escapes the next character outside single quotes."""
    segs: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            buf.append(ch)
            if ch == "\\" and i + 1 < len(cmd):
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(cmd):
            buf.append(cmd[i : i + 2])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in "|;&\n`" or (ch == "$" and cmd[i + 1 : i + 2] == "("):
            segs.append("".join(buf))
            buf = []
            i += 2 if ch == "$" else 1
            continue
        buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return segs


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
#: Prefixes through which a detour tool can still be the real command word:
#: ``sudo prod-psql``, ``xargs prod-psql``, ``env A=1 psql``,
#: ``uv run scripts/prod-psql``. Shells are NOT here — they live in
#: ``_SHELL_WORDS`` so a ``-c`` argument can be descended into.
_WRAPPER_WORDS = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "time",
        "command",
        "nice",
        "nohup",
        "watch",
        "timeout",
        "xargs",
        "exec",
        "run",
        "uv",
    }
)
#: Shells whose ``-c`` argument is itself a command line —
#: ``bash -c 'echo x; prod-psql'`` still counts, because the quoted text
#: is a script that runs, not literal output (td580721).
_SHELL_WORDS = frozenset({"bash", "sh", "zsh", "fish"})
#: A word that IS a detour tool (optionally path-qualified): ``psql``,
#: ``prod-psql``, ``scripts/prod-psql``, ``scripts/precis``, bare
#: ``precis``, ``curl``. Anything merely containing the name —
#: ``cat scripts/prod-psql``, ``grep prod-psql``, ``git log --
#: scripts/prod-psql`` — is a mention, not a call; on the 2026-11-05 pass
#: that mention-vs-call confusion was most of the ``other`` bucket's 792
#: hits (gr570090(c)).
_DETOUR_PROG_RE = re.compile(r"(?:[\w./~-]+/)?(?:prod-psql|psql|precis|curl)$", re.I)
_TABLE_RE = re.compile(r"\b(?:from|join|into|update)\s+([a-z_][a-z0-9_.]*)", re.I)
#: SQL arriving via a file rather than inline — ``prod-psql < q.sql``,
#: ``psql -f q.sql``, ``\i q.sql``. The table is hidden inside the file but
#: the file's name still says what was being queried, which is what the
#: census bucket needs (gr570090(c)).
_SQL_FILE_RES = (
    re.compile(r"(?<!<)<(?![<(])\s*([\w./~+-]+)"),
    re.compile(r"(?<!\S)(?:-f|--file)[=\s]+([\w./~+-]+)"),
    re.compile(r"\\i\s+([\w./~+-]+)"),
)
_CMD_JSON_RE = re.compile(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)')
#: URL-shaped arguments to a curl call — a full ``http(s)://`` URL, or a
#: bare ``host[:port][/path]`` that still names the precis surface
#: (localhost, loopback, a scrubbed cluster address, or a ``precis``-named
#: host carrying a port/path/domain). What curl *points at* decides, not
#: the rest of the command line: flag values like ``-o /tmp/precis.json``,
#: ``-d @payload`` or ``-H 'x: precis'`` never match, so an external API
#: call whose output file merely says "precis" is not a detour (td580721).
_CURL_ARG_RE = re.compile(
    r"https?://[^\s\"'\\]+"
    r"|(?<![/\w$.~@=-])"
    r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]"
    r"|\[redacted:(?:tailnet-ip|lan-ip|host)\]"
    r"|[\w.$-]*precis[\w.$-]*(?::\d+|\.|/))"
    r"[^\s\"'\\]*",
    re.I,
)
#: curl counts only when it aims at the precis HTTP surface — the ``/mcp``
#: endpoint, a precis-named host/path, localhost, or a scrubbed cluster
#: address (an IP/hostname is already ``[redacted:…]`` by the time the
#: digest reaches us). A curl to an external API is ordinary web use, and
#: it was the second big ``other``-bucket noise source (gr570090(c)).
_PRECIS_CURL_RE = re.compile(
    r"/mcp\b|precis|localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]"
    r"|\[redacted:(?:tailnet-ip|lan-ip|host)\]",
    re.I,
)


def _bash_command(digest: str) -> str:
    """The shell command inside a Bash call's ``arg_digest``.

    Local extraction renders the digest as ``json.dumps(args)``, so the
    command sits in the ``command`` field — but the digest is a 400-char
    head slice, so a long arg list truncates the JSON and the parse fails;
    the regex fallback reads the still-intact front of the string. Codex
    exec calls may carry ``input`` or a ``command`` list instead.
    """
    try:
        obj = json.loads(digest)
    except (TypeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        cmd = obj.get("command") or obj.get("input")
        if isinstance(cmd, str):
            return cmd
        if isinstance(cmd, list):
            return " ".join(str(x) for x in cmd)
    m = _CMD_JSON_RE.search(digest or "")
    if m:
        frag = m.group(1)
        try:
            return json.loads(f'"{frag}"')
        except ValueError:
            return frag
    return digest or ""


def _sql_intent(seg: str, prog: str) -> str:
    """What a psql/prod-psql invocation was pointed at — the bucket the
    census exists for. An inline ``FROM``/``JOIN``/``INTO``/``UPDATE``
    table read off the SQL, else a ``.sql`` file name (redirect, ``-f``,
    ``\\i`` — the file's name says what was being queried), else the bare
    ``cli:`` bucket for an invocation whose target isn't visible at all."""
    m = _TABLE_RE.search(seg)
    if m:
        return f"table:{m.group(1).lower()}"
    for pat in _SQL_FILE_RES:
        m = pat.search(seg)
        if m:
            return f"sql-file:{m.group(1).rsplit('/', 1)[-1]}"
    return f"cli:{prog}"


def _url_path(url: str) -> str:
    """The path component of a URL argument — bare ``host:port/path``
    tokens get a ``//`` scheme-relative parse. Scrubbed host markers
    (``[redacted:…]``) make urlsplit raise on the bracketed "IPv6", so a
    regex peels scheme+host off instead."""
    try:
        path = urlsplit(url).path if "://" in url else urlsplit(f"//{url}").path
    except ValueError:
        m = re.match(r"(?:[a-z]+://|//)?[^/\s]*", url, re.I)
        rest = url[m.end() :] if m else ""
        path = rest.split("?", 1)[0].split("#", 1)[0]
        if not path.startswith("/"):
            path = ""
    return path or "-"


def _curl_intent(seg: str) -> str | None:
    """The precis-surface endpoint a curl call was pointed at, or ``None``
    for ordinary web use. Only URL arguments decide — the old
    whole-segment search counted ``curl https://api.github.com -o
    /tmp/precis.json`` as precis traffic because the ``-o`` path said
    "precis" (td580721)."""
    for m in _CURL_ARG_RE.finditer(seg):
        url = m.group(0)
        if not _PRECIS_CURL_RE.search(url):
            continue
        return f"endpoint:{_url_path(url)}"
    return None


def _segment_intent(seg: str) -> str | None:
    """The detour signature one shell segment earns, or ``None``. A detour
    tool only counts in command position — the first word after env
    assignments and wrappers — so ``cat scripts/prod-psql`` and
    ``echo use prod-psql`` are mentions, not calls."""
    words = seg.split()
    i = 0
    while i < len(words):
        w = words[i].strip("\"'")
        if _ENV_ASSIGN_RE.match(w) or w.startswith("-"):
            i += 1
            continue
        if w.rsplit("/", 1)[-1] in _SHELL_WORDS:
            # ``bash -c 'cmd…'`` — the real command line sits inside the
            # -c string, so descend into it: ``bash -c 'x; prod-psql'``
            # counts even though the separator was quoted.
            if i + 1 < len(words) and words[i + 1].strip("\"'") == "-c":
                inner = " ".join(words[i + 2 :]).strip("\"'")
                for sub in _shell_segments(inner):
                    hit = _segment_intent(sub)
                    if hit is not None:
                        return hit
                return None
            i += 1
            continue
        if w in _WRAPPER_WORDS:
            i += 1
            continue
        break
    if i >= len(words):
        return None
    prog = words[i].strip("\"'")
    if not _DETOUR_PROG_RE.fullmatch(prog):
        return None
    tool = prog.rsplit("/", 1)[-1].lower()
    if tool == "curl":
        return _curl_intent(seg)
    if tool == "precis":
        return "cli:precis"
    return _sql_intent(seg, tool)


def detour_bucket(arg_digest: str) -> str | None:
    """The detour signature a Bash ``arg_digest`` earns, or ``None`` when it
    holds no detour invocation. Shared by ``detour_census``,
    ``abandon_detour``'s mention check, and (through ``detour_hits``) the
    scoreboard's census counter — the single implementation rule this
    module already applies to ``exec_class``/``percentile``."""
    cmd = _bash_command(arg_digest)
    for seg in _shell_segments(cmd):
        intent = _segment_intent(seg)
        if intent is not None:
            return intent
    return None


def detour_hits(events: Sequence[Event]) -> list[tuple[Event, str]]:
    """``(event, bucket)`` for every Bash call invoking a detour tool in
    command position — with the census's duplicate suppression applied:
    identical ``(seq, ts, arg_digest)`` in different sessions are the same
    observed call extracted under two file paths, counted once.
    ``stats.py``'s census counter consumes this rather than re-filtering,
    so the scoreboard and the cards can never disagree (td580721)."""
    hits: list[tuple[Event, str]] = []
    seen: set[tuple[int, str, str]] = set()
    for ev in events:
        if ev.tool != "Bash":
            continue
        sig = detour_bucket(ev.arg_digest or "")
        if sig is None:
            continue
        if ev.ts:
            key = (ev.seq, ev.ts, ev.arg_digest)
            if key in seen:
                continue
            seen.add(key)
        hits.append((ev, sig))
    return hits


@detector("detour_census")
def detect_detour_census(events: Sequence[Event]) -> list[Candidate]:
    """Every ``Bash`` call that invokes ``psql``/``prod-psql``/the
    ``precis`` CLI/a precis-surface ``curl`` *in command position*
    (``detour_bucket``), bucketed by the table, SQL file, endpoint or CLI
    it touched (``metrics['result_bytes_total']`` alongside the count).
    Mentions — a command line that merely contains ``prod-psql``, including
    inside a quoted string — do not count, and neither does a curl to an
    external API; both were most of the old ``other`` bucket's 792 noise
    hits (gr570090(c)). The event stream comes from ``detour_hits``, which
    suppresses the same call seen at identical seq+ts in two sessions.
    A bucket with a lot of calls and bytes is a missing verb with usage
    data already attached. Fix class: MCP capability — this ranks *which*
    verb to build next by demand, not just that one is missing.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    bytes_by_sig: dict[str, int] = defaultdict(int)
    for ev, sig in detour_hits(events):
        groups[sig].append(ev)
        bytes_by_sig[sig] += ev.result_bytes
    cands = [
        _candidate(
            "detour_census",
            sig,
            evs,
            metrics={"result_bytes_total": bytes_by_sig[sig]},
        )
        for sig, evs in groups.items()
    ]
    cands.sort(key=lambda c: c.n, reverse=True)
    return cands


_VIEW_RE = re.compile(r"view=['\"]?([\w-]+)")


def _extract_view(digest: str) -> str | None:
    m = _VIEW_RE.search(digest or "")
    return m.group(1) if m else None


@detector("render_obesity")
def detect_render_obesity(events: Sequence[Event]) -> list[Candidate]:
    """Per ``(verb, kind, view)``: count and ``p50``/``p95``/``max``/
    ``total`` ``result_bytes``. Plus a sub-signal, signature prefixed
    ``narrow-after-fat|``: the same ``kind`` re-fetched within 3 turns with
    MORE ``arg_keys`` than the first call — evidence the first render was
    too fat and the agent had to narrow it by hand. Fix class: render diet.
    Per the runbook's Levers section, the fix is usually a narrower
    *default* view, not a brand-new one.
    """
    cands: list[Candidate] = []
    groups: dict[tuple[str | None, str | None, str | None], list[Event]] = defaultdict(
        list
    )
    for ev in events:
        if not is_precis(ev):
            continue
        view = _extract_view(ev.arg_digest)
        groups[(ev.verb, ev.kind, view)].append(ev)
    for (verb, kind, view), evs in groups.items():
        sizes = sorted(e.result_bytes for e in evs)
        metrics = {
            "p50_bytes": round(percentile(sizes, 0.5), 1),
            "p95_bytes": round(percentile(sizes, 0.95), 1),
            "max_bytes": max(sizes) if sizes else 0,
            "total_bytes": sum(sizes),
        }
        sig = f"{verb}|{kind}|{view or '-'}"
        cands.append(_candidate("render_obesity", sig, evs, metrics=metrics))

    # narrow-after-fat: the SAME entity re-fetched with a narrower selector,
    # which is evidence the first render handed back more than the caller
    # wanted. All three conditions below were learned the hard way — the
    # first version required only `same kind` + `more arg_keys`, and so
    # counted the ordinary `get(id=X)` → `search(q='something else')`
    # workflow as narrowing, purely because `search`'s argument shape has
    # more keys than `get`'s. That inflated the signal to n=1,830 of which
    # essentially none were real. Demand: both calls are `get`, the same
    # `id`, and strictly more arguments the second time.
    narrow_groups: dict[str, list[Event]] = defaultdict(list)
    for sess_events in by_session(events).values():
        precis_evs = [e for e in sess_events if is_precis(e) and e.kind]
        for i, first in enumerate(precis_evs):
            if first.verb != "get":
                continue
            first_id = _arg_value(first, "id")
            if first_id is None:
                continue
            for later in precis_evs[i + 1 :]:
                if later.seq - first.seq > 3:
                    break
                if (
                    later.verb == "get"
                    and later.kind == first.kind
                    and _arg_value(later, "id") == first_id
                    and len(later.arg_keys) > len(first.arg_keys)
                ):
                    narrow_groups[f"narrow-after-fat|{first.kind}"].extend(
                        [first, later]
                    )
                    break
    for sig, evs in narrow_groups.items():
        cands.append(_candidate("render_obesity", sig, evs))
    return cands


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./-]{5,}")


def _distinctive_token(text: str) -> str | None:
    tokens = _TOKEN_RE.findall(text or "")
    if not tokens:
        return None
    return max(tokens, key=len)


_READ_THEN_UNUSED_CAVEAT = (
    "PROXY, not a verdict: token-reuse absence doesn't prove the read was "
    "wasted (the agent may act on a render without ever quoting it back). "
    "Report and read this as a per-verb rate over many calls, never as a "
    "claim about one specific occurrence."
)


@detector("read_then_unused")
def detect_read_then_unused(events: Sequence[Event]) -> list[Candidate]:
    """A precis result over 4096 bytes whose most distinctive token never
    reappears in the next 2 assistant text blocks in the same session —
    grouped and reported per verb as a rate (``metrics['rate']`` over
    ``metrics['considered']``), always carrying ``caveat``. Fix class:
    render diet, but only act on this alongside ``render_obesity`` —
    it is a proxy for "was this fat render read", not a direct measurement.
    """
    considered: dict[str, int] = defaultdict(int)
    unused: dict[str, list[Event]] = defaultdict(list)
    for sess_events in by_session(events).values():
        assistant_texts = [
            (e.seq, e.result_head or "") for e in sess_events if e.tool is None
        ]
        for ev in sess_events:
            if not is_precis(ev) or ev.result_bytes <= 4096:
                continue
            token = _distinctive_token(ev.result_head)
            if not token:
                continue
            verb = ev.verb or "?"
            considered[verb] += 1
            following = [t for seq, t in assistant_texts if seq > ev.seq][:2]
            if not any(token.lower() in t.lower() for t in following):
                unused[verb].append(ev)
    cands = []
    for verb, evs in unused.items():
        total = considered.get(verb, 0)
        rate = len(evs) / total if total else 0.0
        cands.append(
            _candidate(
                "read_then_unused",
                verb,
                evs,
                metrics={"rate": round(rate, 3), "considered": total},
                caveat=_READ_THEN_UNUSED_CAVEAT,
            )
        )

    # Saturation self-check. On the first real pass this proxy scored 0.995
    # / 1.0 / 1.0 across get / search / more — it fired on essentially every
    # large render regardless of verb or content, which means it was
    # measuring "agents rarely quote raw tool output verbatim in their next
    # two text blocks", not "this render was wasted". A detector that fires
    # on everything discriminates nothing, and the per-verb candidates above
    # are worse than useless then, because they look like findings. Say so
    # in-band rather than leaving a reader to notice.
    all_considered = sum(considered.values())
    all_unused = sum(len(v) for v in unused.values())
    if all_considered >= 20 and all_unused / all_considered >= 0.9:
        cands.append(
            _candidate(
                "read_then_unused",
                "PROXY-SATURATED",
                [],
                metrics={
                    "rate": round(all_unused / all_considered, 3),
                    "considered": all_considered,
                },
                caveat=(
                    "This proxy fired on >=90% of all large renders this "
                    "window, so it is not discriminating and its per-verb "
                    "rates above carry no signal. Treat render_obesity's "
                    "direct byte measurements as the evidence instead, and "
                    "consider retiring or re-specifying this detector."
                ),
            )
        )
    return cands


_SKILLS_DIR = Path(__file__).resolve().parents[2] / "src" / "precis" / "data" / "skills"
_SKILL_KINDS_RE = re.compile(r"^kinds:\s*(.+?)\s*$", re.M)
_SKILL_ID_RE = re.compile(r"^id:\s*(\S+)\s*$", re.M)


def _load_skill_kinds(skills_dir: Path = _SKILLS_DIR) -> dict[str, frozenset[str]]:
    """skill id -> the ``kinds:`` frontmatter list, for every skill file on
    disk (``src/precis/data/skills/*.md``). A skill that names its kinds is
    also naming the errors it can plausibly have taught: an errored call
    on a kind outside the list cannot have been taught by it — the rule
    that removes most of this detector's false positives (gr570090(e)).
    A skill absent from the map (unreadable file, no ``kinds:`` line)
    keeps the old any-error rule — absence of metadata proves nothing."""
    out: dict[str, frozenset[str]] = {}
    try:
        files = sorted(skills_dir.glob("*.md"))
    except OSError:
        return out
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")[:6000]
        except OSError:
            continue
        fm_end = text.find("\n---", 3)
        if not text.startswith("---") or fm_end == -1:
            continue
        frontmatter = text[3:fm_end]
        m = _SKILL_KINDS_RE.search(frontmatter)
        if not m:
            continue
        kinds = frozenset(
            k.strip() for k in m.group(1).strip().strip("[]").split(",") if k.strip()
        )
        if not kinds:
            continue
        out[path.stem] = kinds
        idm = _SKILL_ID_RE.search(frontmatter)
        if idm:
            out[idm.group(1)] = kinds
    return out


@detector("skill_taught_wrong")
def detect_skill_taught_wrong(events: Sequence[Event]) -> list[Candidate]:
    """``get(kind='skill', id=...)`` followed within 3 turns by an errored
    precis call in the same session. The signature names the skill id
    (recovered from ``arg_digest``) — that is the file to fix. Two
    exclusions learned the hard way (gr570090(e)): an errored call on a
    kind outside the read skill's ``kinds:`` frontmatter cannot have been
    taught by it, and an over-cap failure on a skill *read* is a budget
    problem, not the previous skill's teaching — of the first five cards
    three were exactly these two false positives. A failed skill read also
    seeds nothing: an errored ``get|skill`` taught nobody. Fix class:
    skill edit.
    """
    skill_kinds = _load_skill_kinds()
    groups: dict[str, list[Event]] = defaultdict(list)
    for sess_events in by_session(events).values():
        skill_calls = [
            e
            for e in sess_events
            if is_precis(e) and e.verb == "get" and e.kind == "skill" and not e.is_error
        ]
        for skill_ev in skill_calls:
            skill_id = _arg_value(skill_ev, "id") or "?"
            kinds = skill_kinds.get(skill_id)
            for cand in sess_events:
                if cand.seq <= skill_ev.seq or cand.seq - skill_ev.seq > 3:
                    continue
                if not (is_precis(cand) and cand.is_error):
                    continue
                if (
                    cand.verb == "get"
                    and cand.kind == "skill"
                    and cand.err_type == "ResultBudgetExceeded"
                ):
                    continue
                if kinds and cand.kind and cand.kind not in kinds:
                    continue
                groups[f"skill:{skill_id}"].extend([skill_ev, cand])
    return [_candidate("skill_taught_wrong", sig, evs) for sig, evs in groups.items()]


def _nearest(value: str, pool: set[str]) -> str | None:
    best: str | None = None
    best_d = 3
    for cand in pool:
        if cand == value:
            continue
        d = _levenshtein(value, cand)
        if d <= 2 and d < best_d:
            best, best_d = cand, d
    return best


@detector("vocab_near_miss")
def detect_vocab_near_miss(events: Sequence[Event]) -> list[Candidate]:
    """A ``kind=`` value or ``arg_keys`` name on an errored call that is
    within edit distance 2 of a value/name seen on a SUCCESSFUL call
    (``search`` vs ``serach``, ``q`` vs ``qq``, ...). Naming that fights the
    caller's intuition. Fix class: doc fix — rename or alias, and document
    the accepted spelling; feeds ``docs/backlog/vocab-compaction.md``.
    """
    success_kinds: set[str] = set()
    success_argkeys: set[str] = set()
    for ev in events:
        if is_precis(ev) and not ev.is_error:
            if ev.kind:
                success_kinds.add(ev.kind)
            success_argkeys.update(ev.arg_keys)

    groups: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        if not (is_precis(ev) and ev.is_error):
            continue
        if ev.kind and ev.kind not in success_kinds:
            near = _nearest(ev.kind, success_kinds)
            if near is not None:
                groups[f"kind:{ev.kind}~{near}"].append(ev)
        for key in ev.arg_keys:
            if key not in success_argkeys:
                near = _nearest(key, success_argkeys)
                if near is not None:
                    groups[f"arg:{key}~{near}"].append(ev)
    return [_candidate("vocab_near_miss", sig, evs) for sig, evs in groups.items()]


#: Why n counts turns handed to the model read, never verified
#: corrections (gr570090(f), td580721).
_CORRECTION_CAVEAT = (
    "MODEL READ, NOT A COUNT: n is the number of human turns in "
    "metrics['human_turns'], material for the pass's model read — the "
    "step that decides which of them are real corrections. On the "
    "2026-11-05 pass a hand model-read found ~65 in ~1,030 turns where "
    "the retired keyword lexicon saw n=2, both false positives. The "
    "``__user__`` stream is human-turn-only (tool results, system "
    "reminders and peer/task notifications are dropped at extraction — "
    "gr570090(f)), so every line here is human-typed; evidence leads "
    "with the roundtrip turns, each rendered beside the tool calls it "
    "followed."
)


@detector("correction_roundtrip")
def detect_correction_roundtrip(events: Sequence[Event]) -> list[Candidate]:
    """One candidate per session containing a roundtrip — a ``__user__``
    turn within 3 seq of a preceding tool call — with the session's WHOLE
    human-turn stream in ``metrics['human_turns']`` (``seq: text``, one
    line per turn, full extraction head). Judging 'the human corrected
    the agent' is a language task, not a pattern match: the keyword
    lexicon this replaces matched n=2 on 2026-11-05, both false
    positives, where a hand model-read found ~65 real corrections in
    ~1,030 turns (gr570090(f)). So the detector no longer filters turns
    at all — it hands them to the pass's model read verbatim, and
    ``evidence`` leads with the first 12 roundtrip turns so the rendered
    card shows each suspect turn next to the calls it may correct. Fix
    class: skill edit or doc fix, whichever document the preceding calls
    point at.
    """
    cands = []
    for session, sess_events in by_session(events).items():
        user_turns = [e for e in sess_events if e.tool == "__user__"]
        if not user_turns:
            continue
        tool_seqs = [e.seq for e in sess_events if e.tool not in (None, "__user__")]
        roundtrips = [
            e for e in user_turns if any(0 < e.seq - s <= 3 for s in tool_seqs)
        ]
        if not roundtrips:
            continue
        tss = sorted(e.ts for e in sess_events if e.ts)
        cands.append(
            Candidate(
                detector="correction_roundtrip",
                signature=f"session:{session}",
                n=len(user_turns),
                n_sessions=1,
                first_ts=tss[0] if tss else None,
                last_ts=tss[-1] if tss else None,
                evidence=[EvidenceRef(e.session, e.seq) for e in roundtrips[:12]],
                metrics={
                    "human_turns": "\n".join(
                        f"{e.seq}: {' '.join((e.result_head or '').split())}"
                        for e in user_turns
                    ),
                    "roundtrip_turns": len(roundtrips),
                },
                caveat=_CORRECTION_CAVEAT,
            )
        )
    cands.sort(key=lambda c: (-c.n, c.signature))
    return cands


@detector("spin")
def detect_spin(events: Sequence[Event]) -> list[Candidate]:
    """The same ``(verb, kind, normalized err_head)`` recurring on 3+
    distinct days OR 3+ distinct sessions — a failure nobody has fixed
    across an extended window, as opposed to one bad afternoon. P0 class:
    whatever fix class the underlying ``hard_error`` signature implies,
    treat a ``spin`` hit as the scheduling override that moves it to the
    front of the queue.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        if not (is_precis(ev) and ev.is_error):
            continue
        sig = f"{ev.verb}|{ev.kind}|{normalize_err_head(ev.err_head)}"
        groups[sig].append(ev)
    cands = []
    for sig, evs in groups.items():
        days = {(e.ts or "")[:10] for e in evs if e.ts}
        sessions = {e.session for e in evs}
        if len(days) >= 3 or len(sessions) >= 3:
            cands.append(_candidate("spin", sig, evs))
    return cands


# ---------------------------------------------------------------------------
# D12 call_chain — result-fed sequences a composite call could collapse
# ---------------------------------------------------------------------------

#: Argument names whose VALUE is a handle an earlier result could have
#: produced — the same addressing fields ``_log_tool_call`` samples because
#: they are "what the agent is pointing at".
_LINK_KEYS = ("id", "name", "target", "link", "slug", "project")

#: Max seq gap between linked calls — an assistant turn and its tool_use
#: land 1–2 seq apart, so 8 covers a think-then-call without letting two
#: unrelated phases of a session merge into a fake chain.
_CHAIN_GAP = 8


def _link_values(ev: Event) -> list[str]:
    """Handle-looking argument values on *ev*, lifted from ``arg_digest``.
    Values under 4 chars are dropped: ``id=5`` substring-matching "5" in
    any render is the false-positive that gate exists for — the same
    lesson as narrow-after-fat's same-id requirement."""
    return [v for name in _LINK_KEYS if (v := _arg_value(ev, name)) and len(v) >= 4]


def _feeds_from(curr: Event, earlier: Sequence[Event]) -> str | None:
    """The first of *curr*'s handle values that literally appears in any
    of *earlier*'s result heads — evidence the call consumed a previous
    call's output. Returns the matched value, or ``None``."""
    for v in _link_values(curr):
        pat = re.compile(rf"(?<![\w.-]){re.escape(v)}(?![\w.-])")
        for prev in earlier:
            if pat.search(prev.result_head or ""):
                return v
    return None


@detector("call_chain")
def detect_call_chain(events: Sequence[Event]) -> list[Candidate]:
    """A run of 2+ precis calls in one session where each later call's
    ``id=``/``name=``/``target=``-style argument literally appears in an
    earlier call's ``result_head`` — the agent fetched a handle, then fed
    it to the next call. ``search|K -> get|K`` (search, then get the id
    it returned) and ``get|K -> get|K`` (look up id-for-A, then get A)
    are the canonical shapes: every link in the chain is a turn a
    composite view or verb would delete. A call may feed from ANY earlier
    member of the run, not only the previous one, so
    ``search -> get -> get`` where both gets take ids from the one search
    result counts as a single three-call chain. A non-precis tool call
    breaks the run; assistant text and user turns do not. The signature
    is the exact sequence shape ``verb|kind -> verb|kind``; candidates
    rank by ``metrics['turns_saved']`` (a run of length L costs L calls
    a composite would make 1). Payload corpora only — the check reads
    results, so the ledger cannot produce it at all; and ``result_head``
    is a 400-char slice, so a handle landing past the cut links nothing
    and ``n`` is a floor, not a count. Fix class: MCP capability — a
    composite verb/view (or a follow-affordance) that does
    look-up-then-fetch in one call.
    """
    runs_by_sig: dict[str, list[list[Event]]] = defaultdict(list)

    def flush(run: list[Event]) -> None:
        if len(run) < 2:
            return
        sig = " -> ".join(f"{e.verb}|{e.kind or '-'}" for e in run)
        runs_by_sig[sig].append(run)

    for sess_events in by_session(events).values():
        run: list[Event] = []
        for ev in sess_events:
            if is_precis(ev):
                if run and ev.seq - run[-1].seq <= _CHAIN_GAP and _feeds_from(ev, run):
                    run.append(ev)
                else:
                    flush(run)
                    run = [ev]
            elif ev.tool not in (None, "__user__"):
                # Another tool (Bash, Read, ...) breaks the chain the same
                # way it breaks a reformulation streak.
                flush(run)
                run = []
        flush(run)
    cands = []
    for sig, runs in runs_by_sig.items():
        evs = [e for r in runs for e in r]
        saved = sum(len(r) - 1 for r in runs)
        cands.append(
            _candidate(
                "call_chain",
                sig,
                evs,
                metrics={"runs": len(runs), "turns_saved": saved, "rank": saved},
                caveat=(
                    "Undercount: a link only registers when the handle lands "
                    "inside result_head's 400-char slice, and payload-free "
                    "corpora cannot chain at all. A hit is real (the id was "
                    "literally in the earlier result); a miss proves nothing."
                ),
            )
        )
    cands.sort(key=lambda c: c.metrics.get("rank", 0), reverse=True)
    return cands


# ---------------------------------------------------------------------------
# D13 session_digest — one bounded timeline card per thread
# ---------------------------------------------------------------------------

_DIGEST_MAX_EVENTS = 60
#: The timeline's byte bound — generous on purpose now that user turns
#: render at full extraction-head length (they are the correction read's
#: material); ``cards.py``'s own cap is the outer bound on anything a
#: reader actually sees.
_DIGEST_MAX_CHARS = 16000

_DIGEST_JSON_ARG_RE = re.compile(r'"(id|q)"\s*:\s*"?([^",}]{1,80})"?')


def _sample_args(ev: Event) -> str:
    """The one field that says *what was asked* — ``id=`` for lookups,
    ``q=`` for searches — lifted out of ``arg_digest``; falls back to bare
    arg names so a digest-free call still has a shape. The JSON regex goes
    first because it keeps a multi-word ``q`` whole where ``_arg_value``'s
    token stop would cut it at the first space."""
    m = _DIGEST_JSON_ARG_RE.search(ev.arg_digest or "")
    if m:
        return f"{m.group(1)}={m.group(2)[:60]}"
    for name in ("id", "q"):
        v = _arg_value(ev, name)
        if v:
            return f"{name}={v[:60]}"
    return " ".join(ev.arg_keys)


def _digest_line(ev: Event) -> str:
    if ev.tool == "__user__":
        # The FULL extraction head, whitespace-folded to one line: human
        # turns are the correction read's material, and an 80-char cut hid
        # the correction itself (td580721).
        return f"{ev.seq} user: {' '.join((ev.result_head or '').split())}"
    if ev.tool is None:
        return f"{ev.seq} assistant: {(ev.result_head or '')[:80]}"
    if is_precis(ev):
        args = _sample_args(ev)
        outcome = f"ERR:{ev.err_type or 'error'}" if ev.is_error else "ok"
        return f"{ev.seq} {ev.verb}|{ev.kind or '-'} {args} -> {outcome}"
    outcome = "ERR" if ev.is_error else "ok"
    return f"{ev.seq} {ev.tool} -> {outcome}"


@detector("session_digest")
def detect_session_digest(events: Sequence[Event]) -> list[Candidate]:
    """One candidate per session, signature ``session:<id>``, with the
    whole thread compressed to a bounded timeline in
    ``metrics['timeline']`` — one line per event
    (``seq verb|kind sampled-args -> outcome``; assistant turns get a head
    snippet, ``__user__`` turns the full extraction head, since they are
    the correction read's material) — so a pass can read *what was asked,
    in order, and what failed* without opening a transcript. Errored
    calls are marked ``ERR:<type>`` inline and their refs lead
    ``evidence``, so the rendered card jumps straight to the failures.
    Bounded: threads longer than ``_DIGEST_MAX_EVENTS`` elide the middle
    — but ``__user__`` turns are pinned, never elided, because the middle
    of a long thread is exactly where corrections hide (td580721); only
    non-user events are dropped, and ``caveat`` says so. The timeline
    caps at ``_DIGEST_MAX_CHARS``. ``n`` is the session's precis-call
    count, so ``--min-n`` drops threads that never touched the surface.
    Fix class: none itself — this is the per-thread evidence the
    aggregate detectors' signatures were abstracted out of.
    """
    cands = []
    for session, sess_events in by_session(events).items():
        precis_evs = [e for e in sess_events if is_precis(e)]
        if not precis_evs:
            continue
        lines = [_digest_line(e) for e in sess_events]
        caveat = ""
        if len(lines) > _DIGEST_MAX_EVENTS:
            pinned = {i for i, e in enumerate(sess_events) if e.tool == "__user__"}
            loose = [i for i in range(len(sess_events)) if i not in pinned]
            budget = max(_DIGEST_MAX_EVENTS - len(pinned), _DIGEST_MAX_EVENTS // 4)
            head_n = budget // 3
            tail_n = budget - head_n
            keep = pinned | set(loose[:head_n]) | set(loose[-tail_n:])
            kept_lines: list[str] = []
            elided_run = 0
            for i, line in enumerate(lines):
                if i in keep:
                    if elided_run:
                        kept_lines.append(f"... {elided_run} events elided ...")
                        elided_run = 0
                    kept_lines.append(line)
                else:
                    elided_run += 1
            if elided_run:
                kept_lines.append(f"... {elided_run} events elided ...")
            lines = kept_lines
            caveat = (
                f"timeline elided: {len(sess_events)} events, {len(keep)} shown "
                f"(all {len(pinned)} user turns pinned)"
            )
        timeline = "\n".join(lines)[:_DIGEST_MAX_CHARS]

        errs = [e for e in sess_events if e.is_error]
        seen: set[int] = set()
        evidence: list[Event] = []
        for e in (*errs, sess_events[0]):
            if e.seq not in seen:
                seen.add(e.seq)
                evidence.append(e)
        tss = sorted(e.ts for e in sess_events if e.ts)
        cands.append(
            Candidate(
                detector="session_digest",
                signature=f"session:{session}",
                n=len(precis_evs),
                n_sessions=1,
                first_ts=tss[0] if tss else None,
                last_ts=tss[-1] if tss else None,
                evidence=[EvidenceRef(e.session, e.seq) for e in evidence[:12]],
                metrics={"timeline": timeline, "errors": len(errs)},
                caveat=caveat,
            )
        )
    cands.sort(key=lambda c: c.n, reverse=True)
    return cands


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_events(path: Path) -> list[Event]:
    events: list[Event] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(from_json(line))
    return events


def run_detectors(events: Sequence[Event], only: str | None = None) -> list[Candidate]:
    ids = [only] if only else list(DETECTORS)
    out: list[Candidate] = []
    for id_ in ids:
        out.extend(DETECTORS[id_](events))
    return out


def _candidate_to_dict(c: Candidate) -> dict[str, Any]:
    return {
        "detector": c.detector,
        "signature": scrub(c.signature),
        "n": c.n,
        "n_sessions": c.n_sessions,
        "first_ts": c.first_ts,
        "last_ts": c.last_ts,
        "evidence": [asdict(e) for e in c.evidence],
        "metrics": {
            k: (scrub(v) if isinstance(v, str) else v) for k, v in c.metrics.items()
        },
        "caveat": scrub(c.caveat) if c.caveat else "",
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the friction-detector catalogue over a normalized event stream.",
    )
    parser.add_argument("--events", type=Path, default=outdir.out_path("events.jsonl"))
    parser.add_argument("--out", type=Path, default=outdir.out_path("candidates.json"))
    parser.add_argument("--only", choices=sorted(DETECTORS), default=None)
    parser.add_argument(
        "--min-n",
        type=int,
        default=2,
        help="suppress candidates below this occurrence count (default 2 — a one-off is not a finding)",
    )
    args = parser.parse_args(argv)

    events = load_events(args.events)
    events.sort(key=lambda e: (e.session, e.seq))

    cands = [c for c in run_detectors(events, args.only) if c.n >= args.min_n]
    cands.sort(key=lambda c: (c.detector, -c.n, c.signature))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            [_candidate_to_dict(c) for c in cands], indent=2, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    print(f"detect: wrote {len(cands)} candidates to {args.out}")


if __name__ == "__main__":
    main()
