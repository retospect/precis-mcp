"""The pcb re-check pass — ``pcb_review``'s queue, evidence, prompt and
verdict write (docs/backlog/pcb-datasheet-review.md slice 7, in-scope 7).

The work queue is the ``pcb-tapeout`` checklist's status on one design
(:func:`precis.checklist.status.compute_statuses`): only ``judgment``
items whose status is ``not checked`` or ``stale (...)`` are worked. A
settled item is never re-asked, so a re-run on an unchanged design makes
no model call. Tool items are read off their live checkers and never
reach the queue.

Each queued item gets ONE fresh-context MEDIUM-tier call (the
independence ruling: the reviewer is not the context that designed or
extracted) with the same evidence block — the latest persisted check run,
the parts with their datasheet facts (or the line that says there are
none), and the nets. The model answers strict JSON:

* ``pass`` / ``fail`` / ``n/a`` → a ``machine``-origin verdict, anchored
  to the refdes/nets it names (fingerprint narrowed to them), the
  decisive line quoted in ``evidence.quote``, and ``inputs`` recording
  the revisions it consumed: every anchored part's datasheet-facts rev
  (``part:<C#>``) plus whatever the model declares it read from the
  offered input list (``margin:fixed-derating``). A later bump of any of
  them marks exactly this verdict stale.
* ``not_checkable`` → no verdict (a pass by omission is the thing this
  pass exists to prevent, and the ledger has no "not checkable" value):
  a ``question`` note on the item's thread, ``not checkable (<why>)``.
  The note name carries a key over (item rev, target fingerprint, the
  facts/margin revisions), so the next run skips the item until one of
  those moves — still zero model calls on an unchanged design.

The fresh check run itself is the caller's: ``put(args={'op':'review'})``
runs ``view='check'`` (persisted) before it enqueues the job, because
the check surface lives in the handler layer the worker may not import.
The job quotes that run by id.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis import pcb as _pcb_pkg
from precis.checklist.status import ItemStatus, bridge_for, compute_statuses
from precis.pcb import datasheet_facts as ds_facts

if TYPE_CHECKING:
    from precis.store import Store

#: The checklist the pass works (``default_for: [pcb]``).
CHECKLIST = "pcb-tapeout"

#: ``checked_by`` on every verdict and ``origin`` on every note this
#: pass writes — what ``op='forget'`` and a reader filter on.
REVIEWER = "pcb_review"

LLM_SOURCE = "pcb-review"
LLM_MAX_USD = 0.25
LLM_TIMEOUT_S = 120.0

#: Model answers the parser accepts; ``not_checkable`` never becomes a
#: ledger verdict.
ANSWERS = ("pass", "fail", "n/a", "not_checkable")

#: Evidence caps (characters / rows): the block is shared by every item
#: in a run, so it is bounded once rather than per item.
_MAX_FINDINGS = 40
_MAX_FACTS_CHARS = 16000
_MAX_NETS_CHARS = 8000

#: The margin policy input name (slice 3/4's ``PcbBridge`` resolves it).
MARGIN_INPUT = "margin:fixed-derating"


def part_input(lcsc: str) -> str:
    """The ``inputs`` key a verdict records for one part's datasheet facts
    (``PcbBridge.input_revision`` resolves it to the current facts rev)."""
    return f"part:{lcsc.strip().upper()}"


@dataclass
class Queue:
    """One design's review state: the checklist row, every item's status,
    and the judgment items the pass must work, in checklist order."""

    checklist: dict[str, Any] | None
    statuses: list[ItemStatus] = field(default_factory=list)
    todo: list[ItemStatus] = field(default_factory=list)
    reason: str | None = None


def queue(store: Store, ref_id: int, *, item: str | None = None) -> Queue:
    """The judgment items on ``pcb-tapeout`` that are ``not checked`` or
    ``stale`` for this design. ``reason`` says why the queue is empty when
    the checklist is missing or not assigned."""
    checklist = store.checklist_get(CHECKLIST)
    if checklist is None:
        return Queue(None, reason=f"checklist {CHECKLIST!r} is not installed")
    explicit = store.checklist_assignment_live(
        target_ref_id=ref_id, checklist_id=checklist["id"]
    )
    if not explicit and "pcb" not in (checklist.get("default_for") or []):
        return Queue(checklist, reason=f"{CHECKLIST} is not assigned to this design")
    items = store.checklist_items_current(checklist["id"], target_ref_id=ref_id)
    verdicts = store.checklist_verdicts_latest_for_target(
        target_ref_id=ref_id, checklist_id=checklist["id"]
    )
    statuses = compute_statuses(
        store, items=items, verdicts=verdicts, kind="pcb", ref_id=ref_id
    )
    todo = [
        st
        for st in statuses
        if st.item["decidability"] == "judgment"
        and (st.status == "not checked" or st.status.startswith("stale"))
        and (item is None or st.name == item)
    ]
    return Queue(checklist, statuses, todo)


def pending(
    store: Store, ref_id: int, q: Queue, revisions: dict[str, Any]
) -> tuple[list[tuple[ItemStatus, str]], int]:
    """``(item, not-checkable key)`` pairs that need a model call, and how
    many queued items were skipped because a ``not checkable`` note at the
    current key is already on their thread — for ``not checked`` AND
    ``stale`` items alike (the key carries the item rev, the board
    fingerprint and every input revision, so it alone decides)."""
    if q.checklist is None:
        return [], 0
    out: list[tuple[ItemStatus, str]] = []
    skipped = 0
    for st in q.todo:
        key = not_checkable_key(store, ref_id, st, revisions)
        if has_note(store, ref_id, int(q.checklist["id"]), st.name, key):
            skipped += 1
            continue
        out.append((st, key))
    return out, skipped


def input_revisions(store: Store, graph: dict[str, Any]) -> dict[str, Any]:
    """Every input this design's verdicts may consume → its current
    revision: the margin policy and each placed part's datasheet-facts rev
    (0 = never extracted, so a later extraction marks the verdict stale)."""
    out: dict[str, Any] = {MARGIN_INPUT: _pcb_pkg.PASSIVE_MARGIN_REVISION}
    for lcsc in sorted(_lcscs(graph)):
        part_ref_id = store.part_ref_id(lcsc)
        if part_ref_id is not None:
            out[part_input(lcsc)] = ds_facts.facts_revision(store, part_ref_id)
    return out


def _lcscs(graph: dict[str, Any]) -> set[str]:
    return {
        str(i["part_lcsc"]).strip().upper()
        for i in graph.get("instances") or []
        if i.get("part_lcsc")
    }


def evidence(store: Store, ref_id: int, graph: dict[str, Any]) -> str:
    """The shared evidence block: the latest persisted check run, the
    parts (with datasheet facts, or the line saying there are none) and
    the nets. Built once per run."""
    run_id, findings = store.pcb_drc_findings_latest(ref_id)
    out: list[str] = []
    if run_id is None:
        out.append("## Check run\n(no check run recorded)")
    else:
        live = [f for f in findings if not f.get("waived_by")]
        by_rule = collections.Counter(f"{f['rule']}/{f['severity']}" for f in live)
        out.append(
            f"## Check run {str(run_id)[:12]} (latest persisted)\n"
            + (", ".join(f"{k}: {v}" for k, v in sorted(by_rule.items())) or "clean")
        )
        for f in live[:_MAX_FINDINGS]:
            out.append(f"- [{f['severity']}] {f['rule']}: {f.get('detail') or ''}")
        if len(live) > _MAX_FINDINGS:
            out.append(f"- … {len(live) - _MAX_FINDINGS} more findings not shown")

    out.append("\n## Parts")
    facts_text: list[str] = []
    facts_len = 0
    by_part: dict[str, list[str]] = collections.defaultdict(list)
    for inst in graph.get("instances") or []:
        lcsc = str(inst.get("part_lcsc") or "").strip().upper()
        out.append(
            f"- {inst['refdes']}: {inst.get('label') or '?'}"
            + (f" ({lcsc})" if lcsc else " (no part number)")
            + (f" — {inst['part_description']}" if inst.get("part_description") else "")
        )
        if lcsc:
            by_part[lcsc].append(str(inst["refdes"]))
    for lcsc, refdes in sorted(by_part.items()):
        facts = ds_facts.part_facts(store, lcsc)
        if facts is None:
            facts_text.append(
                f"### {lcsc} ({', '.join(refdes)})\nno datasheet facts for {lcsc}"
            )
            continue
        block = f"### {lcsc} ({', '.join(refdes)})\n" + ds_facts.render_facts(
            facts, lcsc=lcsc
        )
        if facts_len + len(block) > _MAX_FACTS_CHARS:
            facts_text.append(
                f"### {lcsc} ({', '.join(refdes)})\nfacts rev {facts.rev} "
                "exist but were cut for length — "
                f"get(kind='part', id='{lcsc}', view='facts')"
            )
            continue
        facts_len += len(block)
        facts_text.append(block)
    out.append("\n## Datasheet facts\n" + ("\n".join(facts_text) or "(no parts)"))

    nets = []
    for n in graph.get("nets") or []:
        members = " ".join(f"{m['refdes']}.{m['pin']}" for m in n.get("members") or [])
        nets.append(f"- {n['name']}: {members}")
    nets_text = "\n".join(nets)
    if len(nets_text) > _MAX_NETS_CHARS:
        nets_text = nets_text[:_MAX_NETS_CHARS] + "\n- … (nets cut for length)"
    out.append("\n## Nets\n" + (nets_text or "(no nets)"))
    return "\n".join(out)


_PROMPT = """You review one item of the pcb pre-tapeout checklist on the
design "{title}". You did not design this board and did not extract its
datasheet facts: check the claim cold, from the evidence below only.

Item: {name} (phase {phase}, severity {severity})
Fails when: {prevents}
{body}
Rules:
- "pass" only when the evidence shows the item is met; quote the decisive
  datasheet line, fact row or finding in "quote".
- "fail" when the evidence shows it is not met; say what is wrong and
  quote the line that shows it.
- "n/a" only when the item cannot apply to this board (say why).
- "not_checkable" when the evidence needed is missing — e.g. a part the
  item depends on has "no datasheet facts" — name what is missing. Never
  pass for lack of evidence.
- "anchors": the refdes and net names the verdict is about (exactly as
  written below); [] for the whole board.
- "inputs": which of {offered} you relied on ([] if none).

Answer with ONE JSON object and nothing else:
{{"verdict": "pass|fail|n/a|not_checkable", "reason": "...",
  "quote": "...", "anchors": [...], "inputs": [...]}}

# Evidence
{evidence}
"""


def prompt(*, title: str, st: ItemStatus, evidence_text: str) -> str:
    item = st.item
    body = str(item.get("body") or "").strip()
    return _PROMPT.format(
        title=title,
        name=st.name,
        phase=item.get("phase") or "—",
        severity=item.get("severity") or "—",
        prevents=item.get("prevents") or "",
        body=(f"Notes: {body}\n" if body else ""),
        offered=json.dumps([MARGIN_INPUT]),
        evidence=evidence_text,
    )


@dataclass(frozen=True)
class Answer:
    verdict: str
    reason: str
    quote: str
    anchors: tuple[str, ...]
    inputs: tuple[str, ...]


def parse(text: str) -> Answer:
    """The model's JSON answer → :class:`Answer`. Raises ``ValueError`` on
    anything that is not one object with a known verdict — a garbled
    answer is a failed item, never a guessed verdict."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m is None:
        raise ValueError("no JSON object in the answer")
    raw = json.loads(m.group(0))
    if not isinstance(raw, dict):
        raise ValueError("the answer is not a JSON object")
    verdict = str(raw.get("verdict") or "").strip().lower().replace(" ", "_")
    if verdict not in ANSWERS:
        raise ValueError(f"verdict {verdict!r} is not one of {list(ANSWERS)}")

    def _strs(v: Any) -> tuple[str, ...]:
        if not isinstance(v, list):
            return ()
        return tuple(str(x).strip() for x in v if str(x).strip())

    return Answer(
        verdict=verdict,
        reason=str(raw.get("reason") or "").strip(),
        quote=str(raw.get("quote") or "").strip(),
        anchors=_strs(raw.get("anchors")),
        inputs=_strs(raw.get("inputs")),
    )


def ask(*, title: str, st: ItemStatus, evidence_text: str, ref_id: int) -> Answer:
    """One fresh-context MEDIUM-tier call for one item. Raises on transport
    failure or an unparseable answer — the job counts it and moves on."""
    from precis.utils.llm.router import LlmRequest, Tier, route

    res = route(
        LlmRequest(
            tier=Tier.MEDIUM,
            source=LLM_SOURCE,
            prompt=prompt(title=title, st=st, evidence_text=evidence_text),
            max_usd=LLM_MAX_USD,
            timeout_s=LLM_TIMEOUT_S,
            ref_id=ref_id,
        )
    )
    if res.error:
        raise RuntimeError(res.error)
    return parse(res.text or "")


def not_checkable_key(
    store: Store, ref_id: int, st: ItemStatus, revisions: dict[str, Any]
) -> str:
    """What a ``not checkable`` note is valid for: the item rev, the whole
    target's fingerprint and every input revision. Any move re-opens it."""
    bridge = bridge_for("pcb")
    fp = bridge.fingerprint(store, ref_id, ()).digest if bridge is not None else ""
    raw = json.dumps(
        [st.name, st.item["rev"], fp, revisions], sort_keys=True, default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def note_name(item_name: str, key: str) -> str:
    return f"not-checkable-{item_name}-{key}"


def has_note(
    store: Store, ref_id: int, checklist_id: int, item_name: str, key: str
) -> bool:
    """Whether a live ``not checkable`` note at this key is already on the
    item's thread (the skip that keeps an unchanged re-run call-free)."""
    want = note_name(item_name, key)
    return any(
        n["name"] == want
        for n in store.checklist_notes_list(
            target_ref_id=ref_id, checklist_id=checklist_id, item_name=item_name
        )
    )


def record(
    store: Store,
    ref_id: int,
    *,
    checklist_id: int,
    st: ItemStatus,
    answer: Answer,
    revisions: dict[str, Any],
    graph: dict[str, Any],
    nc_key: str,
    job_ref_id: int | None = None,
) -> str:
    """Write one answer: a machine verdict, or a ``not checkable`` note.
    Returns a one-line account for the job summary."""
    bridge = bridge_for("pcb")
    anchors = [
        a
        for a in answer.anchors
        if bridge is not None and bridge.anchor_exists(store, ref_id, a)
    ]
    if answer.verdict == "not_checkable":
        store.checklist_note_insert(
            target_ref_id=ref_id,
            checklist_id=checklist_id,
            item_name=st.name,
            name=note_name(st.name, nc_key),
            kind="question",
            body=f"not checkable ({answer.reason or 'evidence missing'})",
            about=anchors,
            origin=REVIEWER,
        )
        return f"{st.name}: not checkable — {answer.reason or 'evidence missing'}"

    inputs = _consumed(answer, anchors, revisions, graph)
    fp = bridge.fingerprint(store, ref_id, anchors).digest if bridge else None
    evidence_out: dict[str, Any] = {"reason": answer.reason, "quote": answer.quote}
    if job_ref_id is not None:
        evidence_out["job"] = job_ref_id
    store.checklist_verdict_insert(
        target_ref_id=ref_id,
        checklist_id=checklist_id,
        item_name=st.name,
        item_rev=int(st.item["rev"]),
        verdict=answer.verdict,
        evidence=evidence_out,
        fingerprint=fp,
        checked_by=REVIEWER,
        anchors=anchors,
        origin="machine",
        inputs=inputs,
    )
    return f"{st.name}: {answer.verdict}"


def _consumed(
    answer: Answer,
    anchors: Sequence[str],
    revisions: dict[str, Any],
    graph: dict[str, Any],
) -> dict[str, Any]:
    """The ``inputs`` map one verdict records: every anchored part's facts
    rev — a net anchor consumes every part on that net, a whole-board
    verdict every part's — plus each offered input the model declared it
    read."""
    instances = {i["refdes"]: i for i in graph.get("instances") or []}
    if anchors:
        nets = {n["name"]: n for n in graph.get("nets") or []}
        refdes = {a for a in anchors if a in instances}
        for a in anchors:
            for m in (nets.get(a) or {}).get("members") or []:
                refdes.add(str(m["refdes"]))
        lcscs = {
            str(instances[r]["part_lcsc"]).strip().upper()
            for r in refdes
            if r in instances and instances[r].get("part_lcsc")
        }
    else:
        lcscs = _lcscs(graph)
    out = {
        part_input(c): revisions[part_input(c)]
        for c in sorted(lcscs)
        if part_input(c) in revisions
    }
    for name in answer.inputs:
        if name == MARGIN_INPUT:
            out[name] = revisions[MARGIN_INPUT]
    return out


__all__ = [
    "ANSWERS",
    "CHECKLIST",
    "LLM_SOURCE",
    "MARGIN_INPUT",
    "REVIEWER",
    "Answer",
    "Queue",
    "ask",
    "evidence",
    "has_note",
    "input_revisions",
    "not_checkable_key",
    "note_name",
    "parse",
    "part_input",
    "pending",
    "prompt",
    "queue",
    "record",
]
