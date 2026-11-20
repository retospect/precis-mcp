"""Track bounded grounding follow-ups on ordinary todos.

A memory row lock serializes lookup, mint/link, and budget updates. Audit
review IDs make commissions replayable after a capped or failed tick.
Returns are ordinary todo comments scoped to the handoff's memory SHA.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from precis.graph.garden.apply import memory_human_hold
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.graph.garden.grounding import AuditRecord, ClaimRecord
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: OPEN tag every handoff todo carries — open or closed, it is the
#: dedup surface :func:`find_existing` scans by meta key.
HANDOFF_TAG = "grounding-handoff"

#: ``refs.meta`` key holding the handoff record (key, pins, budget,
#: stopping condition, return destination).
META_KEY = "grounding_handoff"

#: Cumulative audit-commission budget per claim key. Each audit that
#: finds the claim still unresolved spends one attempt; at the cap the
#: handoff reports ``exhausted`` instead of re-commissioning.
MAX_ATTEMPTS = 3

#: Handoffs one garden tick may commission, across every audited memory.
MAX_HANDOFFS_PER_TICK = 3

#: Missing checks no research job can settle — they route to an
#: ``ask-user`` todo on the Reto queue instead of a research handoff.
ASK_CHECKS: frozenset[str] = frozenset(
    {"ambiguous_term", "identity_or_authority", "substantive_decision"}
)

#: Ordinary todo comments starting with this marker enter the next audit.
RETURN_PREFIX = "candidate-evidence:"
MAX_RETURNS = 12

#: The todo → memory relation, same as the garden's memory-review asks.
_RETURN_REL = "raises-concern-about"

HandoffStatus = Literal[
    "skipped",
    "minted",
    "reused",
    "linked",
    "asked",
    "exhausted",
    "closed",
]

_TERMINAL = sorted(TERMINAL_TODO_STATUSES)

#: Every handoff todo is findable by its OPEN tag and its meta record.
_TAGGED = (
    "EXISTS (SELECT 1 FROM ref_tags rt JOIN tags g ON g.tag_id = rt.tag_id "
    "WHERE rt.ref_id = t.ref_id AND g.namespace = 'OPEN' "
    f"AND g.value = '{HANDOFF_TAG}')"
)


@dataclass(frozen=True, slots=True)
class HandoffResult:
    """What one claim's commission produced — inspectable, never applied."""

    status: HandoffStatus
    todo_id: int | None
    key: str
    attempts: int
    #: Classifier receipts and the checks reported by research.
    checks: tuple[str, ...]


def handoff_key(target_sha: str, span_text: str, missing_check: str) -> str:
    """Deterministic dedup key: same content + same claim + same gap."""
    blob = json.dumps(
        [target_sha, " ".join(span_text.split()), missing_check],
        ensure_ascii=False,
    )
    return "gh-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def find_existing(store: Store, key: str) -> int | None:
    """The handoff todo carrying ``key``, open or closed (a closed one is
    the answer — re-minting would restart a spent budget)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.ref_id FROM refs t "
            "WHERE t.kind = 'todo' AND t.retired_at IS NULL "
            f"AND {_TAGGED} "
            f"AND t.meta->'{META_KEY}'->>'key' = %s "
            "ORDER BY t.ref_id LIMIT 1",
            (key,),
        ).fetchone()
    return int(row[0]) if row is not None else None


@dataclass(frozen=True, slots=True)
class _Prior:
    todo_id: int
    open: bool
    meta: dict[str, Any]
    external: bool


def _priors(store: Store, ref_id: int, *, lock: bool = False) -> list[_Prior]:
    """Read handoffs and existing research questions scoped to this memory."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.ref_id, "
            "NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags g "
            "ON g.tag_id = rt.tag_id WHERE rt.ref_id = t.ref_id "
            "AND g.namespace = 'STATUS' AND g.value = ANY(%s)) AS open, "
            f"t.meta->'{META_KEY}', t.meta->'research_question' "
            "FROM refs t "
            "WHERE t.kind = 'todo' AND t.retired_at IS NULL AND ("
            f"({_TAGGED} AND t.meta->'{META_KEY}'->>'ref_id' = %s) "
            "OR (jsonb_typeof(t.meta->'research_question') = 'object' "
            "AND t.meta->'research_question'->'claim_pins'->>'ref' = %s)) "
            "ORDER BY t.ref_id" + (" FOR UPDATE OF t" if lock else ""),
            (_TERMINAL, str(ref_id), f"me{ref_id}"),
        ).fetchall()
    return [
        _Prior(int(tid), bool(is_open), h or question, not bool(h))
        for tid, is_open, h, question in rows
        if isinstance(h or question, dict)
    ]


def _matching(priors: list[_Prior], pin: dict[str, Any]) -> _Prior | None:
    for external in (False, True):
        if external and pin["route"] != "research":
            break
        for prior in priors:
            if prior.external != external:
                continue
            fields: tuple[str, ...] = ("claim_pins", "source_pins", "missing_check")
            if not external:
                fields += ("route", "claim")
            if all(prior.meta.get(field) == pin[field] for field in fields):
                return prior
    return None


def _checks(audit: AuditRecord, claim: ClaimRecord) -> list[str]:
    checks = [f"Grounding classifier inspected span {claim.span}: {claim.reason}"]
    for returned in audit.coverage.get("candidate_returns", []):
        if returned["span"] != claim.span or " ".join(
            returned["claim"].split()
        ) != " ".join(claim.text.split()):
            continue
        checks.extend(returned["checks_performed"])
    return checks


def _bump(
    store: Store,
    conn: Connection,
    prior: _Prior,
    audit: AuditRecord,
    claim: ClaimRecord,
    pin: dict[str, Any],
) -> HandoffResult:
    old = prior.meta
    attempts = int((old.get("budget") or {}).get("attempts") or 0)
    # Older handoffs stored the missing-check label as if it were a receipt.
    checks = tuple(
        dict.fromkeys(
            [c for c in old.get("checks", []) if c != pin["missing_check"]]
            + _checks(audit, claim)
        )
    )
    key = str(old.get("key") or pin["key"])
    if old.get("outcome") == "exhausted":
        return HandoffResult("exhausted", prior.todo_id, key, attempts, checks)
    if not prior.external and not prior.open and attempts < MAX_ATTEMPTS:
        return HandoffResult("closed", prior.todo_id, key, attempts, checks)
    if (
        not prior.external
        and audit.review_id
        and old.get("last_review_id", 0) >= audit.review_id
    ):
        return HandoffResult("reused", prior.todo_id, key, attempts, checks)

    if prior.external:
        attempts = 1
    elif pin["route"] == "research":
        attempts = min(attempts + 1, MAX_ATTEMPTS)
    exhausted = attempts >= MAX_ATTEMPTS
    if exhausted and prior.open and not old.get("reused_research"):
        store.add_tag(
            prior.todo_id,
            Tag.closed("STATUS", "won't-do"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
    meta = {
        **pin,
        "key": key,
        "last_review_id": audit.review_id,
        "budget": {"attempts": attempts, "max_attempts": MAX_ATTEMPTS},
        "checks": list(checks),
        "outcome": "exhausted" if exhausted else "unresolved",
        "reused_research": prior.external or bool(old.get("reused_research")),
    }
    store.stamp_ref_meta(prior.todo_id, {META_KEY: meta}, conn=conn)
    if prior.external:
        _link(store, conn, prior.todo_id, pin["ref_id"])
        store.add_tag(prior.todo_id, Tag.open(HANDOFF_TAG), set_by="system", conn=conn)
    status: HandoffStatus = (
        "exhausted" if exhausted else "linked" if prior.external else "reused"
    )
    return HandoffResult(status, prior.todo_id, key, attempts, checks)


def _mint_body(
    ref_id: int, audit: AuditRecord, claim: ClaimRecord, check: str, question: str
) -> str:
    pins = ", ".join(sorted(set(claim.premises + claim.evidence))) or "none named"
    return "\n".join(
        [
            f"Grounding handoff for memory me{ref_id} (audit sha {audit.target_sha}).",
            "",
            question,
            f"Claim (span {claim.span[0]}–{claim.span[1]}): {claim.text}",
            f"Subject: {claim.subject} · scope: {claim.scope} · kind: {claim.kind}",
            f"Missing check: {check} (audit reason: {claim.reason or 'none'})",
            f"Named claim/source pins: {pins}",
            "",
            "Success: return candidate evidence sufficient for the next "
            "grounding audit to re-check this claim at the memory's current sha.",
            f"Budget: {MAX_ATTEMPTS} cumulative audit attempts for this claim.",
            "Stop on a supported answer, budget exhaustion, unavailable evidence, "
            "a scientific dispute, or a question requiring a human ruling.",
            f"Return: comment on this todo starting with {RETURN_PREFIX} and "
            "include one 'Checks performed:' line per check. Close the todo "
            "when research ends, including when the claim stays unresolved.",
        ]
    )


def _handoff_meta(
    ref_id: int,
    audit: AuditRecord,
    claim: ClaimRecord,
    check: str,
    key: str,
    route: str,
    question: str,
) -> dict[str, Any]:
    return {
        META_KEY: {
            "key": key,
            "route": route,
            "ref_id": ref_id,
            "target_sha": audit.target_sha,
            "claim": " ".join(claim.text.split()),
            "span": list(claim.span),
            "subject": claim.subject,
            "question": question,
            "claim_pins": {
                "ref": f"me{ref_id}",
                "span": list(claim.span),
                "sha": audit.target_sha,
            },
            "source_pins": sorted(set(claim.premises + claim.evidence)),
            "missing_check": check,
            "success_condition": (
                "returned candidate evidence lets the next grounding audit "
                "re-check the claim at the memory's current sha"
            ),
            "budget": {"attempts": 1, "max_attempts": MAX_ATTEMPTS},
            "stopping_condition": (
                "answer found, attempts exhausted, evidence unavailable, scientific "
                "dispute, or a human ruling needed; report the checks performed"
            ),
            "return_to": (
                f"{RETURN_PREFIX} comment on this todo; the next audit "
                f"of me{ref_id} re-checks at its current sha"
            ),
            "checks": _checks(audit, claim),
            "last_review_id": audit.review_id,
            "outcome": "unresolved",
            "outward": "none",
        }
    }


def _link(store: Store, conn: Connection, todo_id: int, ref_id: int) -> None:
    store.add_link(
        src_ref_id=todo_id,
        dst_ref_id=ref_id,
        relation=_RETURN_REL,
        set_by="system",
        conn=conn,
    )


def _file_todo(
    store: Store,
    conn: Connection,
    ref_id: int,
    *,
    title: str,
    body: str,
    meta: dict[str, Any],
    ask: bool,
) -> int:
    from precis.todo.guards import check_ask_meta

    if ask:
        check_ask_meta(meta)
    ref = store.insert_ref(kind="todo", slug=None, title=title, meta=meta, conn=conn)
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", "open"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    store.add_tag(ref.id, Tag.open(HANDOFF_TAG), set_by="system", conn=conn)
    if ask:
        store.add_tag(ref.id, Tag.open("ask-user"), set_by="system", conn=conn)
    _link(store, conn, ref.id, ref_id)
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
        conn=conn,
    )
    return int(ref.id)


def open_handoff(
    store: Store,
    audit: AuditRecord,
    claim: ClaimRecord,
    *,
    ref_id: int,
) -> HandoffResult:
    """Commission the one bounded handoff for an unresolved claim.

    Reuses the open todo carrying the same key, links a covering research
    question instead of minting a duplicate, and stops at
    ``MAX_ATTEMPTS`` or on an :data:`ASK_CHECKS` reason — which files an
    ``ask-user`` todo and sends nothing outward. A closed match is the
    answer to the handoff: it is returned, never re-minted.
    """
    check = claim.missing_check
    skipped = HandoffResult("skipped", None, "", 0, ())
    if claim.status != "unresolved" or not check:
        return skipped
    with store.atomic() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE ref_id = %s AND kind = 'memory' "
            "AND retired_at IS NULL FOR UPDATE",
            (ref_id,),
        ).fetchone()
        if (
            row is None
            or store.target_sha("ref", ref_id, conn=conn) != audit.target_sha
            or memory_human_hold(store, ref_id)
        ):
            return skipped
        meta = _claim_meta(ref_id, audit, claim)
        prior = _matching(_priors(store, ref_id, lock=True), meta[META_KEY])
        if prior is not None:
            return _bump(store, conn, prior, audit, claim, meta[META_KEY])
        return _mint(store, conn, ref_id, audit, claim, meta)


def _claim_meta(ref_id: int, audit: AuditRecord, claim: ClaimRecord) -> dict[str, Any]:
    check = claim.missing_check
    ask = check in ASK_CHECKS
    question = (
        f"How should a human resolve {check} for “{claim.text}” in me{ref_id}?"
        if ask
        else f"What evidence establishes or refutes “{claim.text}” in me{ref_id}, "
        f"specifically for the {check} check?"
    )
    # Source pins and offsets distinguish separate occurrences and scopes
    # within the same memory revision.
    key = handoff_key(
        audit.target_sha,
        claim.text,
        json.dumps(
            [ref_id, check, claim.span, sorted(set(claim.premises + claim.evidence))]
        ),
    )
    return _handoff_meta(
        ref_id, audit, claim, check, key, "ask" if ask else "research", question
    )


def _mint(
    store: Store,
    conn: Connection,
    ref_id: int,
    audit: AuditRecord,
    claim: ClaimRecord,
    meta: dict[str, Any],
) -> HandoffResult:
    pin = meta[META_KEY]
    check = claim.missing_check
    key = pin["key"]
    checks = tuple(pin["checks"])
    body = _mint_body(ref_id, audit, claim, check, pin["question"])
    if check in ASK_CHECKS:
        meta["ask"] = {
            "kind": "ruling",
            "options": [
                "answer the question and the next audit re-checks the claim",
                "the claim is wrong or out of scope — edit or retire the memory",
            ],
            "blocks": "nothing",
            "context": (
                f"me{ref_id} grounding audit: unresolved claim needs "
                f"{check} — {claim.reason or 'no researchable check'}"
            )[:600],
        }
        todo_id = _file_todo(
            store,
            conn,
            ref_id,
            title=f"ask: me{ref_id} grounding — {check} ({claim.text[:80]})"[:200],
            body=body,
            meta=meta,
            ask=True,
        )
        return HandoffResult("asked", todo_id, key, 1, checks)

    todo_id = _file_todo(
        store,
        conn,
        ref_id,
        title=f"grounding handoff me{ref_id}: {check} — {claim.text[:80]}"[:200],
        body=body,
        meta=meta,
        ask=False,
    )
    return HandoffResult("minted", todo_id, key, 1, checks)


def _pending(store: Store, ref_id: int, audit: AuditRecord) -> list[ClaimRecord]:
    gaps = [
        claim
        for claim in audit.claims
        if claim.status == "unresolved" and claim.missing_check
    ]
    if not gaps:
        return []
    priors = _priors(store, ref_id)
    pending = []
    for claim in gaps:
        prior = _matching(priors, _claim_meta(ref_id, audit, claim)[META_KEY])
        if (
            prior is None
            or prior.external
            or (
                prior.open
                and prior.meta.get("outcome") != "exhausted"
                and prior.meta.get("last_review_id", 0) < audit.review_id
            )
        ):
            pending.append(claim)
    return pending


def has_pending_handoffs(store: Store, ref_id: int, audit: AuditRecord) -> bool:
    return bool(_pending(store, ref_id, audit))


def commission_handoffs(
    store: Store, ref_id: int, audit: AuditRecord, *, cap: int = MAX_HANDOFFS_PER_TICK
) -> int:
    """Resume the uncommissioned gaps of a saved audit, within the tick cap."""
    if cap <= 0:
        return 0
    pending = _pending(store, ref_id, audit)[:cap]
    for claim in pending:
        try:
            open_handoff(store, audit, claim, ref_id=ref_id)
        except Exception:
            log.warning(
                "grounding handoff failed on me%s claim %s",
                ref_id,
                claim.span,
                exc_info=True,
            )
    return len(pending)


class CandidateReturn(TypedDict):
    comment_id: int
    todo_id: int
    claim: str
    span: list[int]
    source_pins: list[str]
    text: str
    checks_performed: list[str]


def candidate_returns(store: Store, ref_id: int, sha: str) -> list[CandidateReturn]:
    """Read a bounded batch of ordinary todo comments pinned to this SHA."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.chunk_id, left(c.text, 2000), t.ref_id, "
            f"t.meta->'{META_KEY}' FROM links l "
            "JOIN refs t ON t.ref_id = l.src_ref_id "
            "JOIN chunks c ON c.ref_id = t.ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation = %s "
            "AND t.kind = 'todo' AND t.retired_at IS NULL "
            f"AND t.meta->'{META_KEY}'->>'ref_id' = %s "
            f"AND t.meta->'{META_KEY}'->>'target_sha' = %s "
            "AND c.chunk_kind = 'todo_comment' AND c.retired_at IS NULL "
            "AND (starts_with(lower(c.text), %s) "
            "OR (c.meta->>'candidate_evidence' = 'true' "
            f"AND c.meta->>'handoff_key' = t.meta->'{META_KEY}'->>'key')) "
            "ORDER BY c.chunk_id DESC LIMIT %s",
            (ref_id, _RETURN_REL, str(ref_id), sha, RETURN_PREFIX, MAX_RETURNS),
        ).fetchall()
    return [
        {
            "comment_id": int(chunk_id),
            "todo_id": int(todo_id),
            "claim": pin["claim"],
            "span": pin["span"],
            "source_pins": pin["source_pins"],
            "text": text,
            "checks_performed": [
                line.partition(":")[2].strip()
                for line in text.splitlines()
                if line.lower().startswith("checks performed:")
                and line.partition(":")[2].strip()
            ],
        }
        for chunk_id, text, todo_id, pin in rows
    ]
