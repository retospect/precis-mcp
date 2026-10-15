"""``datasheet_facts`` job_type — one BIG-tier LLM read of a part's
datasheet, stored as the part's fact block (datasheet-facts-mesh slice 2).

Enqueued next to the pull at put time —
:func:`precis.pcb.datasheets.enqueue_facts` mints one job per part that
has (or is pulling) a datasheet, parented on the board, idem
``datasheet_facts:<lcsc>``; a facts job that wins the race against its
pull parks itself (``waiting_pull`` → ``ctx.defer``) rather than recording
a spurious ``no_datasheet``. It also runs stand-alone:

    put(kind='job', job_type='datasheet_facts', parent_id=<pcb ref>,
        params={'lcsc': 'C5446'})

The job gathers the datasheet's table-bearing body chunks (keyword-scored,
char-capped), asks the model for the extraction payload
``{"packages": {<pkg>: {pins, required_externals, conditional_tables}}}``
with a ``chunk_id`` + ``page`` citation per fact, and hands it to
:func:`precis.pcb.datasheet_facts.write_facts`, which validates the schema
and every citation. The outcome — including ``no_datasheet``,
``llm:<class>`` and ``bad_payload`` — is recorded on the part ref's
``meta.datasheet_facts.extraction``, so "extracted" reads differently from
"never tried" (the ``meta.datasheet_pull`` shape). A recorded failure is a
state, not a raised error; re-run with ``put(kind='job', id=N,
mode='retry')`` or a fresh submit with a different idem key.

Lane: ``claude_inproc`` (``REQUIRES claude_bin``) — one BIG-tier
``route()`` call per datasheet, no tools, no MCP (the model answers from
the prompt's chunks, so it cannot touch anything).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.pcb import datasheet_facts as facts
from precis.pcb import datasheets as pcb_datasheets
from precis.utils.llm.router import LlmRequest, LlmResult, Tier, route
from precis.utils.timeutil import now_iso
from precis.workers.job_types import JobTypeSpec, defer_on_pause

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

NAME = "datasheet_facts"

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lcsc": {"type": "string", "minLength": 2},
    },
    "required": ["lcsc"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"claude_inproc"})
REQUIRES: frozenset[str] = frozenset({"claude_bin"})
DESCRIPTION = (
    "Extract one part's datasheet facts (pin table, required externals, "
    "conditional tables) into refs.meta.datasheet_facts — one BIG-tier "
    "read, enqueued next to datasheet_pull at pcb put time."
)

#: Spend + wall-clock on the single BIG-tier call.
MAX_USD = 1.50
TIMEOUT_S = 900.0
#: How much datasheet text one call may carry.
MAX_CHUNKS = 60
MAX_CHARS = 60_000

#: Chunk-table signal: a body chunk whose text or section path names one of
#: these likely carries pins, ratings, externals or strap tables.
_TABLE_HINT = re.compile(
    r"pin|pad|terminal|absolute|maximum|recommended|rating|electrical|"
    r"characteristic|address|strap|register|package|application|capacitor|"
    r"decoupl|typical|operating|threshold|vih|vil|i2c|i²c|spi|ordering",
    re.IGNORECASE,
)

_PROMPT = """You are extracting design facts from a component datasheet for part {lcsc}.
Below are its numbered text chunks, each marked [chunk <id>, page <n>].

Return ONE JSON object (no prose, no code fence) of this shape:

{{
  "packages": {{
    "<package name>": {{
      "pins": [
        {{"pin": "<pin number as printed>", "name": "<pin name>",
          "function": "<one-line role>", "etype": "<one of {etypes}>",
          "rail": "<supply domain name this pin belongs to, e.g. VDD>",
          "max_voltage_v": <abs-max volts, number>, "vih": <V>,
          "vil": <V>, "max_current_a": <A>,
          "nc": <true only if the sheet says no-connect>,
          "confidence": <0..1>, "page": <int>, "chunk_id": <int>}}
      ],
      "required_externals": [
        {{"pins": ["<pin number>"], "part_class": "capacitor|resistor|inductor|crystal",
          "value": "<e.g. 10uF>", "min_dielectric": "<e.g. X5R>",
          "min_voltage_v": <V>, "max_distance_mm": <mm>, "note": "<why>",
          "confidence": <0..1>, "page": <int>, "chunk_id": <int>}}
      ],
      "conditional_tables": [
        {{"name": "<e.g. i2c address>", "inputs": ["<pin name>"],
          "output_key": "<the produced value, e.g. address>",
          "rows": [{{"<input pin>": "<level>", "<output_key>": "<value>"}}],
          "confidence": <0..1>, "page": <int>, "chunk_id": <int>}}
      ]
    }}
  }}
}}

Rules:
- Cite every fact: "chunk_id" is the [#] chunk it came from, "page" its page.
- If a value is not printed, OMIT the field — never guess. Unknown is an
  honest answer; the design review asks the human instead.
- etype is the electrical role for ERC: power_in/power_out for supply pins,
  input/output/bidi/open_drain/tristate/passive/nc otherwise.
- rail is the pin's supply domain as the datasheet names it (VDD, AVDD…);
  omit for a signal pin with no domain.
- If the sheet covers several packages, use one block per package; if it
  names none, use the key "default".
- If the chunks contain no pin table at all, return {{"packages": {{}}}}.

Chunks:
{chunks}
"""


def _chunks(store: Store, ds_ref_id: int) -> list[dict[str, Any]]:
    """Body chunks of the datasheet, table-signal-scored, char-capped."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, ord, text, page_first, section_path "
            "FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (ds_ref_id,),
        ).fetchall()
    picked: list[dict[str, Any]] = []
    total = 0
    for cid, o, text, page, path in rows:
        blob = (text or "") + " " + " ".join(path or [])
        if not _TABLE_HINT.search(blob):
            continue
        picked.append({"chunk_id": int(cid), "ord": int(o), "text": text, "page": page})
        total += len(text or "")
    if not picked:  # no signal — take the head, a small sheet reads whole
        for cid, o, text, page, path in rows[:MAX_CHUNKS]:
            picked.append(
                {"chunk_id": int(cid), "ord": int(o), "text": text, "page": page}
            )
            total += len(text or "")
    out: list[dict[str, Any]] = []
    used = 0
    for c in picked[:MAX_CHUNKS]:
        if used + len(c["text"] or "") > MAX_CHARS and out:
            break
        used += len(c["text"] or "")
        out.append(c)
    return out


def build_prompt(lcsc: str, chunks: list[dict[str, Any]]) -> str:
    """The extraction prompt: schema + numbered chunks with ids/pages the
    model cites back."""
    body = "\n\n".join(
        f"[chunk {c['chunk_id']}, page {c['page'] or '?'}]\n{c['text']}" for c in chunks
    )
    return _PROMPT.format(
        lcsc=lcsc, etypes="/".join(facts.PIN_ETYPES or ()), chunks=body
    )


def parse_payload(text: str) -> dict[str, Any]:
    """The model's reply → the payload dict. Accepts a bare object or one
    inside a ```json fence; anything else is a ValueError."""
    s = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, re.DOTALL)
    if m:
        s = m.group(1)
    else:
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in the model reply")
        s = s[start : end + 1]
    payload = json.loads(s)
    if not isinstance(payload, dict):
        raise ValueError("model reply is not a JSON object")
    return payload


def _llm(prompt: str, *, ref_id: int | None) -> LlmResult:
    """The routed BIG-tier call — module-level so a test can swap it."""
    return route(
        LlmRequest(
            tier=Tier.BIG,
            source="datasheet_facts",
            prompt=prompt,
            max_usd=MAX_USD,
            timeout_s=TIMEOUT_S,
            ref_id=ref_id,
        )
    )


class LlmCallFailed(Exception):
    """The routed call came back with ``res.error`` — carries the
    :class:`LlmResult` so the dispatch can park on a window-scoped pause
    (``defer_on_pause``) instead of recording a defect."""

    def __init__(self, res: LlmResult) -> None:
        super().__init__(str(res.error))
        self.res = res


def extract(
    store: Store,
    lcsc: str,
    *,
    job_ref_id: int | None = None,
    llm: Callable[..., LlmResult] | None = None,
    event: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run one extraction and record its outcome on the part ref's
    ``meta.datasheet_facts.extraction``; returns the record. Deterministic
    failures are records, not raises (the ``datasheet_pull.pull`` shape).

    ``llm`` is the test seam (defaults to :func:`_llm`); it must return an
    :class:`LlmResult`. An ``LlmResult.error`` is recorded as
    ``llm:<reason_class>`` and then raised as :class:`LlmCallFailed` so the
    dispatch can park the job when the failure is a quota/budget pause —
    the parked re-run overwrites the record."""
    lcsc = lcsc.strip().upper()
    part_id = store.ensure_part_ref(lcsc, uncatalogued_ok=True)
    record: dict[str, Any] = {"at": now_iso(), "job_ref_id": job_ref_id}

    state = pcb_datasheets.datasheet_state(store, lcsc)
    ds = state["datasheet"]
    if ds is None:
        if pcb_datasheets.pull_in_flight(store, lcsc):
            # The facts job is enqueued at put time next to the pull and
            # can win the race — report a wait state instead of a
            # permanent-looking failure; the dispatch parks on it.
            record.update(status="waiting_pull")
            return record
        record.update(status="failed", reason="no_datasheet")
        facts.record_extraction(store, part_id, record)
        return record
    ds_ref_id = int(ds[0])
    record["datasheet_ref_id"] = ds_ref_id

    chunks = _chunks(store, ds_ref_id)
    if not chunks:
        record.update(status="failed", reason="no_text")
        facts.record_extraction(store, part_id, record)
        return record

    res = (llm or _llm)(build_prompt(lcsc, chunks), ref_id=part_id)
    if res.error:
        record.update(
            status="failed",
            reason=f"llm:{res.reason_class or 'error'}",
            detail=str(res.error)[:300],
        )
        facts.record_extraction(store, part_id, record)
        raise LlmCallFailed(res)
    if res.model:
        record["model"] = res.model

    try:
        payload = parse_payload(res.text)
    except ValueError as exc:
        if event is not None:
            event(f"unparseable reply:\n{(res.text or '')[:2000]}")
        record.update(status="failed", reason="bad_payload", detail=str(exc)[:200])
        facts.record_extraction(store, part_id, record)
        return record

    try:
        rev = facts.write_facts(
            store,
            part_id,
            payload,
            datasheet_ref_id=ds_ref_id,
            job_ref_id=job_ref_id,
            allow_empty=True,
        )
    except BadInput as exc:
        if event is not None:
            event(f"rejected payload: {exc}")
        record.update(status="failed", reason="bad_payload", detail=str(exc)[:200])
        facts.record_extraction(store, part_id, record)
        return record

    pins = sum(
        len(p.get("pins") or []) for p in (payload.get("packages") or {}).values()
    )
    record.update(status="ok", pins=pins, rev=rev)
    facts.record_extraction(store, part_id, record)
    return record


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    params = (ctx.meta or {}).get("params") or {}
    lcsc = str(params.get("lcsc") or "").strip().upper()
    if not re.fullmatch(r"C\d+", lcsc):
        ctx.record_failure(
            f"datasheet_facts: {lcsc!r} is not an LCSC C-number",
            failure_class="infra",
        )
        return
    try:
        record = extract(
            ctx.store,
            lcsc,
            job_ref_id=ctx.ref_id,
            event=lambda t: ctx.append_chunk("job_event", t),
        )
    except LlmCallFailed as exc:
        # The failure is already recorded on the part; a window-scoped
        # pause parks the job for a retry instead of ending it.
        if defer_on_pause(ctx, exc.res, label=NAME):
            return
        ctx.append_chunk(
            "job_summary",
            f"datasheet_facts {lcsc}: llm call failed ({exc.res.error})",
        )
        ctx.set_meta(lcsc=lcsc, facts_status="failed", facts_reason="llm")
        return
    status = record.get("status")
    if status == "waiting_pull":
        # The pull is queued/running but hasn't landed — park until the
        # default backoff re-runs us; past the park cap (or on a ctx with
        # no defer) record the honest no_datasheet end state.
        defer = getattr(ctx, "defer", None)
        if defer is not None and defer(
            None, f"{NAME}: datasheet_pull for {lcsc} still in flight"
        ):
            return
        facts.record_extraction(
            ctx.store,
            ctx.store.ensure_part_ref(lcsc, uncatalogued_ok=True),
            {"status": "failed", "reason": "no_datasheet", "at": now_iso()},
        )
        status = "failed"
        record["reason"] = "no_datasheet"
    ctx.append_chunk(
        "job_summary",
        f"datasheet_facts {lcsc}: {status}"
        + (f" ({record['reason']})" if record.get("reason") else "")
        + (
            f" — {record['pins']} pin(s), rev {record['rev']}" if status == "ok" else ""
        ),
    )
    ctx.set_meta(
        lcsc=lcsc,
        facts_status=status,
        facts_reason=record.get("reason"),
        facts_rev=record.get("rev"),
    )
    if status == "ok":
        ctx.set_status("succeeded")
    # recorded failure: the job itself still "succeeded" in doing the read —
    # leave default status transition to the executor like datasheet_pull.


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=None,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "NAME",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "SPEC",
    "build_prompt",
    "extract",
    "load",
    "parse_payload",
]
