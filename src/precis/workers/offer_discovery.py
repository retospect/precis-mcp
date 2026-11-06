"""offer_discovery — three-day lead discovery + the qualification path.

S9 (td568212, spec root ``me568172``). Two things, both dark by default
(``PRECIS_OFFER_DISCOVERY_ENABLED`` / ``--only offer_discovery``):

**Discovery** (every three days, like :mod:`precis.workers.paper_reconcile`'s
throttle + single-runner advisory-lock shape): ONE Perplexity call
(``perplexity-reasoning`` / sonar-reasoning-pro, ~$0.005 — see the note
below on why not the ``perplexity-research`` tier) asks Groq, Cerebras and
OpenRouter's current best free-tier model. A model not already on some
``llm_account`` or ``llm`` card is a **lead**; each lead gets AT MOST ONE
decision row, ever — an ``ask-user`` todo (``meta.ask``, per
``precis-work-help``) asking Reto whether to pursue it. Nothing is written
to ``llm_account`` at this stage.

**The qualification path** (advanced on every pass, not just the
throttled discovery leg — so an approved lead keeps moving without
waiting three more days): once a lead's decision todo closes with an
"answer: pursue" comment —

1. a **dark** ``llm_account`` row (``enabled = false``) + a dark ``llm``
   card are seeded for the candidate;
2. :func:`precis.llm_eval.harness.run_eval` runs precis's own gold set
   against the candidate (recorded onto its card — the ``measured-eval``
   provenance, same path ``precis llm eval`` uses) and, for comparison,
   against the tier's current incumbent (:func:`resolve_model`, not
   recorded — the incumbent's own card is untouched);
3. once the candidate actually produced a recorded ordinal (void axes —
   no profile/key wired yet for this vendor — leave the lead in place for
   the next pass to retry; nothing proceeds on a void eval), a SECOND
   ask-user todo asks Reto to enable it, quoting the candidate-vs-incumbent
   ordinals;
4. that answer closing "enable" flips the account's ``enabled`` column;
   "reject" stamps ``entitlement.qualification_status = 'rejected'`` and
   leaves it dark.

Groq, Cerebras and OpenRouter free models enter the catalog ONLY through
this path — no other pass seeds an account or card for them.

**Why ``perplexity-reasoning``, not ``perplexity-research``, for the
discovery call:** sonar-deep-research is ~$0.50/call; at a 3-day cadence
that is 7/3 × $0.50 ≈ $1.17/week — over the $1/week accept budget before
counting anything else. sonar-reasoning-pro is ~$0.005/call (≈
$0.012/week) and still returns a cited, reasoned answer; it is called
with ``refresh=True`` (the kind's default TTL is "forever" — a stale
answer would defeat the point of a recurring discovery pass). The
qualification eval's own cost is a separate, occasional, human-gated
spend (only after an explicit "pursue" answer), not part of the
steady-state three-day cost this budget is about.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.store import Store
from precis.store.advisory import try_xact_advisory_lock
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES
from precis.todo.guards import check_ask_meta
from precis.workers import _throttle
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.llm_eval.harness import EvalReport

log = logging.getLogger(__name__)

#: Fixed signed-bigint key for the single-runner advisory lock — "offrdisc",
#: namespaced away from the llm_reconcile / paper_reconcile keys.
_LOCK_KEY = 0x6F_66_66_72_64_69_73_63
#: app_state key holding the ISO-8601 timestamp of the last completed pass.
_STATE_KEY = "offer_discovery:last_run"
#: Env var + default for the cadence throttle — three days.
_REFRESH_ENV_VAR = "PRECIS_OFFER_DISCOVERY_REFRESH_HOURS"
_DEFAULT_REFRESH_HOURS = 72.0

#: Vendors this path is scoped to (td568212 brief) — free models from any
#: other vendor are out of scope for this pass.
_VENDORS: frozenset[str] = frozenset({"groq", "cerebras", "openrouter"})

#: The eval tier a candidate (and the incumbent it's compared against)
#: runs under. These are fast/cheap free-tier offerings, not
#: frontier-class — MEDIUM is the general-purpose judge/triage rung.
_EVAL_TIER_STR = "medium"

#: ``meta`` key on the lead todo carrying the whole state machine:
#: ``{vendor, model_id, state, discovered_at, account?, decision_todo_id?,
#: resolution?}``. ``state`` walks pending -> qualifying ->
#: awaiting_decision -> resolved.
_LEAD_META_KEY = "offer_discovery_lead"

#: One line per vendor, naming its current best free-tier chat model (or
#: "none"). Deliberately rigid so the answer parses without a second,
#: LLM-structuring call.
_RESEARCH_QUERY = (
    "For each of Groq, Cerebras and OpenRouter, name the single model id "
    "of their best currently-free (no-cost, no-trial-credit) chat "
    "completion API offering today, or 'none' if they currently have no "
    "free tier. Answer in EXACTLY three lines, one per vendor, this "
    "literal format and nothing else: 'groq: <model id or none>' / "
    "'cerebras: <model id or none>' / 'openrouter: <model id or none>'."
)

#: ``<vendor>: <value>`` on ONE line, tolerant of the markdown a model wraps
#: round it — a bullet or quote prefix, emphasis on the label (``**Groq:**
#: x``, ``**Groq**: x``) or on the value. Spaces and tabs only between the
#: pieces: ``\s`` would let an empty ``groq:`` swallow the next line.
_VENDOR_LINE_RE = re.compile(
    r"^[ \t>\-*_`]*(groq|cerebras|openrouter)[ \t*_`]*:[ \t*_`]*(\S.*)$",
    re.IGNORECASE | re.MULTILINE,
)
#: A value (first token, lower-cased) that says "no free model": the literal
#: ``none``, its spellings, and ``no`` — which is how a sentence such as
#: "No free tier" starts (the first token is what gets read as the id).
_NONE_TOKENS = frozenset({"no", "none", "n/a", "na", "-", "null"})


def _due(store: Store) -> bool:
    """True when the throttle window has elapsed since the last pass."""
    return _throttle.due(store, _STATE_KEY, _REFRESH_ENV_VAR, _DEFAULT_REFRESH_HOURS)


def run_research_call(store: Store, query: str = _RESEARCH_QUERY) -> str | None:
    """One Perplexity call (sonar-reasoning-pro) — see the module docstring
    for why this tier, not ``perplexity-research``. Best-effort: ``None``
    on any failure (no key, transport, vendor refusal) so a dead feed
    degrades discovery to a no-op, never a crash.
    """
    from precis.dispatch import Hub
    from precis.errors import Upstream

    try:
        # The hub's sibling mechanism, not an import of the handler class: a
        # worker never imports ``precis.handlers`` (import-linter contract).
        handler = Hub(store=store).sibling("perplexity-reasoning")
        resp = handler.get(q=query, refresh=True)
    except Upstream:
        log.warning("offer_discovery: perplexity call refused", exc_info=True)
        return None
    except Exception:  # pragma: no cover — defensive, mirrors llm_reconcile
        log.warning("offer_discovery: perplexity call raised", exc_info=True)
        return None
    return resp.body


def _parse_vendor_lines(text: str) -> list[tuple[str, str]]:
    """Pull ``(vendor, model_id)`` pairs out of the rigid three-line answer.

    A vendor naming "none" (or an equivalent token: ``n/a``, ``no``, so also a
    sentence such as "No free tier") is skipped — it has no free tier right
    now, not a lead. The first whitespace-delimited token after the colon is
    taken as the model id, markdown wrapper characters stripped.
    """
    out: list[tuple[str, str]] = []
    for m in _VENDOR_LINE_RE.finditer(text):
        vendor = m.group(1).lower()
        raw = m.group(2).strip()
        head = raw.split()[0] if raw.split() else ""
        model_id = head.strip("`*_[](),.").strip()
        if not model_id or model_id.lower() in _NONE_TOKENS:
            continue
        out.append((vendor, model_id))
    return out


def _known_model_ids(store: Store) -> set[str]:
    """Every model id already tracked by an ``llm_account`` or ``llm``
    card — a lead naming one of these isn't new."""
    from precis.utils.llm.accounts import list_accounts

    known: set[str] = set()
    for acc in list_accounts(store):
        known.update(acc.models)
    for card in store.list_refs(kind="llm", limit=2000):
        mid = (card.meta or {}).get("model_id")
        if mid:
            known.add(str(mid))
    return known


def _status_of(conn: Any, ref_id: int) -> str:
    """The ref's current ``STATUS`` tag value, ``'open'`` when unset."""
    row = conn.execute(
        """
        SELECT COALESCE(
                 (SELECT t.value FROM ref_tags rt
                    JOIN tags t ON t.tag_id = rt.tag_id
                   WHERE rt.ref_id = %s AND t.namespace = 'STATUS'
                   LIMIT 1),
                 'open'
               )
        """,
        (ref_id,),
    ).fetchone()
    return str(row[0]) if row else "open"


def _answer_of(store: Store, ref_id: int) -> str | None:
    """The newest ``answer:``-prefixed comment on a todo, or ``None``."""
    chunks = store.chunks.list_chunks_for_ref_by_kind(ref_id, "todo_comment")
    for c in reversed(chunks):
        text = (c.text or "").strip()
        if text.lower().startswith("answer:"):
            return text
    return None


def _answer_choice(answer: str | None) -> str:
    """The chosen option's lead word, lowercased, checked by PREFIX —
    never substring: "reject — do not enable" contains "enable" as a
    substring, which a naive ``"enable" in text`` check would misread as
    approval. Strips the leading ``answer:`` marker first."""
    low = (answer or "").strip().lower()
    if low.startswith("answer:"):
        low = low[len("answer:") :].strip()
    return low


def _mint_ask_todo(
    store: Store,
    *,
    title: str,
    body: str,
    ask: dict[str, Any],
    meta_extra: dict[str, Any],
) -> int:
    """Mint one ``ask-user`` todo — the ``_mint_ask`` shape
    (``insert_ref`` + STATUS + tags + a body chunk, inside one
    ``store.tx()``). ``ask.blocks`` is always ``'nothing'`` here: a lead
    or a qualification decision blocks no slice, it just waits on Reto."""
    check_ask_meta({"ask": ask})
    meta = {"ask": ask, **meta_extra}
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo", slug=None, title=title, meta=meta, conn=conn
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.add_tag(ref.id, Tag.open("ask-user"), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
    return int(ref.id)


def _find_lead(conn: Any, vendor: str, model_id: str) -> int | None:
    """The lead todo id already filed for ``(vendor, model_id)``, if any —
    "at most one decision row per lead, ever" (not just "one open one")."""
    row = conn.execute(
        """
        SELECT ref_id FROM refs
         WHERE kind = 'todo' AND retired_at IS NULL
           AND meta->%s->>'vendor' = %s
           AND meta->%s->>'model_id' = %s
         ORDER BY ref_id DESC LIMIT 1
        """,
        (_LEAD_META_KEY, vendor, _LEAD_META_KEY, model_id),
    ).fetchone()
    return int(row[0]) if row else None


def _file_lead_todo(
    store: Store, *, vendor: str, model_id: str, report_text: str
) -> int:
    title = f"offer_discovery: pursue {vendor} free model {model_id!r}?"
    ask = {
        "kind": "ruling",
        "options": [
            "pursue — seed a dark account and run the eval",
            "skip — not worth pursuing",
        ],
        "blocks": "nothing",
        "context": (
            f"Perplexity (sonar-reasoning-pro) named {model_id!r} as "
            f"{vendor}'s current free-tier offering; not yet in the catalog."
        ),
    }
    body = (
        f"offer_discovery found a candidate lead: **{vendor}** — `{model_id}` "
        "(free tier, not yet qualified).\n\n"
        "Source: Perplexity AI (sonar-reasoning-pro). Perplexity is **not** "
        "a primary source — verify before relying on it.\n\n"
        f"Full research answer:\n\n{report_text}\n"
    )
    return _mint_ask_todo(
        store,
        title=title,
        body=body,
        ask=ask,
        meta_extra={
            _LEAD_META_KEY: {
                "vendor": vendor,
                "model_id": model_id,
                "state": "pending",
                "discovered_at": datetime.now(UTC).isoformat(),
            }
        },
    )


def _discover_leads(store: Store, report_text: str) -> int:
    """Discovery leg: at most one decision row per lead, no account write."""
    known = _known_model_ids(store)
    found = 0
    for vendor, model_id in _parse_vendor_lines(report_text):
        if vendor not in _VENDORS or model_id in known:
            continue
        with store.pool.connection() as conn:
            existing = _find_lead(conn, vendor, model_id)
        if existing is not None:
            continue
        _file_lead_todo(
            store, vendor=vendor, model_id=model_id, report_text=report_text
        )
        found += 1
    return found


def _account_name(vendor: str, model_id: str) -> str:
    from precis.utils.slug import slug_from_text

    tail = slug_from_text(model_id, max_len=40) or "model"
    return f"{vendor}-{tail}"[:63]


def _seed_dark_account(
    store: Store, *, vendor: str, model_id: str, lead_todo_id: int
) -> str:
    """A **dark** (``enabled = false``) ``llm_account`` row for the
    candidate — step 1 of the qualification path. No secret, no profile:
    those are a human's follow-up once a lead is worth routing traffic to."""
    from psycopg.types.json import Jsonb

    name = _account_name(vendor, model_id)
    entitlement = {
        "source": "offer_discovery",
        "lead_todo": lead_todo_id,
        "qualification_status": "dark",
        "created_at": datetime.now(UTC).isoformat(),
    }
    with store.pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO llm_account (
                name, vendor, plan, adapter, concurrency, task_classes,
                models, default_model, enabled, entitlement, pools
            ) VALUES (%s, %s, 'free', 'api', 1, %s, %s, %s, false, %s, %s)
            ON CONFLICT (name) DO NOTHING
            """,
            (name, vendor, [], [model_id], model_id, Jsonb(entitlement), []),
        )
        conn.commit()
    return name


def _seed_dark_card(store: Store, *, vendor: str, model_id: str) -> int:
    """A dark ``llm`` card for the candidate — :func:`run_eval` needs an
    existing card to record its ordinals onto (:func:`precis.llm_catalog.
    record_eval` refuses a model with none).

    A card that already exists (curated by hand, or seeded since the lead was
    filed) is returned as it is: :func:`~precis.llm_catalog.upsert_card`
    rewrites an existing card's text, and the only card fields this path owns
    are the measured-eval ordinals :func:`run_eval` records itself."""
    from precis.llm_catalog import LLM_KIND, upsert_card

    existing = store.find_ref_by_meta(kind=LLM_KIND, key="model_id", value=model_id)
    if existing is not None:
        return int(existing.id)
    text = (
        f"{model_id} — a {vendor} free-tier candidate discovered via "
        "offer_discovery (td568212). Not yet qualified: no measured eval "
        "on this card yet, and its account is disabled."
    )
    ref_id, _created = upsert_card(store, model_id=model_id, text=text)
    return ref_id


def _advance_pending_leads(store: Store) -> int:
    """pending -> qualifying (an approved lead) or resolved (skipped)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, meta FROM refs
             WHERE kind = 'todo' AND retired_at IS NULL
               AND meta->%s->>'state' = 'pending'
            """,
            (_LEAD_META_KEY,),
        ).fetchall()
    advanced = 0
    for ref_id, meta in rows:
        ref_id = int(ref_id)
        lead = dict((meta or {}).get(_LEAD_META_KEY) or {})
        with store.pool.connection() as conn:
            status = _status_of(conn, ref_id)
        if status not in TERMINAL_TODO_STATUSES:
            continue
        choice = _answer_choice(_answer_of(store, ref_id))
        vendor, model_id = lead.get("vendor"), lead.get("model_id")
        if choice.startswith("pursue") and vendor and model_id:
            account = _seed_dark_account(
                store, vendor=vendor, model_id=model_id, lead_todo_id=ref_id
            )
            _seed_dark_card(store, vendor=vendor, model_id=model_id)
            lead["state"] = "qualifying"
            lead["account"] = account
        else:
            lead["state"] = "resolved"
            lead["resolution"] = "skipped"
        store.stamp_ref_meta(ref_id, {_LEAD_META_KEY: lead})
        advanced += 1
    return advanced


def _file_qualification_todo(
    store: Store,
    *,
    vendor: str,
    model_id: str,
    account: str,
    incumbent: str,
    candidate_ordinals: dict[str, int],
    incumbent_ordinals: dict[str, int],
) -> int:
    title = f"offer_discovery: enable {vendor} candidate {model_id!r}?"
    axes = sorted(set(candidate_ordinals) | set(incumbent_ordinals))
    lines = [
        f"- {axis}: candidate {candidate_ordinals.get(axis, '—')} vs "
        f"incumbent ({incumbent}) {incumbent_ordinals.get(axis, '—')}"
        for axis in axes
    ]
    ask = {
        "kind": "approval",
        "options": [
            "enable — ordinals look sound",
            "reject — do not enable",
        ],
        "blocks": "nothing",
        "context": f"measured-eval recorded on {model_id!r}'s card; "
        f"account {account!r} is dark pending this answer.",
    }
    body = (
        f"**{vendor}** candidate `{model_id}` (dark account `{account}`) ran "
        f"precis's golden eval against the incumbent (`{incumbent}`):\n\n"
        + "\n".join(lines)
        + "\n\nEnabling flips the account's `enabled` column; rejecting "
        "leaves it dark permanently.\n"
    )
    return _mint_ask_todo(
        store,
        title=title,
        body=body,
        ask=ask,
        meta_extra={},
    )


def _measured_ordinals(report: EvalReport, *, recorded: bool) -> dict[str, int]:
    """The axes of an eval report that were actually measured.

    ``EvalReport.ordinals`` lists EVERY axis, and a void one (a transport or
    dispatch error on any task: no key wired, the vendor refusing) carries
    ordinal 1 — the bucket of a 0.0 mean over zero scored tasks — which, quoted
    to Reto, reads as "measured, and bad". ``recorded=True`` (the candidate,
    run with ``record=True``) keeps only axes whose ordinal reached the card;
    ``False`` (the incumbent, not recorded) keeps every non-void axis.
    """
    return {
        r.axis: r.ordinal
        for r in report.results
        if (r.recorded if recorded else not r.void)
    }


def _advance_qualifying_leads(
    store: Store, *, dispatch_fn: Callable[[Any], Any] | None = None
) -> int:
    """qualifying -> awaiting_decision, once the candidate's eval actually
    recorded an ordinal (void — no transport wired yet — retries next pass:
    no decision todo, no lead change, a WARNING).

    ``dispatch_fn`` is :func:`precis.llm_eval.harness.run_eval`'s own
    injection seam, threaded through so a test can run this without a real
    model call — ``None`` uses the live router, same as a bare ``precis
    llm eval``.
    """
    from precis.llm_eval.harness import run_eval
    from precis.utils.llm.router import Tier, resolve_model

    tier = Tier(_EVAL_TIER_STR)
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, meta FROM refs
             WHERE kind = 'todo' AND retired_at IS NULL
               AND meta->%s->>'state' = 'qualifying'
            """,
            (_LEAD_META_KEY,),
        ).fetchall()
    advanced = 0
    for ref_id, meta in rows:
        ref_id = int(ref_id)
        lead = dict((meta or {}).get(_LEAD_META_KEY) or {})
        vendor, model_id, account = (
            lead.get("vendor"),
            lead.get("model_id"),
            lead.get("account"),
        )
        if not (vendor and model_id and account):
            continue
        candidate_report = run_eval(
            store, model=model_id, tier=tier, record=True, dispatch_fn=dispatch_fn
        )
        candidate_ordinals = _measured_ordinals(candidate_report, recorded=True)
        if not candidate_ordinals:
            # Not yet routable (every axis void) or nothing reached the card:
            # no measurement, so no decision todo and no state change — the
            # lead is retried on the next pass.
            log.warning(
                "offer_discovery: %s candidate %r has no recorded eval axis "
                "(%d of %d void) — no decision filed, lead todo %d stays "
                "'qualifying'",
                vendor,
                model_id,
                sum(1 for r in candidate_report.results if r.void),
                len(candidate_report.results),
                ref_id,
            )
            continue
        incumbent = resolve_model(tier)
        incumbent_report = run_eval(
            store, model=incumbent, tier=tier, record=False, dispatch_fn=dispatch_fn
        )
        decision_id = _file_qualification_todo(
            store,
            vendor=vendor,
            model_id=model_id,
            account=account,
            incumbent=incumbent,
            candidate_ordinals=candidate_ordinals,
            incumbent_ordinals=_measured_ordinals(incumbent_report, recorded=False),
        )
        lead["state"] = "awaiting_decision"
        lead["decision_todo_id"] = decision_id
        store.stamp_ref_meta(ref_id, {_LEAD_META_KEY: lead})
        advanced += 1
    return advanced


def _resolve_qualifying_decisions(store: Store) -> int:
    """awaiting_decision -> resolved: "enable" flips the account on;
    anything else (including "reject") leaves it dark, stamped rejected."""
    from psycopg.types.json import Jsonb

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, meta FROM refs
             WHERE kind = 'todo' AND retired_at IS NULL
               AND meta->%s->>'state' = 'awaiting_decision'
            """,
            (_LEAD_META_KEY,),
        ).fetchall()
    resolved = 0
    for ref_id, meta in rows:
        ref_id = int(ref_id)
        lead = dict((meta or {}).get(_LEAD_META_KEY) or {})
        decision_id, account = lead.get("decision_todo_id"), lead.get("account")
        if not decision_id or not account:
            continue
        decision_id = int(decision_id)
        with store.pool.connection() as conn:
            status = _status_of(conn, decision_id)
        if status not in TERMINAL_TODO_STATUSES:
            continue
        choice = _answer_choice(_answer_of(store, decision_id))
        with store.pool.connection() as conn:
            if choice.startswith("enable"):
                conn.execute(
                    "UPDATE llm_account SET enabled = true, updated_at = now() "
                    "WHERE name = %s",
                    (account,),
                )
                lead["resolution"] = "enabled"
            else:
                conn.execute(
                    "UPDATE llm_account SET entitlement = entitlement || %s::jsonb, "
                    "updated_at = now() WHERE name = %s",
                    (Jsonb({"qualification_status": "rejected"}), account),
                )
                lead["resolution"] = "rejected"
            conn.commit()
        lead["state"] = "resolved"
        store.stamp_ref_meta(ref_id, {_LEAD_META_KEY: lead})
        resolved += 1
    return resolved


def run_offer_discovery_pass(
    store: Store,
    *,
    force: bool = False,
    _fetch: bool = True,
    research_fn: Callable[[Store], str | None] | None = None,
    eval_dispatch_fn: Callable[[Any], Any] | None = None,
) -> BatchResult:
    """One pass: the throttled discovery leg (one Perplexity call, if due)
    plus the qualification path's three advance steps (always run, not
    throttled — an approved lead must not wait another three days).

    ``research_fn`` is the injectable Perplexity call (defaults to
    :func:`run_research_call` when ``None`` and ``_fetch`` is set) — tests
    inject a stub so no real paid call fires; ``_fetch=False`` with no
    ``research_fn`` skips discovery entirely (the qualification advances
    still run, against whatever leads already exist). ``eval_dispatch_fn``
    is :func:`run_eval`'s own injection seam, forwarded for the same
    reason — ``None`` uses the live router.

    ``claimed``/``ok`` count new leads filed + leads advanced through the
    qualification path's three stages this pass.
    """
    idle = BatchResult(handler="offer_discovery", claimed=0, ok=0, failed=0)
    if not store.dsn:
        return idle
    due = force or _due(store)

    # Single-runner lock via the sanctioned pgbouncer-safe holder (a dedicated
    # connection that only holds a transaction-scoped advisory lock, with prod's
    # idle-in-transaction kill lifted for it: this pass sits in a Perplexity
    # call and model evals for minutes). The work below runs on the store pool.
    with try_xact_advisory_lock(store.dsn, _LOCK_KEY) as got:
        if not got:
            return idle  # another node owns the sweep this cycle

        leads_found = 0
        if due:
            # Stamp the window BEFORE the paid call: a raise anywhere below
            # (the call, the parse, a todo insert) then costs one window, not
            # one Perplexity call per cycle.
            store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())
            if research_fn is None and _fetch:
                research_fn = run_research_call
            report_text = research_fn(store) if research_fn is not None else None
            if report_text:
                leads_found = _discover_leads(store, report_text)

        advanced = _advance_pending_leads(store)
        qualified = _advance_qualifying_leads(store, dispatch_fn=eval_dispatch_fn)
        resolved = _resolve_qualifying_decisions(store)

        work = leads_found + advanced + qualified + resolved
        if work:
            log.info(
                "offer_discovery: %d new lead(s), %d -> qualifying, "
                "%d sent for a qualification decision, %d resolved",
                leads_found,
                advanced,
                qualified,
                resolved,
            )
        return BatchResult(handler="offer_discovery", claimed=work, ok=work, failed=0)


__all__ = [
    "run_offer_discovery_pass",
    "run_research_call",
]
