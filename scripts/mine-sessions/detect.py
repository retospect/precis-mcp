#!/usr/bin/env python3
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

    Only payload-bearing corpora have a digest at all — the ledger records
    argument names and never values — so a ``None`` here means "not
    knowable", not "not passed". Callers comparing two events must treat
    ``None`` as a non-match rather than as equality, or every ledger pair
    looks identical.
    """
    if name not in ev.arg_keys or not ev.arg_digest:
        return None
    pat = _ARG_VALUE_RE_CACHE.get(name)
    if pat is None:
        pat = re.compile(rf"\b{re.escape(name)}\s*=\s*['\"]?([\w./:-]+)")
        _ARG_VALUE_RE_CACHE[name] = pat
    m = pat.search(ev.arg_digest)
    return m.group(1) if m else None


def by_session(events: Sequence[Event]) -> dict[str, list[Event]]:
    """Group events by session, each list sorted by ``seq`` — **only for
    corpora whose ``session`` is a real conversational thread.**

    The ledger and llmlog have no thread: ``extract._day_bucket`` assigns
    them a synthetic ``ledger:YYYY-MM-DD`` key, so one "session" is the
    entire fleet's calls for a calendar day, interleaved across dozens of
    unrelated concurrent jobs. Every adjacency rule ("within 3 turns", "3+
    consecutive calls") is meaningless there and fires constantly: on the
    first real pass this reported ``get|quest`` reformulating 26 times
    inside a **19-second** range — 26 different jobs each making one call,
    read as one agent hunting for an argument shape.

    The filter lives here rather than in each detector because every
    adjacency detector reaches for this function, and a future one would
    otherwise have to remember a rule it cannot see. Aggregate detectors
    (``hard_error``, ``detour_census``, ``vocab_near_miss``, ``spin``) walk
    ``events`` directly and so keep the full fleet-wide denominator.
    """
    out: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        if not has_payload(ev):
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
PAYLOAD_CORPORA = frozenset({"local", "codex", "jobs"})


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


_DETOUR_MARKER_RE = re.compile(r"psql|prod-psql", re.I)


@detector("abandon_detour")
def detect_abandon_detour(events: Sequence[Event]) -> list[Candidate]:
    """An error or ``zero`` on ``kind`` K, then within 5 turns a ``Bash``
    call mentioning ``psql``/``prod-psql`` or K's own name. The agent gave
    up on the MCP surface and went around it through raw SQL. Fix class:
    MCP capability — the surface is missing the verb/view the agent needed.
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
                    if _DETOUR_MARKER_RE.search(cmd) or k.lower() in cmd:
                        sig = f"{ev.verb}|{k}"
                        groups[sig].extend([ev, cand])
                        break
    return [_candidate("abandon_detour", sig, evs) for sig, evs in groups.items()]


DETOUR_CENSUS_RE = re.compile(r"psql|prod-psql|scripts/precis|curl .*api", re.I)
_TABLE_RE = re.compile(r"\bfrom\s+([a-z_][a-z0-9_.]*)", re.I)
_ENDPOINT_RE = re.compile(r"curl\s+\S*?(/[a-z0-9_./-]+)", re.I)


def _guess_intent(cmd: str) -> str:
    m = _TABLE_RE.search(cmd)
    if m:
        return f"table:{m.group(1).lower()}"
    m = _ENDPOINT_RE.search(cmd)
    if m:
        return f"endpoint:{m.group(1)}"
    if "scripts/precis" in cmd:
        return "cli:scripts/precis"
    return "other"


@detector("detour_census")
def detect_detour_census(events: Sequence[Event]) -> list[Candidate]:
    """Every ``Bash`` call matching ``psql|prod-psql|scripts/precis|curl
    .*api``, bucketed by a crude guess at the table/endpoint it touched
    (``metrics['result_bytes_total']`` alongside the count). A bucket with
    a lot of calls and bytes is a missing verb with usage data already
    attached. Fix class: MCP capability — this ranks *which* verb to build
    next by demand, not just that one is missing.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    bytes_by_sig: dict[str, int] = defaultdict(int)
    for ev in events:
        if ev.tool != "Bash":
            continue
        cmd = ev.arg_digest or ""
        if not DETOUR_CENSUS_RE.search(cmd):
            continue
        sig = _guess_intent(cmd)
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


_SKILL_ID_RE = re.compile(r"id=['\"]?([\w./-]+)")


@detector("skill_taught_wrong")
def detect_skill_taught_wrong(events: Sequence[Event]) -> list[Candidate]:
    """``get(kind='skill', id=...)`` followed within 3 turns by an errored
    precis call in the same session. The signature names the skill id
    (recovered from ``arg_digest``) — that is the file to fix. Fix class:
    skill edit.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    for sess_events in by_session(events).values():
        skill_calls = [
            e
            for e in sess_events
            if is_precis(e) and e.verb == "get" and e.kind == "skill"
        ]
        for skill_ev in skill_calls:
            m = _SKILL_ID_RE.search(skill_ev.arg_digest or "")
            skill_id = m.group(1) if m else "?"
            for cand in sess_events:
                if cand.seq <= skill_ev.seq or cand.seq - skill_ev.seq > 3:
                    continue
                if is_precis(cand) and cand.is_error:
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


_CORRECTION_RE = re.compile(
    r"^no[,. ]|that's wrong|i meant|actually,|not what i|"
    r"you (just )?(did|used|read) the wrong|^stop\b|^don't\b",
    re.I,
)


@detector("correction_roundtrip")
def detect_correction_roundtrip(events: Sequence[Event]) -> list[Candidate]:
    """A ``__user__`` turn matching a correction lexicon ("no,", "that's
    wrong", "i meant", "actually,", "not what i", "you used the wrong ...",
    "stop", "don't"), within 3 turns of a preceding tool sequence, local
    corpus only. Highest-value class: the preceding tool calls (kept in
    ``evidence`` alongside the correction) name the doc or skill that
    taught the agent wrong, in a human's own words. Fix class: skill edit
    or doc fix, whichever document the preceding calls point at.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    for sess_events in by_session(events).values():
        for ev in sess_events:
            if ev.tool != "__user__":
                continue
            text = (ev.result_head or "").strip()
            if not _CORRECTION_RE.search(text):
                continue
            preceding = [
                e
                for e in sess_events
                if e.seq < ev.seq
                and ev.seq - e.seq <= 3
                and e.tool not in (None, "__user__")
            ]
            if not preceding:
                continue
            anchor = preceding[-1]
            sig = f"{anchor.verb or anchor.tool}|{anchor.kind or '-'}"
            groups[sig].extend([*preceding, ev])
    return [_candidate("correction_roundtrip", sig, evs) for sig, evs in groups.items()]


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
