"""Quest ``commits`` — the non-materials artifact channel (gr573805).

The tick's only progress channel used to be `proposals`: a candidate that
mints a `structure` and dispatches a sim. Operational quests — qu573655's
compute scout is the motivating case — make real forward motion that has
no atomistic representation (a lead qualified, an ask queued for the
owner, a lead killed with a reason), and the harness read every such tick
as a stall or pushed the model to fabricate a slab/structure spec.

Reto's taxonomy call (2026-11-11): operational quests may commit progress
that isn't a structure — an ask queued for Reto, an account row created
disabled, a lead ruled out with a reason. So every body's tick schema
accepts an optional ``commits`` list beside ``proposals``:

* ``{"type": "ask", "question": …, "options": […], "context": …}`` —
  mints an ``ask-user`` todo carrying ``meta.ask`` (the same shape
  gripe_triage's ``reto:ask`` door writes), linked ``blocks`` → quest so
  the asks view and the quest itself read the waiting work.
* ``{"type": "account", "name": …, "vendor": …, "plan": …,
  "adapter": …, "promo_expiry": …, "models": […], "note": …}`` — records
  a compute/account lead as a DISABLED ``llm_account`` row. This is a
  deliberately narrower door than the owner-only ``put(kind='llm',
  args={'account': …})``: create-only (``ON CONFLICT DO NOTHING`` — a
  commit can never rewrite an existing row), ``enabled`` hard-false, and
  no ``api_key_ref``/``profile``/``pools``/``task_classes`` at all — the
  row is inert lead data; enabling and credentialing stay the owner's
  door. The lead's note + quest provenance ride
  ``entitlement.quest_lead`` so the reviewer can see where the row came
  from without colliding with the routed ``entitlement.models`` /
  ``entitlement.limits`` vocabulary.
* ``{"type": "rule-out", "lead": …, "reason": …}`` — closes a lead or
  direction durably: pinned ``ruled-out`` in the ledger
  (:func:`precis.quest.dossier.add_attempt`, which upserts a near-dup
  node and never regresses a status) plus a ``dead-end`` logbook entry
  carrying the reason. A repeat rule-out of the same lead is a silent
  dedup, not a second deed.

The contract mirrors :mod:`precis.quest.needs`'s: the model *declares*
commits; the executor (this module) writes the artifacts and a logbook
receipt per entry — a malformed entry drops with an observation note,
never a tick failure. Applied commits count as forward motion: they
feed ``did_work`` like any logbook write and reset the tick's
``ticks_since_experiment`` stall counter — the commit ladder's job is
guaranteeing the agent ACTS, and a real artifact is an act.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from precis.errors import PrecisError

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: ``commits`` entries accepted per tick — real progress is a short list;
#: a looping payload must not fan out unboundedly (same cap discipline as
#: ``needs``/``dialectic_ops``).
COMMITS_MAX = 3

#: The declared ``type`` vocabulary — Reto's ruling (gr573805): queued
#: ask, disabled account row, lead ruled out with reason.
COMMIT_TYPES: tuple[str, ...] = ("ask", "account", "rule-out")

#: ``type`` spellings that normalise to :data:`COMMIT_TYPES` — the model
#: writes ``rule_out`` / ``ruleout`` / ``lead-rule-out`` variants; the
#: parse maps them onto the canonical key rather than dropping the claim.
_TYPE_ALIASES: dict[str, str] = {
    "ask": "ask",
    "queue-ask": "ask",
    "queued-ask": "ask",
    "account": "account",
    "account-lead": "account",
    "account-row": "account",
    "lead": "account",
    "rule-out": "rule-out",
    "rule_out": "rule-out",
    "ruleout": "rule-out",
    "ruled-out": "rule-out",
}

#: Per-field length caps — short strings, like a gap line (same caps as
#: :mod:`precis.quest.needs`).
FIELD_CHARS = 240

#: The ask todo's title cap — mirrors gripe_triage's ``_ASK_TITLE_CAP``.
_ASK_TITLE_CAP = 160

#: Dead-end receipt prefix — the dedup marker for a repeat rule-out of the
#: same lead (the ledger pin is idempotent on its own).
_RULE_OUT_PREFIX = "commit: rule-out "


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _norm(text: str) -> str:
    """Case/whitespace-insensitive compare key for ask dedup."""
    return _one_line(text).lower()


@dataclass(frozen=True)
class Commit:
    """One validated ``commits`` entry — ``type`` is canonical
    (:data:`_TYPE_ALIASES`), ``data`` the kept fields."""

    type: str
    data: dict[str, Any]


def _str(data: dict[str, Any], key: str) -> str:
    return str(data.get(key) or "").strip()


def _parse_entry(e: Any) -> Commit | None:
    """Validate one ``commits`` entry; ``None`` when unusable.

    Each type has a required-text floor: ``ask`` needs ``question``,
    ``account`` needs ``name``/``vendor``/``plan``/``adapter``, ``rule-
    out`` needs ``lead`` + ``reason``. Missing floor → the entry is
    dropped and counted, never guessed at.
    """
    if not isinstance(e, dict):
        return None
    raw_type = _str(e, "type") or _str(e, "commit") or _str(e, "kind")
    ctype = _TYPE_ALIASES.get(raw_type.lower())
    if ctype is None:
        return None
    if ctype == "ask":
        question = _str(e, "question") or _str(e, "text") or _str(e, "title")
        if not question:
            return None
        options = e.get("options")
        options = (
            [str(o).strip() for o in options if str(o).strip()]
            if isinstance(options, list)
            else []
        )
        kind = _str(e, "kind") if e.get("kind") != raw_type else ""
        return Commit(
            type="ask",
            data={
                "question": question,
                "options": options,
                "context": _str(e, "context") or _str(e, "why"),
                "kind": kind,
            },
        )
    if ctype == "account":
        data: dict[str, Any] = {
            "name": _str(e, "name"),
            "vendor": _str(e, "vendor"),
            "plan": _str(e, "plan"),
            "adapter": _str(e, "adapter"),
            "promo_expiry": _str(e, "promo_expiry") or _str(e, "expiry"),
            "note": _str(e, "note") or _str(e, "why") or _str(e, "context"),
        }
        models = e.get("models")
        if isinstance(models, list):
            data["models"] = [str(m).strip() for m in models if str(m).strip()]
        else:
            data["models"] = []
        if not all(data[k] for k in ("name", "vendor", "plan", "adapter")):
            return None
        return Commit(type="account", data=data)
    # rule-out
    lead = _str(e, "lead") or _str(e, "node") or _str(e, "text") or _str(e, "name")
    reason = _str(e, "reason") or _str(e, "why")
    if not lead or not reason:
        return None
    return Commit(type="rule-out", data={"lead": lead, "reason": reason})


def parse_commits(raw: Any) -> tuple[list[Commit], int]:
    """Validate a ``commits`` field value → ``(commits, dropped)``.

    Lenient reader, same contract as :func:`precis.quest.needs.parse_needs`:
    a non-list top level, a non-dict entry, an unknown ``type`` or a missing
    required field is dropped and counted, never a failure. Strings are
    truncated to the field caps rather than rejected.
    """
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        return [], 1
    commits: list[Commit] = []
    dropped = 0
    for e in raw[:COMMITS_MAX]:
        parsed = _parse_entry(e)
        if parsed is None:
            dropped += 1
            continue
        data = {
            k: (v[:FIELD_CHARS] if isinstance(v, str) else v)
            for k, v in parsed.data.items()
        }
        commits.append(Commit(type=parsed.type, data=data))
    dropped += max(0, len(raw) - COMMITS_MAX)
    return commits, dropped


@dataclass(frozen=True)
class CommitsOutcome:
    """What :func:`apply_commits` did with one ``commits`` field value."""

    commits: tuple[Commit, ...]
    dropped: int
    #: Entries that produced an artifact (ask todo minted, account row
    #: inserted, ledger rule-out written) — deduped/already-present
    #: commits count as applied too: the artifact exists.
    applied: int
    #: ``td<id>`` of each ask todo minted (dedup hits name the live one).
    todos: tuple[int, ...]
    #: ``llm_account.name``s the commit actually inserted this call.
    accounts: tuple[str, ...]
    #: quest-log entries this call wrote (the caller's ``added`` counter
    #: counts them toward engagement).
    logbook_entries: int


# ── ask: an ask-user todo the quest reads blocked-by ──────────────────


def _open_blocking_asks(store: Store, quest_id: int) -> dict[str, int]:
    """``{normalised title: todo_id}`` for live ``ask-user`` todos holding
    a ``blocks`` edge to this quest — the ask dedup map (the edge is the
    single source of truth, same marker discipline as the needs gripe
    scan in :func:`precis.quest.needs._open_need_gripes`)."""
    from precis.todo import TERMINAL_TODO_STATUSES

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title
              FROM links l
              JOIN refs r ON r.ref_id = l.src_ref_id
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE l.dst_ref_id = %s AND l.relation = 'blocks'
               AND r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'ask-user'
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2
                        JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS'
                       LIMIT 1),
                     'open') <> ALL(%s)
            """,
            (quest_id, sorted(TERMINAL_TODO_STATUSES)),
        ).fetchall()
    return {_norm(str(title)): int(rid) for rid, title in rows}


def _apply_ask(
    store: Store, quest_id: int, data: dict[str, Any], *, by: str
) -> tuple[int | None, str]:
    """Mint (or dedup) the ask's ``ask-user`` todo; returns
    ``(todo_id, receipt)`` — ``todo_id`` is the live ask's id on a dedup
    hit too, so the receipt can always name it."""
    from precis.plans.nodes import finish_mint
    from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
    from precis.todo.guards import (
        _ASK_CONTEXT_CAP,
        _ASK_MAX_OPTIONS,
        ASK_KINDS,
        check_ask_meta,
    )
    from precis.utils.text import excerpt

    question = _one_line(data["question"])[:_ASK_TITLE_CAP]
    options = [str(o)[:200] for o in (data.get("options") or [])][:_ASK_MAX_OPTIONS]
    # An unknown ask-kind coerces to `ruling` (the plain "a call for the
    # owner" kind) — same lenience as the field caps: a verbose model is
    # still making a claim, never a tick failure.
    kind = str(data.get("kind") or "")
    if kind not in ASK_KINDS:
        kind = "ruling"
    meta_ask = {
        "kind": kind,
        "options": options,
        # quests wait on a ref that isn't a todo — `blocks` only names
        # todos, so the dependency rides the `blocks` link written below
        # (same shape as gripe_triage's reto:ask asks).
        "blocks": "nothing",
        "context": excerpt(
            data.get("context") or f"Queued by quest qu{quest_id}.",
            limit=_ASK_CONTEXT_CAP - 1,
        ),
    }
    check_ask_meta({"ask": meta_ask}, question=question)

    existing = _open_blocking_asks(store, quest_id)
    if (dup := existing.get(_norm(question))) is not None:
        return dup, f"ask already queued as td{dup} — not minting a second"

    body = (
        f"{meta_ask['context']}\n\n"
        f"Queued by quest qu{quest_id}'s tick (`commits` ask, gr573805) — "
        "the quest reads `blocked-by` this todo until answered. "
        "Answer on this todo (`answer: …` + close)."
        + ("\n\nOptions:\n" + "\n".join(f"- {o}" for o in options) if options else "")
        + "\n"
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=question,
            meta={"ask": meta_ask},
            conn=conn,
        )
        tid = int(ref.id)
        store.add_tag(
            tid,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.add_tag(tid, Tag.open("ask-user"), set_by="system", conn=conn)
        store.add_tag(tid, Tag.open(f"quest:{quest_id}"), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            tid,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        # The quest is blocked-by this ask — stored as the canonical
        # (todo --blocks--> quest) edge, same as a needs gripe's.
        store.add_link(
            src_ref_id=tid,
            dst_ref_id=quest_id,
            relation="blocks",
            set_by="system",
            conn=conn,
        )
        finish_mint(store, tid, conn=conn)
    return tid, f"queued ask td{tid} for the owner — {question!r}"


# ── account: a disabled llm_account lead row ───────────────────────────

#: Create-only INSERT for a quest-recorded lead. Deliberately names every
#: writeable column and never touches ``api_key_ref`` / ``profile`` /
#: ``pools`` / ``task_classes`` / ``enabled`` beyond their inert defaults:
#: the row can route nothing until the owner's ``put`` door enables it.
_LEAD_INSERT_SQL = """
INSERT INTO llm_account (
    name, vendor, plan, adapter, models, promo_expiry,
    entitlement, privacy, enabled
) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, '{}'::jsonb, false)
ON CONFLICT (name) DO NOTHING
"""


def _apply_account(
    store: Store, quest_id: int, data: dict[str, Any], *, by: str
) -> tuple[str | None, str]:
    """Insert the disabled lead row; returns ``(name_or_None, receipt)`` —
    ``None`` on a conflict (the lead is already recorded). Raises
    :class:`PrecisError` on a validation refusal — the caller writes the
    refusal as a receipt WITHOUT counting it applied: a refused commit
    wrote no artifact, so it buys no forward motion (a model emitting
    refusable commits every tick must still stall)."""
    from precis.handlers.llm import _ACCOUNT_ADAPTERS, _ACCOUNT_NAME_RE

    name = str(data["name"]).strip()
    adapter = str(data["adapter"]).strip()
    if not _ACCOUNT_NAME_RE.match(name):
        raise PrecisError(
            f"account lead {name!r} refused — name must be a lowercase "
            "slug ([a-z0-9][a-z0-9-]*)"
        )
    if adapter not in _ACCOUNT_ADAPTERS:
        raise PrecisError(
            f"account lead {name!r} refused — adapter {adapter!r} is not "
            f"one of {sorted(_ACCOUNT_ADAPTERS)}"
        )
    promo = data.get("promo_expiry") or ""
    promo_date: date | None = None
    if promo:
        try:
            promo_date = date.fromisoformat(str(promo)[:10])
        except ValueError:
            raise PrecisError(
                f"account lead {name!r} refused — promo_expiry {promo!r} "
                "is not an ISO date (YYYY-MM-DD)"
            ) from None
    models = [str(m)[:FIELD_CHARS] for m in (data.get("models") or [])]
    note = str(data.get("note") or "").strip()
    entitlement = {"quest_lead": {"quest": f"qu{quest_id}", "by": by}}
    if note:
        entitlement["quest_lead"]["note"] = note
    with store.tx() as conn:
        cur = conn.execute(
            _LEAD_INSERT_SQL,
            (
                name,
                str(data["vendor"]).strip(),
                str(data["plan"]).strip(),
                adapter,
                models,
                promo_date,
                json.dumps(entitlement),
            ),
        )
        inserted = cur.rowcount > 0
    if not inserted:
        return None, (
            f"account lead {name!r} already recorded — create-only, "
            "the existing row stands"
        )
    tail = f"; promo expires {promo_date.isoformat()}" if promo_date else ""
    return name, (
        f"account lead {name!r} recorded — {data['vendor']} {data['plan']} "
        f"({adapter}){tail}; DISABLED — enabling and credentials stay the "
        "owner's put door"
    )


# ── rule-out: a pinned ledger status + a dead-end deed ─────────────────


def _already_ruled_out(store: Store, quest_id: int, lead: str) -> bool:
    """A previous commit already recorded this lead's rule-out — the
    dead-end receipt is the marker (the ledger pin dedups on its own via
    :func:`add_attempt`'s near-dup merge)."""
    marker = f"{_RULE_OUT_PREFIX}{lead}"
    return any(
        (b.chunk_kind == "quest_log")
        and (b.meta or {}).get("entry_type") == "dead-end"
        and (b.text or "").splitlines()[0].startswith(marker[:200])
        for b in store.chunks.list_chunks_for_ref(quest_id)
    )


def _apply_rule_out(
    store: Store, quest_id: int, data: dict[str, Any], *, by: str
) -> str:
    """Pin the lead ``ruled-out`` in the ledger and write the dead-end
    deed; returns the receipt text."""
    from precis.quest import dossier as dossier_mod
    from precis.quest.logbook import append_entry

    lead, reason = data["lead"], data["reason"]
    # The pinned ledger node is the durable half: `add_attempt` upserts a
    # near-dup of `lead` and advances its status to ruled-out (the status
    # rank never regresses), so an open direction with the same wording is
    # killed in place and a never-pinned lead is pinned ruled-out.
    pinned = dossier_mod.add_attempt(store, quest_id, lead, status="ruled-out")
    if _already_ruled_out(store, quest_id, lead):
        return f"lead already ruled out — {lead!r} stands"
    append_entry(
        store,
        quest_id,
        text=f"{_RULE_OUT_PREFIX}{lead} — {reason}",
        entry_type="dead-end",
        by=by,
    )
    how = "pinned ruled-out" if pinned else "advanced to ruled-out"
    return f"rule-out committed ({how}): {lead!r} — {reason}"


# ── the apply path ─────────────────────────────────────────────────────


def apply_commits(
    store: Store, quest_id: int, raw: Any, *, by: str = "agent"
) -> CommitsOutcome:
    """Parse + apply a tick payload's ``commits`` (gr573805).

    Same executor-side contract as :func:`precis.quest.needs.apply_needs`:
    malformed entries drop with a logbook note (never a tick failure);
    each valid commit writes its artifact and one logbook receipt; a
    per-commit failure is logged and skipped so a commit-write bug never
    sinks the tick's other writes.
    """
    from precis.quest.logbook import append_entry

    commits, dropped = parse_commits(raw)
    entries = 0
    applied = 0
    todos: list[int] = []
    accounts: list[str] = []
    if dropped:
        append_entry(
            store,
            quest_id,
            text=(
                f"dropped {dropped} malformed `commits` "
                f"entr{'y' if dropped == 1 else 'ies'} — each is "
                "{'type': 'ask'|'account'|'rule-out', …} with its required "
                "fields (ask: question; account: name/vendor/plan/adapter; "
                "rule-out: lead + reason)"
            ),
            entry_type="observation",
            by=by,
        )
        entries += 1
        log.info("quest %s: dropped %d malformed commits entries", quest_id, dropped)

    for c in commits:
        try:
            if c.type == "ask":
                tid, receipt = _apply_ask(store, quest_id, c.data, by=by)
                if tid is not None:
                    todos.append(tid)
            elif c.type == "account":
                name, receipt = _apply_account(store, quest_id, c.data, by=by)
                if name is not None:
                    accounts.append(name)
            else:
                receipt = _apply_rule_out(store, quest_id, c.data, by=by)
            applied += 1
        except PrecisError as exc:
            receipt = f"commit refused — {exc}"
            log.info("quest %s: %s commit refused: %s", quest_id, c.type, exc)
        except Exception:
            log.exception(
                "quest %s: commits apply failed for type %r", quest_id, c.type
            )
            continue
        append_entry(
            store,
            quest_id,
            text=f"commit: {c.type} — {receipt}"[:400],
            entry_type="observation",
            by=by,
        )
        entries += 1
    return CommitsOutcome(
        commits=tuple(commits),
        dropped=dropped,
        applied=applied,
        todos=tuple(todos),
        accounts=tuple(accounts),
        logbook_entries=entries,
    )


__all__ = [
    "COMMITS_MAX",
    "COMMIT_TYPES",
    "FIELD_CHARS",
    "Commit",
    "CommitsOutcome",
    "apply_commits",
    "parse_commits",
]
