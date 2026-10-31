"""gr484070 — file gripes from failed MCP calls recorded in the tool ledger.

Fired from the daily host-agnostic ``call_errors`` scheduler cadence.
Reads the last :data:`LOOKBACK_HOURS` of ``tool_calls`` rows that were
failed in a clusterable class (``error_type`` in :data:`_CLUSTER_CLASSES` —
``BadInput``/``Unsupported``/``ResultBudgetExceeded`` the
schema/param-validation class, plus ``NotFound``/``Gone`` lookup misses
and ``Internal`` faults),
clusters them by ``(verb, kind, normalised error head)``, and for each
cluster over threshold (:data:`MIN_CALLS` calls or :data:`MIN_SESSIONS`
distinct sessions) files ONE gripe — or bumps the live one it already
filed with a comment carrying the new counts.

Dedupe: a stable fingerprint (sha256 of the cluster signature) is stored
on the gripe as ``refs.meta['call_cluster']`` and echoed on the body's
first line. A *live* gripe with the fingerprint suppresses re-filing —
live means any non-terminal STATUS (``done``/``wontfix`` drop out, so a
closed cluster that recurs re-files fresh). Same live-status convention
as ``health_digest``'s marker-gripe scan.

Classification is deterministic, ordered, and never guesses:

1. ``skill`` — a skill file documents the rejected call shape (the doc
   shows a form the server rejects — fix the doc).
2. ``hint`` — the rendered error body was a single line (no ``next:``
   was rendered — ``result_bytes`` ≈ ``error_head`` length), or one
   session repeated the identical failing call (whatever the error said
   didn't land).
3. ``mcp`` — the error head matches a schema/param-gap pattern (unknown
   key/kind, missing required arg, bad mode/view) with no doc or hint
   signal — the surface itself is the gap.
4. ``needs-triage`` — nothing fired; a human reads the cluster.

``Internal`` clusters short-circuit to ``bug`` (a server fault is never a
doc or hint problem).

``skill`` wins over ``hint`` when both fire: a wrong doc is the root
cause and the repeat failure follows from it.

Lifecycle (precis.workers.auto_gripe, ruling 2026-10-18): a cluster
gripe files at prio 7, so human-filed gripes sort above it, and closes
itself once the cluster has been absent for :data:`CLEAR_AFTER_HOURS`
(three daily passes): no matching error in the ledger in that window. The
close is a ``resolved: auto-closed: …`` comment + ``STATUS:done``, naming
the last matching error found in the :data:`CLEAR_SCAN_HOURS` scan (else
the gripe's last filing/bump).

Flood control: at most :data:`MAX_NEW_GRIPES` new gripes per run — a bad
deploy that turns every call into the same rejection cannot fill the
queue; the overflow count goes to the run log. Bumps are uncapped (a
comment is cheap and capped by cluster count anyway).

Everything in the gripe body comes from the ledger's already-masked
bounded columns (``error_head``/``arg_sample``/``client``); samples are
``mask_secrets``'d again on render, the same belt-and-suspenders the
session miner applies on read.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Window scanned per fire — the cadence fires daily, so this is "since
#: the last run" with a small overlap.
LOOKBACK_HOURS = 24
#: A cluster files when it has >= this many calls OR >= MIN_SESSIONS
#: distinct sessions in the window.
MIN_CALLS = 3
MIN_SESSIONS = 2
#: New gripes filed per run, max — the bad-deploy flood cap.
MAX_NEW_GRIPES = 5
#: A live cluster gripe with no matching error in this window (three daily
#: passes) closes itself.
CLEAR_AFTER_HOURS = 72
#: Ledger window the close sweep reads to name a cleared cluster's last
#: matching error.
CLEAR_SCAN_HOURS = 7 * 24
#: Rejected-before-real-work error classes (typed envelope, before the
#: verb's real work ran). NotFound/Gone did real lookups; Upstream/
#: Internal/RateLimited are runtime faults, not call-shape errors.
#: ``ResultBudgetExceeded`` is a ``BadInput`` subclass the ledger records
#: under its own ``error_type`` — the ``ResultBudgetFitted`` marker stays
#: out because those are ``ok`` rows, not refusals.
_REJECT_CLASSES = ("BadInput", "Unsupported", "ResultBudgetExceeded")
#: Lookup-miss and fault classes clustered alongside the rejects (M3,
#: gr494831): NotFound/Gone are the agent guessing ids (the error should
#: carry options), Internal is a server fault every occurrence of which is
#: a bug or a missing boundary mapping. Upstream/RateLimited stay out —
#: they are other systems' weather, not a precis defect.
_LOOKUP_CLASSES = ("NotFound", "Gone")
_FAULT_CLASSES = ("Internal",)
_CLUSTER_CLASSES = _REJECT_CLASSES + _LOOKUP_CLASSES + _FAULT_CLASSES
#: ``refs.meta`` key carrying the cluster fingerprint on a filed gripe.
META_CLUSTER = "call_cluster"
META_CLUSTER_CLASS = "call_cluster_class"
#: Fingerprint prefix — also the classification tag prefix
#: (``callerr:mcp`` / ``callerr:skill`` / ``callerr:hint`` /
#: ``callerr:needs-triage``).
FP_PREFIX = "callerr:"
ORIGIN_TAG = "origin:call-errors"

#: STATUS values that keep a cluster gripe "live" — duplicates the
#: health_digest marker-scan convention (non-terminal lifecycle stages);
#: local copy because workers -> precis_web is a forbidden edge.
_LIVE_STATUSES = (
    "open",
    "triaged",
    "fixing",
    "ready_for_fix",
    "in_review",
    "verify",
    "auto_verify",
    "user_verify",
    "needs_spec",
    "parked",
)

# ── error-head normalisation ─────────────────────────────────────────────
# Same collapse the session miner applies (scripts/mine-sessions/
# detect.py::normalize_err_head — kept twin because that script is not a
# package): paths -> <path>, quoted values -> <id>, digit runs -> #.

_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_DIGITS_RE = re.compile(r"\d+")
_PATHISH_RE = re.compile(r"(?:/[\w.\-]+){2,}")


def normalize_err_head(text: str) -> str:
    """Collapse an error head to a family signature (mining twin)."""
    t = (text or "").strip()
    t = _PATHISH_RE.sub("<path>", t)
    t = _QUOTED_RE.sub("<id>", t)
    t = _DIGITS_RE.sub("#", t)
    return t[:160]


# ── row + cluster shapes ──────────────────────────────────────────────────


@dataclass(slots=True)
class _Row:
    verb: str
    kind: str | None
    error_head: str
    arg_sample: Any
    result_bytes: int | None
    client: str | None
    session: str
    ts: datetime | None
    error_type: str = "BadInput"


@dataclass(slots=True)
class Cluster:
    """One failure family — all rows sharing (verb, kind, norm head)."""

    verb: str
    kind: str | None
    head: str  # normalised signature head
    fingerprint: str
    n: int = 0
    sessions: set[str] = field(default_factory=set)
    session_counts: Counter[str] = field(default_factory=Counter)
    clients: Counter[str] = field(default_factory=Counter)
    samples: list[str] = field(default_factory=list)  # distinct masked arg_sample
    raw_head: str = ""
    error_type: str = "BadInput"
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    single_line: bool = False
    repeated_in_session: bool = False
    # single-line votes: rows carrying result_bytes, and how many of them
    # rendered a one-line body (no next: line — see classify).
    _bytes_rows: int = 0
    _bytes_single: int = 0


def _fingerprint(
    verb: str, kind: str | None, head: str, error_type: str = "BadInput"
) -> str:
    """Stable cluster id — survives ts/ordering drift between runs.

    The reject classes hash exactly as before (live gripes keep their
    fingerprint); the lookup/fault classes add the class so a NotFound and
    an Internal sharing a head never merge."""
    extra = "" if error_type in _REJECT_CLASSES else f"|{error_type}"
    digest = hashlib.sha256(f"{verb}|{kind or ''}|{head}{extra}".encode()).hexdigest()
    return f"{FP_PREFIX}{digest[:16]}"


def _cluster(rows: list[_Row]) -> list[Cluster]:
    out: dict[tuple[str, str | None, str, str], Cluster] = {}
    for r in rows:
        head = normalize_err_head(r.error_head)
        key = (r.verb, r.kind, head, r.error_type)
        cl = out.get(key)
        if cl is None:
            cl = out[key] = Cluster(
                verb=r.verb,
                kind=r.kind,
                head=head,
                fingerprint=_fingerprint(r.verb, r.kind, head, r.error_type),
                error_type=r.error_type,
            )
        cl.n += 1
        cl.sessions.add(r.session)
        cl.session_counts[r.session] += 1
        cl.clients[r.client or "unattributed"] += 1
        if r.error_head and not cl.raw_head:
            cl.raw_head = r.error_head
        if r.arg_sample is not None:
            sample = _render_sample(r.arg_sample)
            if sample and sample not in cl.samples and len(cl.samples) < 3:
                cl.samples.append(sample)
        if r.result_bytes is not None:
            cl._bytes_rows += 1
            if r.result_bytes <= len(r.error_head.encode("utf-8")) + 8:
                cl._bytes_single += 1
        if r.ts is not None:
            if cl.first_ts is None or r.ts < cl.first_ts:
                cl.first_ts = r.ts
            if cl.last_ts is None or r.ts > cl.last_ts:
                cl.last_ts = r.ts
    for cl in out.values():
        cl.repeated_in_session = bool(
            cl.session_counts and max(cl.session_counts.values()) >= 2
        )
        # Majority rule: >50% of rows with a byte count rendered a
        # one-line body ⇒ the error carried no options:/next: lines.
        cl.single_line = cl._bytes_rows > 0 and cl._bytes_single * 2 > cl._bytes_rows
    return sorted(out.values(), key=lambda c: (-c.n, c.fingerprint))


# ── scan ──────────────────────────────────────────────────────────────────

_SCAN_SQL = """
SELECT verb, kind, error_head, arg_sample, result_bytes, client, ts, error_type,
       COALESCE(session_id,
                'agentlog:' || agentlog_id::text,
                'day:' || ts::date::text) AS sess
  FROM tool_calls
 WHERE ts >= now() - (%s || ' hours')::interval
   AND outcome = 'error'
   AND error_type = ANY(%s)
   AND error_head IS NOT NULL
 ORDER BY ts
"""


def _scan(store: Store, lookback_hours: int) -> list[_Row]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            _SCAN_SQL, (str(lookback_hours), list(_CLUSTER_CLASSES))
        ).fetchall()
    return [
        _Row(
            verb=str(verb),
            kind=str(kind) if kind is not None else None,
            error_head=str(error_head),
            arg_sample=arg_sample,
            result_bytes=int(result_bytes) if result_bytes is not None else None,
            client=str(client) if client else None,
            session=str(sess),
            ts=ts,
            error_type=str(error_type),
        )
        for (
            verb,
            kind,
            error_head,
            arg_sample,
            result_bytes,
            client,
            ts,
            error_type,
            sess,
        ) in rows
    ]


def _render_sample(arg_sample: Any) -> str | None:
    """One masked one-line rendering of a stored ``arg_sample`` — the
    ledger column is JSONB (already masked at write); this re-masks on
    read, same posture as the session miner's scrub-on-read."""
    import json

    from precis.utils.secret_scan import mask_secrets

    if isinstance(arg_sample, str):
        text = arg_sample
    else:
        try:
            text = json.dumps(arg_sample, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return None
    text = mask_secrets(" ".join(text.split()), warn=False)
    return text[:300]


# ── classification ───────────────────────────────────────────────────────

#: Param-gap signatures in the normalised head — "the server rejected a
#: shape" tells, after masking (quoted values are already ``<id>``).
_PARAM_GAP_RE = re.compile(
    r"unknown (kind|key|arg|param|verb|mode|view|field|identifier)"
    r"|unexpected (key|arg|param|keyword|argument)"
    r"|missing|required|unrecognized|unsupported|invalid"
    r"|must be one of|not allowed|no such|extra (key|field|arg)"
    r"|rejects?|refus",
    re.IGNORECASE,
)


#: ``verb( ... )`` call snippets inside a skill doc — the "a skill shows
#: this form" check scans these, bounded by the closing paren.
def _call_snippets(text: str, verb: str) -> list[str]:
    return [
        m.group(0) for m in re.finditer(rf"{re.escape(verb)}\([^)]*\)", text, re.DOTALL)
    ]


def _rejected_tokens(raw_head: str, *, verb: str, kind: str | None) -> list[str]:
    """Quoted values in the raw error head minus the verb/kind echoes —
    the *rejected* things, e.g. ``'body'`` out of
    ``unknown key 'body' for put(kind='todo')``."""
    out: list[str] = []
    for single, double in re.findall(r"'([^']+)'|\"([^\"]+)\"", raw_head):
        tok = single or double
        if len(tok) < 2 or tok in (verb, kind):
            continue
        out.append(tok)
    return out


def _documenting_skill(cl: Cluster, skill_texts: dict[str, str]) -> str | None:
    """First skill id whose doc shows the rejected call shape, else None.

    A skill "documents the call" when a ``verb(...)`` call snippet in its
    text carries one of the rejected tokens — and, when the cluster has a
    kind, that skill also names ``kind='<kind>'``. Reading the real
    packaged skills keeps this honest: a fixed allowlist would drift.
    """
    tokens = _rejected_tokens(cl.raw_head, verb=cl.verb, kind=cl.kind)
    if not tokens:
        return None
    for sid in sorted(skill_texts):
        text = skill_texts[sid]
        if cl.kind is not None and (
            f"kind='{cl.kind}'" not in text and f'kind="{cl.kind}"' not in text
        ):
            continue
        for snippet in _call_snippets(text, cl.verb):
            if any(tok in snippet for tok in tokens):
                return sid
    return None


def classify(cl: Cluster, skill_texts: dict[str, str]) -> tuple[str, str | None, str]:
    """``(tag, documenting_skill_id, reason)`` — deterministic, ordered.

    ``skill`` evidence beats ``hint``: a doc that teaches a rejected form
    is the root cause; the caller repeating it follows from that. Anything
    with no signal is ``needs-triage`` — never a guess.
    """
    if cl.error_type in _FAULT_CLASSES:
        return "bug", None, "server fault (Internal) — a bug or an unmapped boundary"
    sid = _documenting_skill(cl, skill_texts)
    if sid is not None:
        return "skill", sid, f"{sid} shows the rejected call shape"
    if cl.single_line:
        return "hint", None, "error body was a single line — no next: rendered"
    if cl.repeated_in_session:
        return (
            "hint",
            None,
            "one session repeated the identical failing call after seeing the error",
        )
    if _PARAM_GAP_RE.search(cl.head):
        return "mcp", None, "param-gap signature in the error head"
    return "needs-triage", None, "no deterministic signal fired"


def _skill_texts() -> dict[str, str]:
    """``{skill_id: markdown}`` for the packaged skill corpus."""
    import importlib.resources as ir

    texts: dict[str, str] = {}
    base = ir.files("precis.data.skills")
    for res in base.iterdir():
        if res.name.endswith(".md"):
            texts[res.name.removesuffix(".md")] = res.read_text(encoding="utf-8")
    return texts


# ── gripe filing ──────────────────────────────────────────────────────────


def _clients_line(cl: Cluster) -> str:
    return ", ".join(f"{c} (x{n})" for c, n in cl.clients.most_common())


def _gripe_body(
    cl: Cluster, cls: str, skill_id: str | None, reason: str, now: datetime
) -> str:
    first = cl.first_ts.isoformat() if cl.first_ts else "?"
    last = cl.last_ts.isoformat() if cl.last_ts else "?"
    lines = [
        f"call-errors fingerprint: {cl.fingerprint}",
        f"{cl.verb}(kind={cl.kind!r}) failed with {cl.error_type}, repeatedly"
        + (
            " (rejected before real work)." if cl.error_type in _REJECT_CLASSES else "."
        ),
        "",
        f"Calls (24h): {cl.n} · distinct sessions: {len(cl.sessions)} "
        f"· first {first} · last {last}",
        f"Clients: {_clients_line(cl)}",
        f"Error head: {cl.raw_head}",
    ]
    if cl.samples:
        lines.append("Arg samples (masked):")
        lines.extend(f"  {s}" for s in cl.samples)
    lines.append(f"Class: {cls} — {reason}")
    if skill_id is not None:
        lines.append(f"Skill: {skill_id} documents this call shape.")
    lines.append(
        "Auto-filed by the call_errors cadence (gr484070). New occurrences "
        "append a count comment; if the cluster recurs after this gripe "
        "closes, a fresh gripe is filed."
    )
    return "\n".join(lines)


def _gripe_title(cl: Cluster, cls: str) -> str:
    title = f"[callerr:{cls}] {cl.verb}(kind={cl.kind!r}): {cl.raw_head}"
    return title[:197].rstrip() + "…" if len(title) > 200 else title


def _live_cluster_gripes(store: Store) -> dict[str, int]:
    """``{fingerprint: gripe_ref_id}`` for live (non-retired, non-terminal
    STATUS) gripes carrying a ``meta.call_cluster`` fingerprint."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta->>%s
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND r.meta ? %s
               AND t.namespace = 'STATUS' AND t.value = ANY(%s)
            """,
            (META_CLUSTER, META_CLUSTER, list(_LIVE_STATUSES)),
        ).fetchall()
    return {str(fp): int(ref_id) for ref_id, fp in rows if fp}


def _file_gripe(
    store: Store,
    cl: Cluster,
    cls: str,
    skill_id: str | None,
    reason: str,
    now: datetime,
) -> int:
    """Insert one cluster gripe — the worker-side ``insert_ref`` +
    body-chunk + ``STATUS:open`` pattern (``health_digest``'s
    ``_file_router_gripe`` precedent; this pass is write-capable, so the
    ``file_gripe_readonly`` carve-out does not apply)."""
    from precis.store.types import ChunkInsert, Tag
    from precis.workers.auto_gripe import AUTO_PRIO

    with store.tx() as conn:
        ref = store.insert_ref(
            kind="gripe",
            slug=None,
            title=_gripe_title(cl, cls),
            meta={META_CLUSTER: cl.fingerprint, META_CLUSTER_CLASS: cls},
            conn=conn,
        )
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text=_gripe_body(cl, cls, skill_id, reason, now),
                    meta={"chunk_kind": "gripe_body"},
                )
            ],
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.add_tag(ref.id, Tag.open(f"{FP_PREFIX}{cls}"), set_by="system", conn=conn)
        store.add_tag(ref.id, Tag.open(ORIGIN_TAG), set_by="system", conn=conn)
        store.set_prio(ref.id, AUTO_PRIO, conn=conn)
    return int(ref.id)


def _bump_gripe(store: Store, gripe_id: int, cl: Cluster, now: datetime) -> None:
    """Append the new-window counts as a gripe comment — the ledger grows,
    the gripe's evidence grows with it (no duplicate filing)."""
    from precis.store.types import ChunkInsert

    next_pos = len(store.chunks.list_chunks_for_ref(gripe_id))
    comment = (
        f"call_errors {now:%Y-%m-%d}: +{cl.n} calls across "
        f"{len(cl.sessions)} session(s) in the last {LOOKBACK_HOURS}h "
        f"(clients: {_clients_line(cl)}) — still recurring."
    )
    store.chunks.insert_chunks(
        gripe_id,
        [ChunkInsert(ord=next_pos, text=comment, meta={"chunk_kind": "gripe_comment"})],
    )


def _last_written(store: Store, gripe_id: int) -> datetime:
    """When the gripe was last filed or bumped (newest chunk, else the ref)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT coalesce(max(c.created_at), r.created_at) FROM refs r "
            "LEFT JOIN chunks c ON c.ref_id = r.ref_id WHERE r.ref_id = %s "
            "GROUP BY r.created_at",
            (gripe_id,),
        ).fetchone()
    assert row is not None
    return row[0]


def _close_cleared(store: Store, live: dict[str, int], now: datetime) -> int:
    """Close each live cluster gripe whose cluster has no matching error in
    the last :data:`CLEAR_AFTER_HOURS`. Returns the number closed."""
    from precis.workers.auto_gripe import close_auto_gripe

    if not live:
        return 0
    cutoff = now - timedelta(hours=CLEAR_AFTER_HOURS)
    last = {
        cl.fingerprint: cl.last_ts
        for cl in _cluster(_scan(store, CLEAR_SCAN_HOURS))
        if cl.last_ts is not None
    }
    closed = 0
    for fp, gripe_id in live.items():
        seen = last.get(fp) or _last_written(store, gripe_id)
        if seen >= cutoff:
            continue
        try:
            close_auto_gripe(
                store,
                gripe_id,
                reason=(
                    f"call_errors cluster {fp} absent from the last "
                    f"{CLEAR_AFTER_HOURS // 24} daily passes"
                ),
                last_seen=seen,
            )
        except Exception:
            log.exception("call_errors: auto-close failed for gripe id=%d", gripe_id)
            continue
        closed += 1
    return closed


# ── the pass ──────────────────────────────────────────────────────────────


def run_call_errors_pass(
    store: Store,
    *,
    lookback_hours: int = LOOKBACK_HOURS,
    max_new: int = MAX_NEW_GRIPES,
    skill_texts: dict[str, str] | None = None,
) -> BatchResult:
    """One daily mining fire. ``claimed`` = over-threshold clusters seen;
    ``ok`` = gripes filed + bumped; ``failed`` = clusters whose file/bump
    raised (logged, never allowed to strand the rest). Cleared clusters'
    gripes close first (:func:`_close_cleared`; logged, not counted)."""
    now = datetime.now(UTC)
    live = _live_cluster_gripes(store)
    closed = _close_cleared(store, live, now)
    if closed:
        log.info("call_errors: auto-closed %d cleared cluster gripe(s)", closed)
        live = _live_cluster_gripes(store)
    rows = _scan(store, lookback_hours)
    clusters = [
        cl
        for cl in _cluster(rows)
        if cl.n >= MIN_CALLS or len(cl.sessions) >= MIN_SESSIONS
    ]
    if not clusters:
        return BatchResult(handler="call_errors", claimed=0, ok=0, failed=0)

    texts = skill_texts if skill_texts is not None else _skill_texts()
    filed = bumped = failed = overflow = 0
    for cl in clusters:
        gripe_id = live.get(cl.fingerprint)
        try:
            if gripe_id is not None:
                _bump_gripe(store, gripe_id, cl, now)
                bumped += 1
                continue
            if filed >= max_new:
                overflow += 1
                continue
            cls, skill_id, reason = classify(cl, texts)
            gid = _file_gripe(store, cl, cls, skill_id, reason, now)
            live[cl.fingerprint] = gid
            filed += 1
            log.info(
                "call_errors: filed gripe id=%d for %s (%s; %d calls / %d sessions)",
                gid,
                cl.fingerprint,
                cls,
                cl.n,
                len(cl.sessions),
            )
        except Exception:
            log.exception(
                "call_errors: file/bump failed for cluster %s", cl.fingerprint
            )
            failed += 1
    if overflow:
        log.warning(
            "call_errors: %d over-threshold cluster(s) not filed — "
            "per-run cap %d reached",
            overflow,
            max_new,
        )
    return BatchResult(
        handler="call_errors",
        claimed=len(clusters),
        ok=filed + bumped,
        failed=failed,
    )


# ── hint follow-through (gr494831) ────────────────────────────────────────
#
# Did the error's hint land? For each failed call in an MCP session, look at
# the same session's next call to the SAME kind within ``FOLLOW_WINDOW``
# ledger calls: ``ok`` = it succeeded, ``error`` = it failed again,
# ``none`` = the session did not retry that kind (gave up or moved on).
# Read-only; the report is ``scripts/hint-followthrough``.

#: Calls after the failing one that may carry the follow-up.
FOLLOW_WINDOW = 2

HINT_FOLLOWTHROUGH_SQL = """
SELECT e.verb, e.kind, e.error_type, e.error_head, f.outcome
  FROM tool_calls e
  LEFT JOIN LATERAL (
        SELECT n.outcome
          FROM tool_calls n
         WHERE n.session_id = e.session_id
           AND n.seq > e.seq AND n.seq <= e.seq + %(win)s
           AND n.kind IS NOT DISTINCT FROM e.kind
         ORDER BY n.seq
         LIMIT 1) f ON true
 WHERE e.outcome = 'error'
   AND e.session_id IS NOT NULL
   AND e.ts >= now() - make_interval(days => %(days)s)
"""


@dataclass(frozen=True, slots=True)
class FollowThrough:
    """One failure family's hint follow-through."""

    verb: str
    kind: str
    error_type: str
    head: str  # normalised error head ('' when the row has none)
    errors: int
    followed_ok: int
    followed_error: int
    no_followup: int

    @property
    def ok_share(self) -> float:
        return self.followed_ok / self.errors if self.errors else 0.0


def hint_followthrough(
    conn: Any, *, days: int = 7, min_errors: int = 1
) -> list[FollowThrough]:
    """Per ``(verb, kind, error_type, normalised head)``: how often the same
    session's next call to that kind (within :data:`FOLLOW_WINDOW` calls)
    succeeded. Families with the most errors first; ``min_errors`` drops
    the long tail."""
    rows = conn.execute(
        HINT_FOLLOWTHROUGH_SQL, {"win": FOLLOW_WINDOW, "days": days}
    ).fetchall()
    return aggregate_followthrough(rows, min_errors=min_errors)


def aggregate_followthrough(
    rows: Iterable[tuple[Any, ...]], *, min_errors: int = 1
) -> list[FollowThrough]:
    """Group :data:`HINT_FOLLOWTHROUGH_SQL` rows (``verb, kind, error_type,
    error_head, next_outcome``) into families. The report script feeds it
    rows parsed from ``scripts/prod-psql --ro`` output."""
    acc: dict[tuple[str, str, str, str], list[int]] = {}
    for verb, kind, etype, head, outcome in rows:
        key = (
            str(verb),
            str(kind) if kind is not None else "-",
            str(etype or "Unknown"),
            normalize_err_head(head or ""),
        )
        slot = acc.setdefault(key, [0, 0, 0, 0])
        slot[0] += 1
        slot[1 if outcome == "ok" else 2 if outcome == "error" else 3] += 1
    out = [
        FollowThrough(v, k, t, h, n, ok, er, no)
        for (v, k, t, h), (n, ok, er, no) in acc.items()
        if n >= min_errors
    ]
    out.sort(key=lambda r: (-r.errors, r.verb, r.kind, r.head))
    return out


__all__ = [
    "FOLLOW_WINDOW",
    "HINT_FOLLOWTHROUGH_SQL",
    "LOOKBACK_HOURS",
    "MAX_NEW_GRIPES",
    "META_CLUSTER",
    "MIN_CALLS",
    "MIN_SESSIONS",
    "FollowThrough",
    "aggregate_followthrough",
    "classify",
    "hint_followthrough",
    "normalize_err_head",
    "run_call_errors_pass",
]
