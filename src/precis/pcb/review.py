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
  decisive line quoted in ``evidence.quote``, a ``fail`` also carrying
  the smallest change that would make the item pass in
  ``evidence.correction`` (gr521605 — an answer with no correction is
  invalid and never lands), and ``inputs`` recording
  the revisions it consumed: every anchored part's datasheet-facts rev
  (``part:<C#>``) plus whatever the model declares it read from the
  offered input list (``margin:fixed-derating``). A later bump of any of
  them marks exactly this verdict stale. A ``pass`` that reads a part
  with no datasheet facts is refused in code and written as
  ``not checkable`` instead (:func:`factless_parts`; Reto 2026-10-16:
  never pass without part facts), except a :func:`generic_passive` R/C/L
  whose value and package are given.
* ``not_checkable`` → a ``machine``-origin ``not checkable`` verdict
  (Reto 2026-10-16 (1a), "so we can see"; migration 0224) with
  ``evidence.reason`` naming what is missing, the same anchors,
  fingerprint and consumed ``inputs`` as a pass/fail. It is a current
  record, never a pass, and keeps a blocking item open for tape-out. It
  is not re-queued until it goes stale like any verdict (item rev, target
  fingerprint, or an input revision such as a newly extracted datasheet
  facts rev), so a re-run on an unchanged design makes no model call.

The job runs the fresh check itself (:func:`precis.pcb.check_run.run`,
persisted; Reto 2026-10-16 (3a)) once the queue has work, then quotes
that run by id.
"""

from __future__ import annotations

import collections
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from precis import pcb as _pcb_pkg
from precis.checklist.status import (
    NOT_CHECKABLE,
    ItemStatus,
    bridge_for,
    compute_statuses,
)
from precis.pcb import datasheet_facts as ds_facts
from precis.pcb.catalog import parse_passive_description

if TYPE_CHECKING:
    from precis.store import Store

#: The checklist the pass works (``default_for: [pcb]``).
CHECKLIST = "pcb-tapeout"

#: ``checked_by`` on every verdict this pass writes — what ``op='forget'`` and a reader filter on.
REVIEWER = "pcb_review"

LLM_SOURCE = "pcb-review"
LLM_MAX_USD = 0.25
LLM_TIMEOUT_S = 120.0

#: Model answers the parser accepts; ``not_checkable`` is recorded as the
#: ledger verdict ``not checkable``.
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
    the checklist is missing or not assigned. A current ``not checkable``
    verdict is neither of those, so it is not queued until it goes stale."""
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


def input_revisions(store: Store, graph: dict[str, Any]) -> dict[str, Any]:
    """Every input this design's verdicts may consume → its current
    revision: the margin policy and each placed part's datasheet-facts rev
    (0 = never extracted, or no part ref yet, so a later pull or
    extraction marks the verdict stale)."""
    out: dict[str, Any] = {MARGIN_INPUT: _pcb_pkg.PASSIVE_MARGIN_REVISION}
    for lcsc in sorted(_lcscs(graph)):
        part_ref_id = store.part_ref_id(lcsc)
        out[part_input(lcsc)] = (
            0 if part_ref_id is None else ds_facts.facts_revision(store, part_ref_id)
        )
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
- "fail" when the evidence shows it is not met; say what is wrong, quote
  the line that shows it, and state in "correction" the smallest change
  that would make this item pass — a concrete part, value or net edit,
  not a restatement of what failed.
- "n/a" only when the item cannot apply to this board (say why).
- "not_checkable" when the evidence needed is missing — e.g. a part the
  item depends on has "no datasheet facts" — name what is missing. Never
  pass for lack of evidence: a pass that rests on a part with no
  datasheet facts is refused (a generic resistor, capacitor or inductor
  whose value and package are given needs none).
- "anchors": the refdes and net names the verdict is about (exactly as
  written below); [] for the whole board.
- "inputs": which of {offered} you relied on ([] if none).
- "correction": required — on "fail" the smallest change that would make
  this item pass; "" on every other verdict.

Answer with ONE JSON object and nothing else:
{{"verdict": "pass|fail|n/a|not_checkable", "reason": "...",
  "quote": "...", "correction": "...", "anchors": [...], "inputs": [...]}}

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
    #: The smallest change that would make the item pass (gr521605 —
    #: actionable, not a restatement of what failed). Required on
    #: ``fail``; "" everywhere else.
    correction: str = ""


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
    correction = str(raw.get("correction") or "").strip()
    if verdict == "fail" and not correction:
        # A bare fail tells the fixing agent nothing new (gr521605): the
        # answer is invalid, the item stays queued, the next run retries.
        raise ValueError(
            "a 'fail' verdict needs a 'correction' — the smallest change "
            "that would make the item pass"
        )

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
        correction=correction,
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


def record(
    store: Store,
    ref_id: int,
    *,
    checklist_id: int,
    st: ItemStatus,
    answer: Answer,
    revisions: dict[str, Any],
    graph: dict[str, Any],
    job_ref_id: int | None = None,
) -> str:
    """Write one answer as a machine verdict (``not checkable`` included).
    Returns a one-line account for the job summary."""
    bridge = bridge_for("pcb")
    anchors = [
        a
        for a in answer.anchors
        if bridge is not None and bridge.anchor_exists(store, ref_id, a)
    ]
    if answer.verdict == "pass":
        factless = factless_parts(anchors, graph, revisions)
        if factless:
            answer = replace(
                answer,
                verdict="not_checkable",
                reason=f"pass refused: no datasheet facts for {', '.join(factless)}",
            )
    inputs = _consumed(answer, anchors, revisions, graph)
    fp = bridge.fingerprint(store, ref_id, anchors).digest if bridge else None
    not_checkable = answer.verdict == "not_checkable"
    verdict = NOT_CHECKABLE if not_checkable else answer.verdict
    reason = answer.reason or ("evidence missing" if not_checkable else "")
    evidence_out: dict[str, Any] = {"reason": reason}
    if not not_checkable:
        evidence_out["quote"] = answer.quote
    if answer.correction:
        # gr521605: a fail carries the smallest change that would make
        # the item pass — the fixing agent consumes it, never re-derives.
        evidence_out["correction"] = answer.correction
    if job_ref_id is not None:
        evidence_out["job"] = job_ref_id
    store.checklist_verdict_insert(
        target_ref_id=ref_id,
        checklist_id=checklist_id,
        item_name=st.name,
        item_rev=int(st.item["rev"]),
        verdict=verdict,
        evidence=evidence_out,
        fingerprint=fp,
        checked_by=REVIEWER,
        anchors=anchors,
        origin="machine",
        inputs=inputs,
    )
    if not_checkable:
        return f"{st.name}: not checkable — {reason}"
    line = f"{st.name}: {answer.verdict}"
    if answer.correction:
        line += f" — fix: {answer.correction}"
    return line


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
    lcscs = {
        str(instances[r]["part_lcsc"]).strip().upper()
        for r in _consumed_refdes(anchors, graph)
        if instances[r].get("part_lcsc")
    }
    out = {
        part_input(c): revisions[part_input(c)]
        for c in sorted(lcscs)
        if part_input(c) in revisions
    }
    for name in answer.inputs:
        if name == MARGIN_INPUT:
            out[name] = revisions[MARGIN_INPUT]
    return out


def _consumed_refdes(anchors: Sequence[str], graph: dict[str, Any]) -> set[str]:
    """The refdes a verdict reads: each anchored refdes plus every part on
    an anchored net; a whole-board verdict (no anchors) reads them all."""
    instances = {str(i["refdes"]) for i in graph.get("instances") or []}
    if not anchors:
        return instances
    nets = {n["name"]: n for n in graph.get("nets") or []}
    refdes = {a for a in anchors if a in instances}
    for a in anchors:
        for m in (nets.get(a) or {}).get("members") or []:
            if str(m["refdes"]) in instances:
                refdes.add(str(m["refdes"]))
    return refdes


#: A generic two-terminal passive's refdes (``R12``, ``C3``, ``L1``,
#: ``R4A``); ``LED1`` and ``CN1`` do not match.
_PASSIVE_REFDES_RE = re.compile(r"^[RCL]\d+[A-Z]?$")
#: A value written as the label: ``10k``, ``4k7``, ``100nF``, ``4.7uH``, ``0R``.
_VALUE_LABEL_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*[pnuµμmkKMGR]?\d*\s*(?:F|H|Ω|ohms?)?\s*$"
)
#: An imperial chip package size in a catalog description.
_PACKAGE_RE = re.compile(r"\b(?:01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)\b")


def generic_passive(inst: dict[str, Any]) -> bool:
    """A generic R/C/L with its value and package both given: value plus
    package is the whole check, so a pass needs no datasheet facts for it
    (Reto's ruling 2026-10-16). The value comes from the catalog
    description's parse or the label, the package from the footprint or a
    size code in the description. Either one missing → facts required."""
    if not _PASSIVE_REFDES_RE.match(str(inst.get("refdes") or "")):
        return False
    raw = inst.get("part_description")
    desc = raw if isinstance(raw, str) else ""
    spec = parse_passive_description(desc) or {}
    has_value = (
        spec.get("value_f") is not None
        or spec.get("ohm") is not None
        or bool(_VALUE_LABEL_RE.match(str(inst.get("label") or "")))
    )
    has_package = bool(str(inst.get("footprint") or "").strip()) or bool(
        _PACKAGE_RE.search(desc)
    )
    return has_value and has_package


def factless_parts(
    anchors: Sequence[str], graph: dict[str, Any], revisions: dict[str, Any]
) -> list[str]:
    """The parts a verdict reads (:func:`_consumed_refdes`) that have no
    datasheet facts (no part number, an unknown part, or facts rev 0) and
    are not :func:`generic_passive`. :func:`record` refuses a pass that
    rests on any of them (Reto's ruling 2026-10-16)."""
    instances = {str(i["refdes"]): i for i in graph.get("instances") or []}
    out: list[str] = []
    for r in sorted(_consumed_refdes(anchors, graph)):
        inst = instances[r]
        if generic_passive(inst):
            continue
        lcsc = str(inst.get("part_lcsc") or "").strip().upper()
        if not lcsc:
            out.append(f"{r} (no part number)")
        elif not revisions.get(part_input(lcsc)):
            out.append(f"{r} ({lcsc})")
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
    "factless_parts",
    "generic_passive",
    "input_revisions",
    "parse",
    "part_input",
    "prompt",
    "queue",
    "record",
]
